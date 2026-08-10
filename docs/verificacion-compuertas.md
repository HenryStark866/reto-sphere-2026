# Verificación de las compuertas eliminatorias

Este documento deja por escrito **cómo se verificó cada compuerta y qué devolvió**, con
los comandos exactos para que el jurado los repita. Todo lo de aquí se ejecutó contra el
repositorio publicado y el servidor local, el 10 de agosto de 2026.

La rúbrica está en [`rubrica-evaluacion.md`](rubrica-evaluacion.md); las compuertas, en su
§3.

---

## G1 — Los 4 entregables

| # | Entregable | Estado |
|---|---|---|
| 01 | Repositorio público | <https://github.com/HenryStark866/reto-sphere-2026> |
| 02 | Diagrama de arquitectura y flujo de decisión | [`arquitectura.md`](arquitectura.md) |
| 03 | Informe final | [`informe-final.md`](informe-final.md) |
| 04 | Video | **pendiente de grabar** |

---

## G2 — Levantable en ≤ 15 minutos

El paso que más riesgo tenía era el primero, porque tres rutas del corpus rozan el límite
de 260 caracteres de Windows. Se cronometró el comando tal como lo escribe el README:

```bash
git clone -c core.longpaths=true https://github.com/HenryStark866/reto-sphere-2026.git
```

| Qué | Resultado |
|---|---|
| Tiempo del clon | **36 s** |
| Archivos traídos | 163 |
| PDFs del corpus | 107 |
| Índice incluido | `index/` con `documentos.json`, `fragmentos.json`, `meta.json`, `vectores.npy` |
| Tamaño en disco | 258 MB |
| Ruta relativa más larga | **255 caracteres** (`dataset/textos/colorectal cancer/Protocolo de recuperación mejorada…`) |

Los 255 caracteres dejan cinco para el directorio base: por eso `core.longpaths=true` no
es opcional en Windows, y por eso el índice viene construido en el repositorio —
reprocesar los 107 PDFs toma 5,8 minutos, más de un tercio del cronómetro.

**Sin la bandera, el clon aborta.** Verificado: `git clone` sin `core.longpaths` falla con
*"unable to create file … Filename too long"* en los tres artículos largos y deja el árbol
a medias.

---

## G3 — Modelo dentro de la lista permitida

Se comprueba en el arranque y queda expuesto en `/api/salud`, sin necesidad de leer el
código:

```bash
curl -s http://localhost:8000/api/salud
```

```json
{
  "modelos": {
    "dialogo":    {"modelo": "llama-3.3-70b-versatile",  "familia_permitida": true},
    "extraccion": {"modelo": "llama-3.1-8b-instant",     "familia_permitida": true},
    "stt":        {"modelo": "whisper-large-v3-turbo",   "familia_permitida": true}
  },
  "indice": {"version": 321, "documentos": 107, "fragmentos": 6239, "dimension": 384},
  "corpus_listo": true
}
```

Los dos modelos de lenguaje son de la familia **Meta Llama servida por Groq**, que
[`stack-tecnico.md`](stack-tecnico.md#1-los-modelos-permitidos) lista como permitida. La
comprobación es de familia y no de versión: [`config.FAMILIAS_PERMITIDAS`](../app/config.py)
acepta cualquier identificador que empiece por `llama` o `meta-llama/llama`, de modo que
retirar un snapshot no rompe la compuerta.

---

## G4 — Conversación de voz en tiempo real

Se verifica hablando: `/llamada`, elegir paciente, micrófono, y el agente responde con
voz. La latencia medida en esa sesión está en el §4 del [README](../README.md) — P50 de
2 520 ms y P95 de 3 224 ms, cerrada en el navegador cuando arranca la síntesis.

La interfaz ofrece además un campo de texto de respaldo que recorre **el mismo camino
salvo la transcripción**; sirve para reproducir una llamada sin micrófono, pero sus turnos
no alimentan el P50 reportado, que sale solo de voz real.

---

## G5 — Conocimiento vivo: aprende y olvida

Se verificó con un documento que no pertenece a ningún corpus entregado, con un término
inventado —`ZORZAL-7742`— para que no pueda confundirse con nada del corpus real.

**Antes del alta.** La consulta recupera cinco pasajes del corpus original; ninguno
menciona el criterio:

```
0.6828  Diagnóstico y tratamiento del paciente con colecistitis aguda calculosa…  p.50
0.5387  diagnóstico, tratamiento y seguimiento del paciente adulto con apendicitis…  p.33
0.6765  GUÍA DE MANEJO PARA EL DIAGNÓSTICO… DEL CÁNCER DE COLON Y RECTO  p.49
```

**Alta.** `POST /api/documentos` con el archivo:

```json
{"documento": {"doc_id": "41cc13b17ac73234", "estado": "procesado", "n_fragmentos": 1},
 "ms_procesamiento": 379.7,
 "indice": {"version": 322, "documentos": 108, "fragmentos": 6240}}
```

El índice pasa de **321 → 322**, de **107 → 108 documentos** y de **6 239 → 6 240
fragmentos**, en 380 ms. La consola lo marca *procesado y disponible*.

**Después del alta.** El documento nuevo entra en primer lugar, por encima de todo el
corpus original:

```
0.7792  protocolo_prueba_g5.txt  p.1      <-- el documento recién subido
0.6828  Diagnóstico y tratamiento del paciente con colecistitis aguda calculosa…  p.50
0.5387  diagnóstico, tratamiento y seguimiento del paciente adulto con apendicitis…  p.33
```

**Baja.** `DELETE /api/documentos/41cc13b17ac73234`:

```json
{"eliminado": "41cc13b17ac73234",
 "indice": {"version": 323, "documentos": 107, "fragmentos": 6239}}
```

**Después de la baja.** El índice vuelve exactamente a 107 documentos y 6 239 fragmentos,
y la misma consulta ya no devuelve ni un fragmento del documento retirado. No queda
residuo: la baja borra los vectores, las entradas de BM25 y el archivo original subido.

---

## Nota sobre lo que estas verificaciones no cubren

G4 exige una persona hablando por un micrófono: lo que está arriba documenta la latencia
de una sesión real, pero la compuerta se aprueba en vivo, no por este documento. Y el
entregable 04 —el video— sigue pendiente de grabar.
