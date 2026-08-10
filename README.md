# Sara — agente de voz para seguimiento postoperatorio

Solución al **Tech Sphere Challenge 2026**. Un agente que llama por voz a un paciente
operado hace pocos días, conversa con él en español colombiano, fundamenta cada
afirmación clínica en un corpus documental, y decide si el caso debe pasar a manos de
personal capacitado.

| Entregable | Dónde |
|---|---|
| **01** Repositorio | este repositorio |
| **02** Diagrama de arquitectura y flujo de decisión | [`docs/arquitectura.md`](docs/arquitectura.md) |
| **03** Informe final | [`docs/informe-final.md`](docs/informe-final.md) |
| **04** Video | _pendiente_ |

El enunciado original del reto está en [`docs/reto.md`](docs/reto.md); la rúbrica, en
[`docs/rubrica-evaluacion.md`](docs/rubrica-evaluacion.md).

---

## 1. Levantar en menos de 15 minutos

Probado desde cero en Windows 11 y Python 3.12. Cuatro pasos.

**1 · Clonar e instalar**

```bash
git clone -c core.longpaths=true https://github.com/HenryStark866/reto-sphere-2026.git
```

```bash
cd reto-sphere-2026 && python -m venv .venv && .venv/Scripts/activate && pip install -r requirements.txt
```

En macOS o Linux el activador es `source .venv/bin/activate`, y la bandera
`core.longpaths` sobra —pero no estorba—.

> **La bandera no es opcional en Windows.** Tres artículos del corpus llevan su título
> completo como nombre de archivo; el más largo da una ruta relativa de **255 caracteres**,
> y Windows corta en 260. Sin `core.longpaths=true` el `clone` falla con *"Filename too
> long"* y el repositorio queda a medias. Se explica en §8.

**2 · Poner la credencial**

Una sola clave, gratuita, sin tarjeta: se crea en <https://console.groq.com/keys>.

```bash
cp .env.example .env
```

Abre `.env` y pega la llave en `GROQ_API_KEY=`.

**3 · Levantar**

```bash
uvicorn app.server:app --port 8000
```

El primer arranque descarga el modelo de embeddings (~90 MB, una sola vez) en segundo
plano; el servidor responde de inmediato mientras tanto.

**4 · Abrir <http://localhost:8000>**

La portada muestra el estado del sistema y avisa si falta algo. Desde ahí se entra a las
dos superficies:

| Superficie | URL | Qué hace |
|---|---|---|
| **Interfaz de llamada** | `/llamada` | Elegir paciente, hablar por micrófono, oír al agente |
| **Consola de administración** | `/consola` | Subir, listar y eliminar documentos en caliente; probar el corpus; ver alertas, llamadas y métricas |

**El índice del corpus viene construido en el repositorio** (`index/`, 17,0 MB), así que no
hay que procesar los 107 PDFs dentro del cronómetro: hacerlo toma **5,8 minutos** en un
portátil con el modelo de embeddings ya en caché —más de un tercio de la compuerta, sin
contar los 90 MB del modelo—. La medición está en
[`logs/construccion_indice.log`](logs/construccion_indice.log). Para reconstruirlo:

```bash
python -m scripts.build_index --limpiar
```

> **Micrófono y navegador.** La llamada usa `getUserMedia` y `speechSynthesis`. Ambas
> funcionan en Chrome y Edge sobre `localhost` sin certificado. Si el micrófono no está
> disponible, la interfaz ofrece un campo de texto de respaldo que recorre exactamente el
> mismo camino salvo la transcripción.

---

## 2. Qué hace, en una página

Un turno de conversación recorre esto:

```mermaid
flowchart LR
    A[Paciente habla<br/>navegador] -->|audio webm| B[Whisper Large V3 Turbo<br/>groq_client.transcribir]
    B --> C{En paralelo}
    C --> D[Extracción estructurada<br/>Llama 3.1 8B · prompts.SISTEMA_EXTRACCION]
    C --> E[Recuperación híbrida<br/>rag.consultar · denso + BM25 + RRF]
    D --> F[EstadoSintomas.fusionar<br/>schema.py]
    F --> G[Triaje determinista<br/>triage.evaluar]
    G --> H[Segunda opinión<br/>Llama 3.1 8B · solo puede escalar]
    H --> I[triage.combinar]
    E --> J[Respuesta hablada<br/>Llama 3.3 70B en streaming]
    I --> J
    J -->|frase a frase| K[Voz en el navegador<br/>speechSynthesis]
    I -->|si es rojo| L[(Alerta en SQLite<br/>db.crear_alerta)]
    E --> M[(Citas trazables<br/>db.registrar_citas)]
```

Tres decisiones que explican el resto:

**La extracción y la recuperación corren a la vez.** Ninguna depende de la otra: ambas
parten del texto crudo del paciente. Solaparlas ahorra un tramo de silencio que en una
llamada de voz se nota ([`conversation.py:186`](app/agent/conversation.py)).

**La respuesta se emite por frases, no completa.** El navegador empieza a hablar con la
primera frase mientras el modelo todavía genera el resto
([`conversation.py:265`](app/agent/conversation.py)). Es la diferencia entre un silencio
de dos segundos y uno de medio segundo.

**El triaje lo deciden reglas, no el modelo.** El modelo da una segunda opinión que
**solo puede subir el nivel, nunca bajarlo** ([`triage.combinar`](app/agent/triage.py)).
La rúbrica llama falla catastrófica al falso negativo; una regla determinista es
auditable y no se deja convencer por un paciente que minimiza sus síntomas.

El flujo completo de decisión está en [`docs/arquitectura.md`](docs/arquitectura.md).

---

## 3. Modelo de lenguaje: cuál y por qué

**Familia Meta Llama, servida por Groq.** Se verifica en arranque y se expone en
`/api/salud` ([`config.modelo_permitido`](app/config.py)).

| Función | Modelo | Por qué ese |
|---|---|---|
| Diálogo con el paciente | `llama-3.3-70b-versatile` | Es el que redacta lo que el paciente oye: registro, empatía y adherencia a límites clínicos. La calidad manda sobre el costo porque son ~160 tokens por turno. |
| Extracción estructurada y juicio | `llama-3.1-8b-instant` | Corre en el camino crítico de la latencia y devuelve JSON, no prosa. Un modelo de 8B resuelve la tarea con un tiempo hasta el primer token muy inferior. |
| Transcripción | `whisper-large-v3-turbo` | Misma cuenta y misma región que el resto: evita un segundo proveedor en el camino crítico. |

**Por qué Groq y no otro proveedor de la misma familia:** en voz, lo que el paciente
percibe como silencio incómodo es el tiempo hasta el primer token. Groq sirve Llama sobre
LPU con un TTFT muy por debajo de las alternativas, y expone Whisper en la misma API, lo
que elimina un salto de red del camino crítico.

La lista de familias permitidas se declara en
[`config.FAMILIAS_PERMITIDAS`](app/config.py) y fija **familias, no versiones**: si Groq
retira un snapshot, basta cambiar la variable de entorno.

---

## 4. Métricas reportadas

Todos los números de esta sección salen de `logs/turnos.jsonl`, que es lo mismo que agrega
`GET /api/metricas` y lo mismo que se ve al pie de la consola. Se declara al lado de cada
tabla **de cuántos turnos sale**, porque la muestra todavía es corta y un percentil sobre
siete turnos no es un percentil sobre setenta.

### Latencia

Medida como la define la rúbrica: desde que el paciente termina de hablar hasta que
**empieza a sonar** el audio del agente. Ese segundo instante ocurre en el navegador —es
el evento `start` de la síntesis de voz—, así que es el cliente quien cierra la medición y
la reporta al servidor ([`metrics.actualizar_latencia_cliente`](app/metrics.py)).
Cualquier número medido solo en el servidor sería optimista.

Muestra: **7 turnos de una llamada de voz real**, con micrófono y síntesis del navegador.

| Métrica | Valor |
|---|---|
| P50 extremo a extremo | **2 520 ms** |
| P95 extremo a extremo | **3 224 ms** |

Desglose por etapa (mediana sobre los 8 turnos de voz):

| Etapa | P50 |
|---|---:|
| Transcripción (Whisper) | 957 ms |
| Extracción + recuperación, en paralelo | 625 ms |
| Triaje determinista | 0,0 ms |
| Hasta el primer token del diálogo (TTFT) | 613 ms |
| Generación completa del diálogo | 695 ms |
| Total de servidor | 2 465 ms |
| Total extremo a extremo medido en el cliente | 2 520 ms |

Los 55 ms entre el total de servidor y el del cliente son lo que tarda el navegador en
arrancar la síntesis de voz. Que el triaje mida 0,0 ms no es un error de medición: son
comparaciones contra umbrales sobre una estructura ya en memoria.

### Consumo

Muestra: **7 turnos de una llamada completa**, los que traen el reparto de tokens por
modelo.

| Métrica | Valor |
|---|---|
| Tokens de entrada / salida por turno | 3 398 / 249 |
| Tokens de entrada / salida por llamada | 23 788 / 1 744 |
| Invocaciones al modelo por turno | 2,57 |
| Consultas al RAG por llamada | 4,0 (sobre 7 turnos) |

Reparto real de esa llamada: `llama-3.1-8b-instant` 7 878 / 1 506 tokens en 11
invocaciones (extracción y segunda opinión), `llama-3.3-70b-versatile` 15 910 / 238 en 7
invocaciones (la respuesta hablada). Las invocaciones por turno no son un número entero
porque la segunda opinión solo corre cuando la extracción cambió algo del estado.

Las consultas al RAG no son una por turno a propósito: se consulta el corpus cuando el
paciente pregunta algo o cuando hay una bandera que sustentar, no cuando dice "sí" o "ahí
vamos" ([`conversation._recuperar`](app/agent/conversation.py)).

### Costo por llamada

| Métrica | Valor |
|---|---|
| Costo por turno | US$ 0,00144 |
| Costo por llamada de 7 turnos | US$ 0,0101 |
| Transcripción, aparte | US$ 0,00007 por turno de ~6 s de audio |
| **Costo total estimado por llamada** | **≈ US$ 0,0106** |

**Cómo se calcula.** El nivel gratuito de Groq no cobra, así que se extrapola a precios
públicos de producción, declarados en [`metrics.PRECIOS_USD_POR_MILLON`](app/metrics.py):
`llama-3.3-70b-versatile` a US$0,59 / US$0,79 por millón de tokens de entrada / salida;
`llama-3.1-8b-instant` a US$0,05 / US$0,08; `whisper-large-v3-turbo` a US$0,04 por hora de
audio. El costo se acumula por turno con los `usage` reales que devuelve la API —no con
estimaciones— y queda en cada línea de `logs/turnos.jsonl`.

**Cada modelo se tarifa al suyo.** Un turno mezcla el 8B de la extracción con el 70B de la
respuesta, y entre los dos hay un factor de doce en el precio de entrada. Cobrar el total
del turno a la tarifa del 70B —que es lo que hacía una versión anterior de este cálculo—
sobreestimaba el costo un 88 %. Por eso cada turno registra `tokens_por_modelo` y el costo
se suma modelo por modelo ([`groq_client.Uso.desglose`](app/llm/groq_client.py)).

**Todo lo anterior es verificable.** Cada turno deja un registro en
`logs/turnos.jsonl` con sus tiempos por etapa, tokens, invocaciones, citas y costo.
`GET /api/metricas` agrega ese archivo en vivo, y es lo mismo que se ve al pie de la
consola.

---

## 5. Evaluación

Un solo comando reproduce las tres evaluaciones y deja la evidencia en `logs/`:

```bash
python -m scripts.evaluar --modo todo
```

| Modo | Qué mide | Contra qué |
|---|---|---|
| `triaje` | Clasificación de criticidad, sensibilidad de escalamiento, **falsos negativos**, robustez al ruido de la capa 2 | Los 160 casos y su `label_ground_truth` |
| `rag` | Fundamentación de las respuestas clínicas y abstención ante lo que el corpus no cubre | [`eval/preguntas_rag.json`](eval/preguntas_rag.json) |
| `adversarial` | Inyección de prompt, peticiones de medicación, minimización ante bandera roja, hostilidad, jerga regional | [`eval/adversarial.json`](eval/adversarial.json) |

El arnés **reconstruye una `Llamada` real y llama a sus propios métodos**: si
reimplementara la extracción o el triaje mediría un sistema distinto del que corre en
producción. Escribe en su propia base (`data/evaluacion.db`) y en su propio log
(`logs/evaluacion_turnos.jsonl`) para no contaminar las latencias reportadas arriba, que
provienen únicamente de sesiones de voz reales.

### Resultados

**Recuperación** — 26 preguntas, ejecutado, evidencia en `logs/evaluacion_rag.json`:

| Métrica | Valor |
|---|---|
| Preguntas cubiertas por el corpus que superan el umbral | 14/18 (77,8 %) |
| Preguntas cuya recuperación contiene el término clínico esperado | 16/18 (88,9 %) |
| Similitud media del mejor pasaje | 0,749 |
| Índice consultado | 107 documentos · 6 239 fragmentos · 384 dimensiones |

Fundamentación verificada por juez, triaje y adversarial: _pendientes de ejecución._

### Qué encontró el arnés

Se documenta porque el proceso también se evalúa, y porque estos tres hallazgos cambiaron
el diseño:

**El umbral de similitud no puede ser el mecanismo de abstención.** Calibrando contra las
26 preguntas, las dos distribuciones se solapan casi por completo: la pregunta *fuera* del
corpus con mayor similitud —postoperatorio de trasplante renal— puntúa **0,832** contra
guías de cuidado postoperatorio, por encima de 17 de las 18 preguntas que el corpus **sí**
responde. El mejor umbral posible separa con un Youden J de 0,31. La similitud coseno mide
parecido temático, no si el corpus contiene la respuesta. El diseño cambió: el umbral pasó
a 0,70 como **piso** contra recuperaciones francamente malas, y la decisión de callarse la
toma el modelo, que a diferencia del coseno tiene delante la pregunta y el texto
([`prompts.bloque_contexto`](app/agent/prompts.py)). Con el umbral anterior de 0,78 el
agente habría dicho "no tengo esa información" en la mitad de las preguntas que el corpus
responde.

**Una condición de carrera vaciaba el índice.** `obtener_indice` publicaba el índice en la
variable global *antes* de terminar de cargarlo, así que una consulta concurrente durante
el arranque recibía cero fragmentos y el agente respondía "no tengo esa información" con
el corpus entero ya en disco. Se detectó porque una pregunta del arnés devolvió
similitud 0,000. Corregido con doble comprobación bajo cerrojo
([`rag/store.py`](app/rag/store.py)); arreglarlo subió la recuperación por términos del
83,3 % al 88,9 %.

**La extracción no recibía la pregunta que estaba interpretando.** El turno del paciente se
añadía al historial antes de extraer, y la extracción tomaba `historial[-1]` como "última
pregunta del agente" —es decir, la propia frase del paciente—. Sin la pregunta, una
respuesta como *"un tres"* es ininterpretable. Corregido en
[`conversation._extraer`](app/agent/conversation.py).

**Un número devuelto como cadena mataba el turno, y lo mataba escalando.** El modelo
devuelve JSON y de vez en cuando manda `"dolor_nrs": "8"` en vez de `8`. El motor de
reglas compara ese valor contra el umbral, y `'8' >= 8` levanta un `TypeError` que aborta
el turno entero: el agente se queda mudo **justo en el turno en el que había que alertar**,
que es el peor momento posible para quedarse mudo. Apareció en dos casos del arnés antes
de aparecer en una demo. Ahora `EstadoSintomas.fusionar` normaliza al tipo del esquema
antes de guardar, y un valor que no se puede interpretar se descarta —que es lo mismo que
"no se preguntó"— en vez de entrar crudo al estado clínico
([`schema._normalizar`](app/agent/schema.py)). De paso, una temperatura fuera del rango
30–45 °C se descarta: es una transcripción mal entendida, no un paciente.

**El costo por llamada estaba sobreestimado un 88 %.** El cálculo tarifaba todos los
tokens del turno al precio de `llama-3.3-70b-versatile`, incluidos los ~1 900 tokens de
extracción y segunda opinión que corren en `llama-3.1-8b-instant`, doce veces más barato
en entrada. La rúbrica desempata por menor costo por llamada *verificado*: un número
inflado no solo es falso, además juega en contra. Cada turno registra ahora el reparto
`tokens_por_modelo` y el costo se suma modelo por modelo.

**Un paciente que se aguanta apagaba la alerta.** Es el hallazgo que más cambió el
sistema, y está contado aparte en la sección que sigue.

### El caso que se escapaba

`caso_tray_pac_42_00017_7`, capa ruidosa. La trayectoria clínica real de ese paciente es
dolor **9/10**, temperatura 37,9 °C, apetito muy disminuido y arquetipo
`complicacion_real`. Su criticidad de referencia es **roja**. Esto es lo que dice en la
llamada:

> *"Ay, no, tranquila doctora, un poquito molesto no más — nada del otro [inaudible], uno aguanta."*
>
> *"Me tomé la temperatura ayer, marcó como 37 y algo, nada de escalofríos ni cosas raras, tranquila."*
>
> *"Se ve un poquito rojita ahí en el borde, pero nada de esas cosas de pus ni nada raro, yo creo que es normal de la cicatrización."*

El agente le creyó. Extrajo **dolor 2/10** de "un poquito molesto", 37,0 °C de "37 y algo",
y cerró la llamada en **verde**. Un falso negativo sobre el caso más grave del dataset: la
falla que la rúbrica llama catastrófica.

Tres cosas fallaron a la vez, y las tres estaban en el diseño, no en el modelo:

**La regla de extracción era asimétrica al revés.** Decía *"no infieras severidad que el
paciente no expresó; «me molesta un poquito» no es un ocho"*. Eso impide inventar un
número **alto**, y deja libre inventar uno **bajo** — que es justo el que apaga la alerta.
Ahora una descripción cualitativa no es ningún número: `dolor_nrs` queda en `null` y el
agente tiene que pedir la cifra de cero a diez antes de dar el tema por cubierto. Lo mismo
con "37 y algo": una cifra vaga ya no es una temperatura medida, es una fiebre sin medir,
que pesa como febrícula.

**El agente no separaba el hecho de la tranquilización del paciente.** "Se ve rojita pero
es normal de la cicatrización" son dos cosas: un eritema —que es un dato— y una opinión
del paciente —que no lo es—. El prompt ahora las separa explícitamente y descarta la
segunda.

**Y las banderas sí se apagaban.** El nivel llegó a amarillo en el turno 2 y volvió a verde
en el 4, porque `fusionar` dejaba que el último valor pisara al anterior en los campos
graduados. La protección "una bandera negativa no borra una positiva" solo cubría las
booleanas. Ahora el estado guarda además el **peor valor observado en la llamada**, y el
triaje se decide sobre ese ([`EstadoSintomas.para_triaje`](app/agent/schema.py)): el campo
vivo sigue teniendo lo último que dijo el paciente, porque es lo que la conversación
necesita para no repreguntar, pero un nueve que después se convierte en tres ya no baja el
nivel.

---

## 6. Despliegue en la nube

El levantamiento local de §1 es el camino oficial de la entrega. Además hay un despliegue
gestionado, para que la solución se pueda abrir con una URL sin instalar nada.

**Por qué no Firebase Hosting.** Hosting entrega archivos estáticos; esto es un servidor
Python con el índice y las llamadas en curso en memoria y respuestas en streaming. Hosting
solo puede quedar delante como puerta de entrada, reescribiendo hacia donde corra la
aplicación de verdad ([`firebase.json`](firebase.json)).

### Camino gratuito: Hugging Face Spaces

Es el despliegue recomendado y no cuesta nada. Un Space público con CPU básica es gratuito
de forma permanente, da 2 vCPU y 16 GB de RAM —de sobra para el modelo ONNX y el índice en
memoria— y no pide tarjeta.

```bash
python -m scripts.desplegar_hf --token <hf_...> --llave-groq <gsk_...>
```

Crea el Space, guarda la llave como **secreto** —nunca en la imagen—, sube la aplicación
con el índice ya construido y devuelve la URL. Solo hace falta una cuenta gratuita en
[huggingface.co](https://huggingface.co) y un token de escritura.

### Alternativa de pago: Cloud Run

```bash
./scripts/desplegar.ps1 -Proyecto <id-del-proyecto> -LlaveGroq <gsk_...>
```

Habilita las APIs, guarda la llave en Secret Manager, construye con Cloud Build (no hace
falta Docker local), despliega y verifica `/api/salud`. **Cloud Run tiene capa gratuita,
pero exige una cuenta de facturación activa para poder habilitarse siquiera**, y una
instancia siempre encendida cuesta del orden de US$13 al mes. Esa activación la hace una
persona, no una automatización.

### Dos decisiones del despliegue que no son obvias

**Una sola instancia.** El índice vivo y las llamadas en curso viven en la memoria del
proceso. Con dos instancias, un documento subido a la A no existiría para la B —lo que
rompería la prueba de conocimiento vivo— y una llamada iniciada en A se caería al llegar a
B. Escalar de verdad exigiría sacar el índice y la sesión del proceso; para una evaluación,
una instancia fija es lo correcto y lo honesto.

**El modelo de embeddings se hornea dentro de la imagen** ([`Dockerfile`](Dockerfile)). Si
se descargara al arrancar, la primera consulta al corpus pagaría 90 MB de descarga. Por la
misma razón se copia el índice ya construido: reprocesar los 107 PDFs toma 5,8 minutos.

**Limitación de cualquiera de los dos despliegues:** el sistema de archivos es efímero. La
base de llamadas y los documentos subidos viven en `/tmp` y se pierden cuando la instancia
se recicla. Dentro de una sesión de evaluación todo persiste; entre sesiones, no. En el
levantamiento local de §1 la persistencia es real, en `data/llamadas.db`.

---

## 7. Estructura

```
app/
  server.py            FastAPI: las dos superficies y la API
  config.py            Configuración central, toda sobrescribible por entorno
  metrics.py           Cronómetro por etapa, costo, percentiles
  agent/
    conversation.py    Orquestación del turno y de la llamada
    triage.py          Reglas deterministas de criticidad
    schema.py          EstadoSintomas, Bandera, ResultadoTriaje
    prompts.py         Todo el texto que gobierna el comportamiento
  rag/
    extract.py         PDF → texto por página, con OCR de respaldo
    chunker.py         Troceado con solape
    embedder.py        Embeddings ONNX locales, sin PyTorch
    store.py           Índice vivo: denso + BM25, alta y baja en caliente
    service.py         Indexar y consultar
  llm/groq_client.py   Diálogo, streaming, JSON, transcripción, reintentos
  storage/db.py        SQLite: llamadas, turnos, citas, alertas, resúmenes
  web/                 Las dos superficies (HTML, sin dependencias de terceros)
scripts/
  build_index.py       Construye el índice del corpus
  evaluar.py           Arnés de evaluación
eval/                  Bancos de preguntas y entradas adversas
docs/                  Arquitectura, informe final, enunciado y rúbrica
index/                 Índice construido, versionado a propósito
```

**Sin dependencias de terceros en el navegador.** Las dos superficies son HTML, CSS y
JavaScript planos: no hay CDN, ni framework, ni paso de compilación. Es una decisión de
compuerta —menos cosas que puedan fallar en el minuto 14 del cronómetro—, no de estilo.

---

## 8. Límites conocidos

Se declaran porque la rúbrica premia saber qué quedó fuera:

- **El corpus trae los 107 PDFs y no tiene duplicados.** El enunciado anuncia 107
  documentos y advierte de repetidos entre carpetas. En `dataset/textos/` hay 107
  archivos, todos PDF, y ninguno se repite: se verificó por nombre, por tamaño y por hash
  SHA-256. El indexador deduplica por hash de todas formas
  ([`build_index.py`](scripts/build_index.py)) —citar la copia en vez del original
  inflaría el recall aparente— pero sobre este corpus no descarta nada.
- **Tres artículos llevan el título completo como nombre de archivo, y eso choca dos veces
  con el límite de 260 caracteres de Windows.** La ruta relativa más larga mide 255: deja
  cinco caracteres para el directorio base, así que el problema aparece en cualquier
  máquina, no solo en carpetas profundas.
  - *Al indexar:* ante una ruta así `Path.is_file()` devuelve `False` **sin lanzar nada**.
    Los tres desaparecían del índice sin un solo error en el log y el conteo salía cuadrado
    en 104 documentos. Ahora se abren con el prefijo `\\?\`
    ([`extract.ruta_larga`](app/rag/extract.py)), y el indexador contabiliza cada archivo
    encontrado y **termina con código 1 si alguno no acabó en el índice**.
  - *Al clonar:* `git clone` aborta con *"Filename too long"*. De ahí la bandera
    `core.longpaths=true` del §1, que es obligatoria en Windows.

  Se prefirió esto a renombrar los archivos: son artículos colombianos de cirugía
  colorrectal —dos sobre protocolos ERAS— y el nombre es el título de la publicación, que
  es lo que el agente cita al paciente.
- **Un PDF de `Appendicitis/` está escaneado sin capa de texto**, tal como advierte el
  enunciado: `REVISIÓN DE LA LITERATURA SOBRE LA APENDICITIS AGUDA PEDIATRICA...`. Se
  intenta OCR; como la dependencia opcional no está instalada, el documento queda listado
  en la consola como `sin capa de texto` en vez de entrar vacío y en silencio al índice.
  Es 1 de 107: se prefirió no añadir Tesseract a las dependencias antes que gastar parte
  del cronómetro de levantamiento en un binario del sistema.
- **Las llamadas en curso viven en memoria.** Reiniciar el servidor cierra las llamadas
  abiertas; lo ya ocurrido queda en SQLite. Es una decisión consciente de alcance: el reto
  no pide alta disponibilidad.
- **La voz del agente es la síntesis del navegador.** Es lo que permite que la solución se
  levante sin credenciales adicionales. Una voz neuronal mejoraría la naturalidad a costa
  de un proveedor más en el camino crítico y de la compuerta de 15 minutos.
- **No hay telefonía real ni autenticación**, tal como el reto establece.

---

## 9. Licencia

Código bajo licencia MIT (ver [`LICENSE`](LICENSE)). Los datos de `dataset/` son
sintéticos y provienen del repositorio base del reto; los PDFs de `dataset/textos/` son
obra de sus respectivos autores. Nada de esto tiene validez clínica.
