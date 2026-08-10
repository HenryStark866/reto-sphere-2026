"""Embeddings locales via ONNX. Sin PyTorch: la instalacion pesa ~200 MB en vez
de ~2.5 GB, lo que mantiene el levantamiento dentro de la compuerta de 15 minutos.
"""
from __future__ import annotations

import logging
import threading

import numpy as np

from app.config import (
    MODELO_EMBEDDING,
    MODELOS_DIR,
    PREFIJO_CONSULTA,
    PREFIJO_PASAJE,
    USA_PREFIJOS_E5,
)

log = logging.getLogger(__name__)

_modelo = None
_lock = threading.Lock()


def _cargar():
    global _modelo
    if _modelo is None:
        with _lock:
            if _modelo is None:
                from fastembed import TextEmbedding

                log.info("Cargando modelo de embeddings %s", MODELO_EMBEDDING)
                _modelo = TextEmbedding(
                    model_name=MODELO_EMBEDDING, cache_dir=str(MODELOS_DIR)
                )
    return _modelo


def dimension() -> int:
    return len(embeber_pasajes(["dimension"])[0])


def _normalizar(m: np.ndarray) -> np.ndarray:
    """Normaliza a norma 1 para que el producto punto sea el coseno."""
    normas = np.linalg.norm(m, axis=1, keepdims=True)
    normas[normas == 0] = 1.0
    return (m / normas).astype(np.float32)


def _embeber(textos: list[str], prefijo: str) -> np.ndarray:
    if not textos:
        return np.zeros((0, 0), dtype=np.float32)
    modelo = _cargar()
    if USA_PREFIJOS_E5:
        textos = [prefijo + t for t in textos]
    vectores = np.array(list(modelo.embed(textos)), dtype=np.float32)
    return _normalizar(vectores)


def embeber_pasajes(textos: list[str]) -> np.ndarray:
    return _embeber(textos, PREFIJO_PASAJE)


def embeber_consulta(texto: str) -> np.ndarray:
    return _embeber([texto], PREFIJO_CONSULTA)[0]


def precalentar() -> None:
    """Se invoca al arrancar para que el primer turno de voz no pague la carga."""
    _cargar()
