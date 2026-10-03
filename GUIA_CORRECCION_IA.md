# Guia de correccion OCR con IA

Esta guia cubre la capa de correccion con IA que se activa despues de la
conversion local (pymupdf4llm + pdfplumber + pytesseract). Esa conversion
local sigue funcionando exactamente igual, sin internet, la elijas o no:
todo lo descrito aqui es una capa adicional que aparece solo en paginas MODERADO
o SEVERO, y solo si tu la activas a proposito.

## Cuando usar cada modo

El sistema ofrece dos modos, elegidos manualmente cada vez (nunca preseleccionados
ni recordados entre archivos o sesiones):

- **Modo API** — para trabajo que vas a entregar a terceros (por ejemplo, un
  corpus de lecturas para un profesor o un cliente), donde el costo por token
  se justifica por el rigor que aporta: escalamiento automatico Sonnet -> Opus
  cuando la confianza es baja, verificacion de conectividad antes de habilitar
  los botones, y un dashboard con checkbox de aprobacion por pagina.

- **Modo Claude Code** — para uso personal (tus propios apuntes, lecturas que
  solo tu vas a leer), donde ese rigor no hace falta y prefieres evitar el
  costo por token, usando en cambio los creditos ya incluidos en tu
  suscripcion de Claude Code.

Ninguno de los dos modos corrige nada automaticamente sin tu confirmacion
explicita. La eleccion del modo es manual y deliberada, no una optimizacion
que el sistema haga por ti.

## Modo API — pasos

1. Sube el documento y conviertelo como siempre. Si el protocolo de auditoria
   detecta paginas MODERADO o SEVERO por encima del umbral configurado (15%
   por defecto, ajustable), los botones de correccion se habilitan.
2. Antes de habilitar los botones, la app intenta una conexion corta (2-3
   segundos) contra la API de Anthropic. Si falla, los botones quedan
   deshabilitados con un aviso — el resto del dashboard de deteccion sigue
   funcionando con normalidad.
3. Elige "Modo API" en el selector, y presiona "Corregir solo errores menores"
   (paginas MODERADO) o "Corregir menores y graves" (MODERADO + SEVERO).
4. La barra de progreso avanza pagina por pagina, con el conteo real
   ("Corrigiendo pagina X de Y"). Si una pagina MODERADO vuelve con confianza
   media o baja, se reintenta automaticamente con un modelo mas capaz — veras
   "(revisando con mas detalle)" mientras eso pasa. No necesitas hacer nada.
5. Al terminar, revisa el resultado: cada pagina aparece con un checkbox
   marcado por defecto, expandible para ver el texto original y el corregido,
   la confianza, y el motivo si no fue alta. Cualquier corte de texto
   irrecuperable queda marcado como "[ILEGIBLE - verificar contra PDF
   original]" — nunca se reconstruye por inferencia.
6. Desmarca las paginas que no quieras aplicar y presiona "Confirmar y
   aplicar correcciones". Ninguna correccion se escribe al archivo final
   antes de este paso, ni siquiera las de confianza alta.

**Costo**: este modo hace llamadas directas a la API de Anthropic con tu
propia clave (variable de entorno `ANTHROPIC_API_KEY`), gestionada desde
console.anthropic.com. Es un costo por token, sin relacion con ninguna
suscripcion de chat (Claude.ai, Claude Code, etc.) — son sistemas de
facturacion completamente separados. Una vez que la clave esta configurada
en el entorno, no hay ningun paso manual adicional: el modo funciona solo.

**Limitacion conocida — lenguaje ofensivo o vulgar en el original**: el
modelo tiende a normalizar o censurar automaticamente palabras que interpreta
como ofensivas o vulgares (por ejemplo, sustituyendo caracteres por
asteriscos), incluso cuando el documento original no las trae censuradas.
Esto se confirmo dos veces contra la misma pagina real, incluso despues de
agregar al prompt una instruccion explicita de transcripcion literal con un
ejemplo concreto — no se resolvio, y no vale la pena seguir intentando
ajustar el prompt para esto. Si tu documento tiene lenguaje de este tipo en
el original, no confies en el resultado sin revisarlo palabra por palabra
contra el PDF: el modelo puede reportar confidence alta en un texto que de
todas formas tiene esta alteracion silenciosa, asi que el indicador de
confianza no es suficiente para detectarla.

## Modo Claude Code — pasos

1. Elige "Modo Claude Code" en el selector, y presiona el boton de correccion
   que corresponda. La app no llama a ninguna API — en su lugar, exporta el
   `.md` ya convertido y el PDF original a una carpeta local
   (`correccion_claude_code/`, junto a `app_universal.py`) y genera un texto
   (prompt) con las rutas absolutas de ambos archivos y la lista de paginas a
   revisar.
2. Copia ese texto (hay un boton de copiar en el cuadro) y pegalo en una
   sesion de Claude Code abierta por tu cuenta, fuera de esta app.
3. En esa sesion, Claude Code deberia: abrir el PDF en cada pagina indicada,
   compararla contra el texto ya convertido, y mostrarte en el chat el
   fragmento antes y despues de cada correccion propuesta — pagina por
   pagina, no todo de una vez. Cualquier corte irrecuperable se marca como
   "[ILEGIBLE - verificar contra PDF original]", explicando por que. El
   prompt le pide explicitamente que no reconstruya nada por inferencia
   gramatical.
4. Revisa lo que te muestra antes de aprobar nada. Claude Code no deberia
   escribir el `.md` corregido a disco hasta que tu confirmes en esa misma
   conversacion.
5. Cuando termines esa correccion, vuelve a esta app y presiona "Ya termine
   la correccion en Claude Code". Aparece un campo de texto libre: pega ahi
   un resumen breve de lo que se corrigio y confirma.
6. Ese resumen queda guardado como registro (archivo, fecha y hora, y el
   resumen que pegaste) y se muestra en el dashboard, separado de las
   metricas del Modo API — es una bitacora, no un dato que la app interprete
   o valide.

**Costo**: ninguno adicional — usa los creditos que ya incluye tu suscripcion
de Claude Code, no la API de Anthropic por separado.

**Nota**: la app misma no relee ni reimporta el `.md` corregido que Claude
Code escribe en disco. El archivo corregido queda en `correccion_claude_code/`
para que lo uses directamente desde ahi; lo que ves en la vista previa y el
boton de descarga de esta app, en este modo, sigue siendo la version sin
corregir.

## El pipeline local es independiente

Todo lo anterior es opcional. La conversion PDF/DOCX -> Markdown (deteccion
de tipo, OCR con Tesseract, extraccion de tablas, todo el pipeline original)
no depende de ninguno de los dos modos, no necesita conexion a internet, y
sigue funcionando exactamente igual si nunca activas la correccion con IA.
