"""Extraccion de texto por pagina. Soporta PDF, TXT y Markdown.

Un PDF de dataset/textos/Appendicitis viene escaneado sin capa de texto. Cuando
una pagina no entrega texto se intenta OCR, pero el OCR es una dependencia
opcional: si no esta instalada el documento se marca como `sin_texto` en vez de
entrar silenciosamente vacio al indice.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF

log = logging.getLogger(__name__)

EXTENSIONES = {".pdf", ".txt", ".md", ".markdown"}

# Una pagina con menos caracteres utiles que esto se considera sin capa de texto.
MINIMO_CARACTERES_PAGINA = 60

# Windows no abre rutas de mas de 260 caracteres salvo que se pidan con el
# prefijo \\?\, y el corpus trae tres articulos cuyo titulo completo es el nombre
# del archivo. Sin esto `Path.is_file()` devuelve False en silencio y el
# documento desaparece del indice sin un solo error en el log.
LIMITE_RUTA_WINDOWS = 250
_PREFIJO_LARGO = "\\\\?\\"


def ruta_larga(ruta: Path) -> Path:
    """Devuelve una ruta que el sistema pueda abrir, sea cual sea su longitud."""
    if os.name != "nt":
        return ruta
    completa = os.path.normpath(os.path.abspath(ruta))
    if len(completa) < LIMITE_RUTA_WINDOWS or completa.startswith(_PREFIJO_LARGO):
        return ruta
    return Path(_PREFIJO_LARGO + completa)


@dataclass
class Pagina:
    numero: int
    texto: str
    via_ocr: bool = False


@dataclass
class Extraccion:
    paginas: list[Pagina] = field(default_factory=list)
    paginas_sin_texto: int = 0
    paginas_ocr: int = 0

    @property
    def vacia(self) -> bool:
        return not any(p.texto.strip() for p in self.paginas)


def limpiar(texto: str) -> str:
    """Normaliza el ruido tipico de un PDF academico."""
    # Une palabras cortadas por guion al final de linea.
    texto = re.sub(r"-\n(?=[a-záéíóúñ])", "", texto)
    # Los saltos simples dentro de un parrafo son artefactos de maquetacion.
    texto = re.sub(r"(?<![\n.:;!?])\n(?![\n•\-\d])", " ", texto)
    texto = re.sub(r"[ \t]+", " ", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()


def _ocr_pagina(pagina_fitz) -> str | None:
    """OCR opcional vía rapidocr-onnxruntime. Devuelve None si no esta disponible."""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return None

    global _OCR
    try:
        _OCR
    except NameError:
        _OCR = RapidOCR()

    try:
        pix = pagina_fitz.get_pixmap(dpi=200)
        import numpy as np

        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if pix.n == 4:
            img = img[:, :, :3]
        resultado, _ = _OCR(img)
        if not resultado:
            return ""
        return "\n".join(linea[1] for linea in resultado)
    except Exception as exc:  # pragma: no cover - el OCR es best effort
        log.warning("OCR fallo: %s", exc)
        return None


def extraer(ruta: Path, intentar_ocr: bool = True) -> Extraccion:
    ruta = ruta_larga(Path(ruta))
    ext = ruta.suffix.lower()
    if ext not in EXTENSIONES:
        raise ValueError(f"Extension no soportada: {ext}")

    if ext != ".pdf":
        texto = limpiar(ruta.read_text(encoding="utf-8", errors="replace"))
        return Extraccion(paginas=[Pagina(numero=1, texto=texto)])

    salida = Extraccion()
    with fitz.open(ruta) as doc:
        for i, pagina in enumerate(doc, start=1):
            texto = limpiar(pagina.get_text("text") or "")
            via_ocr = False
            if len(texto) < MINIMO_CARACTERES_PAGINA:
                reconocido = _ocr_pagina(pagina) if intentar_ocr else None
                if reconocido:
                    texto = limpiar(reconocido)
                    via_ocr = True
                    salida.paginas_ocr += 1
                else:
                    salida.paginas_sin_texto += 1
            if texto.strip():
                salida.paginas.append(Pagina(numero=i, texto=texto, via_ocr=via_ocr))
    return salida
