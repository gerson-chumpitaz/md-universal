#!/usr/bin/env python3
"""
Protocolo de auditoria de calidad para archivos .md generados por app_universal.py
(pipeline pymupdf4llm + pdfplumber + pytesseract).

Detecta y clasifica automaticamente 5 patrones de dano conocidos en la conversion
PDF -> Markdown, sin necesidad de leer el archivo completo a ojo:

  Patron A - Bug CID-Unicode: caracteres acentuados (ej. "o" con tilde) corrompidos
             en PDFs con texto digital nativo, por mapeo de fuente roto en el PDF
             original. Deteccion basada en diccionario (heuristica, no calibrada
             con evidencia directa en el corpus de referencia usado para este script).

  Patron B - Mezcla de columnas OCR, severidad localizada: fragmentos de texto de
             columnas distintas intercalados letra por letra, tipicamente en zonas
             de portada, imagenes o graficos con texto superpuesto.

  Patron C - Mezcla de columnas OCR, severidad extrema y generalizada: el mismo
             dano que el patron B pero extendido de forma sostenida por el cuerpo
             del documento, no solo en portada.

  Patron D - Errores de reconocimiento de palabra individual en zonas de layout
             complejo (recuadros multi-columna), con el resto de la prosa intacta.
             Deteccion por diccionario en espanol con lista de excepciones de
             jerga de marketing y nombres propios frecuentes.

  Patron E - Colapso de espacios entre palabras al extraer tablas de texto digital
             nativo con celdas angostas (bug de extraccion tabular, no de OCR).

USO:
    python3 protocolo_auditoria.py archivo1.md [archivo2.md ...]
    python3 protocolo_auditoria.py --dir "ruta/a/carpeta/Lecturas"

Requiere: pyspellchecker (pip install pyspellchecker --break-system-packages)
Si no esta instalado, el protocolo sigue funcionando pero el patron D se reporta
como "no evaluado" en vez de detectarse.

LIMITACIONES CONOCIDAS (version 2, validada contra 8 archivos de lecturas de un
curso universitario de marketing el 2026-09; ver detalle y evidencia en
LIMITACIONES_CONOCIDAS mas abajo):

1. Falsos positivos por ruido aislado sin firma estructural. Confirmado en
   un capitulo de libro de texto (documento A), paginas 7, 11, 22 y 26: el protocolo las marca
   MODERADO por fragmentos cortos de ruido (etiquetas de eje de diagrama,
   numeros sueltos, boilerplate de fin de capitulo tipo "MINICASOS Y
   APLICACIONES") que no tienen un patron de texto comun filtrable por regex.
   El contenido de esas 4 paginas es, en su gran mayoria, prosa limpia y
   correctamente reconocida. Se intentaron tres correcciones estadisticas
   (filtro por linea fisica, ratio de diccionario por ventana, mediana de
   stop_ratio por ventana) y ninguna resolvio el problema sin introducir
   regresiones en otras paginas. Tratar estas 4 paginas especificas como
   falsos positivos confirmados hasta nueva evidencia.

2. Limitacion estructural: el protocolo detecta fragmentacion lexica
   (palabras rotas o fusionadas por OCR) pero NO detecta mezcla de parrafos
   completos donde las palabras individuales estan bien escritas pero el
   orden narrativo esta roto (dos columnas de texto intercaladas en
   fragmentos grandes de oracion, no letra por letra). Confirmado en
   un segundo capitulo de libro de texto (documento B), pagina 3: texto con dos parrafos de
   columnas distintas mezclados, verificado a ojo como dano severo, pero
   con metricas de stop_ratio/avg_len/single_ratio normales en todas sus
   ventanas internas. CONSECUENCIA PRACTICA: una pagina puede salir LIMPIO
   o MODERADO en este reporte y aun asi tener dano real de este tipo. El
   protocolo prioriza que revisar primero, NO sustituye la lectura final de
   paginas marcadas como limpias en documentos que provienen de fuente
   escaneada.
"""

import re
import sys
import os
import argparse
import statistics
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Intento de cargar el corrector ortografico en espanol (opcional).
# ---------------------------------------------------------------------------
try:
    from spellchecker import SpellChecker
    _SPELL_ES = SpellChecker(language="es")
    _SPELL_AVAILABLE = True
except Exception:
    _SPELL_ES = None
    _SPELL_AVAILABLE = False

# Jerga de marketing / negocios y nombres propios recurrentes que el
# diccionario en espanol no reconoce pero son terminos validos en este dominio.
# Amplia esta lista si el reporte marca muchos falsos positivos consistentes.
EXCEPCIONES_DICCIONARIO = {
    "marketing", "branding", "benchmarking", "briefing", "coaching",
    "networking", "crowdfunding", "ecommerce", "retail", "vending",
    "kotler", "armstrong", "esan", "starbucks", "ikea", "unilever",
    "pearson", "prentice", "hall", "cid", "pdf", "app", "streamlit",
    "online", "offline", "millennials", "millennial", "app_universal",
    "coca", "cola", "b2b", "b2c", "crm", "roi", "kpi", "seo", "sem",
}

LIMITACIONES_CONOCIDAS = [
    ("Falsos positivos por ruido aislado sin firma estructural",
     "Confirmado en un capitulo de libro de texto (documento A), paginas 7, 11, 22 y 26: marcadas "
     "MODERADO por fragmentos cortos de ruido (etiquetas de eje de diagrama, "
     "numeros sueltos, boilerplate de fin de capitulo) sin patron de texto "
     "comun filtrable por regex. Contenido confirmado como mayormente limpio. "
     "Tres intentos de correccion estadistica no resolvieron esto sin generar "
     "regresiones en otras paginas."),
    ("Limitacion estructural: no detecta mezcla de parrafos completos",
     "El protocolo detecta fragmentacion lexica (palabras rotas o fusionadas) "
     "pero no mezcla de parrafos completos con palabras individualmente bien "
     "escritas y orden narrativo roto. Confirmado en el documento B, "
     "pagina 3. Una pagina puede salir LIMPIO o MODERADO y aun asi tener dano "
     "real de este tipo. El protocolo prioriza que revisar primero, no "
     "sustituye la lectura final de paginas marcadas limpias en documentos de "
     "fuente escaneada."),
]

STOPWORDS_ES = set("""
de la el en que y los del un una para con es se por las lo como mas pero sus
le ya o este si porque esta entre cuando muy sin sobre tambien me hasta hay
donde quien desde todo nos durante todos uno les ni contra otros ese eso ante
ellos mi antes algunos al son fue ser han ha su menos cada otra otro tiene
tienen tan solo eran era fueron sido siendo estas estos aquel aquella cual
cuales quienes qué cómo más así aún sí él ella ellas nosotros ustedes vosotros
""".split())

PAGE_MARKER_RE = re.compile(r"^#{1,3}\s*(?:📄\s*)?P[aá]gina\s+\d+", re.MULTILINE | re.IGNORECASE)
WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)  # letras (incluye acentos), sin digitos ni "_"
LONG_TOKEN_RE = re.compile(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]{20,}")
TABLE_LINE_RE = re.compile(r"^\s*\|")

WINDOW_SIZE_WORDS = 650  # tamano de ventana cuando no hay marcadores de pagina

# ---------------------------------------------------------------------------
# Filtro de "aparato de pagina": lineas que no son prosa del capitulo y que
# distorsionan las metricas si se cuentan como texto (encabezados de capitulo
# repetidos en cada pagina, numeros de pagina sueltos, comentarios HTML de
# imagen, marcado de figura, notas al pie en superindice suelto). Se filtran
# ANTES de calcular metricas, pero el texto original se conserva para mostrar
# fragmentos legibles al usuario.
# ---------------------------------------------------------------------------
APARATO_PAGINA_PATTERNS = [
    re.compile(r"^\s*\d{1,4}\s*$"),
    re.compile(r"^\s*cap[ií]tulo\s+\d+.{0,140}$", re.IGNORECASE),
    re.compile(r"^\s*parte\s+\d+\s*:.{0,140}$", re.IGNORECASE),
    re.compile(r"<!--\s*(start|end) of picture text\s*-->", re.IGNORECASE),
    re.compile(r"^\s*<sup>.{0,40}</sup>\s*$", re.IGNORECASE),
    re.compile(r"^\s*!\[.*?\]\(.*?\)\s*$"),
]


def es_linea_aparato(ln: str) -> bool:
    s = ln.strip()
    if not s:
        return False
    return any(p.search(s) for p in APARATO_PAGINA_PATTERNS)


def limpiar_aparato(text: str) -> str:
    lineas = text.split("\n")
    lineas_limpias = [ln for ln in lineas if not es_linea_aparato(ln)]
    return "\n".join(lineas_limpias)


# ---------------------------------------------------------------------------
# Estructuras de datos
# ---------------------------------------------------------------------------

@dataclass
class BlockMetrics:
    index: int
    label: str            # "Pagina N" o "Ventana N (palabras X-Y)"
    text: str
    n_words: int = 0
    stop_ratio: float = 0.0
    avg_len: float = 0.0
    single_ratio: float = 0.0
    long_tokens: list = field(default_factory=list)      # posibles patron E
    dict_unknown_ratio: float = None                       # posible patron D
    dict_unknown_samples: list = field(default_factory=list)
    accent_suspects: list = field(default_factory=list)   # posible patron A


@dataclass
class FileReport:
    path: str
    total_words: int
    n_blocks: int
    severity: str
    flags: list = field(default_factory=list)   # lista de dicts: {patron, bloque, detalle, muestra}


# ---------------------------------------------------------------------------
# Segmentacion: por pagina si hay marcadores, si no por ventana de palabras
# ---------------------------------------------------------------------------

def segmentar(text: str):
    marcadores = list(PAGE_MARKER_RE.finditer(text))
    if len(marcadores) >= 2:
        bloques = []
        for i, m in enumerate(marcadores):
            start = m.end()
            end = marcadores[i + 1].start() if i + 1 < len(marcadores) else len(text)
            contenido = text[start:end]
            etiqueta = m.group(0).strip().replace("#", "").strip()
            bloques.append((etiqueta, contenido))
        return bloques, "pagina"
    else:
        # sin marcadores: partir en ventanas de tamano fijo por palabra
        palabras_pos = list(re.finditer(r"\S+", text))
        if not palabras_pos:
            return [], "ventana"
        bloques = []
        n = len(palabras_pos)
        i = 0
        idx = 1
        while i < n:
            j = min(i + WINDOW_SIZE_WORDS, n)
            start_char = palabras_pos[i].start()
            end_char = palabras_pos[j - 1].end()
            contenido = text[start_char:end_char]
            etiqueta = f"Ventana {idx} (palabras {i+1}-{j})"
            bloques.append((etiqueta, contenido))
            i = j
            idx += 1
        return bloques, "ventana"


# ---------------------------------------------------------------------------
# Metricas por bloque
# ---------------------------------------------------------------------------

def medir_bloque(index: int, label: str, text: str) -> BlockMetrics:
    texto_metricas = limpiar_aparato(text)
    words = [w for w in WORD_RE.findall(texto_metricas) if len(w) > 0]
    bm = BlockMetrics(index=index, label=label, text=text)
    bm.n_words = len(words)
    if bm.n_words == 0:
        return bm

    bm.stop_ratio = sum(1 for w in words if w.lower() in STOPWORDS_ES) / bm.n_words
    bm.avg_len = sum(len(w) for w in words) / bm.n_words
    bm.single_ratio = sum(1 for w in words if len(w) == 1) / bm.n_words

    # patron E: tokens largos sin espacios, en lineas de tabla o con <br>
    for ln in text.split("\n"):
        if TABLE_LINE_RE.match(ln) or "<br>" in ln:
            for tok in LONG_TOKEN_RE.findall(ln):
                bm.long_tokens.append(tok)

    # patron D: ratio de palabras desconocidas por el diccionario espanol
    if _SPELL_AVAILABLE and bm.n_words >= 15:
        candidatos = [w for w in words if len(w) >= 4 and w.lower() not in EXCEPCIONES_DICCIONARIO]
        if candidatos:
            desconocidas = [w for w in candidatos if w.lower() not in _SPELL_ES]
            bm.dict_unknown_ratio = len(desconocidas) / len(candidatos)
            bm.dict_unknown_samples = desconocidas[:8]

    # patron A: palabras terminadas en "-cion/-sion" style sin tilde donde el
    # diccionario reconoce la version con tilde pero no la version sin tilde
    # ni con otro caracter (heuristica basada en la descripcion del bug, no
    # calibrada con evidencia directa de este corpus)
    stems = ["relaci", "organizaci", "situaci", "funci", "posici", "promoci",
             "gesti", "decisi", "comunicaci", "evaluaci", "segmentaci",
             "satisfacci", "poblaci", "informaci", "atenci", "administraci"]
    for stem in stems:
        for m in re.finditer(stem + r"([^oó\s])n\b", texto_metricas, re.IGNORECASE):
            frag = texto_metricas[max(0, m.start()-25):min(len(texto_metricas), m.end()+25)]
            bm.accent_suspects.append(frag.replace("\n", " "))

    return bm


# ---------------------------------------------------------------------------
# Clasificacion de severidad por bloque, relativa a la mediana del documento
# ---------------------------------------------------------------------------

def severidad_absoluta(b) -> str:
    if b.n_words < 15:
        return "LIMPIO"
    if b.stop_ratio < 0.22 or b.single_ratio > 0.16 or (b.avg_len < 4.2 and b.single_ratio > 0.13):
        return "SEVERO"
    if b.stop_ratio < 0.28 or b.single_ratio > 0.12:
        return "MODERADO"
    return "LIMPIO"


_ORDEN_SEVERIDAD = {"LIMPIO": 0, "LEVE": 1, "MODERADO": 2, "SEVERO": 3}


def peor(sev_a: str, sev_b: str) -> str:
    return sev_a if _ORDEN_SEVERIDAD[sev_a] >= _ORDEN_SEVERIDAD[sev_b] else sev_b


def clasificar_bloques(bloques_metrics: list):
    validos = [b for b in bloques_metrics if b.n_words >= 30]
    if len(validos) < 3:
        for b in bloques_metrics:
            b.severidad = severidad_absoluta(b)
            b._z = (0, 0, 0)
        return

    med_stop = statistics.median(b.stop_ratio for b in validos)
    med_avglen = statistics.median(b.avg_len for b in validos)
    med_single = statistics.median(b.single_ratio for b in validos)

    def madev(vals, med):
        d = [abs(v - med) for v in vals]
        m = statistics.median(d)
        return m if m > 0 else 0.01

    mad_stop = madev([b.stop_ratio for b in validos], med_stop)
    mad_avglen = madev([b.avg_len for b in validos], med_avglen)
    mad_single = madev([b.single_ratio for b in validos], med_single)

    for b in bloques_metrics:
        if b.n_words < 15:
            b.severidad = "LIMPIO"
            b._z = (0, 0, 0)
            continue
        z_stop = (med_stop - b.stop_ratio) / mad_stop        # positivo = peor
        z_avglen = (med_avglen - b.avg_len) / mad_avglen      # positivo = peor
        z_single = (b.single_ratio - med_single) / mad_single  # positivo = peor
        b._z = (z_stop, z_avglen, z_single)

        senales_fuertes = sum([z_stop > 3, z_avglen > 3, z_single > 3])
        senales_moderadas = sum([z_stop > 1.5, z_avglen > 1.5, z_single > 1.5])

        # perfil "patron C": avg_len y single_ratio caen juntos de forma marcada
        colapso_conjunto = (b.avg_len < 4.2 and b.single_ratio > 0.13)

        if colapso_conjunto or senales_fuertes >= 2:
            sev_relativa = "SEVERO"
        elif senales_fuertes >= 1 or senales_moderadas >= 2:
            sev_relativa = "MODERADO"
        elif senales_moderadas >= 1:
            sev_relativa = "LEVE"
        else:
            sev_relativa = "LIMPIO"

        b.severidad = peor(sev_relativa, severidad_absoluta(b))


# ---------------------------------------------------------------------------
# Analisis de un archivo completo
# ---------------------------------------------------------------------------

def analizar_archivo(path: str) -> FileReport:
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    bloques_raw, modo = segmentar(text)
    metrics = [medir_bloque(i, label, contenido) for i, (label, contenido) in enumerate(bloques_raw)]
    clasificar_bloques(metrics)

    total_words = sum(b.n_words for b in metrics)
    flags = []

    for b in metrics:
        sev = getattr(b, "severidad", "LIMPIO")

        if sev in ("SEVERO", "MODERADO"):
            muestra = b.text.strip().replace("\n", " ")
            muestra = re.sub(r"\s+", " ", muestra)[:180]
            if b.avg_len < 4.2 and b.single_ratio > 0.13:
                patron = "C (mezcla de columnas OCR, severidad extrema/generalizada)" if sev == "SEVERO" \
                         else "B/C (posible mezcla de columnas OCR)"
            else:
                patron = "B (mezcla de columnas OCR, localizada)" if sev == "SEVERO" \
                         else "B? (indicios de mezcla de columnas OCR, revisar)"
            flags.append(dict(patron=patron, bloque=b.label, severidad=sev,
                               detalle=f"avg_len={b.avg_len:.2f} stop_ratio={b.stop_ratio:.3f} single_ratio={b.single_ratio:.3f}",
                               muestra=muestra, texto_completo=b.text))

        if b.long_tokens:
            flags.append(dict(patron="E (colapso de espacios en tabla)", bloque=b.label,
                               severidad="MODERADO",
                               detalle=f"{len(b.long_tokens)} token(s) largo(s) sin espacio",
                               muestra=", ".join(b.long_tokens[:4])))

        if b.dict_unknown_ratio is not None and b.dict_unknown_ratio > 0.22 and sev == "LIMPIO":
            # solo se reporta como D cuando el resto del bloque luce sano
            # (si ya esta SEVERO/MODERADO por mezcla de columnas, el ruido de
            # diccionario es redundante con el patron B/C)
            muestra = ", ".join(b.dict_unknown_samples)
            flags.append(dict(patron="D (posibles errores de palabra individual)", bloque=b.label,
                               severidad="LEVE",
                               detalle=f"dict_unknown_ratio={b.dict_unknown_ratio:.2f} (sobre palabras >=4 letras, excluyendo jerga conocida)",
                               muestra=muestra))

        if b.accent_suspects:
            flags.append(dict(patron="A (posible bug CID/acento roto, heuristica no calibrada)", bloque=b.label,
                               severidad="PENDIENTE VERIFICAR",
                               detalle=f"{len(b.accent_suspects)} coincidencia(s) de patron sospechoso",
                               muestra=" | ".join(b.accent_suspects[:3])))

    severidades_presentes = {f["severidad"] for f in flags}
    if "SEVERO" in severidades_presentes:
        severidad_global = "SEVERO"
    elif "MODERADO" in severidades_presentes:
        severidad_global = "MODERADO"
    elif "LEVE" in severidades_presentes or "PENDIENTE VERIFICAR" in severidades_presentes:
        severidad_global = "LEVE"
    else:
        severidad_global = "LIMPIO"

    return FileReport(path=path, total_words=total_words, n_blocks=len(metrics),
                       severity=severidad_global, flags=flags)


# ---------------------------------------------------------------------------
# Salida en texto
# ---------------------------------------------------------------------------

def imprimir_reporte(rep: FileReport, fragmentos_completos: bool = False):
    print("=" * 100)
    print(f"ARCHIVO: {rep.path}")
    print(f"  bloques analizados: {rep.n_blocks}   palabras totales: {rep.total_words}")
    print(f"  SEVERIDAD GLOBAL: {rep.severity}")
    if not _SPELL_AVAILABLE:
        print("  [nota] pyspellchecker no disponible: patron D no evaluado en este archivo.")
    if not rep.flags:
        print("  Sin hallazgos. El archivo no muestra indicios de los patrones conocidos.")
    else:
        for f in rep.flags:
            print(f"  - [{f['severidad']:18s}] {f['patron']}")
            print(f"      bloque: {f['bloque']}")
            print(f"      detalle: {f['detalle']}")
            if fragmentos_completos and f.get("texto_completo"):
                print(f"      --- FRAGMENTO COMPLETO ({f['bloque']}) ---")
                for ln in f["texto_completo"].strip().split("\n"):
                    print(f"      | {ln}")
                print(f"      --- fin fragmento ---")
            else:
                print(f"      muestra: {f['muestra']}")
    print()


def main():
    ap = argparse.ArgumentParser(description="Protocolo de auditoria de calidad para .md de app_universal.py")
    ap.add_argument("paths", nargs="*", help="archivos .md a analizar")
    ap.add_argument("--dir", help="analizar todos los .md dentro de esta carpeta (recursivo)")
    ap.add_argument("--fragmentos-completos", action="store_true",
                     help="mostrar el texto completo de cada pagina/ventana SEVERO o MODERADO")
    args = ap.parse_args()

    archivos = list(args.paths)
    if args.dir:
        for root, _, files in os.walk(args.dir):
            for fn in files:
                if fn.lower().endswith(".md"):
                    archivos.append(os.path.join(root, fn))

    if not archivos:
        print("Uso: python3 protocolo_auditoria.py archivo1.md [archivo2.md ...]")
        print("  o: python3 protocolo_auditoria.py --dir 'ruta/a/carpeta'")
        sys.exit(1)

    if not _SPELL_AVAILABLE:
        print("[AVISO] pyspellchecker no esta instalado. El patron D no se evaluara.")
        print("        Instalar con: pip install pyspellchecker --break-system-packages\n")

    print("[LIMITACIONES CONOCIDAS DE ESTE PROTOCOLO — leer antes de confiar en LIMPIO]")
    for titulo, detalle in LIMITACIONES_CONOCIDAS:
        print(f"  * {titulo}")
        print(f"    {detalle}")
    print()

    resumen = []
    for path in archivos:
        rep = analizar_archivo(path)
        imprimir_reporte(rep, fragmentos_completos=args.fragmentos_completos)
        resumen.append((os.path.basename(path), rep.severity, len(rep.flags)))

    print("=" * 100)
    print("RESUMEN")
    for name, sev, nflags in resumen:
        print(f"  {sev:10s}  {nflags:3d} hallazgo(s)   {name}")


if __name__ == "__main__":
    main()
