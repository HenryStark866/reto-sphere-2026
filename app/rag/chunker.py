"""Troceado del texto conservando la pagina de origen.

La pagina viaja con cada fragmento porque la trazabilidad se evalua contra la
fuente real: una cita que no se puede abrir y verificar no vale.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.config import CHUNK_CARACTERES, CHUNK_SOLAPE
from app.rag.extract import Extraccion


@dataclass
class Fragmento:
    texto: str
    pagina: int
    orden: int


def _partir_parrafo(parrafo: str, limite: int) -> list[str]:
    """Un parrafo mas largo que el limite se corta por frase, no a ciegas."""
    if len(parrafo) <= limite:
        return [parrafo]
    frases = re.split(r"(?<=[.:;!?])\s+", parrafo)
    piezas: list[str] = []
    actual = ""
    for frase in frases:
        if len(frase) > limite:
            # Frase monstruosa (tablas, listas sin puntuacion): corte duro.
            if actual:
                piezas.append(actual)
                actual = ""
            for i in range(0, len(frase), limite):
                piezas.append(frase[i : i + limite])
            continue
        if len(actual) + len(frase) + 1 > limite:
            piezas.append(actual)
            actual = frase
        else:
            actual = f"{actual} {frase}".strip()
    if actual:
        piezas.append(actual)
    return piezas


def trocear(extraccion: Extraccion, limite: int | None = None, solape: int | None = None) -> list[Fragmento]:
    limite = limite or CHUNK_CARACTERES
    solape = solape or CHUNK_SOLAPE

    fragmentos: list[Fragmento] = []
    buffer = ""
    pagina_buffer = 1

    def volcar():
        nonlocal buffer
        if buffer.strip():
            fragmentos.append(
                Fragmento(texto=buffer.strip(), pagina=pagina_buffer, orden=len(fragmentos))
            )
        buffer = ""

    for pagina in extraccion.paginas:
        for parrafo in re.split(r"\n\s*\n", pagina.texto):
            parrafo = parrafo.strip()
            if not parrafo:
                continue
            for pieza in _partir_parrafo(parrafo, limite):
                if not buffer:
                    pagina_buffer = pagina.numero
                if len(buffer) + len(pieza) + 1 > limite:
                    cola = buffer[-solape:] if solape else ""
                    volcar()
                    # El solape arranca el fragmento siguiente en la pagina actual.
                    pagina_buffer = pagina.numero
                    buffer = f"{cola} {pieza}".strip() if cola else pieza
                else:
                    buffer = f"{buffer}\n{pieza}".strip() if buffer else pieza
    volcar()
    return fragmentos
