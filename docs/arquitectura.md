# Arquitectura y flujo de decisión

Entregable 02. Cada elemento de estos diagramas nombra un símbolo real del código, con su
archivo, para que se pueda verificar uno por uno.

---

## 1. Componentes

```mermaid
flowchart TB
    subgraph NAV["Navegador — sin dependencias de terceros"]
        L["/llamada<br/><i>app/web/llamada.html</i><br/>MediaRecorder · detección de silencio por RMS<br/>speechSynthesis"]
        C["/consola<br/><i>app/web/consola.html</i><br/>alta y baja de documentos · probador del corpus"]
    end

    subgraph SRV["Servidor — FastAPI · app/server.py"]
        API["Rutas HTTP<br/>POST /api/llamadas/{id}/turno → SSE<br/>POST · DELETE /api/documentos<br/>GET /api/salud · /api/metricas"]
        ORQ["Orquestación del turno<br/><i>agent/conversation.py</i><br/>clase Llamada"]
        TRI["Triaje determinista<br/><i>agent/triage.py</i><br/>evaluar · combinar"]
        RAGS["Servicio de RAG<br/><i>rag/service.py</i><br/>indexar_archivo · consultar · eliminar_documento"]
        MET["Observabilidad<br/><i>metrics.py</i><br/>Cronometro · costo_usd · registrar_turno"]
    end

    subgraph LOCAL["Local — sin red"]
        IDX["Índice vivo<br/><i>rag/store.py</i> · IndiceVivo<br/>vectores numpy + BM25<br/><b>index/</b>"]
        EMB["Embeddings ONNX<br/><i>rag/embedder.py</i><br/>MiniLM multilingüe · 384 dim"]
        DB[("SQLite<br/><i>storage/db.py</i><br/>llamadas · turnos · citas<br/>alertas · resumenes")]
        LOG[("logs/turnos.jsonl<br/>un registro por turno")]
    end

    subgraph GROQ["Groq — familia Meta Llama"]
        W["whisper-large-v3-turbo<br/>transcripción"]
        E["llama-3.1-8b-instant<br/>extracción · segunda opinión"]
        D["llama-3.3-70b-versatile<br/>diálogo en streaming"]
    end

    L <-->|"audio · eventos SSE"| API
    C <-->|"multipart · JSON"| API
    API --> ORQ
    API --> RAGS
    ORQ --> TRI
    ORQ --> RAGS
    ORQ --> MET
    ORQ --> W
    ORQ --> E
    ORQ --> D
    RAGS --> IDX
    IDX --> EMB
    ORQ --> DB
    MET --> LOG
```

---

## 2. Un turno de conversación

```mermaid
sequenceDiagram
    autonumber
    participant P as Paciente
    participant N as Navegador
    participant S as server.turno
    participant LL as Llamada.procesar
    participant G as Groq
    participant R as rag.consultar
    participant T as triage

    P->>N: habla
    N->>N: detecta silencio (RMS < 0.015 por 1200 ms)
    Note over N: t0 — arranca el reloj de latencia
    N->>S: POST /api/llamadas/{id}/turno (audio webm)
    S->>G: transcribir (Whisper)
    G-->>S: texto
    S-->>N: evento «transcripcion»

    S->>LL: procesar(texto)
    par En paralelo — ThreadPoolExecutor
        LL->>G: _extraer → JSON clínico (Llama 8B)
        and
        LL->>R: _recuperar → pasajes (solo si hay pregunta o bandera)
    end
    LL->>LL: EstadoSintomas.fusionar(delta)
    LL->>T: evaluar(estado, dia_postop, procedimiento)
    T-->>LL: nivel por reglas
    opt si hubo cambios
        LL->>G: _segunda_opinion (Llama 8B)
        LL->>T: combinar — el modelo solo puede subir el nivel
    end
    LL-->>N: evento «estado» (nivel, banderas, pendientes)
    opt nivel rojo y sin alerta previa
        LL->>LL: db.crear_alerta
        LL-->>N: evento «alerta»
    end
    opt hubo pasajes
        LL->>LL: db.registrar_citas
        LL-->>N: evento «citas» (documento, página, similitud)
    end

    LL->>G: completar_stream (Llama 70B)
    loop por cada frase completa
        G-->>LL: trozos
        LL-->>N: evento «frase»
        N->>P: speechSynthesis habla
        Note over N: la primera frase cierra el reloj de latencia
    end
    LL-->>N: evento «fin» (ms por etapa, tokens, costo)
    N->>S: POST /latencia (ms extremo a extremo medido en el cliente)
```

**Por qué la respuesta se emite por frases.** El paciente empieza a oír al agente con la
primera frase, mientras el modelo todavía genera el resto. Es la diferencia entre un
silencio de dos segundos y uno de medio segundo, y es la razón de que el endpoint devuelva
un stream de eventos en vez de un JSON.

**Por qué el reloj lo cierra el cliente.** La rúbrica define la latencia hasta que
*empieza a sonar* el audio. Ese instante ocurre en el navegador, no en el servidor.

---

## 3. Flujo de decisión del agente

```mermaid
flowchart TD
    A["EstadoSintomas acumulado<br/><i>agent/schema.py</i>"] --> B{"¿Alguna bandera roja?<br/><i>triage._rojas</i>"}

    B -->|sí| ROJO["🔴 ROJO"]
    B -->|no| C["Suma de pesos de banderas amarillas<br/><i>triage._amarillas</i>"]

    C --> D{"puntaje ≥ 8<br/>UMBRAL_ROJO_ACUMULADO"}
    D -->|sí| ROJO
    D -->|no| E{"puntaje ≥ 3<br/>UMBRAL_AMARILLO"}
    E -->|sí| AMA["🟡 AMARILLO"]
    E -->|no| VER["🟢 VERDE"]

    ROJO --> F["Segunda opinión del modelo<br/><i>Llamada._segunda_opinion</i>"]
    AMA --> F
    VER --> F
    F --> G{"triage.combinar<br/>¿el modelo propone un nivel MÁS ALTO?"}
    G -->|sí| H["Sube el nivel · origen «reglas+llm»"]
    G -->|no| I["Manda la regla · origen «reglas»"]

    H --> J{"¿Nivel final rojo?"}
    I --> J
    J -->|sí| K["db.crear_alerta<br/>+ el agente le dice al paciente<br/>que su caso se reporta"]
    J -->|amarillo| M["Sigue indagando<br/>no da el hallazgo por resuelto"]
    J -->|verde| N{"¿Los seis dominios cubiertos?"}
    N -->|no| O["Pregunta el dominio pendiente<br/><i>estado.dominios_pendientes</i>"]
    N -->|sí| P["Cierra la llamada"]

    K --> Q["Al colgar: Llamada.resumen<br/>db.guardar_resumen"]
    M --> Q
    O --> Q
    P --> Q
```

### Las banderas rojas — escalan siempre, solas

Definidas en [`triage._rojas`](../app/agent/triage.py). Ninguna depende de un puntaje: una
sola basta.

| Código | Condición |
|---|---|
| `fiebre_alta` | Temperatura ≥ 38,0 °C (`FIEBRE_ROJA`) |
| `herida_purulenta` | Secreción purulenta en la herida |
| `dehiscencia` | Apertura de la herida |
| `sangrado_activo` | Sangrado activo por la herida |
| `movilidad_incapacitante` | Incapacidad nueva para moverse |
| `dolor_severo` | Dolor ≥ 8/10 |
| `dolor_subito` | Dolor de aparición súbita e intensa |
| `disnea` | Dificultad para respirar |
| `dolor_toracico` | Dolor en el pecho |
| `sincope` | Desmayo o pérdida de conciencia |
| `confusion` | Confusión o desorientación nueva |
| `vomito_persistente` | Vómito que no cede |
| `intolerancia_oral` | No tolera líquidos ni alimentos |
| `signos_tvp` | Pierna hinchada, caliente, roja o dolorosa |
| `ictericia` | Piel u ojos amarillos |
| `sin_transito_intestinal` | Sin gases ni deposición desde el día 3, **solo** en cirugía abdominal |

### Las banderas amarillas — suman

| Código | Peso | Condición |
|---|---:|---|
| `febricula` | 2 | 37,5 °C ≤ temperatura < 38,0 °C |
| `fiebre_no_medida` | 2 | Refiere fiebre pero no tiene termómetro |
| `eritema_leve` | 2 | Enrojecimiento alrededor de la herida |
| `dolor_sobre_lo_esperado` | 1–2 | Dolor por encima de lo esperable ese día |
| `movilidad_limitada_tardia` | 1 | Movilidad limitada desde el día 7 |
| `apetito_muy_disminuido` | 2 | Apetito muy disminuido |
| `apetito_disminuido_tardio` | 1 | Apetito disminuido desde el día 7 |
| `sueno_muy_alterado` | 2 | Sueño muy alterado |

**El dolor se juzga contra el día postoperatorio, no contra un número fijo**
([`triage.dolor_esperado`](../app/agent/triage.py)): un 5/10 el día 1 es curso normal; el
mismo 5/10 el día 14 no lo es. Umbrales esperados: día 1 → 6, día 3 → 5, día 7 → 4,
día 14 → 3.

### Las tres decisiones de diseño que sostienen esto

**Las reglas deciden; el modelo solo puede escalar.** Un modelo de lenguaje no da garantía
dura sobre un umbral, y la rúbrica declara el falso negativo como falla catastrófica. Con
reglas, una temperatura de 38,0 °C escala siempre, sin importar lo tranquilizador que suene
el paciente. El modelo aporta lo que las reglas no anticiparon —una combinación rara, un
contexto que agrava— pero [`triage.combinar`](../app/agent/triage.py) descarta cualquier
propuesta de bajar el nivel.

**`None` no es lo mismo que "normal".** Todo campo de `EstadoSintomas` empieza en `None`,
que significa *no se preguntó o el paciente no respondió*. El agente no puede cerrar la
llamada dando por sano un dominio que nunca consultó, y el resumen lo declara
explícitamente en `temas_no_cubiertos`.

**Una bandera positiva no se apaga.** En [`EstadoSintomas.fusionar`](../app/agent/schema.py),
un `false` posterior no borra un `true` ya confirmado: si el paciente reportó dificultad
para respirar en el turno 3 y en el turno 6 dice que ya está bien, la bandera sigue activa
para el triaje.

---

## 4. Conocimiento vivo

```mermaid
flowchart LR
    subgraph ALTA["Aprender — POST /api/documentos"]
        A1["Archivo desde la consola"] --> A2["extract.extraer<br/>PyMuPDF · OCR de respaldo"]
        A2 --> A3["chunker.trocear<br/>1100 caracteres · 180 de solape"]
        A3 --> A4["embedder.embeber_pasajes<br/>ONNX local"]
        A4 --> A5["IndiceVivo.agregar<br/>vstack + BM25 reconstruido"]
        A5 --> A6["version += 1<br/>guardar en index/"]
    end

    subgraph BAJA["Olvidar — DELETE /api/documentos/{doc_id}"]
        B1["IndiceVivo.eliminar"] --> B2["Filtra fragmentos, vectores y tokens<br/>del documento"]
        B2 --> B3["_reconstruir_bm25<br/>sin sus filas"]
        B3 --> B4["Borra el original de data/subidas/"]
        B4 --> B5["version += 1"]
    end

    A6 --> Q["rag.consultar<br/>denso + BM25 → RRF → boost por escenario"]
    B5 --> Q
    Q --> R{"mejor similitud ≥ 0,78<br/>UMBRAL_EVIDENCIA"}
    R -->|sí| S["prompts.bloque_contexto<br/>pasajes con documento y página"]
    R -->|no| T["ADVERTENCIA en el prompt:<br/>declara el límite, no improvises"]
```

**Por qué un índice propio y no una base vectorial empaquetada.** La compuerta G5 exige que
eliminar un documento lo borre de verdad. Aquí el borrado reconstruye las estructuras sin
las filas del documento, así que no queda residuo posible en un índice aproximado ni en una
caché interna. El contador `version` sube con cada mutación y la consola lo muestra como
prueba de que el índice que responde es el mismo que se acaba de modificar.

**Por qué búsqueda híbrida.** El corpus es bilingüe y mezcla guías clínicas con
instructivos para el paciente. La búsqueda densa capta la intención («se me abrió la
herida» → *wound dehiscence*); BM25 capta el término exacto cuando el paciente usa la
palabra técnica. Se fusionan con Reciprocal Rank Fusion, que es robusto ante escalas de
puntaje incomparables entre ambos métodos ([`IndiceVivo.buscar`](../app/rag/store.py)).

**Por qué el escenario sube el puntaje pero no filtra.** El corpus tiene guías
transversales —manejo del dolor, protocolos ERAS, tromboprofilaxis— que aplican a
cualquier cirugía. Filtrar por escenario las dejaría fuera; multiplicar por 1,25 las del
escenario del paciente las prioriza sin perderlas.

---

## 5. Qué queda registrado

| Dónde | Qué | Cuándo |
|---|---|---|
| `llamadas` | paciente, procedimiento, día postop, nivel final | al iniciar y al cerrar |
| `turnos` | cada intervención, con el nivel vigente | cada turno |
| `citas` | documento, página, `chunk_id`, similitud | cada vez que se cita el corpus |
| `alertas` | nivel, motivo, banderas y estado clínico completo | al alcanzar rojo, una sola vez por llamada |
| `resumenes` | el resumen estructurado íntegro | al cerrar la llamada |
| `logs/turnos.jsonl` | ms por etapa, tokens, invocaciones, costo, latencia del cliente | cada turno |

Todo es inspeccionable durante la evaluación con cualquier cliente de SQLite, o desde
`GET /api/llamadas/{id}` y la consola.
