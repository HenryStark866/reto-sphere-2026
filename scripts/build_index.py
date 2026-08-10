"""Construye el indice del corpus clinico a partir de dataset/textos/.

Se ejecuta una sola vez y deja el indice en index/. El repositorio versiona ese
indice ya construido para que el jurado no tenga que reprocesar 107 PDFs dentro
de los 15 minutos que mide la compuerta G2.

Uso:
    python -m scripts.build_index            # incremental, salta lo ya indexado
    python -m scripts.build_index --limpiar  # reconstruye desde cero
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import CORPUS_DIR  # noqa: E402
from app.rag import extract  # noqa: E402
from app.rag.service import indexar_archivo, sha256_archivo  # noqa: E402
from app.rag.store import obtener_indice  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("build_index")


def normalizar_escenario(nombre: str) -> str:
    """Dos carpetas del corpus traen espacios en el nombre; se unifican aqui."""
    n = unicodedata.normalize("NFD", nombre.lower())
    n = "".join(c for c in n if unicodedata.category(c) != "Mn")
    return n.replace(" ", "_").replace("-", "_")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limpiar", action="store_true", help="reconstruye el indice desde cero")
    ap.add_argument("--sin-ocr", action="store_true", help="omite el OCR de PDFs escaneados")
    args = ap.parse_args()

    indice = obtener_indice()
    if args.limpiar:
        for doc_id in list(indice.documentos):
            indice.eliminar(doc_id)
        log.info("Indice vaciado")
    else:
        indice.cargar()

    # sha256 -> nombre ya indexado. El corpus trae documentos repetidos entre
    # carpetas; indexarlos dos veces inflaria el recall aparente y haria que el
    # agente cite la copia en vez del original.
    vistos = {d.sha256: d.nombre for d in indice.documentos.values()}

    # El filtro es solo por extension: un archivo que exista pero no se pueda
    # abrir tiene que llegar al bucle y fallar con un error visible. Filtrarlo
    # aqui por `is_file()` lo haria desaparecer del conteo -- asi se perdieron
    # tres articulos del corpus, cuyas rutas superan el limite de Windows.
    archivos = sorted(
        p for p in CORPUS_DIR.rglob("*") if p.suffix.lower() in extract.EXTENSIONES
    )
    log.info("Encontrados %d archivos en %s", len(archivos), CORPUS_DIR)

    inicio = time.time()
    indexados = duplicados = sin_texto = ilegibles = 0

    for i, ruta in enumerate(archivos, start=1):
        escenario = normalizar_escenario(ruta.parent.name)
        ruta = extract.ruta_larga(ruta)
        if not ruta.is_file():
            ilegibles += 1
            log.error("[%d/%d] ILEGIBLE, no se indexa: %s", i, len(archivos), ruta.name)
            continue
        try:
            sha = sha256_archivo(ruta)
        except OSError as exc:
            ilegibles += 1
            log.error("[%d/%d] no se pudo leer %s: %s", i, len(archivos), ruta.name, exc)
            continue

        if sha in vistos:
            duplicados += 1
            log.info("[%d/%d] duplicado, se omite: %s", i, len(archivos), ruta.name)
            continue

        try:
            doc = indexar_archivo(
                ruta, escenario=escenario, origen="corpus", intentar_ocr=not args.sin_ocr
            )
        except Exception as exc:
            ilegibles += 1
            log.error("[%d/%d] fallo %s: %s", i, len(archivos), ruta.name, exc)
            continue

        vistos[sha] = ruta.name
        indexados += 1
        if doc.estado == "sin_texto":
            sin_texto += 1
            log.warning("[%d/%d] SIN CAPA DE TEXTO: %s", i, len(archivos), ruta.name)
        else:
            log.info(
                "[%d/%d] %s -> %d fragmentos%s",
                i,
                len(archivos),
                ruta.name,
                doc.n_fragmentos,
                f" ({doc.paginas_ocr} pags por OCR)" if doc.paginas_ocr else "",
            )

    indice.guardar()
    resumen = indice.resumen()
    log.info(
        "Listo en %.1fs | indexados=%d duplicados=%d sin_texto=%d ilegibles=%d "
        "| %d documentos, %d fragmentos, dim=%d",
        time.time() - inicio,
        indexados,
        duplicados,
        sin_texto,
        ilegibles,
        resumen["documentos"],
        resumen["fragmentos"],
        resumen["dimension"],
    )

    # Cuadrar la cuenta en voz alta: si los archivos encontrados no acaban todos
    # en una de las categorias anteriores, el corpus indexado no es el del disco
    # y cualquier metrica de recuperacion que se reporte estaria medida de menos.
    if indexados + duplicados + ilegibles != len(archivos):
        log.error(
            "DESCUADRE: %d archivos encontrados, %d contabilizados",
            len(archivos),
            indexados + duplicados + ilegibles,
        )
        return 1
    if ilegibles:
        log.error("%d archivos del corpus quedaron FUERA del indice", ilegibles)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
