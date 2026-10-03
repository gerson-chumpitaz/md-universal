# MD Universal

Convertidor de PDF y DOCX a Markdown pensado para trabajar con modelos de lenguaje. Conserva el orden de lectura, marca cada página, audita la calidad de la conversión con señales de riesgo explícitas y ofrece una capa opcional de corrección asistida por IA que nunca escribe nada sin aprobación humana.

## Cómo se conecta con los otros repositorios

Esta herramienta es la parte práctica de dos marcos de trabajo que se complementan.

- [Criterio de Activación](https://github.com/gerson-chumpitaz/criterio-de-activacion) propone trabajar con una representación barata del documento (el Markdown) y consultar la fuente original (el PDF) solo cuando aparece una señal de riesgo. Aquí, la conversión produce esa representación, `protocolo_auditoria.py` implementa las señales de riesgo y la corrección guiada por el PDF original cubre la verificación selectiva.
- Protocolo de Trazabilidad [enlace pendiente de publicación] exige anclar cada afirmación a un documento y a una página. Los marcadores `## 📄 Página N` que genera este convertidor son los que hacen posible ese anclaje.

## Qué hace

Detecta si un PDF es nativo o escaneado y aplica el camino adecuado. Para PDF nativo usa `pymupdf4llm` para el diseño y las columnas, y `pdfplumber` para las tablas, con reparaciones para ligaduras perdidas. Para PDF escaneado aplica OCR con Tesseract a 300 ppp, en español y en modo de un solo bloque de texto (PSM 6), y usa `img2table` para las tablas. Para DOCX extrae títulos, texto y tablas. Cada página queda precedida por un marcador `## 📄 Página N`. Se pueden subir varios archivos a la vez y descargar el resultado en `.md`.

El archivo `protocolo_auditoria.py` revisa el Markdown resultante y clasifica cada página como limpia, leve, moderada o severa, según cinco patrones de daño conocidos de la conversión (caracteres corruptos por mapeo de fuente, mezcla de columnas localizada o generalizada, errores de palabra individual y colapso de espacios en tablas). Se puede ejecutar sola con `python protocolo_auditoria.py archivo.md` o `python protocolo_auditoria.py --dir carpeta`.

## Instalación

Desarrollado y usado en Windows por el autor; probado de forma independiente en Linux con Python 3.11.

1. Instala los paquetes de Python en un entorno virtual con `pip install -r requirements.txt`.
2. Instala Tesseract OCR con los datos del idioma español. En Windows, el instalador de UB-Mannheim permite elegir idiomas. En Debian o Ubuntu, `sudo apt install tesseract-ocr tesseract-ocr-spa`. En macOS, `brew install tesseract tesseract-lang`.
3. Instala Poppler. En Debian o Ubuntu, `sudo apt install poppler-utils`. En macOS, `brew install poppler`. En Windows, descarga un binario de Poppler y descomprímelo.
4. Si Tesseract o Poppler no están en el PATH del sistema, indica dónde están con variables de entorno: `TESSERACT_CMD` con la ruta del ejecutable de Tesseract y `POPPLER_PATH` con la carpeta `bin` de Poppler. En Windows, por ejemplo, `setx POPPLER_PATH "C:\ruta\a\poppler\Library\bin"`.
5. Ejecuta `python -m streamlit run app_universal.py` y abre la dirección que indica la terminal.

La tipografía de la interfaz es opcional. Si quieres usar Space Grotesk, descarga el archivo `SpaceGrotesk-Variable.woff2` y colócalo en `assets/fonts/`; sin él, la interfaz usa fuentes del sistema.

## Corrección asistida por IA (opcional)

Se activa solo cuando el protocolo de auditoría encuentra páginas moderadas o severas por encima de un umbral ajustable. Hay dos modos, que se eligen de forma manual cada vez. El detalle paso a paso está en [GUIA_CORRECCION_IA.md](GUIA_CORRECCION_IA.md).

El Modo API usa tu propia clave en la variable de entorno `ANTHROPIC_API_KEY` y tiene costo por token. Envía a la API de Anthropic la imagen de cada página que se corrige y, en las páginas moderadas, también el texto que la conversión local extrajo de ella, por lo que no conviene usarlo con documentos confidenciales sin revisar antes los términos del proveedor. Los modelos por defecto son `claude-sonnet-5` y `claude-opus-5`, y se pueden cambiar con `MD_UNIVERSAL_MODEL_SONNET` y `MD_UNIVERSAL_MODEL_OPUS`. Este modo fuerza el uso de una herramienta con `tool_choice`, y según la documentación de Anthropic consultada el 2 de octubre de 2026, Claude Sonnet 5.5 rechaza esa opción con un error 400. Antes de cambiar de modelo, verifica que acepte `tool_choice` forzado.

El Modo Claude Code no llama a ninguna API. Exporta el Markdown y el PDF original a una carpeta local y genera un texto para pegar en una sesión de Claude Code abierta por separado.

En ambos modos, ninguna corrección se aplica sin confirmación explícita, y lo que no se puede leer se marca como `[ILEGIBLE - verificar contra PDF original]` en lugar de reconstruirse por inferencia.

## Limitaciones conocidas

Estas limitaciones se conocen y se documentan a propósito; ninguna cifra de este proyecto debe leerse como una métrica formal de precisión.

La auditoría puede marcar como moderadas páginas que en realidad están limpias, cuando contienen fragmentos cortos de ruido como etiquetas de ejes. Tampoco detecta párrafos completos mezclados cuando las palabras individuales están bien escritas, de modo que una página clasificada como limpia en un documento escaneado puede tener daño real y no sustituye la lectura final. El patrón de errores de palabra individual depende del diccionario de `pyspellchecker`: en una prueba con un texto limpio de 114 palabras y la versión 0.9.0, marcó entre 26% y 41% de palabras comunes como desconocidas, con severidad leve, que no activa la corrección. Conviene ampliar `EXCEPCIONES_DICCIONARIO` con la jerga de tu dominio.

En la corrección con IA, el modelo tiende a normalizar o censurar palabras que interpreta como ofensivas, incluso con instrucción explícita de transcripción literal, y el indicador de confianza no lo detecta. Si el original contiene ese tipo de lenguaje, hay que revisarlo palabra por palabra contra el PDF.

Para esta publicación se verificó, en Linux, la conversión de un PDF nativo y de uno escaneado de texto simple, la carga de la auditoría, y desde la interfaz la carga de un archivo y su conversión. No se probaron de forma independiente las tablas, los diseños de varias columnas, los DOCX ni el Modo API. El autor usa la herramienta en Windows.

## Licencia

Este proyecto se distribuye bajo la licencia GNU AGPL-3.0. La razón es una dependencia: PyMuPDF y pymupdf4llm se publican bajo doble licencia, AGPL-3.0 o licencia comercial de Artifex, y esta herramienta las importa. Si necesitas otros términos, puedes adquirir la licencia comercial de Artifex o reemplazar esa dependencia, teniendo en cuenta que la calibración de `protocolo_auditoria.py` se hizo sobre la salida de `pymupdf4llm`. Cada dependencia conserva su propia licencia.

## Autoría

Gerson Chumpitaz (Universidad ESAN, Lima), 2026. Desarrollado con asistencia de IA (Claude), con revisión, pruebas y uso real por el autor.
