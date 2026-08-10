"""Configuracion central. Todo se puede sobreescribir por variable de entorno."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


# --------------------------------------------------------------------------
# Modelo de lenguaje  (compuerta G3: familia Meta Llama servida por Groq)
# --------------------------------------------------------------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

# Modelo que redacta lo que el agente le dice al paciente.
MODEL_DIALOGO = _env("MODEL_DIALOGO", "llama-3.3-70b-versatile")
# Modelo pequeno para extraccion estructurada: se prioriza latencia.
MODEL_EXTRACCION = _env("MODEL_EXTRACCION", "llama-3.1-8b-instant")
# Transcripcion de voz a texto.
MODEL_STT = _env("MODEL_STT", "whisper-large-v3-turbo")

# La lista de modelos permitidos fija familias, no versiones. Si Groq retira un
# snapshot, basta cambiar la variable de entorno: cualquier identificador que
# empiece por uno de estos prefijos sigue perteneciendo a la familia Meta Llama.
FAMILIAS_PERMITIDAS = ("llama", "meta-llama/llama")

# Intentos por llamada al proveedor. El nivel gratuito de Groq limita por
# tokens por minuto, asi que un 429 puede pedir una espera de decenas de
# segundos; con pocos intentos el arnes acaba midiendo la cuota de la cuenta y
# no el sistema. Ver groq_client._con_reintentos.
LLM_REINTENTOS = _env_int("LLM_REINTENTOS", 5)

TEMPERATURA_DIALOGO = _env_float("TEMPERATURA_DIALOGO", 0.3)
TEMPERATURA_EXTRACCION = _env_float("TEMPERATURA_EXTRACCION", 0.0)
# Respuestas habladas: cortas por diseno. Una respuesta larga es inviable en voz.
MAX_TOKENS_DIALOGO = _env_int("MAX_TOKENS_DIALOGO", 160)

# --------------------------------------------------------------------------
# RAG
# --------------------------------------------------------------------------
# MiniLM multilingue: 384 dimensiones y 0.22 GB. Se prefiere sobre
# multilingual-e5-large (2.24 GB) porque la compuerta G2 cronometra el
# levantamiento y la descarga del modelo corre dentro de ese reloj.
MODELO_EMBEDDING = _env(
    "MODELO_EMBEDDING", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)
# Solo la familia e5 espera estos prefijos; en los modelos paraphrase estorban.
PREFIJO_CONSULTA = "query: "
PREFIJO_PASAJE = "passage: "
USA_PREFIJOS_E5 = "e5" in MODELO_EMBEDDING.lower()

CHUNK_CARACTERES = _env_int("CHUNK_CARACTERES", 1100)
CHUNK_SOLAPE = _env_int("CHUNK_SOLAPE", 180)

RECUPERAR_DENSO = _env_int("RECUPERAR_DENSO", 30)
RECUPERAR_BM25 = _env_int("RECUPERAR_BM25", 30)
RECUPERAR_FINAL = _env_int("RECUPERAR_FINAL", 5)

# Piso de similitud coseno, NO el mecanismo de abstencion.
#
# Se calibro contra 26 preguntas (18 que el corpus cubre, 8 que no) con
# `scripts/evaluar.py --modo rag`: las dos distribuciones se solapan casi por
# completo. La pregunta fuera de corpus con mayor similitud -postoperatorio de
# trasplante renal- puntua 0.832 contra guias de cuidado postoperatorio, por
# encima de 17 de las 18 preguntas que el corpus si responde. El mejor umbral
# posible separa con un Youden J de 0.31: la similitud mide parecido tematico,
# no si el corpus contiene la respuesta.
#
# De ahi el diseno: el umbral filtra recuperaciones francamente malas (a 0.70
# conserva el 77.8% de las respondibles) y quien decide si los pasajes
# responden la pregunta es el modelo, que a diferencia del coseno tiene delante
# la pregunta y el texto. Ver prompts.bloque_contexto.
UMBRAL_EVIDENCIA = _env_float("UMBRAL_EVIDENCIA", 0.70)

# --------------------------------------------------------------------------
# Triaje  (umbrales calibrados contra los 160 casos; ver docs/informe-final.md)
# --------------------------------------------------------------------------
FIEBRE_ROJA = _env_float("FIEBRE_ROJA", 38.0)
FIEBRE_FEBRICULA = _env_float("FIEBRE_FEBRICULA", 37.5)
UMBRAL_AMARILLO = _env_int("UMBRAL_AMARILLO", 3)
UMBRAL_ROJO_ACUMULADO = _env_int("UMBRAL_ROJO_ACUMULADO", 8)

# --------------------------------------------------------------------------
# Rutas
# --------------------------------------------------------------------------
DATASET_DIR = BASE_DIR / "dataset"
CORPUS_DIR = DATASET_DIR / "textos"
INDEX_DIR = Path(os.getenv("INDEX_DIR", str(BASE_DIR / "index")))
# Cache del modelo de embeddings. Por defecto fastembed usa el directorio
# temporal del sistema; fijarlo aqui hace que el segundo arranque no descargue
# nada, que es lo que se cronometra en la compuerta G2.
MODELOS_DIR = Path(os.getenv("MODELOS_DIR", str(BASE_DIR / ".modelos")))
LOG_DIR = Path(os.getenv("LOG_DIR", str(BASE_DIR / "logs")))
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "data" / "llamadas.db")))
SUBIDAS_DIR = Path(os.getenv("SUBIDAS_DIR", str(BASE_DIR / "data" / "subidas")))

for _d in (INDEX_DIR, LOG_DIR, DB_PATH.parent, SUBIDAS_DIR, MODELOS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def modelo_permitido(nombre: str) -> bool:
    """Compuerta G3. Se verifica en el arranque y se expone en /api/salud."""
    n = nombre.lower()
    return any(n.startswith(f) for f in FAMILIAS_PERMITIDAS)
