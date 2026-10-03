# correccion_deteccion.py
# Puente entre protocolo_auditoria.py (protocolo de auditoria de calidad ya
# validado) y la capa de correccion con IA. No modifica ese archivo: lo
# importa y traduce su salida a una estructura propia (PageAudit) para que
# el resto del sistema no dependa de sus detalles internos.
#
# Tambien vive aqui el calculo de porcentajes/umbrales y la seleccion de
# fragmentos de ejemplo — ambos modos de correccion (API y Claude Code)
# comparten exactamente esta misma logica; lo unico que cambia entre modos
# es que ocurre al presionar los botones de accion.

import re
import importlib.util
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# CONSTANTES DE CONFIGURACION
# ---------------------------------------------------------------------------
# protocolo_auditoria.py vive en la misma carpeta que este archivo.
PROTOCOLO_AUDITORIA_PATH = Path(__file__).resolve().parent / "protocolo_auditoria.py"

DEFAULT_THRESHOLD = 0.15  # 15%, umbral inicial para MODERADO y SEVERO (ajustable en UI)

FRAGMENT_DEFAULT_LIMIT = 12
FRAGMENT_EXPAND_STEP = 12
FRAGMENT_SEVERO_RESERVED_SLOTS = 5

MENSAJE_MODO_VENTANA = (
    "Este documento no tiene paginas identificables individualmente "
    "(no se detectaron marcadores de pagina en el Markdown convertido), "
    "asi que no se puede hacer corresponder cada fragmento con una imagen "
    "real de pagina. La correccion con IA no esta disponible para este "
    "documento; el resto del pipeline funciona con normalidad."
)

# Valores de referencia usados SOLO para elegir que metrica mostrar como
# "disparadora" en la seleccion de fragmentos de ejemplo — son los mismos
# limites LIMPIO/MODERADO que ya usa protocolo_auditoria.py en
# severidad_absoluta(), copiados aqui como aproximacion visual. Si ese
# archivo cambia sus umbrales internos esta etiqueta puede quedar
# ligeramente desalineada; no afecta la severidad real, que siempre viene
# directo de clasificar_bloques().
_REF_STOP_RATIO = 0.28
_REF_AVG_LEN = 4.2
_REF_SINGLE_RATIO = 0.12


def _cargar_protocolo_auditoria():
    """Carga protocolo_auditoria.py desde su ruta absoluta, sin modificarlo."""
    if not PROTOCOLO_AUDITORIA_PATH.exists():
        raise FileNotFoundError(
            f"No se encontro protocolo_auditoria.py en {PROTOCOLO_AUDITORIA_PATH}. "
            "Actualiza PROTOCOLO_AUDITORIA_PATH en correccion_deteccion.py si se movio."
        )
    spec = importlib.util.spec_from_file_location("protocolo_auditoria", PROTOCOLO_AUDITORIA_PATH)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


_protocolo = _cargar_protocolo_auditoria()
segmentar = _protocolo.segmentar
medir_bloque = _protocolo.medir_bloque
clasificar_bloques = _protocolo.clasificar_bloques


# ---------------------------------------------------------------------------
# TRADUCCION protocolo_auditoria.py -> PageAudit
# ---------------------------------------------------------------------------

@dataclass
class PageAudit:
    page_number: int
    label: str
    raw_text: str
    severity: str          # LIMPIO | LEVE | MODERADO | SEVERO
    metric_flag: str        # "stop_ratio" | "avg_len" | "single_ratio"
    accent_suspect: bool    # patron A / PENDIENTE VERIFICAR (heuristica no calibrada)


_PAGE_NUMBER_RE = re.compile(r"\d+")


def _metric_flag_for(bm) -> str:
    dev_stop = max(0.0, _REF_STOP_RATIO - bm.stop_ratio) / _REF_STOP_RATIO
    dev_avglen = max(0.0, _REF_AVG_LEN - bm.avg_len) / _REF_AVG_LEN
    dev_single = max(0.0, bm.single_ratio - _REF_SINGLE_RATIO) / _REF_SINGLE_RATIO
    desviaciones = {"stop_ratio": dev_stop, "avg_len": dev_avglen, "single_ratio": dev_single}
    return max(desviaciones, key=desviaciones.get)


def build_page_audits(markdown_text: str):
    """
    Traduce la salida de protocolo_auditoria.py a una lista de PageAudit.
    Retorna (page_audits, modo) — modo es "pagina" o "ventana", tal como lo
    devuelve segmentar(). Si modo es "ventana", page_audits viene vacia: el
    documento no tiene paginas identificables para correccion con IA.
    """
    bloques_raw, modo = segmentar(markdown_text)
    if modo != "pagina":
        return [], modo

    metrics = [medir_bloque(i, label, contenido) for i, (label, contenido) in enumerate(bloques_raw)]
    clasificar_bloques(metrics)

    page_audits = []
    for bm in metrics:
        match = _PAGE_NUMBER_RE.search(bm.label)
        page_number = int(match.group()) if match else bm.index + 1
        page_audits.append(PageAudit(
            page_number=page_number,
            label=bm.label,
            raw_text=bm.text,
            severity=bm.severidad,
            metric_flag=_metric_flag_for(bm),
            accent_suspect=bool(bm.accent_suspects),
        ))
    return page_audits, modo


# ---------------------------------------------------------------------------
# UMBRALES Y PORCENTAJES
# ---------------------------------------------------------------------------

def calcular_porcentajes(page_audits: list) -> dict:
    """Porcentajes y conteos absolutos de severidad sobre el total de paginas."""
    total = len(page_audits)
    if total == 0:
        return {
            "total_paginas": 0,
            "moderado_count": 0, "moderado_pct": 0.0,
            "severo_count": 0, "severo_pct": 0.0,
            "leve_count": 0,
            "accent_suspect_count": 0,
        }
    moderado_count = sum(1 for p in page_audits if p.severity == "MODERADO")
    severo_count = sum(1 for p in page_audits if p.severity == "SEVERO")
    leve_count = sum(1 for p in page_audits if p.severity == "LEVE")
    accent_count = sum(1 for p in page_audits if p.accent_suspect)
    return {
        "total_paginas": total,
        "moderado_count": moderado_count,
        "moderado_pct": moderado_count / total,
        "severo_count": severo_count,
        "severo_pct": severo_count / total,
        "leve_count": leve_count,
        "accent_suspect_count": accent_count,
    }


def evaluar_umbrales(porcentajes: dict, umbral_moderado: float, umbral_severo: float) -> dict:
    """Booleanos de si cada porcentaje supera su umbral (ambos en fraccion 0-1)."""
    return {
        "moderado_supera_umbral": porcentajes["moderado_pct"] > umbral_moderado,
        "severo_supera_umbral": porcentajes["severo_pct"] > umbral_severo,
    }


# ---------------------------------------------------------------------------
# SELECCION DE FRAGMENTOS DE EJEMPLO (cuota ponderada)
# ---------------------------------------------------------------------------

def seleccionar_fragmentos(page_audits: list) -> list:
    """
    Ordena las paginas MODERADO/SEVERO para la vista de fragmentos de ejemplo:
    hasta 5 primeros lugares para SEVERO (si hay menos, se liberan a MODERADO),
    luego MODERADO priorizando diversidad de metric_flag antes de repetir uno
    ya representado. Dentro de cada grupo, orden ascendente de pagina. No
    aplica ningun limite — la UI corta esta lista con el paso de "ver mas".
    """
    severos = sorted((p for p in page_audits if p.severity == "SEVERO"), key=lambda p: p.page_number)
    moderados = sorted((p for p in page_audits if p.severity == "MODERADO"), key=lambda p: p.page_number)

    severos_seleccionados = severos[:FRAGMENT_SEVERO_RESERVED_SLOTS]
    severos_restantes = severos[FRAGMENT_SEVERO_RESERVED_SLOTS:]

    vistos = set()
    moderados_diversos, moderados_resto = [], []
    for p in moderados:
        if p.metric_flag not in vistos:
            moderados_diversos.append(p)
            vistos.add(p.metric_flag)
        else:
            moderados_resto.append(p)

    return severos_seleccionados + moderados_diversos + severos_restantes + moderados_resto


# ---------------------------------------------------------------------------
# SEVERIDAD GLOBAL Y METRICAS AGREGADAS DE BATCH
# ---------------------------------------------------------------------------

def severidad_global(porcentajes: dict) -> tuple:
    """Retorna (etiqueta, icono) — LIMPIO/MODERADO/SEVERO segun el peor caso del documento."""
    if porcentajes["severo_count"] > 0:
        return "SEVERO", "🔴"
    if porcentajes["moderado_count"] > 0:
        return "MODERADO", "🟠"
    return "LIMPIO", "🟢"


def calcular_metricas_batch(porcentajes_por_archivo: list) -> dict:
    """
    porcentajes_por_archivo: lista con el resultado de calcular_porcentajes()
    para cada archivo del batch, uno por archivo (incluye los que cayeron en
    modo "ventana" — su calcular_porcentajes sobre lista vacia ya cuenta como
    LIMPIO de forma natural, sin caso especial aqui).
    """
    limpio = moderado = severo = 0
    paginas_danadas = 0
    for porcentajes in porcentajes_por_archivo:
        sev, _ = severidad_global(porcentajes)
        if sev == "SEVERO":
            severo += 1
        elif sev == "MODERADO":
            moderado += 1
        else:
            limpio += 1
        paginas_danadas += porcentajes["moderado_count"] + porcentajes["severo_count"]
    return {
        "total_archivos": len(porcentajes_por_archivo),
        "limpio_count": limpio,
        "moderado_count": moderado,
        "severo_count": severo,
        "paginas_danadas_total": paginas_danadas,
    }
