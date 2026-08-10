"""Servicio de RAG: indexar un archivo y consultar el corpus.

Lo usan por igual el script de construccion inicial y la consola de
administracion, de modo que un documento subido en caliente atraviesa
exactamente el mismo camino que el corpus original.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from app.config import (
    RECUPERAR_BM25,
    RECUPERAR_DENSO,
    RECUPERAR_FINAL,
    UMBRAL_EVIDENCIA,
)
from app.rag import extract
from app.rag.chunker import trocear
from app.rag.store import Documento, obtener_indice

log = logging.getLogger(__name__)


def sha256_archivo(ruta: Path) -> str:
    h = hashlib.sha256()
    with open(extract.ruta_larga(Path(ruta)), "rb") as f:
        for bloque in iter(lambda: f.read(65536), b""):
            h.update(bloque)
    return h.hexdigest()


def indexar_archivo(
    ruta: Path,
    escenario: str = "general",
    origen: str = "consola",
    intentar_ocr: bool = True,
    nombre: str | None = None,
) -> Documento:
    """Indexa un archivo. `nombre` es como se va a citar el documento.

    El archivo en disco puede llamarse distinto -la consola le antepone una marca
    de tiempo para que dos subidas con el mismo nombre no se pisen-, pero lo que
    el agente le lee al paciente y lo que el jurado contrasta contra la fuente es
    este nombre, no la ruta interna.
    """
    ruta = Path(ruta)
    extraccion = extract.extraer(ruta, intentar_ocr=intentar_ocr)
    fragmentos = trocear(extraccion)
    doc = obtener_indice().agregar(
        nombre=nombre or ruta.name,
        escenario=escenario,
        fragmentos_texto=[(f.texto, f.pagina) for f in fragmentos],
        origen=origen,
        sha256=sha256_archivo(ruta),
        paginas=len(extraccion.paginas) + extraccion.paginas_sin_texto,
        paginas_ocr=extraccion.paginas_ocr,
    )
    log.info("Indexado %s -> %d fragmentos (%s)", ruta.name, doc.n_fragmentos, doc.estado)
    return doc


def eliminar_documento(doc_id: str) -> bool:
    indice = obtener_indice()
    ok = indice.eliminar(doc_id)
    if ok:
        indice.guardar()
    return ok


def consultar(texto: str, escenario: str | None = None, k: int | None = None) -> dict:
    """Devuelve pasajes y una senal explicita de si el corpus respalda o no la consulta.

    `hay_evidencia` en False es la senal que usa el agente para declarar su
    limite en vez de improvisar: cuando ningun pasaje supera el umbral de
    similitud, el corpus cargado sencillamente no habla del tema.
    """
    indice = obtener_indice()
    pasajes = indice.buscar(
        consulta=texto,
        k_denso=RECUPERAR_DENSO,
        k_bm25=RECUPERAR_BM25,
        k_final=k or RECUPERAR_FINAL,
        escenario=escenario,
    )
    similitudes = sorted((p["similitud"] for p in pasajes), reverse=True)
    mejor = similitudes[0] if similitudes else 0.0
    # Un solo pasaje bueno puede ser casualidad lexica; que los tres mejores
    # concuerden es mas dificil de conseguir por azar. Se expone para poder
    # calibrar el umbral contra datos en vez de a ojo.
    top3 = round(sum(similitudes[:3]) / len(similitudes[:3]), 4) if similitudes else 0.0

    return {
        "pasajes": pasajes,
        "mejor_similitud": mejor,
        "similitud_top3": top3,
        "hay_evidencia": mejor >= UMBRAL_EVIDENCIA,
        "version_indice": indice.version,
    }
