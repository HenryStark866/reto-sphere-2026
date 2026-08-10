# AGENTS.md

Estas instrucciones orientan a agentes de coding y análisis sobre este repositorio.

## Proyecto

Este repositorio implementa un agente de voz para seguimiento postoperatorio, con una API FastAPI y dos superficies web:
- el flujo de llamada en `/llamada`
- la consola de administración en `/consola`

La documentación principal está en [README.md](README.md), [docs/arquitectura.md](docs/arquitectura.md) y [docs/stack-tecnico.md](docs/stack-tecnico.md). No duplicar esos documentos; enlazarlos y mantener cambios pequeños y específicos.

## Dominio y límites funcionales

- El servidor está en [app/server.py](app/server.py). La aplicación se levanta con `uvicorn app.server:app`.
- La orquestación del turno está en [app/agent/conversation.py](app/agent/conversation.py) a través de la clase `Llamada`.
- El triaje determinista vive en [app/agent/triage.py](app/agent/triage.py) y debe seguir siendo una capa de reglas que puede ser reforzada, pero no reemplazada, por una segunda opinión del modelo.
- La recuperación del corpus está en [app/rag/service.py](app/rag/service.py) y el índice vivo en [app/rag/store.py](app/rag/store.py).
- La persistencia y acceso a alertas/llamadas/citas está en [app/storage/db.py](app/storage/db.py).
- Las métricas y el registro de turnos se concentran en [app/metrics.py](app/metrics.py) y en `logs/turnos.jsonl`.

## Reglas de trabajo

- Mantener la arquitectura de streaming de eventos: el turno de voz se procesa con SSE y el navegador recibe eventos de transcripción, estado, citas, alerta y frases de audio.
- No introducir cambios que conviertan el triaje en un LLM puro; la rúbrica y el sistema contemplan falsos negativos como fallo crítico.
- Preferir cambios pequeños y localizados. El proyecto no es un monorepo de microservicios: las rutas HTTP, la lógica de negocio y la RAG están acopladas por diseño.
- Si se toca el flujo de extracción/recuperación, revisar el contrato de eventos y el registro de `turnos.jsonl` para no romper la interfaz cliente.

## Entorno y arranque

- El entorno Python se instala con [requirements.txt](requirements.txt).
- La clave de Groq se lee desde `.env` en la raíz y se expone como `GROQ_API_KEY`.
- Si el sistema no tiene `GROQ_API_KEY`, el servidor arranca pero la ruta de llamada se degrada; la consola puede seguir funcionando.

Comandos de uso frecuente:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.server:app --port 8000
```

## Evaluación y construcción del índice

- Reconstruir el índice de documentos: `python -m scripts.build_index --limpiar`.
- Ejecutar las evaluaciones: `python -m scripts.evaluar --modo todo`.
- Para validación de RAG o triage, usar el mismo enfoque del proyecto en los módulos de [scripts/evaluar.py](scripts/evaluar.py) y los JSON de [eval](eval).

## Convenciones de archivos y datos

- Los documentos de dominio y el índice ya vienen preparados en [index](index) para una demo rápida; no considerar que el corpus inicial es vacío.
- Al subir documentos desde la consola, mantener la validación por extensiones y tamaño en [app/server.py](app/server.py) y el flujo de indexación de [app/rag/service.py](app/rag/service.py).
- El índice y los trabajos de evaluación son evidencias de entrega; no “reducir” la implicación clínica de los scripts ni su uso de umbrales observables.

## Cuando se modifica código

1. Leer la ruta HTTP o el contrato que la invoca; no inventar un endpoint nuevo sin entender la API web.
2. Ajustar el servicio específico del dominio y, si afecta el cliente, mantener la serialización de eventos en español y el formato de respuesta SSE.
3. Ejecutar la verificación relevante antes de declarar que el cambio es estable: arranque del servidor, script de evaluación o import del módulo afectado.
