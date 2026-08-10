# Imagen de produccion para Cloud Run.
#
# Dos cosas se hacen en tiempo de construccion a proposito, y las dos existen
# por la misma razon: en produccion el primer paciente no puede pagar el
# arranque en frio.
#   1. Se descarga el modelo de embeddings dentro de la imagen. Si no, la
#      primera consulta al corpus dispara una descarga de ~90 MB.
#   2. Se copia el indice ya construido. Reprocesar los 104 PDFs toma 20 minutos.
FROM python:3.12-slim

# onnxruntime necesita libgomp; el resto de las dependencias trae wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    MODELOS_DIR=/app/.modelos \
    INDEX_DIR=/app/index \
    # El sistema de archivos de Cloud Run es efimero: la base y las subidas
    # viven en /tmp, que es tmpfs. Se documenta como limite en el README.
    DB_PATH=/tmp/datos/llamadas.db \
    SUBIDAS_DIR=/tmp/datos/subidas \
    LOG_DIR=/tmp/logs

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/
COPY eval/ ./eval/
COPY index/ ./index/
# El registro de pacientes se lee de estos cuatro libros al arrancar.
COPY dataset/*.xlsx ./dataset/

# Precalienta el modelo de embeddings dentro de la imagen.
RUN python -c "from app.rag import embedder; embedder.precalentar(); print('embeddings listos')"

# Cloud Run inyecta PORT. Se corre como usuario sin privilegios.
RUN useradd --create-home --uid 10001 sara \
    && mkdir -p /tmp/datos /tmp/logs \
    && chown -R sara:sara /app /tmp/datos /tmp/logs
USER sara

ENV PORT=8080
EXPOSE 8080

# Un solo worker a proposito: el indice vivo y las llamadas en curso viven en
# memoria del proceso. Varios workers darian respuestas distintas segun cual
# atendiera la peticion. La concurrencia la resuelve el bucle async, no forks.
CMD exec uvicorn app.server:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 75
