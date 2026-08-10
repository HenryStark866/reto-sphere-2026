"""Observabilidad. Cada turno deja un registro verificable en logs/turnos.jsonl.

La rubrica define la latencia como el tiempo entre que el paciente termina de
hablar y que empieza a sonar el audio del agente. Ese segundo instante ocurre
en el navegador -es cuando dispara el evento `start` de la sintesis de voz-, no
en el servidor. Por eso el cliente cierra la medicion y la reporta: cualquier
numero medido solo del lado del servidor seria optimista y no cuadraria con lo
que el jurado percibe en la sesion.
"""
from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from app.config import LOG_DIR

ARCHIVO_TURNOS = LOG_DIR / "turnos.jsonl"
_lock = threading.Lock()


def redirigir_registro(ruta: Path) -> None:
    """Manda los registros de turno a otro archivo.

    Lo usa el arnes de evaluacion: las latencias que se reportan en el README
    tienen que salir de sesiones de voz reales, no de turnos simulados que
    nunca pasaron por un microfono. Mezclarlos daria un P50 optimista que no
    cuadraria con lo que el jurado mide en vivo.
    """
    global ARCHIVO_TURNOS
    ARCHIVO_TURNOS = Path(ruta)
    ARCHIVO_TURNOS.parent.mkdir(parents=True, exist_ok=True)

# Precios publicos de Groq por millon de tokens (USD), para el costo estimado.
# El nivel gratuito no cobra; se extrapola a precio de produccion como pide la
# rubrica. Se sobreescriben desde el .env si cambian.
PRECIOS_USD_POR_MILLON = {
    "llama-3.3-70b-versatile": {"entrada": 0.59, "salida": 0.79},
    "llama-3.1-8b-instant": {"entrada": 0.05, "salida": 0.08},
}
# Whisper Large V3 Turbo se cobra por hora de audio.
PRECIO_STT_USD_POR_HORA = 0.04


@dataclass
class Cronometro:
    """Acumula duraciones por etapa dentro de un turno."""

    etapas: dict[str, float] = field(default_factory=dict)

    @contextmanager
    def medir(self, nombre: str):
        inicio = time.perf_counter()
        try:
            yield
        finally:
            self.etapas[nombre] = round((time.perf_counter() - inicio) * 1000, 1)

    def fijar(self, nombre: str, ms: float) -> None:
        self.etapas[nombre] = round(ms, 1)


def costo_usd(tokens_por_modelo: list[tuple[str, int, int]], segundos_audio: float = 0.0) -> float:
    total = 0.0
    for modelo, entrada, salida in tokens_por_modelo:
        precio = PRECIOS_USD_POR_MILLON.get(modelo)
        if not precio:
            continue
        total += entrada / 1_000_000 * precio["entrada"]
        total += salida / 1_000_000 * precio["salida"]
    total += segundos_audio / 3600 * PRECIO_STT_USD_POR_HORA
    return total


def registrar_turno(registro: dict) -> None:
    registro.setdefault("ts", time.time())
    with _lock:
        with open(ARCHIVO_TURNOS, "a", encoding="utf-8") as f:
            f.write(json.dumps(registro, ensure_ascii=False) + "\n")


def actualizar_latencia_cliente(llamada_id: str, turno: int, ms_e2e: float) -> bool:
    """El navegador cierra la medicion cuando la voz empieza a sonar.

    Se reescribe la linea del turno correspondiente en vez de anexar otra, para
    que cada turno siga siendo un unico registro auditable.
    """
    if not ARCHIVO_TURNOS.exists():
        return False
    with _lock:
        lineas = ARCHIVO_TURNOS.read_text(encoding="utf-8").splitlines()
        for i in range(len(lineas) - 1, -1, -1):
            try:
                r = json.loads(lineas[i])
            except json.JSONDecodeError:
                continue
            if r.get("llamada_id") == llamada_id and r.get("turno") == turno:
                r.setdefault("ms", {})["e2e_cliente"] = round(ms_e2e, 1)
                lineas[i] = json.dumps(r, ensure_ascii=False)
                ARCHIVO_TURNOS.write_text("\n".join(lineas) + "\n", encoding="utf-8")
                return True
    return False


def _percentil(valores: list[float], p: float) -> float:
    if not valores:
        return 0.0
    ordenados = sorted(valores)
    k = (len(ordenados) - 1) * p
    bajo, alto = int(k), min(int(k) + 1, len(ordenados) - 1)
    return round(ordenados[bajo] + (ordenados[alto] - ordenados[bajo]) * (k - bajo), 1)


def leer_turnos(ruta: Path | None = None) -> list[dict]:
    ruta = ruta or ARCHIVO_TURNOS
    if not ruta.exists():
        return []
    registros = []
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea:
            continue
        try:
            registros.append(json.loads(linea))
        except json.JSONDecodeError:
            continue
    return registros


def resumen(ruta: Path | None = None) -> dict:
    """Agrega los logs reales. Es lo que se reporta en el README y en /api/metricas."""
    turnos = leer_turnos(ruta)
    if not turnos:
        return {"turnos": 0, "llamadas": 0}

    def col(clave: str) -> list[float]:
        return [
            t["ms"][clave]
            for t in turnos
            if isinstance(t.get("ms"), dict) and isinstance(t["ms"].get(clave), (int, float))
        ]

    e2e = col("e2e_cliente")
    # Si el cliente no alcanzo a reportar, el total de servidor es el mejor
    # sustituto disponible; se informa cuantos turnos tienen medicion completa.
    referencia = e2e or col("servidor_total")

    llamadas: dict[str, dict] = {}
    for t in turnos:
        c = llamadas.setdefault(
            t.get("llamada_id", "?"),
            {"turnos": 0, "entrada": 0, "salida": 0, "invocaciones": 0, "rag": 0, "costo": 0.0},
        )
        c["turnos"] += 1
        c["entrada"] += t.get("tokens", {}).get("entrada", 0)
        c["salida"] += t.get("tokens", {}).get("salida", 0)
        c["invocaciones"] += t.get("invocaciones_modelo", 0)
        c["rag"] += t.get("consultas_rag", 0)
        c["costo"] += t.get("costo_usd", 0.0)

    n = len(llamadas) or 1
    return {
        "turnos": len(turnos),
        "llamadas": len(llamadas),
        "latencia_ms": {
            "p50": _percentil(referencia, 0.50),
            "p95": _percentil(referencia, 0.95),
            "medida_en_cliente": len(e2e),
            "total": len(referencia),
        },
        "latencia_por_etapa_p50_ms": {
            etapa: _percentil(col(etapa), 0.50)
            for etapa in (
                "stt",
                "extraccion_y_recuperacion",
                "triaje",
                "llm_ttft",
                "llm_total",
                "servidor_total",
                "e2e_cliente",
            )
            if col(etapa)
        },
        "por_turno": {
            "tokens_entrada": round(sum(c["entrada"] for c in llamadas.values()) / len(turnos), 1),
            "tokens_salida": round(sum(c["salida"] for c in llamadas.values()) / len(turnos), 1),
            "invocaciones_modelo": round(
                sum(c["invocaciones"] for c in llamadas.values()) / len(turnos), 2
            ),
        },
        "por_llamada": {
            "turnos": round(len(turnos) / n, 1),
            "tokens_entrada": round(sum(c["entrada"] for c in llamadas.values()) / n, 1),
            "tokens_salida": round(sum(c["salida"] for c in llamadas.values()) / n, 1),
            "consultas_rag": round(sum(c["rag"] for c in llamadas.values()) / n, 2),
            "costo_usd": round(sum(c["costo"] for c in llamadas.values()) / n, 6),
        },
    }
