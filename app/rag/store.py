"""Indice hibrido con alta y baja en caliente.

Se implementa a mano sobre numpy + BM25 en vez de usar una base vectorial
empaquetada por una razon concreta: la compuerta G5 exige que eliminar un
documento lo borre de verdad. Aqui el borrado reconstruye las estructuras sin
las filas del documento, asi que no queda residuo posible en un indice
aproximado ni en una cache interna.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from rank_bm25 import BM25Okapi

from app.config import INDEX_DIR
from app.rag import embedder

log = logging.getLogger(__name__)

VACIAS = {
    "de", "la", "el", "en", "y", "a", "los", "las", "del", "un", "una", "por",
    "con", "para", "que", "se", "es", "al", "lo", "su", "sus", "o", "como",
    "the", "of", "and", "in", "to", "for", "is", "on", "with", "a", "an",
}


def _sin_acentos(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")


def tokenizar(texto: str) -> list[str]:
    texto = _sin_acentos(texto.lower())
    return [t for t in re.findall(r"[a-z0-9]+", texto) if len(t) > 2 and t not in VACIAS]


@dataclass
class Documento:
    doc_id: str
    nombre: str
    escenario: str
    sha256: str
    n_fragmentos: int
    origen: str  # "corpus" | "consola"
    estado: str  # "procesado" | "sin_texto"
    paginas: int = 0
    paginas_ocr: int = 0
    subido_ts: float = field(default_factory=time.time)


@dataclass
class Fragmento:
    chunk_id: str
    doc_id: str
    nombre: str
    escenario: str
    pagina: int
    texto: str


class IndiceVivo:
    def __init__(self, directorio: Path | None = None):
        self.dir = Path(directorio or INDEX_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.documentos: dict[str, Documento] = {}
        self.fragmentos: list[Fragmento] = []
        self.vectores: np.ndarray = np.zeros((0, 0), dtype=np.float32)
        self._bm25: BM25Okapi | None = None
        self._tokens: list[list[str]] = []
        # Cambia con cada mutacion: la consola lo muestra como prueba de que el
        # indice que responde es el mismo que se acaba de modificar.
        self.version: int = 0

    # ------------------------------------------------------------------ estado
    @property
    def n_fragmentos(self) -> int:
        return len(self.fragmentos)

    def resumen(self) -> dict:
        with self._lock:
            return {
                "version": self.version,
                "documentos": len(self.documentos),
                "fragmentos": len(self.fragmentos),
                "dimension": int(self.vectores.shape[1]) if self.vectores.size else 0,
                "modelo_embedding": embedder.MODELO_EMBEDDING,
            }

    def listar(self) -> list[dict]:
        with self._lock:
            docs = sorted(self.documentos.values(), key=lambda d: -d.subido_ts)
            return [asdict(d) for d in docs]

    # ------------------------------------------------------------------ escritura
    def agregar(
        self,
        nombre: str,
        escenario: str,
        fragmentos_texto: list[tuple[str, int]],
        origen: str,
        sha256: str,
        paginas: int = 0,
        paginas_ocr: int = 0,
        doc_id: str | None = None,
    ) -> Documento:
        """Indexa un documento. `fragmentos_texto` son pares (texto, pagina)."""
        doc_id = doc_id or hashlib.sha1(f"{nombre}:{sha256}".encode()).hexdigest()[:16]

        with self._lock:
            if doc_id in self.documentos:
                self.eliminar(doc_id)

            estado = "procesado" if fragmentos_texto else "sin_texto"
            doc = Documento(
                doc_id=doc_id,
                nombre=nombre,
                escenario=escenario,
                sha256=sha256,
                n_fragmentos=len(fragmentos_texto),
                origen=origen,
                estado=estado,
                paginas=paginas,
                paginas_ocr=paginas_ocr,
            )

            if fragmentos_texto:
                nuevos = [
                    Fragmento(
                        chunk_id=f"{doc_id}:{i}",
                        doc_id=doc_id,
                        nombre=nombre,
                        escenario=escenario,
                        pagina=pagina,
                        texto=texto,
                    )
                    for i, (texto, pagina) in enumerate(fragmentos_texto)
                ]
                vectores = embedder.embeber_pasajes([f.texto for f in nuevos])
                self.fragmentos.extend(nuevos)
                self.vectores = (
                    vectores if self.vectores.size == 0 else np.vstack([self.vectores, vectores])
                )
                self._tokens.extend(tokenizar(f.texto) for f in nuevos)
                self._reconstruir_bm25()

            self.documentos[doc_id] = doc
            self.version += 1
            return doc

    def eliminar(self, doc_id: str) -> bool:
        """Borra el documento y sus fragmentos. El agente deja de poder citarlo."""
        with self._lock:
            if doc_id not in self.documentos:
                return False

            conservar = [i for i, f in enumerate(self.fragmentos) if f.doc_id != doc_id]
            self.fragmentos = [self.fragmentos[i] for i in conservar]
            self._tokens = [self._tokens[i] for i in conservar]
            if self.vectores.size:
                self.vectores = (
                    self.vectores[conservar]
                    if conservar
                    else np.zeros((0, self.vectores.shape[1]), dtype=np.float32)
                )
            del self.documentos[doc_id]
            self._reconstruir_bm25()
            self.version += 1
            return True

    def _reconstruir_bm25(self) -> None:
        self._bm25 = BM25Okapi(self._tokens) if self._tokens else None

    # ------------------------------------------------------------------ lectura
    def buscar(
        self, consulta: str, k_denso: int, k_bm25: int, k_final: int, escenario: str | None = None
    ) -> list[dict]:
        """Busqueda hibrida: coseno + BM25 fusionados por Reciprocal Rank Fusion."""
        with self._lock:
            if not self.fragmentos:
                return []

            vector = embedder.embeber_consulta(consulta)
            similitudes = self.vectores @ vector
            n = len(self.fragmentos)

            top_denso = np.argsort(-similitudes)[: min(k_denso, n)]
            rank_denso = {int(idx): r for r, idx in enumerate(top_denso)}

            rank_lexico: dict[int, int] = {}
            if self._bm25 is not None:
                tokens = tokenizar(consulta)
                if tokens:
                    puntajes = np.asarray(self._bm25.get_scores(tokens))
                    top_lexico = np.argsort(-puntajes)[: min(k_bm25, n)]
                    rank_lexico = {int(idx): r for r, idx in enumerate(top_lexico) if puntajes[idx] > 0}

            # RRF: robusto ante escalas de puntaje incomparables entre ambos metodos.
            K = 60
            fusion: dict[int, float] = {}
            for idx, r in rank_denso.items():
                fusion[idx] = fusion.get(idx, 0.0) + 1.0 / (K + r)
            for idx, r in rank_lexico.items():
                fusion[idx] = fusion.get(idx, 0.0) + 1.0 / (K + r)

            # El escenario del procedimiento del paciente sube, no filtra: el
            # corpus tiene guias transversales que aplican a cualquier cirugia.
            if escenario:
                for idx in list(fusion):
                    if self.fragmentos[idx].escenario == escenario:
                        fusion[idx] *= 1.25

            ordenados = sorted(fusion.items(), key=lambda kv: -kv[1])[:k_final]
            return [
                {
                    "chunk_id": self.fragmentos[i].chunk_id,
                    "doc_id": self.fragmentos[i].doc_id,
                    "documento": self.fragmentos[i].nombre,
                    "escenario": self.fragmentos[i].escenario,
                    "pagina": self.fragmentos[i].pagina,
                    "texto": self.fragmentos[i].texto,
                    "similitud": round(float(similitudes[i]), 4),
                    "puntaje_fusion": round(float(score), 5),
                }
                for i, score in ordenados
            ]

    # ------------------------------------------------------------------ persistencia
    def guardar(self) -> None:
        with self._lock:
            (self.dir / "documentos.json").write_text(
                json.dumps({k: asdict(v) for k, v in self.documentos.items()}, ensure_ascii=False),
                encoding="utf-8",
            )
            (self.dir / "fragmentos.json").write_text(
                json.dumps([asdict(f) for f in self.fragmentos], ensure_ascii=False),
                encoding="utf-8",
            )
            np.save(self.dir / "vectores.npy", self.vectores)
            (self.dir / "meta.json").write_text(
                json.dumps(
                    {"version": self.version, "modelo_embedding": embedder.MODELO_EMBEDDING},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

    def cargar(self) -> bool:
        docs = self.dir / "documentos.json"
        frags = self.dir / "fragmentos.json"
        vecs = self.dir / "vectores.npy"
        if not (docs.exists() and frags.exists() and vecs.exists()):
            return False

        with self._lock:
            self.documentos = {
                k: Documento(**v) for k, v in json.loads(docs.read_text(encoding="utf-8")).items()
            }
            self.fragmentos = [
                Fragmento(**f) for f in json.loads(frags.read_text(encoding="utf-8"))
            ]
            self.vectores = np.load(vecs)
            self._tokens = [tokenizar(f.texto) for f in self.fragmentos]
            self._reconstruir_bm25()
            meta = self.dir / "meta.json"
            if meta.exists():
                self.version = json.loads(meta.read_text(encoding="utf-8")).get("version", 0)
            log.info(
                "Indice cargado: %d documentos, %d fragmentos",
                len(self.documentos),
                len(self.fragmentos),
            )
            return True


_indice: IndiceVivo | None = None
_lock_singleton = threading.Lock()


def obtener_indice() -> IndiceVivo:
    """Devuelve el indice, cargandolo la primera vez.

    El indice se publica en la global SOLO cuando termino de cargar. Asignarlo
    antes abre una ventana en la que otro hilo lo recibe vacio y concluye que el
    corpus no sabe nada: una consulta concurrente durante el arranque
    respondia "no tengo esa informacion" con los 6091 fragmentos ya en disco.
    """
    global _indice
    if _indice is None:
        with _lock_singleton:
            if _indice is None:
                indice = IndiceVivo()
                indice.cargar()
                _indice = indice
    return _indice
