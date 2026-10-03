# app_universal.py
# Convertidor Universal de Documentos (.pdf / .docx) a Markdown optimizado para LLMs
#
# Dependencias (instalar si no están presentes):
# pip install streamlit pymupdf pymupdf4llm pdfplumber pytesseract pdf2image python-docx img2table opencv-python

# ---------------------------------------------------------------------------
# IMPORTS — stdlib → terceros → streamlit
# ---------------------------------------------------------------------------
import base64
import io
import logging
import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

import re

import cv2
import fitz  # pymupdf
import pymupdf.layout  # noqa: F401 — requerido para activar OCR automático en pymupdf4llm sobre imágenes embebidas
import numpy as np
import pymupdf4llm
import pdfplumber
import pytesseract
from img2table.document import Image as Img2TableImage
from img2table.ocr import TesseractOCR
from pdf2image import convert_from_bytes
from docx import Document
from docx.oxml.ns import qn

import correccion_deteccion as cd
import correccion_api as ca

import streamlit as st

# ---------------------------------------------------------------------------
# CONSTANTES DE CONFIGURACIÓN — ajustar aquí sin tocar las funciones
# ---------------------------------------------------------------------------
# Rutas de las herramientas externas. Orden de búsqueda para Tesseract:
# variable de entorno TESSERACT_CMD, luego el PATH del sistema, luego el nombre
# "tesseract" (que pytesseract resuelve por su cuenta). Para Poppler: variable
# de entorno POPPLER_PATH (carpeta "bin" de Poppler); si no existe, pdf2image
# lo busca en el PATH del sistema, que es lo habitual en Linux y macOS.
TESSERACT_CMD = os.environ.get("TESSERACT_CMD") or shutil.which("tesseract") or "tesseract"
POPPLER_PATH = os.environ.get("POPPLER_PATH") or None
OCR_DPI = 300
OCR_LANG = "spa"
# PSM 6 = "assume a single uniform block of text". Tesseract con motor LSTM tiene
# un bug documentado y sin fix oficial (tesseract-ocr/tesseract#3489, "Text
# fragments of neighboring lines repeated") que se dispara con más frecuencia
# bajo PSM 3 (segmentación automática de columnas/bloques) en páginas de layout
# mixto: produce repetición mecánica de fragmentos de frase en escalón
# ("una gran cantidad de" -> "gran cantidad de" -> "cantidad de") y, a veces,
# duplicación de bloques completos entre páginas consecutivas. PSM 6 asume un
# único bloque uniforme, evitando la ambigüedad de segmentación que alimenta
# el bug — apropiado aquí porque las páginas de libro son de una sola columna.
OCR_PSM_CONFIG = "--psm 6"
NATIVE_PDF_CHAR_THRESHOLD = 50   # chars en primeras páginas para distinguir nativo vs escaneado
PAGES_FOR_CLASSIFICATION = 10    # cuántas páginas leer en classify_pdf (portadas/hojas en blanco pueden ocupar varias)
PREVIEW_WORD_LIMIT = 1000        # palabras en la vista previa del resultado

# Carpeta donde se exportan .md + PDF original cuando el usuario elige Modo
# Claude Code, para que el prompt generado pueda referenciar rutas absolutas
# reales que una sesión externa de Claude Code pueda abrir.
CLAUDE_CODE_EXPORT_DIR = Path(__file__).resolve().parent / "correccion_claude_code"

# Configurar Tesseract
pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

# Logger para consola (no se muestra al usuario final)
logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DISEÑO VISUAL — fondo "espacio profundo" + superficies de vidrio
#
# Selectores data-testid verificados grepeando el bundle JS instalado
# (streamlit==1.58.0, C:\...\site-packages\streamlit\static\static\js),
# no adivinados. Todo el CSS vive en UNA sola inyección (_inject_global_css,
# llamada una vez al inicio de main()); el theme nativo de widgets se
# complementa con .streamlit/config.toml. Excepción explícita: el texto
# extraído del PDF/DOCX (bloques st.code) NUNCA cambia de tipografía.
# ---------------------------------------------------------------------------
_FONT_PATH = Path(__file__).resolve().parent / "assets" / "fonts" / "SpaceGrotesk-Variable.woff2"


@st.cache_resource
def _cargar_fuente_base64() -> str:
    """Lee el .woff2 local una sola vez por proceso (no hay red involucrada).

    La tipografía es opcional: si el archivo no existe, devuelve una cadena
    vacía y la interfaz usa la pila de fuentes de respaldo (Segoe UI, system-ui).
    """
    if not _FONT_PATH.is_file():
        return ""
    return base64.b64encode(_FONT_PATH.read_bytes()).decode("ascii")


_GLOBAL_CSS_TEMPLATE = """
<style>
@font-face {
    font-family: 'Space Grotesk';
    src: url(data:font/woff2;base64,__FONT_B64__) format('woff2');
    font-weight: 400 700;
    font-style: normal;
    font-display: swap;
}

:root {
    --font-display: 'Space Grotesk', 'Segoe UI Variable Display', 'Segoe UI', -apple-system, system-ui, sans-serif;

    --bg-void: #06050a;

    --glass-bg: rgba(255,255,255,0.07);
    --glass-bg-hover: rgba(255,255,255,0.10);
    --glass-border: rgba(255,255,255,0.18);
    --glass-blur: 28px;
    --glass-saturate: 190%;

    --accent-violet: #8B5CF6;
    --accent-magenta: #D946EF;
    --accent-blue: #3B82F6;

    --status-ok: #34D399;
    --status-leve: #FBBF24;
    --status-moderado: #F97316;
    --status-severo: #F43F5E;

    --text-primary: #F4F3F8;
    --text-secondary: #A8A6B8;

    --track-bg: #15131c;
    --track-border: rgba(255,255,255,0.12);
}

@keyframes fadeInUp {
    from { opacity: 0; transform: translateY(8px); }
    to   { opacity: 1; transform: translateY(0); }
}
@keyframes twinkle {
    0%, 100% { opacity: 0.55; }
    50%      { opacity: 0.9; }
}

/* ---------------- CAPA DE FONDO — nebulosa CSS pura ---------------- */
html, body, [data-testid="stAppViewContainer"] {
    background-color: var(--bg-void) !important;
}
/* stMain vive DENTRO de stAppViewContainer y por defecto trae su propio
   fondo solido, que de otro modo tapa el gradiente del padre. */
[data-testid="stMain"], [data-testid="stMainBlockContainer"], [data-testid="stBottomBlockContainer"] {
    background-color: transparent !important;
}
[data-testid="stAppViewContainer"] {
    background-image:
        radial-gradient(ellipse 900px 700px at 12% 8%,  rgba(139, 92, 246, 0.40), transparent 60%),
        radial-gradient(ellipse 800px 900px at 88% 82%, rgba(59, 130, 246, 0.34), transparent 60%),
        radial-gradient(ellipse 700px 600px at 65% 25%, rgba(217, 70, 239, 0.24), transparent 55%),
        radial-gradient(ellipse 600px 800px at 20% 90%, rgba(59, 130, 246, 0.18), transparent 55%),
        linear-gradient(180deg, #06050a 0%, #0A0812 50%, #06050a 100%);
    background-attachment: fixed;
}
[data-testid="stAppViewContainer"]::before {
    content: "";
    position: fixed;
    inset: 0;
    pointer-events: none;
    z-index: 0;
    background-image:
        radial-gradient(1.5px 1.5px at 10% 20%, #fff, transparent),
        radial-gradient(1px 1px at 25% 65%, #fff, transparent),
        radial-gradient(1.5px 1.5px at 40% 15%, #fff, transparent),
        radial-gradient(1px 1px at 60% 80%, #fff, transparent),
        radial-gradient(1.5px 1.5px at 75% 35%, #fff, transparent),
        radial-gradient(1px 1px at 85% 60%, #fff, transparent),
        radial-gradient(1.5px 1.5px at 92% 12%, #fff, transparent),
        radial-gradient(1px 1px at 15% 85%, #fff, transparent),
        radial-gradient(1px 1px at 50% 45%, #fff, transparent),
        radial-gradient(1.5px 1.5px at 33% 92%, #fff, transparent);
    background-repeat: repeat;
    background-size: 340px 340px;
    opacity: 0.7;
    animation: twinkle 5s ease-in-out infinite;
}
@media (prefers-reduced-motion: reduce) {
    [data-testid="stAppViewContainer"]::before { animation: none; opacity: 0.6; }
}

[data-testid="stHeader"] {
    background: rgba(6,5,10,0.55) !important;
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--glass-border);
}
[data-testid="stAppDeployButton"] { display: none !important; }

[data-testid="stMainBlockContainer"] {
    /* stHeader mide ~60px y flota fijo sobre el contenido (no reserva
       espacio en el flujo normal) — el padding tiene que cubrirlo o el
       header nuevo queda por detras de esa barra. */
    padding-top: 6rem;
    padding-bottom: 4rem;
}

/* ---------------- TIPOGRAFIA — todo excepto codigo extraido --------
   Una regla en `body` NO le gana a la regla propia que Streamlit pone
   directo sobre <p>/<span>/<li> (la herencia siempre pierde frente a
   una declaracion en el propio elemento) — hay que apuntar a esos
   elementos explicitamente. */
body, [data-testid="stAppViewContainer"] {
    font-family: var(--font-display);
    color: var(--text-primary);
}
[data-testid="stAppViewContainer"] p,
[data-testid="stAppViewContainer"] li,
[data-testid="stAppViewContainer"] span:not([data-testid="stIconMaterial"]):not([data-testid="stIconEmoji"]),
[data-testid="stAppViewContainer"] label,
[data-testid="stAppViewContainer"] div {
    font-family: var(--font-display) !important;
}
/* Los iconos de Streamlit (flecha de expander, checks, etc.) son un
   glifo de una fuente de iconos (Material Symbols) referenciado por
   nombre de ligadura — si se les aplica Space Grotesk se ve el texto
   literal ("arrow_down") en vez del icono. */
[data-testid="stIconMaterial"] { font-family: 'Material Symbols Rounded' !important; }
/* Excepcion explicita: el texto extraido del PDF/DOCX (bloques st.code)
   sigue monoespaciado — esta regla va AL FINAL de la hoja a proposito
   para ganarle a la regla amplia de arriba (mismo peso !important,
   gana la que aparece despues en la cascada). */

.app-header {
    display: flex;
    align-items: center;
    gap: 1rem;
    margin: 0 0 0.4rem 0;
    animation: fadeInUp 380ms ease-out both;
}
@media (prefers-reduced-motion: reduce) { .app-header { animation: none; } }
.app-header-icon {
    flex-shrink: 0;
    width: 60px;
    height: 60px;
    border-radius: 16px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 1.8rem;
    background: var(--glass-bg);
    backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-saturate));
    -webkit-backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-saturate));
    border: 1px solid var(--glass-border);
}
.app-header-icon.glow-violet { box-shadow: 0 0 32px rgba(139, 92, 246, 0.45), inset 0 1px 0 rgba(255,255,255,0.14); }
.app-header-icon.glow-blue   { box-shadow: 0 0 32px rgba(59, 130, 246, 0.45), inset 0 1px 0 rgba(255,255,255,0.14); }
.app-header-title {
    font-size: 2.1rem;
    font-weight: 700;
    letter-spacing: -0.03em;
    color: var(--text-primary);
    line-height: 1.15;
}
.app-header-title-sub {
    font-size: 1.3rem;
    font-weight: 500;
    letter-spacing: -0.02em;
    color: var(--text-secondary);
    line-height: 1.15;
}

/* ---------------- BADGES de severidad ------------------------------ */
.badge {
    display: inline-flex;
    align-items: center;
    gap: 0.35em;
    font-family: var(--font-display);
    font-weight: 600;
    font-size: 0.9rem;
    padding: 0.3em 0.9em;
    border-radius: 999px;
    letter-spacing: 0.01em;
}
.badge-ok       { color: var(--status-ok);       background: rgba(52, 211, 153, 0.14); border: 1px solid rgba(52, 211, 153, 0.4);  box-shadow: 0 0 16px rgba(52, 211, 153, 0.25); }
.badge-leve     { color: var(--status-leve);     background: rgba(251, 191, 36, 0.14); border: 1px solid rgba(251, 191, 36, 0.4);  box-shadow: 0 0 16px rgba(251, 191, 36, 0.25); }
.badge-moderado { color: var(--status-moderado); background: rgba(249, 115, 22, 0.14); border: 1px solid rgba(249, 115, 22, 0.4);  box-shadow: 0 0 16px rgba(249, 115, 22, 0.25); }
.badge-severo   { color: var(--status-severo);   background: rgba(244, 63, 94, 0.14);  border: 1px solid rgba(244, 63, 94, 0.4);   box-shadow: 0 0 16px rgba(244, 63, 94, 0.25); }

/* ---------------- TARJETAS — expanders como vidrio ------------------ */
[data-testid="stExpander"] {
    background: var(--glass-bg);
    backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-saturate));
    -webkit-backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-saturate));
    border: 1px solid var(--glass-border);
    border-radius: 16px;
    box-shadow: 0 8px 32px rgba(0,0,0,0.5), inset 0 1px 0 rgba(255,255,255,0.12);
    overflow: hidden;
    margin-bottom: 0.6rem;
}
[data-testid="stExpander"] summary {
    font-family: var(--font-display);
    padding: 0.4rem 0.2rem;
}
[data-testid="stExpander"] summary:hover {
    background: var(--glass-bg-hover);
}
[data-testid="stExpanderDetails"] { padding-top: 0.4rem; }

/* ---------------- METRICAS -------------------------------------- */
[data-testid="stMetric"] {
    background: var(--glass-bg);
    backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-saturate));
    -webkit-backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-saturate));
    border: 1px solid var(--glass-border);
    border-radius: 14px;
    padding: 0.8rem 1rem;
    box-shadow: 0 4px 20px rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,255,255,0.10);
}
[data-testid="stMetricLabel"] { color: var(--text-secondary) !important; }
[data-testid="stMetricValue"] { color: var(--text-primary) !important; }

/* ---------------- ALERTAS ---------------------------------------- */
[data-testid="stAlert"] {
    background: var(--glass-bg) !important;
    backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-saturate));
    -webkit-backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-saturate));
    border: 1px solid var(--glass-border) !important;
    border-left: 3px solid var(--accent-violet) !important;
    border-radius: 12px !important;
    box-shadow: 0 4px 20px rgba(0,0,0,0.4);
}

/* ---------------- STATUS (st.status durante conversion) ----------- */
[data-testid="stStatusWidget"] {
    background: var(--glass-bg) !important;
    backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-saturate));
    border: 1px solid var(--glass-border) !important;
    border-radius: 14px !important;
    box-shadow: 0 4px 20px rgba(0,0,0,0.4);
}

/* ---------------- BOTONES ------------------------------------------ */
[data-testid^="stBaseButton"] {
    font-family: var(--font-display) !important;
    background: var(--glass-bg) !important;
    backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-saturate));
    -webkit-backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-saturate));
    border: 1px solid var(--glass-border) !important;
    border-radius: 14px !important;
    color: var(--text-primary) !important;
    box-shadow: 0 4px 20px rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,255,255,0.12);
    transition: all 150ms ease;
}
[data-testid^="stBaseButton"]:hover:not(:disabled) {
    background: var(--glass-bg-hover) !important;
    box-shadow: 0 4px 24px rgba(0,0,0,0.45), 0 0 30px rgba(139, 92, 246, 0.3), inset 0 1px 0 rgba(255,255,255,0.16);
    transform: translateY(-1px);
}
[data-testid^="stBaseButton"]:active:not(:disabled) { transform: scale(0.98); }
[data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"] {
    background: linear-gradient(135deg, rgba(139, 92, 246, 0.35), rgba(59, 130, 246, 0.35)) !important;
    border: 1px solid rgba(139, 92, 246, 0.5) !important;
    box-shadow: 0 4px 24px rgba(0,0,0,0.4), 0 0 30px rgba(139, 92, 246, 0.35);
}
@media (prefers-reduced-motion: reduce) {
    [data-testid^="stBaseButton"] { transition: none; }
    [data-testid^="stBaseButton"]:hover:not(:disabled) { transform: none; }
    [data-testid^="stBaseButton"]:active:not(:disabled) { transform: none; }
}

/* ---------------- INPUTS / UPLOADER / CHECKBOX / RADIO / SLIDER ---- */
[data-testid="stTextInputRootElement"], [data-testid="stNumberInputContainer"] {
    background: var(--glass-bg) !important;
    border: 1px solid var(--glass-border) !important;
    border-radius: 12px !important;
}
[data-testid="stTextInputRootElement"]:focus-within, [data-testid="stNumberInputContainer"]:focus-within {
    box-shadow: 0 0 0 3px rgba(139, 92, 246, 0.25);
}
[data-testid="stFileUploaderDropzone"] {
    background: var(--glass-bg) !important;
    backdrop-filter: blur(var(--glass-blur)) saturate(var(--glass-saturate));
    border: 1px dashed var(--glass-border) !important;
    border-radius: 16px !important;
}
[data-testid="stFileUploaderDropzone"]:hover {
    border-color: rgba(139, 92, 246, 0.5) !important;
    box-shadow: 0 0 30px rgba(139, 92, 246, 0.2);
}
[data-testid="stCheckbox"] label, [data-testid="stRadioGroup"] label {
    font-family: var(--font-display);
}
[data-testid="stSlider"] {
    background: var(--glass-bg);
    border: 1px solid var(--glass-border);
    border-radius: 14px;
    padding: 0.6rem 1rem 0.2rem 1rem;
}

hr, [data-testid="stDivider"] { border-color: var(--glass-border) !important; }

/* Excepcion tipografica del bloque de markdown extraido — va al final a
   proposito, ver comentario junto a la regla amplia de arriba. */
code, pre,
[data-testid="stCodeBlock"], [data-testid="stCodeBlock"] * {
    font-family: "SF Mono", "Cascadia Code", Consolas, monospace !important;
}
</style>
"""


def _inject_global_css():
    css = _GLOBAL_CSS_TEMPLATE.replace("__FONT_B64__", _cargar_fuente_base64())
    st.markdown(css, unsafe_allow_html=True)


def _header_html(icono: str, fuerte: str, secundario: str, glow: str = "violet") -> str:
    """Reemplazo animado de st.title(): placa de icono con resplandor +
    titulo en dos niveles de peso tipografico, con fade-in de 380ms."""
    return (
        '<div class="app-header">'
        f'<div class="app-header-icon glow-{glow}">{icono}</div>'
        '<div>'
        f'<div class="app-header-title">{fuerte}</div>'
        f'<div class="app-header-title-sub">{secundario}</div>'
        '</div></div>'
    )


_BADGE_TIPO_POR_SEVERIDAD = {"LIMPIO": "ok", "LEVE": "leve", "MODERADO": "moderado", "SEVERO": "severo"}


def _badge_html(texto: str, severidad: str) -> str:
    tipo = _BADGE_TIPO_POR_SEVERIDAD.get(severidad, "ok")
    return f'<span class="badge badge-{tipo}">{texto}</span>'


# ---------------------------------------------------------------------------
# FASE A — Detección de tipo de archivo
# ---------------------------------------------------------------------------

def detect_file_type(filename: str) -> str:
    """Retorna 'DOCX', 'PDF' o 'UNSUPPORTED' según la extensión del archivo."""
    ext = Path(filename).suffix.lower()
    if ext == ".docx":
        return "DOCX"
    elif ext == ".pdf":
        return "PDF"
    else:
        return "UNSUPPORTED"


# ---------------------------------------------------------------------------
# FASE B — Clasificación de PDF: nativo vs escaneado
# ---------------------------------------------------------------------------

def classify_pdf(file_bytes: bytes) -> str:
    """Retorna 'PDF_NATIVE' o 'PDF_SCANNED' analizando texto en las primeras páginas."""
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception as exc:
        raise ValueError(f"No se pudo abrir el PDF (¿está protegido o corrupto?): {exc}") from exc

    pages_to_check = min(PAGES_FOR_CLASSIFICATION, len(doc))
    total_chars = 0

    for i in range(pages_to_check):
        try:
            total_chars += len(doc[i].get_text("text").strip())
        except Exception:
            pass  # página ilegible; contar como vacía

    doc.close()

    if total_chars > NATIVE_PDF_CHAR_THRESHOLD:
        return "PDF_NATIVE"
    return "PDF_SCANNED"


# ---------------------------------------------------------------------------
# FASE C1 — Procesamiento DOCX
# ---------------------------------------------------------------------------

HEADING_MAP = {
    "Heading 1": "#",
    "Heading 2": "##",
    "Heading 3": "###",
    "Heading 4": "####",
    "Heading 5": "#####",
}


def _table_to_markdown(table) -> str:
    """Convierte una tabla python-docx a sintaxis Markdown."""
    rows = []
    for i, row in enumerate(table.rows):
        cells = [cell.text.replace("\n", " ").strip() for cell in row.cells]
        rows.append("| " + " | ".join(cells) + " |")
        if i == 0:
            rows.append("| " + " | ".join(["---"] * len(cells)) + " |")
    return "\n".join(rows)


def process_docx(file_bytes: bytes) -> str:
    """Extrae texto, headings jerarquizados y tablas de un archivo DOCX."""
    doc = Document(io.BytesIO(file_bytes))
    parts = []

    # Iteramos sobre el cuerpo del documento en orden DOM (párrafos + tablas)
    body = doc.element.body
    for child in body:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag

        if tag == "p":
            # Párrafo normal o heading
            para_text = child.text_content() if hasattr(child, "text_content") else ""
            # Reconstruir texto del párrafo desde runs
            para_obj = None
            for p in doc.paragraphs:
                if p._element is child:
                    para_obj = p
                    break
            if para_obj is None:
                continue
            text = para_obj.text.strip()
            if not text:
                continue
            style_name = para_obj.style.name if para_obj.style else ""
            prefix = HEADING_MAP.get(style_name, "")
            if prefix:
                parts.append(f"{prefix} {text}")
            else:
                parts.append(text)

        elif tag == "tbl":
            # Tabla: buscarla en doc.tables por referencia de elemento
            for tbl in doc.tables:
                if tbl._element is child:
                    parts.append(_table_to_markdown(tbl))
                    break

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# FASE C2 — Procesamiento PDF Escaneado (OCR)
# ---------------------------------------------------------------------------

def process_pdf_scanned(
    file_bytes: bytes,
    progress_callback=None,
) -> str:
    """Ejecuta OCR con Tesseract sobre cada página del PDF escaneado."""
    fitz_doc = fitz.open(stream=file_bytes, filetype="pdf")
    total = len(fitz_doc)
    fitz_doc.close()
    sections = []

    for n in range(1, total + 1):
        if progress_callback:
            progress_callback(n - 1, total)
        # Renderizado página por página (no todo el PDF de una vez) para que
        # la barra de progreso empiece a moverse desde la primera página en
        # vez de quedar congelada hasta terminar de rasterizar el documento entero.
        img = convert_from_bytes(
            file_bytes,
            dpi=OCR_DPI,
            poppler_path=POPPLER_PATH,
            first_page=n,
            last_page=n,
        )[0]
        try:
            text = pytesseract.image_to_string(img, lang=OCR_LANG, config=OCR_PSM_CONFIG)
            text = _collapse_stutter_repetitions(text)
        except Exception as exc:
            log.warning("OCR falló en página %d: %s", n, exc)
            text = "⚠️ OCR falló para esta página."

        # Intentar extraer tablas de la imagen con img2table
        table_md = _img2table_from_pil(img)
        if table_md:
            table_md = f"### 📊 Tablas — Página {n}\n\n{table_md}"

        page_content = f"---\n## 📄 Página {n}\n\n{text.strip()}"
        if table_md:
            page_content += f"\n\n{table_md}"
        sections.append(page_content)

    if progress_callback:
        progress_callback(total, total)

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# FASE C3 — Procesamiento PDF Nativo
# ---------------------------------------------------------------------------

EMPTY_CELL_RATIO_THRESHOLD = 0.60  # descartar tabla si más del 60% de celdas están vacías
PAGE_TEXT_THRESHOLD = 50           # chars mínimos por página para considerar texto nativo válido
IMG2TABLE_MIN_CONFIDENCE = 50      # confianza mínima (0-100) para aceptar celdas de img2table

# TesseractOCR se instancia de forma lazy para garantizar que PATH esté configurado.
# img2table verifica Tesseract vía subprocess, no mediante pytesseract.tesseract_cmd.
_img2table_ocr_instance = None


def _get_img2table_ocr() -> TesseractOCR:
    """Retorna la instancia de TesseractOCR, creándola la primera vez con PATH corregido."""
    global _img2table_ocr_instance
    if _img2table_ocr_instance is None:
        # Inyectar el directorio de Tesseract en PATH para que img2table lo encuentre
        # Solo hace falta cuando TESSERACT_CMD es una ruta absoluta; si es el
        # nombre "tesseract", ya está en el PATH.
        if Path(TESSERACT_CMD).is_absolute():
            tesseract_dir = str(Path(TESSERACT_CMD).parent)
            if tesseract_dir not in os.environ.get('PATH', ''):
                os.environ['PATH'] = tesseract_dir + os.pathsep + os.environ.get('PATH', '')
        _img2table_ocr_instance = TesseractOCR(
            n_threads=1,
            lang=OCR_LANG,
            tessdata_dir=None,
        )
    return _img2table_ocr_instance


def _is_image_only_page(md_text: str) -> bool:
    """Retorna True si el contenido es solo marcadores de imagen y no contiene texto real."""
    # Eliminar los placeholders de imagen y ver si queda texto sustantivo
    cleaned = re.sub(r'==>\s*picture\s*\[.*?\]\s*intentionally omitted\s*<==', '', md_text)
    return len(cleaned.strip()) < PAGE_TEXT_THRESHOLD


# ---------------------------------------------------------------------------
# Post-procesamiento — mitigación parcial del bug de repetición de Tesseract
# ---------------------------------------------------------------------------
# Aunque PSM 6 reduce mucho la frecuencia del bug LSTM de repetición de texto
# (ver comentario junto a OCR_PSM_CONFIG; tesseract-ocr/tesseract#3489), no lo
# elimina al 100% porque es un bug de la librería, no de configuración.
#
# El patrón completo que produce ese bug tiene dos formas:
#   1) Bloque repetido de forma EXACTA varias veces seguidas
#      ("pueden configurar pueden configurar configurar las propias").
#   2) "Cola decreciente": cada repetición es un sufijo cada vez más corto
#      de la anterior, no idéntica a ella
#      ("una gran cantidad de" -> "gran cantidad de" -> "cantidad de" -> "de").
#
# Esta función solo colapsa el caso (1), que es detectable sin ambigüedad.
# El caso (2) se dejó fuera deliberadamente: para colapsarlo hace falta
# decidir qué versión de la frase es la "correcta", y una heurística de texto
# no tiene forma confiable de saberlo sin riesgo de borrar contenido legítimo
# que por coincidencia tenga estructura de sufijos anidados. Por eso la
# mitigación real para el caso (2) es evitar que el bug ocurra en origen
# (PSM 6 + 300 DPI + Tesseract actualizado), no repararlo después del hecho.
def _collapse_stutter_repetitions(text: str, min_words: int = 2, max_words: int = 15) -> str:
    """
    Colapsa bloques de 2 a max_words palabras que aparecen repetidos de forma
    EXACTA e inmediatamente consecutiva (incluye 3+ repeticiones seguidas) a
    una sola ocurrencia. Conservador por diseño: no toca repeticiones de una
    sola palabra (evita falsos positivos en construcciones legítimas como
    "que que" en habla citada) ni colas decrecientes no idénticas.
    """
    if not text:
        return text
    words = text.split(" ")
    n = len(words)
    out = []
    i = 0
    while i < n:
        collapsed = False
        # probar de mayor a menor longitud de bloque para preferir la
        # repetición más larga cuando hay varias posibles
        max_wl = min(max_words, (n - i) // 2)
        for wl in range(max_wl, min_words - 1, -1):
            block = words[i:i + wl]
            if words[i + wl:i + 2 * wl] == block:
                out.extend(block)
                j = i + 2 * wl
                while words[j:j + wl] == block:
                    j += wl
                i = j
                collapsed = True
                break
        if not collapsed:
            out.append(words[i])
            i += 1
    return " ".join(out)


_REAL_WORD_PATTERN = re.compile(r"[A-Za-zÁÉÍÓÚÑÜáéíóúñü]{3,}")
OCR_QUALITY_DROP_THRESHOLD = 0.35  # caída relativa máxima tolerada en densidad de texto reconocible


def _real_word_density(text: str) -> float:
    """
    Proporción de caracteres que forman parte de palabras alfabéticas reconocibles
    (secuencias de 3+ letras) sobre el total de caracteres. Sirve como proxy barato
    de calidad de texto: texto humano normal ronda 0.6-0.8; ruido de OCR sobre
    fotografías o texturas decorativas suele caer por debajo de 0.2.
    """
    if not text.strip():
        return 0.0
    words = _REAL_WORD_PATTERN.findall(text)
    real_chars = sum(len(w) for w in words)
    return real_chars / max(len(text), 1)


CONTENT_LOSS_THRESHOLD = 0.5  # si md_page conserva menos de esta fracción del volumen nativo, es sospechoso


def _pymupdf4llm_degraded_quality(native_text: str, md_page: str) -> bool:
    """
    Retorna True si el resultado de pymupdf4llm es de peor calidad que el texto
    nativo puro de la página. Cubre dos patrones de degradación distintos:

    1) RUIDO: el texto sobreviviente es abundante pero ilegible (típico de OCR
       sobre una fotografía decorativa — ver caso de portada con fondo de campus).
       Se detecta por una caída relevante en densidad de palabras reconocibles.

    2) PÉRDIDA: el texto sobreviviente es limpio pero casi inexistente frente al
       volumen real de la página (típico cuando pymupdf4llm reconoce solo un
       logo o sello y descarta el resto del texto nativo — ver caso de página
       de referencias bibliográficas). La densidad de palabras no delata esto,
       porque el fragmento que sobrevive puede ser perfectamente "limpio"; lo
       que delata el problema es cuánto volumen se perdió, no su calidad.

    Solo se evalúa cuando la página YA tenía texto nativo sustantivo; páginas
    genuinamente escaneadas (sin texto nativo) no entran en esta comparación,
    porque ahí cualquier texto reconocible es una mejora, no una degradación.
    """
    native_stripped = native_text.strip()
    if len(native_stripped) < PAGE_TEXT_THRESHOLD:
        return False  # no hay línea base nativa confiable para comparar

    md_stripped = md_page.strip()

    # Patrón 2 — pérdida masiva de volumen, independiente de la calidad del resto
    retained_fraction = len(md_stripped) / len(native_stripped)
    if retained_fraction < CONTENT_LOSS_THRESHOLD:
        return True

    # Patrón 1 — ruido: cae la densidad de palabras reconocibles
    native_density = _real_word_density(native_text)
    md_density = _real_word_density(md_page)
    if native_density <= 0:
        return False

    relative_drop = (native_density - md_density) / native_density
    return relative_drop > OCR_QUALITY_DROP_THRESHOLD


_LIGATURE_CLUSTER_PATTERN = re.compile(r"ffi|ffl|fi|fl")
_WORD_PATTERN_FOR_LIGATURE_REPAIR = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]+")


def _simulate_ligature_drop(word: str) -> str:
    """Reproduce el error de pymupdf4llm sobre una palabra correcta: colapsa
    los clústeres de ligadura tipográfica ('fi', 'fl', 'ffi', 'ffl') a un
    simple 'f', tal como ocurre en el Markdown que produce la librería."""
    return _LIGATURE_CLUSTER_PATTERN.sub("f", word)


def _build_ligature_repair_map(reference_text: str) -> dict:
    """Construye un mapa palabra-rota -> palabra-correcta a partir de un
    texto de referencia limpio (sin el bug). Si dos palabras distintas
    colapsan a la misma forma rota, se descarta esa entrada por ambigua
    para no arriesgar una sustitución incorrecta."""
    repair_map: dict = {}
    for word in _WORD_PATTERN_FOR_LIGATURE_REPAIR.findall(reference_text):
        broken = _simulate_ligature_drop(word)
        if broken == word:
            continue
        if broken in repair_map and repair_map[broken] != word:
            repair_map[broken] = None
        else:
            repair_map[broken] = word
    return {k: v for k, v in repair_map.items() if v is not None}


def _repair_dropped_ligatures(text: str, reference_text: str) -> str:
    """
    pymupdf4llm pierde la 'i' o la 'l' de las ligaduras tipográficas
    'fi'/'fl' en PDFs cuya fuente las embebe como glifos de un subconjunto
    auxiliar (en este documento, spans con nombre de fuente terminado en
    "+fb"), produciendo palabras rotas como "Identifcation" o "infammatory".
    fitz sí decodifica esas ligaduras correctamente cuando no se fuerza su
    preservación (flag TEXT_PRESERVE_LIGATURES desactivado), así que se usa
    ese texto nativo de la misma página como referencia para reconstruir las
    palabras afectadas dentro del Markdown ya generado (texto y tablas).
    """
    repair_map = _build_ligature_repair_map(reference_text)
    if not repair_map:
        return text

    pattern = re.compile(r"\b(" + "|".join(re.escape(k) for k in repair_map) + r")\b")
    return pattern.sub(lambda m: repair_map[m.group(0)], text)


def _img2table_from_pil(pil_image) -> str:
    """Extrae tablas de una imagen PIL usando img2table; retorna Markdown o cadena vacía."""
    try:
        # Convertir PIL → numpy array BGR (formato que espera OpenCV/img2table)
        img_array = cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)
        doc = Img2TableImage(src=img_array)
        tables = doc.extract_tables(
            ocr=_get_img2table_ocr(),
            implicit_rows=True,
            borderless_tables=True,
            min_confidence=IMG2TABLE_MIN_CONFIDENCE,
        )
        if not tables:
            return ""
        rows_md = []
        for tbl in tables:
            tbl_rows = tbl.html_repr()  # img2table puede dar HTML; convertimos a Markdown
            # Extraer celdas desde la representación interna de img2table
            if not tbl.content:
                continue
            md_rows = []
            for row_idx, row in tbl.content.items():
                cells = [cell.value.strip() if cell.value else '' for cell in row.values()]
                md_rows.append('| ' + ' | '.join(cells) + ' |')
                if row_idx == 0:
                    md_rows.append('| ' + ' | '.join(['---'] * len(cells)) + ' |')
            if md_rows:
                rows_md.append('\n'.join(md_rows))
        return '\n\n'.join(rows_md)
    except Exception as exc:
        log.debug('img2table falló: %s', exc)
        return ''


def _is_table_mostly_empty(data: list) -> bool:
    """Retorna True si la tabla tiene más del 60% de celdas vacías o None."""
    all_cells = [c for row in data for c in row]
    if not all_cells:
        return True
    empty = sum(1 for c in all_cells if c is None or str(c).strip() == "")
    return (empty / len(all_cells)) > EMPTY_CELL_RATIO_THRESHOLD


ROTATED_TEXT_RATIO_THRESHOLD = 0.5  # más de esta fracción de chars rotados: pdfplumber no puede ordenar el texto


def _page_is_mostly_rotated_text(plumber_page) -> bool:
    """
    Retorna True si la mayoría de los caracteres de la página están rotados
    (p. ej. una tabla apaisada incrustada en una página vertical). pdfplumber
    ordena las celdas asumiendo texto horizontal, así que con texto rotado
    arma las palabras al revés (ej. "Pharmacological" -> "lacigolocamrahP").
    pymupdf4llm sí interpreta la matriz de rotación de cada carácter y ya
    deja esas tablas correctamente ordenadas dentro de md_page, por lo que
    en este caso es preferible omitir la extracción de pdfplumber en vez de
    agregar una tabla duplicada e ilegible.
    """
    chars = plumber_page.chars
    if not chars:
        return False
    rotated = sum(1 for c in chars if not c.get("upright", True))
    return (rotated / len(chars)) > ROTATED_TEXT_RATIO_THRESHOLD


def _pdfplumber_tables(plumber_doc, page_index: int, page_label: int) -> str:
    """Extrae tablas de una página con pdfplumber y las retorna como Markdown."""
    try:
        page = plumber_doc.pages[page_index]
        if _page_is_mostly_rotated_text(page):
            log.debug(
                "Página %d: texto mayormente rotado — se omite pdfplumber.find_tables() "
                "(pymupdf4llm ya la incluye correctamente en el texto de la página).",
                page_label,
            )
            return ""
        tables = page.find_tables()
        if not tables:
            return ""
        md_tables = [f"### 📊 Tablas — Página {page_label}"]
        valid_tables_found = False
        for tbl in tables:
            data = tbl.extract()
            if not data:
                continue
            # Descartar tablas que son mayormente decorativas o bordes vacíos
            if _is_table_mostly_empty(data):
                log.debug("Tabla descartada en página %d por exceso de celdas vacías.", page_label)
                continue
            valid_tables_found = True
            rows = []
            for i, row in enumerate(data):
                cells = [str(c or "").replace("\n", " ").strip() for c in row]
                rows.append("| " + " | ".join(cells) + " |")
                if i == 0:
                    rows.append("| " + " | ".join(["---"] * len(cells)) + " |")
            md_tables.append("\n".join(rows))
        # Si todas las tablas fueron descartadas, no emitir el encabezado
        if not valid_tables_found:
            return ""
        return "\n\n".join(md_tables)
    except Exception as exc:
        log.debug("pdfplumber no pudo extraer tablas en página %d: %s", page_index + 1, exc)
        return ""


def _table_redundant_with_text(table_md: str, reference_text: str, overlap_threshold: float = 0.8) -> bool:
    """
    Retorna True si el contenido de una tabla ya está cubierto por reference_text,
    señal de que la tabla es un fragmento redundante (típicamente una detección
    rota de pdfplumber sobre texto que en realidad es prosa corrida, no una
    tabla real) y no aporta información nueva sobre el texto ya presente.

    Se compara por palabras de 4+ letras (evita ruido de conectores/artículos)
    normalizadas a minúsculas; si al menos overlap_threshold de las palabras
    distintivas de la tabla ya aparecen en el texto de referencia, se considera
    redundante.
    """
    table_words = set(re.findall(r"[a-zA-ZÁÉÍÓÚÑáéíóúñ]{4,}", table_md.lower()))
    if not table_words:
        return False
    reference_words = set(re.findall(r"[a-zA-ZÁÉÍÓÚÑáéíóúñ]{4,}", reference_text.lower()))
    overlap = len(table_words & reference_words) / len(table_words)
    return overlap >= overlap_threshold


def process_pdf_native(
    file_bytes: bytes,
    progress_callback=None,
) -> str:
    """Extrae texto y tablas de un PDF nativo usando pymupdf4llm para layout y columnas."""
    fitz_doc = fitz.open(stream=file_bytes, filetype="pdf")
    plumber_doc = pdfplumber.open(io.BytesIO(file_bytes))

    total = len(fitz_doc)
    sections = []

    for i in range(total):
        n = i + 1
        if progress_callback:
            progress_callback(i, total)

        page_label_suffix = ""  # se marca con [OCR] si la página cae al fallback

        # Capturar el texto nativo ANTES de invocar pymupdf4llm: esta librería
        # muta el estado interno de la página al procesarla (su heurístico de OCR
        # reescribe el texto extraíble), así que leerlo después ya daría un valor
        # contaminado y la comparación de calidad perdería su línea base confiable.
        # Se desactiva TEXT_PRESERVE_LIGATURES para que fitz decodifique glifos de
        # ligadura ('fi', 'fl', etc.) en sus letras reales en vez de un solo
        # carácter Unicode de ligadura; esto también deja este texto listo para
        # usarse como referencia limpia en _repair_dropped_ligatures más abajo.
        native_text = fitz_doc[i].get_text(
            "text", flags=fitz.TEXTFLAGS_TEXT & ~fitz.TEXT_PRESERVE_LIGATURES
        )

        try:
            # pymupdf4llm maneja detección de columnas, orden de lectura y tablas sin bordes.
            # ocr_language='spa' asegura que el OCR interno sobre imágenes embebidas
            # (mapas conceptuales, infografías) use español en vez del inglés por defecto.
            md_page = pymupdf4llm.to_markdown(
                fitz_doc,
                pages=[i],
                show_progress=False,
                ocr_language=OCR_LANG,
            )
        except Exception as exc:
            log.warning("pymupdf4llm falló en página %d: %s", n, exc)
            md_page = ""

        if md_page:
            # pymupdf4llm pierde letras de ligaduras tipográficas ('fi'/'fl') en
            # fuentes que las embeben como glifos de un subconjunto auxiliar;
            # se reparan usando native_text (ya limpio) como referencia.
            md_page = _repair_dropped_ligatures(md_page, native_text)

        # Salvaguarda de calidad: el OCR automático de pymupdf4llm sobre imágenes
        # embebidas puede, en fotografías complejas (portadas con fondo decorativo),
        # producir texto ilegible en vez de reconocer que no hay texto real que leer.
        # Si esto degrada la página respecto a su propio texto nativo, se descarta
        # el resultado contaminado y se usa el texto nativo puro como base.
        used_native_fallback = _pymupdf4llm_degraded_quality(native_text, md_page)
        if used_native_fallback:
            log.info(
                "Página %d: pymupdf4llm degradó la calidad del texto (probable OCR "
                "ruidoso sobre imagen decorativa) — usando texto nativo como respaldo.",
                n,
            )
            md_page = native_text.strip()

        # Fallback OCR: página sin texto suficiente o con solo marcadores de imagen
        needs_ocr = (
            len(md_page.strip()) < PAGE_TEXT_THRESHOLD
            or _is_image_only_page(md_page)
        )

        pil_image = None  # se reutiliza para OCR de texto y para img2table

        if needs_ocr:
            log.info("Página %d sin texto nativo — aplicando OCR.", n)
            try:
                page_images = convert_from_bytes(
                    file_bytes,
                    dpi=OCR_DPI,
                    poppler_path=POPPLER_PATH,
                    first_page=n,
                    last_page=n,
                )
                if page_images:
                    pil_image = page_images[0]
                    md_page = pytesseract.image_to_string(pil_image, lang=OCR_LANG, config=OCR_PSM_CONFIG)
                    md_page = _collapse_stutter_repetitions(md_page)
                    page_label_suffix = " [OCR]"
                else:
                    md_page = "⚠️ No se pudo extraer texto de esta página."
            except Exception as exc:
                log.warning("OCR falló en página %d: %s", n, exc)
                md_page = "⚠️ OCR falló para esta página."

        # Extracción de tablas: pdfplumber para páginas nativas, img2table para páginas OCR
        if pil_image is not None:
            # Página escaneada: usar img2table sobre la imagen rasterizada
            table_md = _img2table_from_pil(pil_image)
            if table_md:
                table_md = f"### 📊 Tablas — Página {n}\n\n{table_md}"
        else:
            # Página nativa: pdfplumber detecta tablas vectoriales
            table_md = _pdfplumber_tables(plumber_doc, i, n)

            # Si se usó el respaldo de texto nativo (patrón "pérdida de contenido"),
            # pdfplumber pudo haber detectado como "tabla" un fragmento roto de la
            # misma prosa que el respaldo ya contiene completa (ej. una referencia
            # bibliográfica partida en dos columnas). En ese caso el fragmento no
            # aporta nada nuevo y solo duplica contenido — se omite. Tablas con
            # estructura genuina (que el respaldo no captura como tal) se conservan.
            if table_md and used_native_fallback and _table_redundant_with_text(table_md, md_page):
                log.info("Página %d: tabla de pdfplumber omitida por ser redundante con el respaldo nativo.", n)
                table_md = ""

        page_content = f"---\n## 📄 Página {n}{page_label_suffix}\n\n{md_page.strip()}"
        if table_md:
            page_content += f"\n\n{table_md}"
        sections.append(page_content)

    if progress_callback:
        progress_callback(total, total)

    fitz_doc.close()
    plumber_doc.close()

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# PIPELINE PRINCIPAL
# ---------------------------------------------------------------------------

def run_conversion_pipeline(uploaded_file) -> dict:
    """
    Orquesta las fases A→B→C y retorna un dict con markdown, tipo, páginas y palabras.
    Recibe el objeto UploadedFile de Streamlit; no usa st.* internamente.
    """
    filename = uploaded_file.name
    file_bytes = uploaded_file.read()

    # --- Fase A ---
    file_type = detect_file_type(filename)
    if file_type == "UNSUPPORTED":
        raise ValueError(f"Formato no soportado: '{Path(filename).suffix}'. Sube un PDF o DOCX.")

    result = {
        "markdown": "",
        "doc_type": file_type,
        "pages": None,
        "words": 0,
    }

    # Placeholders para progreso (se rellenan en la UI, aquí solo callbacks)
    _progress_bar = None
    _status_text = None

    # Los callbacks se inyectan desde la UI; aquí definimos el default vacío.
    def noop_callback(current, total):
        pass

    if file_type == "DOCX":
        result["markdown"] = process_docx(file_bytes)
        result["pages"] = "N/A"

    elif file_type == "PDF":
        # --- Fase B ---
        pdf_class = classify_pdf(file_bytes)
        result["doc_type"] = pdf_class

        if pdf_class == "PDF_SCANNED":
            result["markdown"] = process_pdf_scanned(file_bytes, progress_callback=noop_callback)
        else:
            result["markdown"] = process_pdf_native(file_bytes, progress_callback=noop_callback)

        # Contar páginas desde el markdown generado
        result["pages"] = result["markdown"].count("## 📄 Página")

    result["words"] = len(result["markdown"].split())
    return result


def _convert_single_file_with_progress(filename: str, file_bytes: bytes) -> dict:
    """
    Convierte un archivo (PDF o DOCX) a Markdown, mostrando progreso en la UI
    de Streamlit. Retorna un dict con markdown, doc_type, pages, words.
    Lanza excepción si el formato no es soportado o si el pipeline falla;
    el llamador (batch o archivo único) decide cómo manejar ese error.
    """
    file_type = detect_file_type(filename)

    if file_type == "UNSUPPORTED":
        raise ValueError(f"Formato no soportado: '{Path(filename).suffix}'. Sube un PDF o DOCX.")

    markdown_result = ""
    doc_type_label = ""
    pages_count = None

    st.write("🔍 Detectando tipo de archivo...")

    if file_type == "DOCX":
        doc_type_label = "DOCX (Word)"
        st.write("📝 Tipo detectado: **DOCX**. Extrayendo texto, headings y tablas...")
        markdown_result = process_docx(file_bytes)
        pages_count = "N/A"

    elif file_type == "PDF":
        st.write("🔎 Clasificando PDF (nativo vs escaneado)...")
        pdf_class = classify_pdf(file_bytes)

        if pdf_class == "PDF_SCANNED":
            doc_type_label = "PDF Escaneado (OCR)"
            st.warning("⚠️ Detectado: **PDF Escaneado**. Iniciando OCR con Tesseract...")

            progress_bar = st.progress(0.0, text="Preparando imágenes de página...")
            status_text = st.empty()

            def scanned_callback(current, total):
                pct = current / total if total > 0 else 0
                progress_bar.progress(pct, text=f"OCR — Página {current} de {total}...")
                status_text.write(f"🔠 Procesando página **{current}** de **{total}**...")

            markdown_result = process_pdf_scanned(file_bytes, progress_callback=scanned_callback)
            progress_bar.progress(1.0, text="✅ OCR completado")
            status_text.empty()

        else:
            doc_type_label = "PDF Nativo"
            st.write("📑 Tipo detectado: **PDF Nativo**. Extrayendo texto y tablas...")

            progress_bar = st.progress(0.0, text="Iniciando extracción...")
            status_text = st.empty()

            def native_callback(current, total):
                pct = current / total if total > 0 else 0
                progress_bar.progress(pct, text=f"Extrayendo página {current + 1} de {total}...")
                status_text.write(f"📄 Procesando página **{current + 1}** de **{total}**...")

            markdown_result = process_pdf_native(file_bytes, progress_callback=native_callback)
            progress_bar.progress(1.0, text="✅ Extracción completada")
            status_text.empty()

        pages_count = markdown_result.count("## 📄 Página")

    return {
        "markdown": markdown_result,
        "doc_type": doc_type_label,
        "pages": pages_count,
        "words": len(markdown_result.split()),
    }


def _sanitize_zip_filename(raw_name: str) -> str:
    """
    Limpia un nombre de archivo ZIP escrito por el usuario: quita caracteres
    invalidos en Windows, recorta espacios en los extremos y asegura ".zip".
    """
    name = raw_name.strip()
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = name.strip()
    if not name.lower().endswith(".zip"):
        name += ".zip"
    return name


def _build_zip_of_results(successful_results: list) -> bytes:
    """
    Empaqueta los .md de una lista de resultados exitosos en un .zip en memoria.
    successful_results: lista de dicts con 'output_filename' y 'markdown'.
    """
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in successful_results:
            zf.writestr(item["output_filename"], item["markdown"].encode("utf-8"))
    zip_buffer.seek(0)
    return zip_buffer.getvalue()


# ---------------------------------------------------------------------------
# CORRECCION CON IA — helpers de aplicacion e integracion con los modulos
# correccion_deteccion.py y correccion_api.py
# ---------------------------------------------------------------------------

_PAGE_MARKER_APP_RE = re.compile(r"---\n## 📄 Página (\d+)(?: \[OCR\])?\n\n")


def _apply_page_corrections(markdown_original: str, correcciones: dict) -> str:
    """
    correcciones: dict[int, str] pagina -> texto_corregido. Reemplaza solo el
    contenido de las paginas indicadas; deja marcadores y el resto del
    documento intactos.
    """
    matches = list(_PAGE_MARKER_APP_RE.finditer(markdown_original))
    if not matches:
        return markdown_original

    resultado = markdown_original[:matches[0].start()]
    for i, m in enumerate(matches):
        page_number = int(m.group(1))
        content_start = m.end()
        content_end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown_original)
        marcador = markdown_original[m.start():m.end()]
        if page_number in correcciones:
            contenido = correcciones[page_number].rstrip() + "\n\n"
        else:
            contenido = markdown_original[content_start:content_end]
        resultado += marcador + contenido
    return resultado


def _exportar_archivos_para_claude_code(r: dict) -> tuple:
    CLAUDE_CODE_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stem = Path(r["filename"]).stem
    md_path = CLAUDE_CODE_EXPORT_DIR / f"{stem}.md"
    pdf_path = CLAUDE_CODE_EXPORT_DIR / r["filename"]
    md_path.write_text(r["markdown"], encoding="utf-8")
    pdf_path.write_bytes(r["file_bytes"])
    return md_path.resolve(), pdf_path.resolve()


def _generar_prompt_claude_code(r: dict, numeros_pagina: list) -> str:
    md_path, pdf_path = _exportar_archivos_para_claude_code(r)
    paginas_str = ", ".join(str(n) for n in numeros_pagina)
    return (
        "Necesito que corrijas errores de OCR en un documento convertido a Markdown.\n\n"
        f"Archivo Markdown ya convertido: {md_path}\n"
        f"PDF original de referencia: {pdf_path}\n"
        f"Paginas a revisar y corregir: {paginas_str}\n\n"
        "Para cada una de esas paginas: abre el PDF original en la pagina "
        "correspondiente, compara contra el texto ya convertido en el "
        "Markdown, y muestra en este chat el fragmento ANTES y DESPUES de tu "
        "correccion propuesta, pagina por pagina.\n\n"
        "Estandar de fidelidad: cualquier corte fisico irrecuperable en el "
        "texto se marca como \"[ILEGIBLE - verificar contra PDF original]\" y "
        "explicas por que lo marcaste asi. Nunca reconstruyas ese fragmento "
        "por inferencia gramatical, aunque el sentido parezca obvio.\n\n"
        "No escribas nada en el archivo .md todavia. Espera mi confirmacion "
        "explicita en este chat antes de guardar cualquier cambio en disco."
    )


_METRICA_LEGIBLE = {
    "stop_ratio": "pocas palabras de enlace de lo esperado",
    "avg_len": "palabras más cortas de lo esperado en promedio",
    "single_ratio": "muchos tokens de una sola letra",
}


def _render_fragmentos(estado: dict, page_audits: list, key_prefix: str):
    fragmentos = cd.seleccionar_fragmentos(page_audits)
    if not fragmentos:
        return

    estado.setdefault("fragmentos_limite", cd.FRAGMENT_DEFAULT_LIMIT)

    col_titulo, col_ayuda = st.columns([10, 1])
    col_titulo.markdown("**Fragmentos de ejemplo**")
    with col_ayuda.popover("❓"):
        st.write(
            f"Se muestran hasta {cd.FRAGMENT_DEFAULT_LIMIT} fragmentos por defecto. "
            f"Las páginas SEVERO tienen prioridad de espacio garantizado (hasta "
            f"{cd.FRAGMENT_SEVERO_RESERVED_SLOTS}), porque son las que el protocolo "
            "de auditoría admite no detectar bien cuando el desorden narrativo es "
            "grande. Las páginas MODERADO se eligen buscando variedad de tipos de "
            "error antes de repetir uno ya representado. El orden va de la página "
            "más temprana a la más tardía dentro de cada grupo."
        )

    resultados_por_pagina = {}
    if estado["resultado_api"]:
        resultados_por_pagina = {x.page_number: x for x in estado["resultado_api"]["resultados"]}

    limite = estado["fragmentos_limite"]
    for p in fragmentos[:limite]:
        resultado = resultados_por_pagina.get(p.page_number)
        indicador = "🟡 " if (resultado and resultado.ok and resultado.confidence in ("media", "baja")) else ""
        metrica = _METRICA_LEGIBLE.get(p.metric_flag, p.metric_flag)
        preview = "\n".join(p.raw_text.strip().splitlines()[:3])

        st.markdown(f"{indicador}**Página {p.page_number}** ({p.severity}) — {metrica}")
        st.code(preview, language="markdown")

        with st.expander("Ver más", expanded=False):
            st.caption("Texto original completo")
            st.code(p.raw_text, language="markdown")
            if resultado and resultado.ok:
                st.caption("Texto corregido")
                st.code(resultado.texto_corregido, language="markdown")
                st.write(f"Confianza: **{resultado.confidence}**")
                if resultado.motivo_baja_confianza:
                    st.caption(resultado.motivo_baja_confianza)
            else:
                st.caption("Esta página aún no ha sido corregida.")

    if len(fragmentos) > limite:
        if st.button("Ver más ejemplos", key=f"{key_prefix}_ver_mas_fragmentos"):
            estado["fragmentos_limite"] += cd.FRAGMENT_EXPAND_STEP


_LIQUID_PROGRESS_CSS = """
<style>
@keyframes correccion_ia_wave_scroll {
    from { transform: translateY(0); }
    to   { transform: translateY(-40px); }
}
@media (prefers-reduced-motion: reduce) {
    .correccion_ia_liquid_wave { animation: none !important; }
}
</style>
"""


def _liquid_progress_html(pct: float, texto: str) -> str:
    """Barra de progreso naranja con borde ondulado (superficie de liquido) en
    el limite del relleno; alto/ancho de la onda ajustados visualmente para
    que se note sin dominar la barra."""
    pct_clamped = max(0.0, min(1.0, pct)) * 100
    alto, ancho_onda = 28, 22
    return (
        _LIQUID_PROGRESS_CSS +
        f'<div style="width:100%; height:{alto}px; background:var(--track-bg, #15131c); border-radius:6px; '
        f'overflow:hidden; position:relative; border:1px solid var(--track-border, rgba(255,255,255,0.12));">'
        f'<div style="position:absolute; top:0; left:0; bottom:0; width:{pct_clamped:.2f}%; '
        f'background:var(--status-moderado, #F97316);"></div>'
        f'<div style="position:absolute; top:-2px; bottom:-2px; '
        f'left:calc({pct_clamped:.2f}% - {ancho_onda // 2}px); width:{ancho_onda}px; overflow:hidden;">'
        f'<svg class="correccion_ia_liquid_wave" width="{ancho_onda}" height="100%" viewBox="0 0 16 40" preserveAspectRatio="none" '
        f'style="display:block; animation: correccion_ia_wave_scroll 1.1s linear infinite;">'
        f'<path d="M16,0 L16,20 L8,20 C2,20 2,10 8,10 C14,10 14,0 8,0 Z '
        f'M16,20 L16,40 L8,40 C2,40 2,30 8,30 C14,30 14,20 8,20 Z" fill="#FDBA74"/>'
        f'</svg></div></div>'
        f'<div style="font-size:0.85em; color:var(--text-secondary, #A8A6B8); margin-top:4px; '
        f'font-family:var(--font-display, sans-serif);">{texto}</div>'
    )


def _obtener_estado_correccion(r: dict) -> dict:
    """
    Estado de correccion para un archivo, inicializado y con page_audits ya
    calculados (idempotente: si ya se calculo antes, no vuelve a segmentar).
    Punto unico de acceso a session_state["correccion"][filename] — tanto la
    vista de conversion (resumen agregado) como el dashboard (metricas,
    tarjetas, botones de batch) pasan por aqui, para no duplicar el calculo
    de severidad en cada lugar que lo necesita.
    """
    filename = r["filename"]
    st.session_state.setdefault("correccion", {})
    estado = st.session_state["correccion"].setdefault(filename, {
        "page_audits": None,
        "modo_segmentacion": None,
        "modo_correccion": None,
        "resultado_api": None,
        "registro_claude_code": None,
    })
    if estado["page_audits"] is None:
        page_audits, modo = cd.build_page_audits(r["markdown"])
        estado["page_audits"] = page_audits
        estado["modo_segmentacion"] = modo

    # El radio de modo vive en su propia key de session_state
    # ("correccion_{filename}_modo"), que Streamlit ya actualiza ANTES de
    # que el script vuelva a correr tras el clic. Sincronizarla aqui evita
    # que el dashboard (que agrupa archivos por modo antes de que el
    # expander de cada archivo llegue a instanciar ese radio) vea el valor
    # de un rerun atras.
    widget_key = f"correccion_{filename}_modo"
    if widget_key in st.session_state:
        estado["modo_correccion"] = st.session_state[widget_key]

    return estado


def _porcentajes_por_archivo(successful: list) -> list:
    return [cd.calcular_porcentajes(_obtener_estado_correccion(r)["page_audits"]) for r in successful]


def _archivos_elegibles(successful: list) -> list:
    """Archivos en modo 'pagina' con al menos una pagina MODERADO o SEVERO."""
    elegibles = []
    for r in successful:
        estado = _obtener_estado_correccion(r)
        if estado["modo_segmentacion"] != "pagina":
            continue
        porcentajes = cd.calcular_porcentajes(estado["page_audits"])
        if porcentajes["moderado_count"] > 0 or porcentajes["severo_count"] > 0:
            elegibles.append(r)
    return elegibles


def _ejecutar_modo_api(r: dict, estado: dict, paginas: list):
    progress_placeholder = st.empty()
    progress_placeholder.markdown(_liquid_progress_html(0.0, "Preparando corrección..."), unsafe_allow_html=True)

    def _cb(actual, total, page_number, fase):
        pct = actual / total if total > 0 else 0
        sufijo = " (revisando con más detalle)" if fase == "escalando" else ""
        texto = f"Corrigiendo página {actual} de {total}...{sufijo}"
        progress_placeholder.markdown(_liquid_progress_html(pct, texto), unsafe_allow_html=True)

    resultados = ca.procesar_lote(r["file_bytes"], paginas, POPPLER_PATH, OCR_DPI, progress_callback=_cb)
    progress_placeholder.markdown(_liquid_progress_html(1.0, "✅ Corrección completada"), unsafe_allow_html=True)

    estado["resultado_api"] = {
        "resultados": resultados,
        "incluir": {x.page_number: True for x in resultados if x.ok},
    }


def _ejecutar_modo_api_batch(archivos_api: list):
    """
    archivos_api: lista de (r, estado, paginas), una entrada por archivo en
    Modo API. Llama a ca.procesar_lote() una vez POR ARCHIVO — no cambia su
    logica interna ni la de procesar_pagina() — pero combina el progreso de
    todas las llamadas en una sola barra liquida con el total real del batch,
    traduciendo el (actual, total) de cada archivo a la posicion acumulada.
    """
    total_batch = sum(len(paginas) for _, _, paginas in archivos_api)
    acumulado = 0
    progress_placeholder = st.empty()
    progress_placeholder.markdown(_liquid_progress_html(0.0, "Preparando corrección..."), unsafe_allow_html=True)

    for r, estado, paginas in archivos_api:
        def _cb(actual, total_archivo, page_number, fase, _acumulado=acumulado, _filename=r["filename"]):
            pct = (_acumulado + actual) / total_batch if total_batch > 0 else 0
            sufijo = " (revisando con más detalle)" if fase == "escalando" else ""
            texto = f"Corrigiendo página {_acumulado + actual} de {total_batch} — {_filename}...{sufijo}"
            progress_placeholder.markdown(_liquid_progress_html(pct, texto), unsafe_allow_html=True)

        resultados = ca.procesar_lote(r["file_bytes"], paginas, POPPLER_PATH, OCR_DPI, progress_callback=_cb)
        estado["resultado_api"] = {
            "resultados": resultados,
            "incluir": {x.page_number: True for x in resultados if x.ok},
        }
        acumulado += len(paginas)

    progress_placeholder.markdown(_liquid_progress_html(1.0, "✅ Corrección completada"), unsafe_allow_html=True)


def _render_resultado_modo_api(r: dict, estado: dict, key_prefix: str):
    resultado_api = estado["resultado_api"]
    resultados = resultado_api["resultados"]
    incluir = resultado_api["incluir"]

    st.markdown("**Resultado de la corrección**")
    for x in sorted(resultados, key=lambda x: x.page_number):
        if x.excluida:
            st.warning(f"Página {x.page_number} excluida — {x.motivo_exclusion}")
            continue
        etiqueta = f"Página {x.page_number}"
        if x.escalada_desde_sonnet:
            etiqueta += " — escalada a Opus"
        checked = st.checkbox(
            etiqueta,
            value=incluir.get(x.page_number, True),
            key=f"{key_prefix}_incluir_{x.page_number}",
        )
        incluir[x.page_number] = checked
        with st.expander(f"Ver corrección — página {x.page_number}", expanded=False):
            st.write(f"Confianza: **{x.confidence}**")
            if x.motivo_baja_confianza:
                st.caption(x.motivo_baja_confianza)
            st.code(x.texto_corregido, language="markdown")

    if st.button("Confirmar y aplicar correcciones", key=f"{key_prefix}_confirmar"):
        correcciones = {
            x.page_number: x.texto_corregido
            for x in resultados if x.ok and incluir.get(x.page_number)
        }
        r["markdown"] = _apply_page_corrections(r["markdown"], correcciones)
        estado["correcciones_aplicadas_msg"] = f"{len(correcciones)} página(s) aplicada(s) al archivo final."
        st.rerun()

    if estado.get("correcciones_aplicadas_msg"):
        st.success(estado["correcciones_aplicadas_msg"])


def _render_modo_claude_code(r: dict, estado: dict, key_prefix: str):
    if estado.get("claude_code_prompt"):
        st.markdown("**Prompt para Claude Code**")
        st.code(estado["claude_code_prompt"], language="text")
        st.caption("Copia este texto (ícono de copiar arriba a la derecha del bloque) y pégalo en una sesión de Claude Code fuera de esta app.")

        if st.button("Ya terminé la corrección en Claude Code", key=f"{key_prefix}_listo"):
            estado["claude_code_mostrar_resumen"] = True

        if estado.get("claude_code_mostrar_resumen"):
            resumen = st.text_area(
                "Pega aquí un resumen breve de lo que se corrigió",
                key=f"{key_prefix}_resumen",
            )
            if st.button("Confirmar resumen", key=f"{key_prefix}_confirmar_resumen"):
                estado["registro_claude_code"] = {
                    "archivo": r["filename"],
                    "fecha_hora": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "modo": "Claude Code",
                    "resumen": resumen,
                }
                estado["claude_code_mostrar_resumen"] = False
                estado["claude_code_prompt"] = None


def _render_correccion_ia(r: dict, conectividad_ok: bool):
    filename = r["filename"]
    estado = _obtener_estado_correccion(r)
    page_audits = estado["page_audits"]
    modo = estado["modo_segmentacion"]

    st.markdown("#### 🔎 Corrección con IA")

    if modo != "pagina":
        st.info(cd.MENSAJE_MODO_VENTANA)
        return

    porcentajes = cd.calcular_porcentajes(page_audits)
    severidad_global, icono = cd.severidad_global(porcentajes)

    resultados_ok = estado["resultado_api"]["resultados"] if estado["resultado_api"] else []
    resultados_ok = [x for x in resultados_ok if x.ok]
    ilegibles_count = sum(len(x.zonas_ilegibles) for x in resultados_ok)
    escaladas_count = sum(1 for x in resultados_ok if x.escalada_desde_sonnet)
    aprobadas_count = sum(1 for v in estado["resultado_api"]["incluir"].values() if v) if estado["resultado_api"] else 0

    col_badge, col1, col2, col3, col4 = st.columns([2, 1, 1, 1, 1])
    col_badge.markdown(_badge_html(f"{icono} {severidad_global}", severidad_global), unsafe_allow_html=True)
    col1.metric("Zonas ILEGIBLE", ilegibles_count)
    col2.metric("Corregidas con IA", len(resultados_ok))
    col3.metric("Auto / Escaladas", f"{len(resultados_ok) - escaladas_count} / {escaladas_count}")
    col4.metric("Aprobadas", aprobadas_count)

    if estado.get("registro_claude_code"):
        reg = estado["registro_claude_code"]
        st.markdown(f"**Corrección vía Claude Code** — {reg['fecha_hora']} — {reg['resumen']}")

    if severidad_global == "LIMPIO":
        st.caption("Sin errores detectados por el protocolo de auditoría.")
        return

    key_prefix = f"correccion_{filename}"

    col_um1, col_um2 = st.columns(2)
    umbral_moderado = col_um1.slider(
        "Umbral MODERADO (%)", 0, 100, int(cd.DEFAULT_THRESHOLD * 100), key=f"{key_prefix}_umbral_mod"
    )
    umbral_severo = col_um2.slider(
        "Umbral SEVERO (%)", 0, 100, int(cd.DEFAULT_THRESHOLD * 100), key=f"{key_prefix}_umbral_sev"
    )
    umbrales = cd.evaluar_umbrales(porcentajes, umbral_moderado / 100, umbral_severo / 100)

    st.write(
        f"MODERADO: {porcentajes['moderado_pct'] * 100:.0f}% "
        f"({porcentajes['moderado_count']} de {porcentajes['total_paginas']} páginas)"
    )
    st.write(
        f"SEVERO: {porcentajes['severo_pct'] * 100:.0f}% "
        f"({porcentajes['severo_count']} de {porcentajes['total_paginas']} páginas)"
    )
    st.caption(
        f"Informativo, sin corrección automática: {porcentajes['leve_count']} página(s) LEVE, "
        f"{porcentajes['accent_suspect_count']} página(s) con posible bug de acento (patrón A, heurística no calibrada)."
    )

    if not conectividad_ok:
        st.caption("⚠️ Sin conexión al API de Anthropic en este momento — el Modo API no estará disponible.")

    modo_correccion = st.radio(
        "Modo de corrección",
        options=["api", "claude_code"],
        format_func=lambda v: (
            "Modo API (uso académico externo, con costo por token)" if v == "api"
            else "Modo Claude Code (uso personal, sin costo adicional)"
        ),
        index=None,
        key=f"{key_prefix}_modo",
    )
    estado["modo_correccion"] = modo_correccion

    modo_habilita_botones = modo_correccion is not None and (modo_correccion == "claude_code" or conectividad_ok)
    btn_moderado_habilitado = umbrales["moderado_supera_umbral"] and modo_habilita_botones
    btn_severo_habilitado = umbrales["severo_supera_umbral"] and modo_habilita_botones

    col_b1, col_b2 = st.columns(2)
    btn_moderado = col_b1.button(
        "Corregir solo errores menores", disabled=not btn_moderado_habilitado,
        key=f"{key_prefix}_btn_mod", use_container_width=True,
    )
    btn_severo = col_b2.button(
        "Corregir menores y graves", disabled=not btn_severo_habilitado,
        key=f"{key_prefix}_btn_sev", use_container_width=True,
    )

    _render_fragmentos(estado, page_audits, key_prefix)

    paginas_moderado = [p for p in page_audits if p.severity == "MODERADO"]
    paginas_severo = [p for p in page_audits if p.severity == "SEVERO"]

    lote_a_procesar = None
    if btn_moderado:
        lote_a_procesar = paginas_moderado
    elif btn_severo:
        lote_a_procesar = paginas_moderado + paginas_severo

    if lote_a_procesar is not None:
        if modo_correccion == "api":
            _ejecutar_modo_api(r, estado, lote_a_procesar)
        elif modo_correccion == "claude_code":
            numeros_pagina = sorted(p.page_number for p in lote_a_procesar)
            estado["claude_code_prompt"] = _generar_prompt_claude_code(r, numeros_pagina)

    if estado["resultado_api"]:
        _render_resultado_modo_api(r, estado, key_prefix)

    _render_modo_claude_code(r, estado, key_prefix)


def _render_dashboard():
    """
    Vista dedicada de corrección, alternativa a la de conversión. Trabaja
    directo sobre st.session_state["batch_results"] — nunca se llega aquí
    sin que ya exista (el botón que activa vista_activa="dashboard" solo se
    muestra despues de una conversion exitosa), pero se cubre el caso raro
    igual, por si algo dejo el estado a medias.
    """
    if "batch_results" not in st.session_state:
        st.session_state["vista_activa"] = "conversion"
        st.rerun()
        return

    batch_results = st.session_state["batch_results"]
    successful = [r for r in batch_results if r["ok"]]
    elegibles = _archivos_elegibles(successful)

    if st.button("← Volver a Conversión"):
        st.session_state["vista_activa"] = "conversion"
        st.rerun()

    st.markdown(_header_html("📊", "Dashboard", "de Corrección", glow="blue"), unsafe_allow_html=True)

    if not elegibles:
        st.info("No hay archivos con páginas MODERADO o SEVERO en este batch.")
        return

    conectividad_ok = ca.verificar_conectividad()

    # Métricas agregadas — mismo cálculo que el gancho de la vista de
    # conversión (cd.calcular_metricas_batch), aquí con el detalle completo.
    metricas = cd.calcular_metricas_batch(_porcentajes_por_archivo(successful))
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Archivos en el batch", metricas["total_archivos"])
    col2.metric("LIMPIO", metricas["limpio_count"])
    col3.metric("MODERADO", metricas["moderado_count"])
    col4.metric("SEVERO", metricas["severo_count"])
    col5.metric("Páginas dañadas", metricas["paginas_danadas_total"])

    # Calculadora de costo — solo el estimado que el usuario ingrese, nunca
    # un costo de referencia del sistema.
    costo_por_pagina = st.number_input(
        "Tu costo estimado por página (USD)",
        min_value=0.0,
        value=None,
        placeholder="Ingresa tu propio estimado, ej. 0.05 — no es un dato oficial de Anthropic",
        step=0.01,
    )
    if costo_por_pagina is not None:
        total_estimado = metricas["paginas_danadas_total"] * costo_por_pagina
        st.write(f"Estimado total del batch: ${total_estimado:.2f} (según tu costo por página ingresado)")

    st.divider()

    # Botones de batch — agrupan los archivos elegibles por el modo que el
    # usuario ya eligio individualmente (dentro de cada tarjeta, mas abajo).
    archivos_api, archivos_claude_code = [], []
    for r in elegibles:
        estado = _obtener_estado_correccion(r)
        paginas = [p for p in estado["page_audits"] if p.severity in ("MODERADO", "SEVERO")]
        if estado["modo_correccion"] == "api":
            archivos_api.append((r, estado, paginas))
        elif estado["modo_correccion"] == "claude_code":
            archivos_claude_code.append((r, estado, paginas))

    if archivos_api:
        if st.button(
            "Corregir todos los archivos elegibles (Modo API)",
            disabled=not conectividad_ok,
            use_container_width=True,
        ):
            _ejecutar_modo_api_batch(archivos_api)

    if archivos_claude_code:
        if st.button(
            "Generar prompts para todos los archivos elegibles (Modo Claude Code)",
            use_container_width=True,
        ):
            for r, estado, paginas in archivos_claude_code:
                numeros_pagina = sorted(p.page_number for p in paginas)
                estado["claude_code_prompt"] = _generar_prompt_claude_code(r, numeros_pagina)

        pendientes = [
            (r, estado["claude_code_prompt"])
            for r, estado, _ in archivos_claude_code if estado.get("claude_code_prompt")
        ]
        if pendientes:
            st.markdown("**Prompts generados — Modo Claude Code**")
            for r, prompt in pendientes:
                st.markdown(f"**{r['filename']}**")
                st.code(prompt, language="text")

    st.divider()

    # Tarjetas por archivo — colapsadas por defecto, con el resumen visible
    # sin expandir; el contenido completo reutiliza _render_correccion_ia
    # tal cual, sin duplicar su logica.
    for r in elegibles:
        estado = _obtener_estado_correccion(r)
        porcentajes = cd.calcular_porcentajes(estado["page_audits"])
        severidad, icono = cd.severidad_global(porcentajes)
        resumen = (
            f"{icono} {r['filename']} — MODERADO {porcentajes['moderado_pct'] * 100:.0f}% "
            f"({porcentajes['moderado_count']}) · SEVERO {porcentajes['severo_pct'] * 100:.0f}% "
            f"({porcentajes['severo_count']})"
        )
        with st.expander(resumen, expanded=False):
            _render_correccion_ia(r, conectividad_ok)


# ---------------------------------------------------------------------------
# INTERFAZ STREAMLIT
# ---------------------------------------------------------------------------

def _mostrar_batch_cacheado():
    """Reutiliza batch_results ya calculado en session_state, sin repetir el pipeline."""
    batch_results = st.session_state["batch_results"]
    successful = [r for r in batch_results if r["ok"]]
    failed = [r for r in batch_results if not r["ok"]]
    _render_resumen_y_resultados(batch_results, successful, failed, len(batch_results))


def _vaciar_cargador():
    """Callback de "Vaciar lista": recrea el file_uploader vacio cambiando su key."""
    st.session_state["uploader_gen"] = st.session_state.get("uploader_gen", 0) + 1


def main():
    st.set_page_config(
        page_title="Convertidor Universal para LLMs",
        page_icon="📄",
        layout="centered",
    )
    _inject_global_css()

    if st.session_state.get("vista_activa", "conversion") == "dashboard":
        _render_dashboard()
        return

    # 1. ENCABEZADO
    st.markdown(
        _header_html("📄", "Convertidor Universal", "de Documentos para LLMs", glow="violet"),
        unsafe_allow_html=True,
    )
    st.markdown(
        """
        Convierte archivos **PDF** y **DOCX** a **Markdown de alta calidad**, 
        optimizado para ser consumido por modelos de lenguaje como Claude, GPT o Gemini.  
        El Markdown estructurado preserva headings, tablas y el orden lógico del contenido,  
        lo que mejora significativamente la comprensión del LLM comparado con texto plano.

        **Formatos soportados:** `.pdf` (nativo o escaneado) · `.docx`  
        **Puedes subir varios archivos a la vez** — se procesan uno por uno y cada uno se descarga por separado.
        """
    )

    # 2. UPLOADER (multi-archivo)
    # Streamlit no permite vaciar un file_uploader por codigo; la unica forma
    # es cambiarle la key, lo que lo recrea vacio. _vaciar_cargador() incrementa
    # este contador desde el boton "Vaciar lista".
    uploaded_files = st.file_uploader(
        "📂 Selecciona uno o varios archivos para convertir",
        type=["pdf", "docx"],
        accept_multiple_files=True,
        help="Sube uno o varios PDF (nativo o escaneado) o documentos Word (.docx).",
        key=f"uploader_{st.session_state.get('uploader_gen', 0)}",
    )

    if not uploaded_files:
        # st.file_uploader pierde los archivos cargados si el widget no se
        # renderiza en algun rerun intermedio — pasa exactamente al volver
        # del Dashboard de Corrección, donde main() ni siquiera llega a
        # llamar a st.file_uploader mientras vista_activa == "dashboard".
        # Que el widget vuelva vacio NO significa que el usuario haya
        # perdido su batch: si ya hay resultados calculados, se muestran
        # esos en vez de pedir "sube archivos" y descartarlos.
        if "batch_results" in st.session_state:
            _mostrar_batch_cacheado()
            return
        st.info("👆 Sube uno o varios archivos para habilitar la conversión.")
        return

    total_files = len(uploaded_files)
    total_size_kb = sum(f.size for f in uploaded_files) / 1024
    if total_files == 1:
        st.markdown(f"**Archivo cargado:** `{uploaded_files[0].name}` ({uploaded_files[0].size / 1024:.1f} KB)")
    else:
        st.markdown(f"**{total_files} archivos cargados** ({total_size_kb:.1f} KB en total)")
        with st.expander("📋 Ver archivos seleccionados", expanded=False):
            for f in uploaded_files:
                st.write(f"- `{f.name}` ({f.size / 1024:.1f} KB)")

    convert_label = "🚀 Convertir Documento" if total_files == 1 else f"🚀 Convertir {total_files} Documentos"
    col_convertir, col_vaciar = st.columns([3, 2])
    with col_convertir:
        convert_btn = st.button(convert_label, type="primary", use_container_width=True)
    with col_vaciar:
        st.button(
            "🗑️ Vaciar lista",
            on_click=_vaciar_cargador,
            use_container_width=True,
            help="Quita todos los archivos cargados de una vez. Los resultados ya convertidos no se borran.",
        )

    if not convert_btn and "batch_results" not in st.session_state:
        return

    if not convert_btn:
        # Rerun disparado por un widget de corrección (no por "Convertir"):
        # reusamos los resultados ya calculados en vez de repetir el pipeline.
        _mostrar_batch_cacheado()
        return

    # 3. PROCESAMIENTO — un archivo a la vez, cada uno con su propio panel de estado
    batch_results = []  # cada item: dict con filename, output_filename, ok, markdown/error, metadata

    overall_progress = None
    if total_files > 1:
        overall_progress = st.progress(0.0, text=f"Procesando archivo 1 de {total_files}...")

    for idx, uploaded_file in enumerate(uploaded_files, start=1):
        filename = uploaded_file.name
        file_bytes = uploaded_file.read()

        if overall_progress:
            overall_progress.progress(
                (idx - 1) / total_files,
                text=f"Procesando archivo {idx} de {total_files}: {filename}",
            )

        with st.status(f"⏳ {filename}", expanded=(total_files == 1)) as status_box:
            try:
                result = _convert_single_file_with_progress(filename, file_bytes)

                if not result["markdown"].strip():
                    status_box.update(label=f"⚠️ {filename} — resultado vacío", state="error")
                    batch_results.append({
                        "filename": filename,
                        "ok": False,
                        "error": "La conversión produjo un resultado vacío (archivo en blanco o ilegible).",
                    })
                    continue

                status_box.update(label=f"✅ {filename}", state="complete", expanded=False)
                batch_results.append({
                    "filename": filename,
                    "output_filename": Path(filename).stem + ".md",
                    "ok": True,
                    "markdown": result["markdown"],
                    "doc_type": result["doc_type"],
                    "pages": result["pages"],
                    "words": result["words"],
                    "file_bytes": file_bytes,
                })

            except Exception as exc:
                log.exception("Error no controlado procesando %s", filename)
                status_box.update(label=f"❌ {filename} — error", state="error")
                batch_results.append({
                    "filename": filename,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                })

    if overall_progress:
        overall_progress.progress(1.0, text=f"✅ {total_files} archivos procesados")

    st.session_state["batch_results"] = batch_results

    successful = [r for r in batch_results if r["ok"]]
    failed = [r for r in batch_results if not r["ok"]]
    _render_resumen_y_resultados(batch_results, successful, failed, total_files)


def _render_resumen_y_resultados(batch_results: list, successful: list, failed: list, total_files: int):
    # 4. RESUMEN DEL BATCH
    if total_files > 1:
        st.success(f"✅ {len(successful)} de {total_files} archivos convertidos correctamente")
        if failed:
            st.warning(f"⚠️ {len(failed)} archivo(s) con errores — revisa el detalle abajo")

    if failed:
        with st.expander(f"❌ Archivos con errores ({len(failed)})", expanded=(len(successful) == 0)):
            for r in failed:
                st.error(f"**{r['filename']}**: {r['error']}")
            st.info(
                "💡 **Sugerencias:**\n"
                "- Verifica que **Tesseract** esté instalado y en el PATH, o define la variable de entorno `TESSERACT_CMD` con la ruta del ejecutable\n"
                "- Verifica que **Poppler** esté instalado y en el PATH, o define la variable de entorno `POPPLER_PATH` con su carpeta `bin`\n"
                "- Si un PDF está protegido con contraseña, desprotégelo primero.\n"
                "- Si ves texto con fragmentos de frase repetidos en escalón "
                "(ej. \"una gran cantidad de\" → \"gran cantidad de\" → \"cantidad de\"), "
                "actualiza Tesseract a la última versión estable del instalador de "
                "UB-Mannheim: es un bug conocido del motor LSTM "
                "(tesseract-ocr/tesseract#3489). PSM 6 y 300 DPI ya configurados aquí "
                "reducen su frecuencia y el post-procesamiento colapsa las repeticiones "
                "exactas, pero el patrón de \"cola decreciente\" no se puede corregir "
                "de forma confiable después del hecho — revisa manualmente pasajes con "
                "este patrón antes de citarlos textualmente."
            )

    if not successful:
        return

    # 5. DESCARGA CONSOLIDADA (solo con más de un archivo exitoso)
    if len(successful) > 1:
        zip_bytes = _build_zip_of_results(successful)
        zip_name_input = st.text_input("Nombre del archivo ZIP", value="documentos_convertidos.zip")
        zip_file_name = _sanitize_zip_filename(zip_name_input)
        st.download_button(
            label=f"⬇️ Descargar los {len(successful)} .md en un .zip",
            data=zip_bytes,
            file_name=zip_file_name,
            mime="application/zip",
            use_container_width=True,
            type="primary",
        )
        st.divider()

    # 6. RESULTADOS INDIVIDUALES
    for r in successful:
        st.markdown(f"### 📄 {r['filename']}")

        col1, col2, col3 = st.columns(3)
        col1.metric("📑 Páginas", r["pages"] if r["pages"] is not None else "—")
        col2.metric("📝 Palabras", f"{r['words']:,}")
        col3.metric("📁 Tipo", r["doc_type"])

        preview_words = " ".join(r["markdown"].split()[:PREVIEW_WORD_LIMIT])
        with st.expander("📋 Vista previa (primeras 1 000 palabras)", expanded=False):
            st.code(preview_words, language="markdown")

        st.download_button(
            label=f"⬇️ Descargar `{r['output_filename']}`",
            data=r["markdown"].encode("utf-8"),
            file_name=r["output_filename"],
            mime="text/markdown",
            use_container_width=True,
            key=f"download_{r['filename']}",
        )

        st.divider()

    # 7. RESUMEN AGREGADO DE CORRECCION — gancho compacto hacia el Dashboard
    metricas = cd.calcular_metricas_batch(_porcentajes_por_archivo(successful))
    archivos_con_correccion = metricas["moderado_count"] + metricas["severo_count"]
    if archivos_con_correccion > 0:
        st.markdown(
            f"**{archivos_con_correccion} de {metricas['total_archivos']} archivo(s)** "
            "tienen páginas MODERADO o SEVERO detectadas por el protocolo de auditoría."
        )
        if st.button("📊 Ir al Dashboard de Corrección", use_container_width=True):
            st.session_state["vista_activa"] = "dashboard"
            st.rerun()


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# CHECKLIST DE VERIFICACIÓN MANUAL
# ---------------------------------------------------------------------------
# [ ] 1. PDF Nativo (vertical): cargar un paper de Scopus → debe producir Markdown con texto correcto
# [ ] 2. PDF Escaneado: cargar una foto de libro → debe activar OCR y mostrar barra de progreso
# [ ] 3. PDF Slides: cargar una presentación exportada a PDF horizontal → texto en orden correcto
# [ ] 4. DOCX: cargar un documento Word con tabla → tabla debe aparecer en Markdown
# [ ] 5. Descarga: el archivo .md descargado debe ser UTF-8 válido y abrirse en cualquier editor
# [ ] 6. Error handling: cargar un PDF protegido con contraseña → debe mostrar st.error() sin crash