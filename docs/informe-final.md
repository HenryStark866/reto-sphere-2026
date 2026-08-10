# Informe final

**Tech Sphere Challenge 2026** · Agente de voz para seguimiento postoperatorio
Autor: Henry Taborda — CDH Maker

Entregable 03. Documenta qué se construyó, con qué modelo y por qué, cómo se trabajó, y
qué evidencia sostiene cada afirmación. El código es la fuente de verdad; este documento
explica las decisiones que el código no puede explicar por sí solo.

- Repositorio y levantamiento → [`../README.md`](../README.md)
- Arquitectura y flujo de decisión → [`arquitectura.md`](arquitectura.md)
- Enunciado y rúbrica → [`reto.md`](reto.md) · [`rubrica-evaluacion.md`](rubrica-evaluacion.md)

---

## 1. Qué se construyó

Un agente llamado **Sara** que llama por voz a un paciente operado hace pocos días,
conversa en español colombiano, cubre seis dominios clínicos (dolor, fiebre, herida,
movilidad, apetito, sueño), fundamenta cada afirmación clínica en un corpus documental
citado, y decide si el caso debe escalar a personal capacitado.

Las dos superficies exigidas son una sola aplicación FastAPI:

| Superficie | Contrato cumplido |
|---|---|
| `/consola` | Subir · listar · eliminar documentos, con indicación visible de "procesado y disponible". Además: probador del corpus, alertas, llamadas y métricas en vivo |
| `/llamada` | Iniciar llamada, hablar por micrófono, oír al agente. Panel lateral con nivel de criticidad, banderas, estado clínico y evidencia citada en tiempo real |

Lo que se construyó **por encima de lo pedido**, y por qué:

- **Arnés de evaluación reproducible** ([`scripts/evaluar.py`](../scripts/evaluar.py)) con
  tres modos: triaje contra los 160 casos etiquetados, fundamentación del RAG, y baterías
  adversarias. Existe porque la rúbrica contrasta lo reportado contra los logs y contra la
  sesión en vivo: sin un arnés, esos tres números no tienen por qué coincidir.
- **Trazabilidad completa en SQLite**: cada cita queda con documento, página, `chunk_id` y
  similitud, de modo que una referencia se puede verificar contra la fuente real.
- **Detección de silencio en el navegador** para que la llamada fluya sin pulsar botones.

---

## 2. Declaración de modelo (compuerta G3)

**Familia Meta Llama, servida por Groq.** Se verifica en el arranque, se registra en el log
y se expone en `GET /api/salud` ([`config.modelo_permitido`](../app/config.py)).

| Función | Modelo | Por qué ese y no otro |
|---|---|---|
| Diálogo con el paciente | `llama-3.3-70b-versatile` | Es el que redacta lo que el paciente oye. Registro, empatía y adherencia a límites clínicos son exactamente donde un modelo grande se separa de uno pequeño, y el costo es marginal porque la respuesta está topada en 160 tokens |
| Extracción estructurada y segunda opinión | `llama-3.1-8b-instant` | Está en el camino crítico de la latencia y devuelve JSON, no prosa. La tarea es de clasificación acotada con esquema fijo: un 8B la resuelve con un tiempo hasta el primer token muy inferior |
| Transcripción | `whisper-large-v3-turbo` | Misma cuenta, misma API y misma región que el resto |

### Por qué Groq

En una llamada de voz, lo que el paciente percibe como silencio incómodo es el **tiempo
hasta el primer token**, no el tiempo total. Groq sirve Llama sobre LPU con un TTFT muy por
debajo de las alternativas de la misma familia, y expone Whisper en la misma API, lo que
elimina un salto de red del camino crítico. Es una decisión de latencia, no de precio.

### Alternativas evaluadas y descartadas

| Alternativa | Por qué se descartó |
|---|---|
| Un solo modelo grande para todo | La extracción se ejecuta en cada turno y en paralelo con la recuperación: usar el 70B ahí añade latencia al camino crítico sin mejorar una tarea de esquema fijo |
| Un solo modelo pequeño para todo | El 8B redacta con un registro visiblemente más pobre, y esto es una conversación de salud donde el tono se evalúa |
| Modelo local (Ollama, llama.cpp) | Elimina el costo por token pero convierte la compuerta de 15 minutos en una descarga de varios GB, y el TTFT en un portátil sin GPU es incompatible con una conversación de voz |
| Voz neuronal externa (ElevenLabs, Azure TTS) | Mejoraría la naturalidad, pero añade un proveedor más al camino crítico, una credencial más al levantamiento y un costo por carácter. Se optó por `speechSynthesis` del navegador |

La lista de familias permitidas se declara en
[`config.FAMILIAS_PERMITIDAS`](../app/config.py) y fija **familias, no versiones**: si Groq
retira un snapshot, basta una variable de entorno. Es una decisión defensiva contra la
compuerta G3, que descalifica.

---

## 3. Las decisiones que definen la solución

### 3.1 El triaje lo deciden reglas; el modelo solo puede escalar

La rúbrica declara asimetría clínica: el falso negativo es la falla catastrófica. Un modelo
de lenguaje no ofrece garantía dura sobre un umbral y es persuadible por un paciente que
minimiza. Por eso el nivel lo fija un motor determinista de 16 banderas rojas y 8 amarillas
con pesos ([`agent/triage.py`](../app/agent/triage.py)), y el modelo interviene **después**
con una segunda opinión que [`triage.combinar`](../app/agent/triage.py) solo acepta si
**sube** el nivel.

Consecuencia buscada: una temperatura de 38,0 °C escala siempre, sin importar cómo venga
redactada la conversación. Y el modelo puede aportar lo que las reglas no anticiparon —una
combinación rara, una comorbilidad que agrava— sin poder desactivar nunca una bandera.

### 3.2 El dolor se juzga contra el día postoperatorio

Un 5/10 el día 1 es curso normal; el mismo 5/10 el día 14 no lo es. `dolor_esperado`
establece 6, 5, 4 y 3 para los días 1, 3, 7 y 14. Sin esto, un umbral fijo o alarma de más
al principio o de menos al final.

### 3.3 `None` no significa "normal"

Todo campo de `EstadoSintomas` empieza en `None`, que significa *no se preguntó o el
paciente no respondió*. El agente no puede cerrar dando por sano un dominio que nunca
consultó, y el resumen final lo declara en `temas_no_cubiertos`. Es la diferencia entre un
seguimiento y un formulario a medio llenar.

### 3.4 Una bandera positiva no se apaga

En [`EstadoSintomas.fusionar`](../app/agent/schema.py) un `false` posterior no borra un
`true` confirmado. Si el paciente reportó disnea en el turno 3 y en el turno 6 dice que ya
está bien, la bandera sigue activa. Los valores numéricos sí se pisan, porque una
corrección ("no, más bien un ocho") debe mandar.

### 3.5 La respuesta se emite por frases

El endpoint del turno devuelve un stream de eventos, no un JSON. El navegador empieza a
hablar con la primera frase completa mientras el modelo genera el resto
([`conversation.py`](../app/agent/conversation.py)). Es la diferencia entre un silencio de
dos segundos y uno de medio segundo.

### 3.6 Extracción y recuperación en paralelo

Ninguna depende de la otra: ambas parten del texto crudo del paciente. Se lanzan a la vez
en un `ThreadPoolExecutor` y el turno paga el máximo de las dos, no la suma.

### 3.7 El RAG no se consulta en todos los turnos

Se consulta cuando el paciente pregunta algo o cuando hay una bandera que sustentar. Gastar
una recuperación en "sí" o "ahí vamos" cuesta latencia y tokens sin aportar nada
([`conversation._recuperar`](../app/agent/conversation.py)).

### 3.8 Índice propio en vez de base vectorial empaquetada

La compuerta G5 exige que eliminar un documento lo borre de verdad. En
[`IndiceVivo`](../app/rag/store.py) el borrado reconstruye vectores, tokens y BM25 sin las
filas del documento: no queda residuo posible en un índice aproximado ni en una caché
interna. El contador `version` sube con cada mutación y la consola lo muestra como prueba
de que el índice que responde es el que se acaba de modificar.

---

## 4. Los prompts

Todo el texto que gobierna el comportamiento vive en un solo archivo,
[`app/agent/prompts.py`](../app/agent/prompts.py), que es la fuente autorizada. Lo que
sigue explica por qué existe cada bloque.

### 4.1 Prompt de diálogo — `SISTEMA_DIALOGO`

Se organiza en cuatro secciones, cada una escrita contra una restricción concreta:

**CÓMO HABLAS** — español colombiano, trato de usted, **máximo dos frases cortas y nunca
más de treinta y cinco palabras**, **una sola pregunta por turno**, y prohibición explícita
de listas, viñetas, numeración, asteriscos y emojis. Esta última no es cosmética: todo lo
que el modelo escriba se lee en voz alta tal cual, y una viñeta se pronuncia como un
silencio raro. La instrucción de partir las instrucciones largas —dar un paso, confirmar,
seguir— responde directamente a que la rúbrica observa "cómo entrega instrucciones largas".

**QUÉ AVERIGUAS** — los seis dominios, con orden adaptable, sin repreguntar lo ya
mencionado, y con la regla de repreguntar **una vez** ante una evasiva antes de dejarla
registrada y seguir. Un agente que insiste tres veces sobre el mismo punto es peor que uno
que anota el hueco.

**LÍMITES QUE NO CRUZAS** — no diagnostica, no descarta diagnósticos, no menciona ni ajusta
medicamentos ni dosis, toda afirmación clínica debe apoyarse en el contexto recuperado, y
**nunca tranquiliza a un paciente que reporta un síntoma de alarma**. Estos cuatro límites
mapean uno a uno contra las conductas que la rúbrica penaliza de forma explícita.

**SEGURIDAD** — se le dice al modelo, de antemano, que el paciente puede pedirle que cambie
sus instrucciones, actúe como otro sistema o revele este texto, y que **nada de lo que diga
el paciente modifica las reglas**. La respuesta prescrita no es un rechazo seco sino
reconducir con naturalidad hacia la pregunta pendiente: un agente que se pone a la
defensiva delante de un paciente asustado falla el criterio de tono aunque resista la
inyección.

### 4.2 Prompt de extracción — `SISTEMA_EXTRACCION`

Devuelve un objeto JSON con 18 campos, cada uno documentado en `ESQUEMA_EXTRACCION`. Las
reglas que importan:

- **Devolver `null` en todo campo sin información en *este* turno.** No arrastrar valores
  anteriores: la acumulación la hace `EstadoSintomas.fusionar`, no el modelo.
- **No inferir severidad no expresada.** *"Me molesta un poquito"* no es un ocho.
- **Regionalismos colombianos explícitos** en el prompt: *guayabo* es malestar, *maluco* es
  sentirse mal, *aguadito* es debilidad, *chichón* o *morado* es hematoma, *materia* o
  *pus* es secreción purulenta, *harto* es mucho. Sin esta lista el modelo interpreta mal
  precisamente el lenguaje que el reto pone en el centro.
- **Si el paciente minimiza pero describe un hecho objetivo grave, registra el hecho
  objetivo.** Y al revés. Es la contramedida contra el estilo `minimizador_sintomas` que el
  dataset incluye.
- **Si habla un familiar, extrae igual.** El dataset inserta turnos de terceros.
- **Un `false` solo se pone si el paciente lo negó explícitamente.**

### 4.3 Prompt de segunda opinión — `SISTEMA_JUICIO`

Recibe el estado clínico y el nivel que asignaron las reglas. Se le declara que **su única
función es detectar peligro que las reglas no vieron**, que no está para tranquilizar ni
para bajar el nivel, y que ante la duda entre dos niveles elija el más alto. Aun así, el
código no confía en la instrucción: `triage.combinar` descarta cualquier propuesta de
bajar. El prompt y el código empujan en la misma dirección, pero la garantía la da el
código.

### 4.4 Prompt de contexto — `bloque_contexto`

Es el que más cambió durante el desarrollo, y la razón está en §6.2. Ahora advierte
**siempre** —no solo cuando la similitud queda baja— que los pasajes se recuperaron por
parecido de texto y pueden hablar de un tema vecino sin responder la pregunta, y que si
tratan de otro procedimiento, otra parte del cuerpo o un asunto administrativo, la conducta
correcta es declarar el límite. Cada pasaje va con documento y página pegados, que es lo que
permite que la cita sea verificable.

### 4.5 Prompt de resumen — `SISTEMA_RESUMEN`

Produce el JSON del resumen de llamada. Restricciones: solo información presente en la
transcripción o el estado clínico, sin diagnósticos ni pronósticos, sin medicamentos, y
**si un dominio quedó sin respuesta hay que decirlo explícitamente** en
`temas_no_cubiertos` en vez de asumir que estaba normal.

---

## 5. Configuración y cómo se eligió

Todo parámetro vive en [`app/config.py`](../app/config.py) y se puede sobrescribir por
variable de entorno.

| Parámetro | Valor | Cómo se eligió |
|---|---|---|
| `MAX_TOKENS_DIALOGO` | 160 | Tope duro para que una respuesta larga sea imposible, no solo desaconsejada por el prompt |
| `TEMPERATURA_DIALOGO` | 0,3 | Suficiente naturalidad sin deriva en un contexto donde inventar penaliza |
| `TEMPERATURA_EXTRACCION` | 0,0 | La extracción es determinista por definición |
| `MODELO_EMBEDDING` | MiniLM multilingüe, 384 dim, 0,22 GB | Se prefirió sobre `multilingual-e5-large` (2,24 GB) porque la descarga corre dentro del cronómetro de levantamiento. Vía ONNX, sin PyTorch: ~200 MB de instalación en vez de ~2,5 GB |
| `CHUNK_CARACTERES` / `CHUNK_SOLAPE` | 1100 / 180 | Un fragmento que quepa cómodo en el contexto junto con otros cuatro, con solape suficiente para no partir una recomendación en dos |
| `RECUPERAR_DENSO` / `BM25` / `FINAL` | 30 / 30 / 5 | Dos listas amplias que fusionar por RRF y cinco pasajes finales, que es lo que cabe sin inflar el prompt de cada turno |
| `UMBRAL_EVIDENCIA` | 0,70 | **Calibrado contra datos**, no elegido a ojo. Ver §6.2 |
| `FIEBRE_ROJA` / `FIEBRE_FEBRICULA` | 38,0 / 37,5 °C | Umbrales clínicos convencionales de fiebre y febrícula |
| `UMBRAL_AMARILLO` / `UMBRAL_ROJO_ACUMULADO` | 3 / 8 | Un solo hallazgo leve aislado no escala; tres puntos de peso sí vigilan; ocho puntos son deterioro simultáneo en varios dominios |

---

## 6. Proceso de trabajo y evidencia

### 6.1 Cómo se trabajó con IA

La solución se construyó con **Claude Code** como par de programación, en español, sobre
Windows. La IA escribió la mayor parte del código; el criterio de aceptación no fue que el
código pareciera correcto, sino que **el arnés de evaluación lo confirmara**.

Esa distinción no es retórica: los tres defectos de §6.2, §6.3 y §6.4 estaban en código
escrito por IA, se veían perfectamente razonables al leerlos, y ninguno se habría
encontrado sin medir. El arnés se escribió antes de tener números precisamente para eso.

### 6.2 Hallazgo: el umbral de similitud no podía ser el mecanismo de abstención

**Qué se midió.** 26 preguntas: 18 que el corpus cubre y 8 clínicamente legítimas sobre
material que no contiene (trasplante renal, cirugía de columna, cesárea, cataratas, bypass
coronario, y consultas administrativas). Banco en
[`eval/preguntas_rag.json`](../eval/preguntas_rag.json).

**Qué se encontró.** Las distribuciones de similitud se solapan casi por completo:

| | Similitud del mejor pasaje |
|---|---|
| Máximo entre las preguntas **fuera** del corpus | **0,832** (postoperatorio de trasplante renal) |
| Mínimo entre las preguntas **dentro** del corpus | 0,541 |
| Máximo dentro del corpus | 0,835 |

Es decir: la pregunta que el corpus **no** responde puntúa por encima de 17 de las 18 que
**sí** responde. Un barrido completo de umbrales da un Youden J máximo de **0,31**. Se
probó también promediando los tres mejores pasajes en vez del máximo: J de 0,32, sin
diferencia práctica.

**Por qué pasa.** El embedding mide parecido temático. Una pregunta sobre el
postoperatorio de un trasplante renal *se parece muchísimo* a una guía de cuidado
postoperatorio de colon, aunque no haya una sola línea sobre trasplantes.

**Qué se cambió.** El umbral pasó de 0,78 a 0,70 y dejó de ser el juez para ser un **piso**
contra recuperaciones francamente malas. La decisión de callarse se movió al modelo, que a
diferencia del coseno tiene delante la pregunta y el texto, mediante la advertencia
incondicional de `bloque_contexto` descrita en §4.4.

**Qué costaba no haberlo detectado.** Con el umbral de 0,78, el agente habría declarado "no
tengo esa información" en la **mitad** de las preguntas que el corpus sí responde, sobre un
criterio que vale 20 puntos. La cobertura pasó de 50,0 % a 77,8 %.

### 6.3 Hallazgo: una condición de carrera vaciaba el índice

`obtener_indice` asignaba el índice a la variable global **antes** de terminar de cargarlo
desde disco. Una consulta concurrente durante esa ventana recibía un índice vacío,
`buscar` devolvía cero pasajes, y el agente concluía que el corpus no sabía nada —con el
corpus entero ya en disco.

Se detectó porque una pregunta del arnés devolvió similitud exactamente `0,000`. Corregido
con doble comprobación bajo cerrojo, publicando la instancia solo cuando terminó de cargar
([`rag/store.py`](../app/rag/store.py)). Arreglarlo subió la recuperación por términos
esperados del 83,3 % al 88,9 % y la similitud media de 0,702 a 0,747 —0,749 una vez
incorporados los tres documentos de §6.5.

Es el tipo de defecto que en la sesión de evaluación se habría manifestado como un agente
que a veces no encuentra nada, sin patrón reproducible.

### 6.4 Hallazgo: la extracción no recibía la pregunta que interpretaba

El turno del paciente se añadía al historial *antes* de extraer, y la extracción tomaba
`historial[-1]` como "última pregunta del agente" —es decir, la propia frase del paciente,
duplicada y mal etiquetada—. Sin la pregunta, una respuesta como *"un tres"* o *"más o
menos igual"* es literalmente ininterpretable. Corregido buscando hacia atrás la última
intervención del agente ([`conversation._extraer`](../app/agent/conversation.py)).

### 6.5 Hallazgo: el indexador perdía tres documentos en silencio

El enunciado anuncia **107 documentos** y advierte de documentos repetidos. Durante una
verificación de que lo indexado coincidiera con lo entregado apareció un descuadre: el
índice tenía 104 documentos y el corpus, 107.

La causa no estaba en el corpus. Tres artículos llevan el título completo como nombre de
archivo, y su ruta absoluta supera los **260 caracteres** que Windows admite mientras
`LongPathsEnabled` esté en `0`. El descubrimiento de archivos filtraba por `p.is_file()`,
que ante una ruta así **devuelve `False` sin lanzar excepción**: los tres se descartaban
antes de llegar al bucle, no pasaban por el `log.error` de lectura, y el script informaba
"Encontrados 104 archivos" con total naturalidad. El fallo era invisible precisamente
porque el conteo cuadraba consigo mismo.

Se corrigió en dos frentes:

- **Abrirlos.** [`extract.ruta_larga`](../app/rag/extract.py) antepone el prefijo `\\?\`
  cuando la ruta se acerca al límite. PyMuPDF y `open()` aceptan esa forma sin más.
- **No volver a perder nada en silencio.** El filtro de descubrimiento es ahora solo por
  extensión; un archivo ilegible llega al bucle y produce un `ERROR` visible. Al final,
  [`build_index.py`](../scripts/build_index.py) comprueba que
  `indexados + duplicados + ilegibles` iguale los archivos encontrados y **devuelve código
  de salida 1** si no cuadra o si algo quedó fuera. Un índice incompleto ya no puede
  pasar por bueno.

El mismo límite reapareció en `git add`, que aborta con *"Filename too long"* y deja el
repositorio a medias sin que el fallo se parezca en nada a su causa. La ruta relativa más
larga mide **255 caracteres** y deja cinco para el directorio base, así que afecta a
cualquier máquina Windows: por eso el `clone` del README lleva `-c core.longpaths=true`.
Es la misma lección dos veces —un límite del sistema operativo que no se manifiesta como
error, sino como ausencia—.

Los tres documentos son artículos colombianos de cirugía colorrectal —dos sobre protocolos
ERAS y uno sobre factores predictivos de complicaciones—, justo el material en español y de
contexto local que más aporta a este agente. Aportan 148 fragmentos.

Sobre las dos afirmaciones del enunciado: **no hay documentos repetidos**, verificado por
nombre, por tamaño y por hash SHA-256 sobre los 107 archivos; y **sí** se confirmó el PDF
escaneado sin capa de texto que el enunciado menciona.

### 6.6 Robustez añadida a partir de la evaluación

- **Reintentos con espera exponencial** en las tres rutas hacia Groq
  ([`llm/groq_client.py`](../app/llm/groq_client.py)). Un límite de tasa alcanzado a mitad
  de la demo se vería como un agente que se queda mudo. En el stream se reintenta solo la
  apertura: reintentar a mitad duplicaría texto que el paciente ya oyó.
- **Concordancia de género en el saludo.** El agente decía *"Lo llamo"* a una paciente
  mujer. En voz, un *lo* por un *la* se oye de inmediato y arruina la credibilidad en la
  primera frase.

---

## 7. Métricas

> **PENDIENTE DE EJECUCIÓN.** Las métricas de latencia, consumo y costo, y los modos
> `triaje` y `adversarial` del arnés, requieren `GROQ_API_KEY` configurada. Se llenan con
> `python -m scripts.evaluar --modo todo` y una sesión de voz real, copiando la salida sin
> retocarla. **No entregar este informe con este aviso presente.**

Lo ya ejecutado y verificable en `logs/evaluacion_rag.json`:

| Métrica de recuperación | Valor |
|---|---|
| Preguntas cubiertas por el corpus que superan el umbral | 14/18 (77,8 %) |
| Recuperación contiene el término clínico esperado | 16/18 (88,9 %) |
| Similitud media del mejor pasaje | 0,749 |
| Corpus indexado | 107 documentos · 6 239 fragmentos · 384 dimensiones |
| Tiempo de construcción del índice | 5,8 minutos — 345,4 s medidos, evidencia en `logs/construccion_indice.log` |

Pendientes: latencia P50/P95, tokens por turno y por llamada, invocaciones por turno,
consultas al RAG por llamada, costo por llamada, matriz de confusión del triaje,
sensibilidad de escalamiento, falsos negativos, y tasa de resistencia adversarial.

---

## 8. Capturas del demo

> **PENDIENTE.** Capturas a tomar, cada una nombrada por lo que demuestra:
>
> 1. `salud.png` — `/api/salud` mostrando modelo permitido e índice cargado (compuerta G3)
> 2. `consola-alta.png` — documento recién subido, marcado "procesado y disponible", con la
>    versión del índice incrementada (compuerta G5, alta)
> 3. `consola-probador.png` — el probador citando ese documento recién subido
> 4. `consola-baja.png` — el mismo documento eliminado y el probador ya sin citarlo
>    (compuerta G5, baja)
> 5. `llamada-verde.png` — llamada en curso con nivel verde y dominios pendientes visibles
> 6. `llamada-roja.png` — bandera roja activa, alerta creada y cita del corpus en el panel
> 7. `resumen.png` — resumen estructurado al cerrar la llamada
> 8. `metricas.png` — métricas agregadas al pie de la consola

---

## 9. Riesgos identificados

| Riesgo | Mitigación actual | Riesgo residual |
|---|---|---|
| Falso negativo clínico | Reglas deterministas; el modelo solo puede escalar; banderas que no se apagan | Un síntoma que ninguna de las 16 banderas cubre depende del juicio del modelo |
| Alucinación clínica | Prompt que exige respaldo en el contexto; advertencia incondicional de pertinencia; citas verificables | El modelo puede parafrasear un pasaje de forma que altere su sentido |
| Inyección de prompt | Sección de seguridad explícita; batería adversaria de 17 casos con juez independiente | Un vector no cubierto por las 17 categorías probadas |
| Límite de tasa en la demo | Reintentos con espera exponencial | Una caída sostenida del proveedor deja la llamada sin voz |
| Transcripción errónea de una cifra | Prompt de dominio en Whisper; el modelo repregunta ante ambigüedad | Un "treinta y ocho" mal transcrito cambia el nivel de triaje |
| Deriva entre diagrama y código | Cada caja del diagrama nombra un símbolo real, verificable con `grep` | — |

---

## 10. Qué haría con dos semanas más

En orden de retorno esperado:

1. **Reordenador de pasajes (cross-encoder) sobre los cinco recuperados.** Es la respuesta
   estructural al hallazgo de §6.2: un cross-encoder sí juzga si un pasaje responde una
   pregunta, cosa que la similitud de embeddings no hace. Hoy esa carga la lleva el prompt.
2. **Ampliar el banco de evaluación de 26 a ~200 preguntas con respuesta anotada contra
   documento y página.** El banco actual mide la dirección correcta pero con muestras
   pequeñas; los intervalos de confianza son amplios.
3. **Calibrar los pesos de las banderas amarillas contra los 160 casos** en vez de fijarlos
   por criterio clínico convencional. El arnés ya produce la métrica objetivo.
4. **Voz neuronal con streaming de audio** en lugar de `speechSynthesis`, aceptando el
   proveedor adicional una vez superada la compuerta de levantamiento.
5. **Persistir las llamadas en curso** para que un reinicio no cierre una conversación
   abierta.
6. **Detección de interrupción (barge-in)**: hoy el micrófono se rearma cuando el agente
   termina de hablar; un paciente que interrumpe tiene que esperar.

---

## 11. Limitaciones declaradas

- Las llamadas en curso viven en memoria; lo ya ocurrido queda en SQLite.
- La voz es la síntesis del navegador: se eligió por la compuerta de levantamiento.
- El OCR es una dependencia opcional no instalada: 1 PDF de 107 queda listado como "sin
  capa de texto" en vez de entrar vacío y en silencio al índice.
- No hay telefonía real ni autenticación, tal como el reto establece.
- Los datos son sintéticos y no tienen validez clínica.
