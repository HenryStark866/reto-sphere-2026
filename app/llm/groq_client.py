"""Cliente de Groq: dialogo, extraccion estructurada y transcripcion.

Groq sirve la familia Meta Llama, que es una de las permitidas por la compuerta
G3. Se eligio por latencia: en una llamada de voz el tiempo hasta el primer
token es lo que el paciente percibe como silencio incomodo.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from groq import Groq

from app.config import (
    GROQ_API_KEY,
    MAX_TOKENS_DIALOGO,
    MODEL_DIALOGO,
    MODEL_EXTRACCION,
    MODEL_STT,
    TEMPERATURA_DIALOGO,
    TEMPERATURA_EXTRACCION,
    modelo_permitido,
)

log = logging.getLogger(__name__)

_cliente: Groq | None = None


class SinCredenciales(RuntimeError):
    pass


def cliente() -> Groq:
    global _cliente
    if not GROQ_API_KEY:
        raise SinCredenciales(
            "Falta GROQ_API_KEY. Copia .env.example a .env y pon tu llave de "
            "console.groq.com (gratuita)."
        )
    if _cliente is None:
        _cliente = Groq(api_key=GROQ_API_KEY)
    return _cliente


# Errores que vale la pena reintentar: limite de tasa y fallos transitorios del
# proveedor. Un 400 por un prompt mal formado no se reintenta nunca.
REINTENTOS = 3
_TRANSITORIOS = ("rate_limit", "429", "500", "502", "503", "504", "timeout", "connection")


def _con_reintentos(operacion: Callable[[], Any], descripcion: str) -> Any:
    """Reintenta con espera exponencial.

    Existe por la sesion de evaluacion: un limite de tasa alcanzado a mitad de
    la demo se veria como un agente que se queda mudo. Tambien hace que el
    arnes de evaluacion mida el sistema y no la cuota de la cuenta.
    """
    for intento in range(REINTENTOS):
        try:
            return operacion()
        except Exception as exc:
            texto = f"{type(exc).__name__} {exc}".lower()
            transitorio = any(t in texto for t in _TRANSITORIOS)
            if not transitorio or intento == REINTENTOS - 1:
                raise
            espera = 2**intento + random.random()
            log.warning(
                "%s fallo (%s). Reintento %d/%d en %.1fs",
                descripcion,
                type(exc).__name__,
                intento + 1,
                REINTENTOS - 1,
                espera,
            )
            time.sleep(espera)


@dataclass
class Uso:
    """Contabilidad de consumo. Alimenta las metricas obligatorias del README."""

    tokens_entrada: int = 0
    tokens_salida: int = 0
    invocaciones: int = 0
    modelos: list[str] = field(default_factory=list)

    def sumar(self, entrada: int, salida: int, modelo: str) -> None:
        self.tokens_entrada += entrada
        self.tokens_salida += salida
        self.invocaciones += 1
        self.modelos.append(modelo)

    def a_dict(self) -> dict:
        return {
            "tokens_entrada": self.tokens_entrada,
            "tokens_salida": self.tokens_salida,
            "invocaciones_modelo": self.invocaciones,
            "modelos": self.modelos,
        }


def verificar_modelos() -> dict:
    """Compuerta G3 explicita: se expone en /api/salud y se registra al arrancar."""
    resultado = {}
    for etiqueta, nombre in (
        ("dialogo", MODEL_DIALOGO),
        ("extraccion", MODEL_EXTRACCION),
    ):
        ok = modelo_permitido(nombre)
        resultado[etiqueta] = {"modelo": nombre, "familia_permitida": ok}
        if not ok:
            log.error(
                "MODELO FUERA DE LA LISTA PERMITIDA: %s (%s). La compuerta G3 "
                "descalifica la entrega.",
                nombre,
                etiqueta,
            )
    resultado["stt"] = {"modelo": MODEL_STT, "familia_permitida": True}
    return resultado


# ---------------------------------------------------------------------------
# Texto
# ---------------------------------------------------------------------------
def completar(
    mensajes: list[dict],
    modelo: str | None = None,
    temperatura: float | None = None,
    max_tokens: int | None = None,
    json_estricto: bool = False,
    uso: Uso | None = None,
) -> str:
    modelo = modelo or MODEL_DIALOGO
    kwargs: dict[str, Any] = {
        "model": modelo,
        "messages": mensajes,
        "temperature": TEMPERATURA_DIALOGO if temperatura is None else temperatura,
        "max_tokens": max_tokens or MAX_TOKENS_DIALOGO,
    }
    if json_estricto:
        kwargs["response_format"] = {"type": "json_object"}

    respuesta = _con_reintentos(
        lambda: cliente().chat.completions.create(**kwargs), f"completar({modelo})"
    )
    if uso is not None and respuesta.usage:
        uso.sumar(respuesta.usage.prompt_tokens, respuesta.usage.completion_tokens, modelo)
    return (respuesta.choices[0].message.content or "").strip()


def completar_stream(
    mensajes: list[dict],
    modelo: str | None = None,
    temperatura: float | None = None,
    max_tokens: int | None = None,
    uso: Uso | None = None,
) -> Iterator[str]:
    """Emite el texto por trozos para que la voz arranque antes de terminar."""
    modelo = modelo or MODEL_DIALOGO
    # Solo se reintenta la apertura del flujo. Reintentar a mitad de stream
    # duplicaria texto ya emitido y el paciente oiria la frase dos veces.
    flujo = _con_reintentos(
        lambda: cliente().chat.completions.create(
            model=modelo,
            messages=mensajes,
            temperature=TEMPERATURA_DIALOGO if temperatura is None else temperatura,
            max_tokens=max_tokens or MAX_TOKENS_DIALOGO,
            stream=True,
        ),
        f"stream({modelo})",
    )
    entrada = salida = 0
    for trozo in flujo:
        if trozo.choices and trozo.choices[0].delta.content:
            yield trozo.choices[0].delta.content
        datos = getattr(trozo, "x_groq", None)
        if datos is not None and getattr(datos, "usage", None):
            entrada = datos.usage.prompt_tokens
            salida = datos.usage.completion_tokens
    if uso is not None:
        uso.sumar(entrada, salida, modelo)


def _extraer_json(texto: str) -> dict:
    texto = texto.strip()
    if texto.startswith("```"):
        texto = re.sub(r"^```[a-z]*\n?", "", texto)
        texto = re.sub(r"```$", "", texto).strip()
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        # El modelo a veces envuelve el objeto en prosa; se rescata el bloque.
        m = re.search(r"\{.*\}", texto, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    log.warning("No se pudo interpretar como JSON: %.200s", texto)
    return {}


def completar_json(
    sistema: str,
    usuario: str,
    modelo: str | None = None,
    uso: Uso | None = None,
    max_tokens: int = 700,
) -> dict:
    texto = completar(
        mensajes=[
            {"role": "system", "content": sistema},
            {"role": "user", "content": usuario},
        ],
        modelo=modelo or MODEL_EXTRACCION,
        temperatura=TEMPERATURA_EXTRACCION,
        max_tokens=max_tokens,
        json_estricto=True,
        uso=uso,
    )
    return _extraer_json(texto)


# ---------------------------------------------------------------------------
# Voz a texto
# ---------------------------------------------------------------------------
def transcribir(audio: bytes, nombre_archivo: str = "turno.webm") -> tuple[str, float]:
    """Devuelve (texto, milisegundos). Whisper Large V3 en la misma cuenta de Groq."""
    inicio = time.perf_counter()
    respuesta = _con_reintentos(
        lambda: cliente().audio.transcriptions.create(
            file=(nombre_archivo, audio),
            model=MODEL_STT,
            language="es",
            response_format="json",
            # Orienta la transcripcion hacia el dominio de la llamada.
            prompt=(
                "Llamada de seguimiento postoperatorio en Colombia. El paciente "
                "describe dolor, fiebre, la herida quirurgica, movilidad, apetito y sueno."
            ),
        ),
        "transcribir",
    )
    ms = (time.perf_counter() - inicio) * 1000
    return (respuesta.text or "").strip(), ms
