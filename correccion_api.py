# correccion_api.py
# Comunicacion con la API de Anthropic para el Modo API de correccion OCR:
# verificacion de conectividad, enrutamiento por severidad, prompts,
# escalamiento automatico Sonnet -> Opus y parseo de respuesta estructurada.
#
# Dependencias nuevas frente al resto del proyecto:
# pip install anthropic
#
# No requiere clave de API en este archivo: anthropic.Anthropic() la toma
# automaticamente de la variable de entorno ANTHROPIC_API_KEY.
#
# LIMITACIONES CONOCIDAS:
#
# 1. El modelo tiende a normalizar o censurar automaticamente palabras que
#    interpreta como ofensivas o vulgares (ej. sustituir caracteres por
#    asteriscos: "sh*t" en vez de transcribir literalmente "sh"t" tal como
#    aparece en la imagen), incluso con instruccion explicita de
#    transcripcion literal en el prompt y un ejemplo concreto anclado al
#    caso real (ver _ESTANDAR_TRANSCRIPCION_LITERAL). Confirmado dos veces
#    contra un PDF de libro de texto escaneado, pagina 3 — la segunda vez ya con el
#    ejemplo explicito agregado al prompt, sin cambio de comportamiento.
#    No se seguira iterando el prompt para esto: dos intentos con evidencia
#    consistente y costo real por intento es suficiente para tratarlo como
#    limitacion del modelo, no del prompt. Cualquier documento con lenguaje
#    potencialmente ofensivo o vulgar en el original necesita revision
#    manual palabra por palabra contra el PDF — el modelo puede reportar
#    confidence alta en un texto que de todas formas tiene esta alteracion
#    silenciosa, asi que el indicador de confianza no alcanza para
#    detectarla.

import base64
import io
import os
from dataclasses import dataclass, field

import fitz  # pymupdf, ya es dependencia del pipeline principal
from pdf2image import convert_from_bytes

try:
    import anthropic
    # anthropic >=1.x trae httpx2 como dependencia (renombre de httpx, misma
    # API de Client/.get); versiones mas viejas del SDK aun usan httpx clasico.
    try:
        import httpx2 as httpx
    except ImportError:
        import httpx
    _SDK_DISPONIBLE = True
except ImportError:
    _SDK_DISPONIBLE = False

# ---------------------------------------------------------------------------
# CONSTANTES DE CONFIGURACION
# ---------------------------------------------------------------------------
# Los modelos se pueden cambiar con variables de entorno. Importante: este
# modulo fuerza el uso de una herramienta con tool_choice de tipo "tool"
# (ver _call_claude_page). Segun la documentacion de Anthropic consultada el
# 2026-10-02, Claude Sonnet 5.5 rechaza tool_choice de tipo "tool" y "any" con
# un error 400, por eso los valores por defecto son modelos que si lo aceptan.
# Si cambias de modelo, verifica primero que acepte tool_choice forzado.
MODEL_SONNET = os.environ.get("MD_UNIVERSAL_MODEL_SONNET", "claude-sonnet-5")   # paginas MODERADO
MODEL_OPUS = os.environ.get("MD_UNIVERSAL_MODEL_OPUS", "claude-opus-5")         # paginas SEVERO y escalamientos desde Sonnet

CONNECTIVITY_CHECK_URL = "https://api.anthropic.com"
CONNECTIVITY_TIMEOUT = 2.5  # segundos

MAX_TOKENS_CORRECCION = 8192

_TOOL_NAME = "reportar_correccion"
_TOOL_SCHEMA = {
    "name": _TOOL_NAME,
    "description": "Reporta el texto corregido de una pagina de documento con su nivel de confianza.",
    "input_schema": {
        "type": "object",
        "properties": {
            "texto_corregido": {"type": "string"},
            "confidence": {"type": "string", "enum": ["alta", "media", "baja"]},
            "motivo_baja_confianza": {"type": "string"},
            "zonas_ilegibles": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["texto_corregido", "confidence"],
    },
}

_ESTANDAR_FIDELIDAD = (
    "Cualquier corte fisico irrecuperable en el texto se marca como "
    "\"[ILEGIBLE - verificar contra PDF original]\", tanto dentro de "
    "texto_corregido como agregado a zonas_ilegibles. Nunca reconstruyas ese "
    "fragmento por inferencia gramatical, aunque el sentido parezca obvio."
)

_ESTANDAR_TRANSCRIPCION_LITERAL = (
    "Transcribe los caracteres exactos que aparecen en la imagen, incluyendo "
    "puntuacion inusual, comillas sueltas, o espaciado atipico dentro de "
    "palabras. Por ejemplo, si la imagen muestra literalmente sh\"t (con una "
    "comilla suelta, sin asterisco), transcribe exactamente sh\"t — nunca lo "
    "conviertas a sh*t ni a ninguna otra convencion de censura, aunque el "
    "resultado incluya lenguaje que pudiera parecer ofensivo. El estandar de "
    "fidelidad exige transcripcion literal del documento fuente, nunca una "
    "version editada, censurada o suavizada por tu propio criterio, sin "
    "importar el contenido."
)


# ---------------------------------------------------------------------------
# RESULTADO POR PAGINA
# ---------------------------------------------------------------------------

@dataclass
class CorrectionResult:
    page_number: int
    ok: bool
    texto_corregido: str = ""
    confidence: str = ""
    motivo_baja_confianza: str = ""
    zonas_ilegibles: list = field(default_factory=list)
    escalada_desde_sonnet: bool = False
    modelo_usado: str = ""
    excluida: bool = False
    motivo_exclusion: str = ""


# ---------------------------------------------------------------------------
# CONECTIVIDAD
# ---------------------------------------------------------------------------

def verificar_conectividad() -> bool:
    """Intento corto de conexion contra un endpoint estable. False si no hay SDK o falla."""
    if not _SDK_DISPONIBLE:
        return False
    try:
        with httpx.Client(timeout=CONNECTIVITY_TIMEOUT) as client:
            client.get(CONNECTIVITY_CHECK_URL)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# PREFLIGHT — validacion e imagen de pagina, antes de gastar ninguna llamada
# ---------------------------------------------------------------------------

def preflight_paginas(file_bytes: bytes, page_numbers: list, poppler_path: str, dpi: int) -> tuple:
    """
    Rasteriza cada pagina del lote una sola vez (se reutiliza despues en la
    llamada real a la API). Retorna (imagenes: dict[int, PIL.Image],
    excluidas: dict[int, str]) — una pagina cae en excluidas si su numero no
    corresponde a una pagina real del PDF o si convert_from_bytes falla;
    nunca se lanza excepcion hacia el llamador ni se envia una imagen dudosa.
    """
    try:
        total_real = fitz.open(stream=file_bytes, filetype="pdf").page_count
    except Exception as exc:
        return {}, {n: f"No se pudo abrir el PDF para validar paginas: {exc}" for n in page_numbers}

    imagenes = {}
    excluidas = {}
    for n in page_numbers:
        if n < 1 or n > total_real:
            excluidas[n] = f"Numero de pagina {n} fuera de rango del documento original (1-{total_real})."
            continue
        try:
            imgs = convert_from_bytes(file_bytes, dpi=dpi, poppler_path=poppler_path, first_page=n, last_page=n)
            if not imgs:
                excluidas[n] = "No se pudo generar la imagen de esta pagina (resultado vacio)."
                continue
            imagenes[n] = imgs[0]
        except Exception as exc:
            excluidas[n] = f"Fallo al generar la imagen de la pagina: {exc}"
    return imagenes, excluidas


def _pil_to_base64_png(pil_image) -> str:
    buf = io.BytesIO()
    pil_image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ---------------------------------------------------------------------------
# PROMPTS Y LLAMADA A LA API
# ---------------------------------------------------------------------------

def _build_prompt(severity: str, raw_text: str = None) -> str:
    partes = [
        "Transcribe y corrige el texto de esta pagina de documento a partir "
        "de la imagen adjunta.",
    ]
    if raw_text:
        partes.append(
            "Esta es una extraccion automatica no confiable, incluida solo "
            "como referencia de palabras probables:\n\n"
            f"{raw_text}\n\n"
            "La imagen es la fuente de verdad."
        )
    partes.append(_ESTANDAR_FIDELIDAD)
    partes.append(_ESTANDAR_TRANSCRIPCION_LITERAL)
    partes.append(
        "Reporta tu nivel de confianza en la transcripcion (alta/media/baja); "
        "si no es alta, explica brevemente por que en motivo_baja_confianza."
    )
    return "\n\n".join(partes)


def _call_claude_page(client, model: str, image_b64: str, prompt_text: str) -> dict:
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_b64}},
        {"type": "text", "text": prompt_text},
    ]
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS_CORRECCION,
        tools=[_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": _TOOL_NAME},
        messages=[{"role": "user", "content": content}],
    )
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    raise RuntimeError("La respuesta de Claude no incluyo el bloque de correccion esperado.")


# ---------------------------------------------------------------------------
# ORQUESTACION POR PAGINA Y POR LOTE
# ---------------------------------------------------------------------------

def procesar_pagina(client, page_audit, imagen, on_escalar=None) -> CorrectionResult:
    """
    Procesa una pagina segun su severidad. MODERADO va a Sonnet con texto
    crudo de referencia; SEVERO va directo a Opus solo con imagen. Si una
    pagina MODERADO vuelve con confidence media/baja, escala automaticamente
    a Opus (imagen sola) sin intervencion del usuario.
    """
    try:
        incluir_texto = page_audit.severity == "MODERADO"
        modelo_inicial = MODEL_SONNET if incluir_texto else MODEL_OPUS
        prompt = _build_prompt(page_audit.severity, page_audit.raw_text if incluir_texto else None)
        imagen_b64 = _pil_to_base64_png(imagen)

        data = _call_claude_page(client, modelo_inicial, imagen_b64, prompt)
        escalada = False
        modelo_final = modelo_inicial

        if incluir_texto and data.get("confidence") in ("media", "baja"):
            if on_escalar:
                on_escalar(page_audit.page_number)
            data = _call_claude_page(client, MODEL_OPUS, imagen_b64, _build_prompt("SEVERO"))
            escalada = True
            modelo_final = MODEL_OPUS

        confidence_final = data.get("confidence", "")
        # motivo_baja_confianza solo tiene sentido si la confianza final no es
        # alta — si el escalamiento a Opus resolvio la duda, no debe quedar
        # colgado el motivo del intento anterior (de Sonnet) ni nada que el
        # modelo haya puesto ahi de forma inconsistente con su propia confianza.
        motivo_final = data.get("motivo_baja_confianza", "") if confidence_final != "alta" else ""

        return CorrectionResult(
            page_number=page_audit.page_number,
            ok=True,
            texto_corregido=data.get("texto_corregido", ""),
            confidence=confidence_final,
            motivo_baja_confianza=motivo_final,
            zonas_ilegibles=data.get("zonas_ilegibles") or [],
            escalada_desde_sonnet=escalada,
            modelo_usado=modelo_final,
        )
    except Exception as exc:
        return CorrectionResult(
            page_number=page_audit.page_number,
            ok=False,
            excluida=True,
            motivo_exclusion=f"Fallo en la llamada a la API de Claude: {exc}",
        )


def procesar_lote(file_bytes: bytes, page_audits: list, poppler_path: str, dpi: int, progress_callback=None) -> list:
    """
    page_audits: paginas MODERADO/SEVERO a procesar (ya filtradas por el
    boton presionado). progress_callback(actual, total, page_number, fase)
    se llama con fase="procesando" al iniciar cada pagina y fase="escalando"
    si esa pagina se reenvia a Opus. El total del progreso ya excluye de
    entrada las paginas que fallaron el preflight.
    """
    if not _SDK_DISPONIBLE:
        raise RuntimeError("El paquete 'anthropic' no esta instalado — ejecuta: pip install anthropic")

    page_numbers = [p.page_number for p in page_audits]
    imagenes, excluidas = preflight_paginas(file_bytes, page_numbers, poppler_path, dpi)
    validas = [p for p in page_audits if p.page_number in imagenes]
    total = len(validas)

    client = anthropic.Anthropic()
    resultados = []

    for idx, page_audit in enumerate(validas, start=1):
        if progress_callback:
            progress_callback(idx, total, page_audit.page_number, "procesando")

        def _on_escalar(pn, idx=idx, total=total):
            if progress_callback:
                progress_callback(idx, total, pn, "escalando")

        resultado = procesar_pagina(client, page_audit, imagenes[page_audit.page_number], on_escalar=_on_escalar)
        resultados.append(resultado)

    for page_number, motivo in excluidas.items():
        resultados.append(CorrectionResult(page_number=page_number, ok=False, excluida=True, motivo_exclusion=motivo))

    return resultados
