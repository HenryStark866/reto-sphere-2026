"""Servidor HTTP: las dos superficies del reto y la API que las sostiene.

Una sola aplicacion expone la consola de administracion y la interfaz de
llamada. El turno de voz viaja como stream de eventos y no como respuesta
unica: el navegador empieza a hablar con la primera frase que llega, sin
esperar a que el modelo termine de generar el resto.

Levantar:
    uvicorn app.server:app --reload
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import metrics
from app.agent import conversation
from app.config import GROQ_API_KEY, SUBIDAS_DIR, UMBRAL_EVIDENCIA
from app.data import patients
from app.llm import groq_client
from app.rag import embedder, extract
from app.rag import service as rag
from app.rag.store import obtener_indice
from app.storage import db

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("server")

WEB_DIR = Path(__file__).resolve().parent / "web"

# Carpetas del corpus. Ordena la consola y sesga la recuperacion hacia el
# escenario del procedimiento del paciente.
ESCENARIOS = (
    "general",
    "appendicitis",
    "cholecystitis",
    "colorectal_cancer",
    "breast_cancer",
    "total_joint_replacement",
)

# Suficiente para cualquier guia clinica del corpus; corta subidas absurdas.
MAX_BYTES_SUBIDA = 40 * 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Arranque: verificar la compuerta de modelo, cargar indice y precalentar.

    El precalentamiento del modelo de embeddings corre en un hilo aparte: el
    servidor queda respondiendo de inmediato y la carga termina mucho antes de
    que alguien alcance a iniciar una llamada.
    """
    estado = groq_client.verificar_modelos()
    for etiqueta, datos in estado.items():
        log.info(
            "Modelo %s: %s (%s)",
            etiqueta,
            datos["modelo"],
            "permitido" if datos["familia_permitida"] else "FUERA DE LA LISTA",
        )
    if not GROQ_API_KEY:
        log.warning("Sin GROQ_API_KEY: la consola funciona, la llamada no.")

    indice = obtener_indice()
    log.info("Indice: %s", indice.resumen())
    db.conexion()
    threading.Thread(target=embedder.precalentar, daemon=True).start()
    yield


app = FastAPI(title="Seguimiento postoperatorio por voz", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


# ---------------------------------------------------------------------------
# Las dos superficies
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def portada() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/consola", include_in_schema=False)
def consola() -> FileResponse:
    return FileResponse(WEB_DIR / "consola.html")


@app.get("/llamada", include_in_schema=False)
def llamada() -> FileResponse:
    return FileResponse(WEB_DIR / "llamada.html")


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    """El service worker se sirve desde la raiz, no desde /static.

    Un service worker solo puede controlar rutas por debajo de la suya, asi que
    uno servido en /static/sw.js no alcanzaria a /llamada ni a /consola. Se
    marca sin cache para que una version futura reemplace a la anterior en la
    siguiente carga: el fichero que decide como se sirve todo lo demas es el
    ultimo que uno querria ver obsoleto.
    """
    return FileResponse(
        WEB_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


# ---------------------------------------------------------------------------
# Salud
# ---------------------------------------------------------------------------
@app.get("/api/salud")
def salud() -> dict:
    """Estado verificable del sistema. Es lo primero que conviene abrir en la demo."""
    modelos = groq_client.verificar_modelos()
    indice = obtener_indice().resumen()
    return {
        "ok": bool(GROQ_API_KEY) and all(m["familia_permitida"] for m in modelos.values()),
        "credenciales_groq": bool(GROQ_API_KEY),
        "modelos": modelos,
        "indice": indice,
        "umbral_evidencia": UMBRAL_EVIDENCIA,
        "corpus_listo": indice["fragmentos"] > 0,
    }


# ---------------------------------------------------------------------------
# Pacientes
# ---------------------------------------------------------------------------
@app.get("/api/pacientes")
def listar_pacientes() -> dict:
    lista = []
    for p in patients.listar_pacientes():
        lista.append(
            {
                **p,
                "dia_postop_sugerido": patients.dia_postop_sugerido(p["fecha_cirugia"]),
                "dias_reales": patients.dias_desde_cirugia(p["fecha_cirugia"]),
            }
        )
    return {"pacientes": lista, "dias_postop": list(patients.DIAS_POSTOP)}


# ---------------------------------------------------------------------------
# Llamada
# ---------------------------------------------------------------------------
@app.post("/api/llamadas")
def crear_llamada(cuerpo: dict) -> dict:
    paciente_id = str(cuerpo.get("paciente_id", "")).strip()
    paciente = patients.obtener_paciente(paciente_id)
    if not paciente:
        raise HTTPException(404, f"No existe el paciente {paciente_id}")

    dia = cuerpo.get("dia_postop")
    dia_postop = int(dia) if dia not in (None, "") else patients.dia_postop_sugerido(
        paciente["fecha_cirugia"]
    )

    llamada = conversation.iniciar(paciente, dia_postop)
    return {
        "llamada_id": llamada.llamada_id,
        "paciente": paciente,
        "dia_postop": dia_postop,
        "saludo": llamada.saludo(),
    }


def _sse(evento: dict) -> str:
    return f"data: {json.dumps(evento, ensure_ascii=False)}\n\n"


@app.post("/api/llamadas/{llamada_id}/turno")
def turno(
    llamada_id: str,
    audio: UploadFile | None = File(None),
    texto: str | None = Form(None),
) -> StreamingResponse:
    """Un turno del paciente. Acepta audio (voz) o texto (respaldo y pruebas).

    Responde como stream de eventos para que la sintesis de voz del navegador
    arranque con la primera frase. La transcripcion ocurre dentro del stream,
    asi que el cliente ve lo que se entendio antes de que exista la respuesta.
    """
    llamada = conversation.obtener(llamada_id)
    if not llamada:
        raise HTTPException(404, "Llamada no encontrada o el servidor se reinicio")
    if llamada.finalizada:
        raise HTTPException(409, "La llamada ya fue cerrada")

    datos_audio = audio.file.read() if audio is not None else b""
    nombre_audio = (audio.filename if audio is not None else None) or "turno.webm"
    texto_directo = (texto or "").strip()

    def generar() -> Iterator[str]:
        try:
            ms_stt = 0.0
            seg_audio = 0.0
            if datos_audio:
                dicho, ms_stt = groq_client.transcribir(datos_audio, nombre_audio)
                # Opus a ~24 kbps: alcanza para estimar el costo del STT.
                seg_audio = len(datos_audio) / 3000.0
            else:
                dicho = texto_directo

            if not dicho:
                yield _sse(
                    {
                        "tipo": "vacio",
                        "mensaje": "No se entendio nada en el audio. Puede repetirlo?",
                    }
                )
                return

            yield _sse({"tipo": "transcripcion", "texto": dicho, "ms_stt": round(ms_stt, 1)})

            for evento in llamada.procesar(dicho, ms_stt=ms_stt, seg_audio=seg_audio):
                yield _sse(evento)

        except groq_client.SinCredenciales as exc:
            yield _sse({"tipo": "error", "mensaje": str(exc)})
        except Exception as exc:  # el navegador debe enterarse, no quedarse colgado
            log.exception("Fallo el turno de la llamada %s", llamada_id)
            yield _sse({"tipo": "error", "mensaje": f"Error procesando el turno: {exc}"})

    return StreamingResponse(
        generar(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/llamadas/{llamada_id}/latencia")
def latencia(llamada_id: str, cuerpo: dict) -> dict:
    """El navegador cierra la medicion cuando la voz del agente empieza a sonar."""
    turno_idx = int(cuerpo.get("turno", -1))
    ms = float(cuerpo.get("ms_e2e", 0))
    ok = metrics.actualizar_latencia_cliente(llamada_id, turno_idx, ms)
    return {"registrado": ok}


@app.post("/api/llamadas/{llamada_id}/cierre")
def cerrar(llamada_id: str) -> dict:
    llamada = conversation.obtener(llamada_id)
    if not llamada:
        raise HTTPException(404, "Llamada no encontrada")
    return llamada.resumen()


@app.get("/api/llamadas")
def historial(limite: int = 50) -> dict:
    return {"llamadas": db.listar_llamadas(limite)}


@app.get("/api/llamadas/{llamada_id}")
def detalle_llamada(llamada_id: str) -> dict:
    datos = db.obtener_llamada(llamada_id)
    if not datos:
        raise HTTPException(404, "Llamada no encontrada")
    return datos


@app.get("/api/alertas")
def alertas(limite: int = 100) -> dict:
    return {"alertas": db.listar_alertas(limite)}


# ---------------------------------------------------------------------------
# Conocimiento: alta y baja en caliente
# ---------------------------------------------------------------------------
def _nombre_seguro(nombre: str) -> str:
    """Un nombre de archivo que venga del navegador no toca rutas del servidor."""
    base = Path(nombre or "documento").name
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base)[:120] or "documento"


@app.get("/api/documentos")
def listar_documentos() -> dict:
    indice = obtener_indice()
    return {
        "documentos": indice.listar(),
        "indice": indice.resumen(),
        "escenarios": list(ESCENARIOS),
    }


@app.post("/api/documentos")
async def subir_documento(
    archivo: UploadFile = File(...),
    escenario: str = Form("general"),
) -> dict:
    """Sube un documento y lo indexa en caliente. Al volver, el agente ya lo cita."""
    nombre = _nombre_seguro(archivo.filename or "documento.pdf")
    if Path(nombre).suffix.lower() not in extract.EXTENSIONES:
        raise HTTPException(
            400, f"Formato no soportado. Se aceptan: {', '.join(sorted(extract.EXTENSIONES))}"
        )

    contenido = await archivo.read()
    if not contenido:
        raise HTTPException(400, "El archivo llego vacio")
    if len(contenido) > MAX_BYTES_SUBIDA:
        raise HTTPException(413, f"El archivo supera {MAX_BYTES_SUBIDA // (1024 * 1024)} MB")

    # En disco lleva una marca de tiempo para que dos subidas homonimas no se
    # pisen; en el indice se guarda con su nombre real, que es el que el agente
    # cita en voz alta y el que el jurado contrasta contra la fuente.
    destino = SUBIDAS_DIR / f"tmp_{int(time.time() * 1000)}_{nombre}"
    destino.write_bytes(contenido)

    inicio = time.perf_counter()
    try:
        doc = rag.indexar_archivo(
            destino,
            escenario=escenario if escenario in ESCENARIOS else "general",
            origen="consola",
            nombre=nombre,
        )
    except Exception as exc:
        destino.unlink(missing_ok=True)
        log.exception("Fallo la indexacion de %s", nombre)
        raise HTTPException(500, f"No se pudo procesar el documento: {exc}") from exc

    # Se conserva el original bajo el doc_id para poder borrarlo de verdad luego.
    final = SUBIDAS_DIR / f"{doc.doc_id}_{nombre}"
    destino.replace(final)

    indice = obtener_indice()
    indice.guardar()
    return {
        "documento": doc.__dict__,
        "ms_procesamiento": round((time.perf_counter() - inicio) * 1000, 1),
        "indice": indice.resumen(),
    }


@app.delete("/api/documentos/{doc_id}")
def eliminar_documento(doc_id: str) -> dict:
    """Baja en caliente. Se borra del indice y se borra el original subido."""
    if not rag.eliminar_documento(doc_id):
        raise HTTPException(404, "El documento no esta en el indice")
    for archivo in SUBIDAS_DIR.glob(f"{doc_id}_*"):
        archivo.unlink(missing_ok=True)
    return {"eliminado": doc_id, "indice": obtener_indice().resumen()}


@app.post("/api/rag/consulta")
def probar_rag(cuerpo: dict) -> dict:
    """Probador del corpus. Sirve para mostrar que el indice aprendio y olvido."""
    texto = str(cuerpo.get("texto", "")).strip()
    if not texto:
        raise HTTPException(400, "Falta el texto de la consulta")
    escenario = cuerpo.get("escenario") or None
    resultado = rag.consultar(texto, escenario=escenario, k=int(cuerpo.get("k", 5)))
    # El texto completo del pasaje no cabe en la tabla de la consola.
    for p in resultado["pasajes"]:
        p["extracto"] = p["texto"][:400]
        p.pop("texto", None)
    return resultado


# ---------------------------------------------------------------------------
# Metricas
# ---------------------------------------------------------------------------
@app.get("/api/metricas")
def metricas() -> JSONResponse:
    return JSONResponse(metrics.resumen())
