# probar_correccion_api.py
# Prueba minima de conexion real con la API de Anthropic para el Modo API de
# correccion OCR. No usa Streamlit ni sube nada por navegador: rasteriza una
# sola pagina de un PDF real y la manda a corregir con correccion_api.py tal
# cual, sin ningun monkeypatch — es el mismo camino de codigo que usa
# app_universal.py cuando presionas un boton de correccion.
#
# Requiere:
#   - anthropic instalado (ya deberia estarlo si llegaste hasta aqui)
#   - variable de entorno ANTHROPIC_API_KEY configurada con una clave valida
#     de console.anthropic.com
#
# Uso:
#   python probar_correccion_api.py ruta\a\un.pdf [numero_de_pagina]
#
# Si no se pasa numero_de_pagina, usa la pagina 1. Este script SI hace una
# llamada real y con costo a la API — no lo corras hasta tener la clave
# configurada y saldo cargado.

import os
import sys
from pathlib import Path

import fitz

import correccion_deteccion as cd
import correccion_api as ca

# Deben coincidir con los mismos valores de app_universal.py.
POPPLER_PATH = os.environ.get("POPPLER_PATH") or None
OCR_DPI = 300


def main():
    if len(sys.argv) < 2:
        print(r"Uso: python probar_correccion_api.py ruta\a\un.pdf [numero_de_pagina]")
        sys.exit(1)

    pdf_path = Path(sys.argv[1])
    page_number = int(sys.argv[2]) if len(sys.argv) > 2 else 1

    if not pdf_path.exists():
        print(f"No existe el archivo: {pdf_path}")
        sys.exit(1)

    if not ca._SDK_DISPONIBLE:
        print("El paquete 'anthropic' no esta instalado. Ejecuta: pip install anthropic --break-system-packages")
        sys.exit(1)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("No esta configurada la variable de entorno ANTHROPIC_API_KEY.")
        sys.exit(1)

    print("Verificando conectividad...")
    if not ca.verificar_conectividad():
        print("Sin conectividad con api.anthropic.com. Revisa tu conexion.")
        sys.exit(1)
    print("Conectividad OK.")

    file_bytes = pdf_path.read_bytes()

    doc = fitz.open(stream=file_bytes, filetype="pdf")
    total_paginas = doc.page_count
    if page_number < 1 or page_number > total_paginas:
        print(f"La pagina {page_number} no existe — el PDF tiene {total_paginas} pagina(s).")
        doc.close()
        sys.exit(1)
    native_text = doc[page_number - 1].get_text("text")
    doc.close()

    # Severidad forzada a MODERADO para probar el camino con texto crudo
    # incluido y la posibilidad de escalamiento automatico a Opus. Cambia a
    # "SEVERO" si prefieres probar el camino solo-imagen directo a Opus.
    page_audit = cd.PageAudit(
        page_number=page_number,
        label=f"Pagina {page_number}",
        raw_text=native_text,
        severity="MODERADO",
        metric_flag="stop_ratio",
        accent_suspect=False,
    )

    print(f"Enviando pagina {page_number} de {pdf_path.name} a la API real (esto tiene costo)...")
    resultados = ca.procesar_lote(
        file_bytes, [page_audit], POPPLER_PATH, OCR_DPI,
        progress_callback=lambda actual, total, pn, fase: print(f"  [{fase}] pagina {pn} ({actual}/{total})"),
    )

    r = resultados[0]
    print()
    print("--- RESULTADO ---")
    print(f"ok: {r.ok}")
    if r.ok:
        print(f"confidence: {r.confidence}")
        print(f"modelo_usado: {r.modelo_usado}")
        print(f"escalada_desde_sonnet: {r.escalada_desde_sonnet}")
        print(f"zonas_ilegibles: {r.zonas_ilegibles}")
        if r.motivo_baja_confianza:
            print(f"motivo_baja_confianza: {r.motivo_baja_confianza}")
        print("texto_corregido:")
        print(r.texto_corregido)
    else:
        print(f"excluida: {r.excluida}")
        print(f"motivo_exclusion: {r.motivo_exclusion}")


if __name__ == "__main__":
    main()
