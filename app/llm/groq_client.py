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
    LLM_REINTENTOS,
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
REINTENTOS = LLM_REINTENTOS
_TRANSITORIOS = ("rate_limit", "429", "500", "502", "503", "504", "timeout", "connection")

# Groq dice cuanto falta para que se libere la cuota, en la cabecera
# `retry-after` y tambien en el texto del error ("Please try again in 10.07s").
_ESPERA_EN_MENSAJE = re.compile(r"try again in ([\d.]+)\s*s", re.I)
# Techo de cortesia: una espera mas larga que esto es un proveedor caido, no un
# limite de tasa, y conviene fallar y dejarlo en el log.
ESPERA_MAXIMA_S = 65.0


def _espera_sugerida(exc: Exception) -> float | None:
    """Cuanto pide esperar el proveedor, si lo dice.

    El nivel gratuito de Groq limita por tokens por minuto, asi que la ventana
    que hay que esperar puede ser de decenas de segundos: mucho mas que el
    2**intento de una espera exponencial ciega. Obedecer el numero que manda la
    API convierte un 429 en una pausa y no en un turno perdido.
    """
    respuesta = getattr(exc, "response", None)
    cabeceras = getattr(respuesta, "headers", None)
    if cabeceras:
        for clave in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
            crudo = cabeceras.get(clave)
            if not crudo:
                continue
            try:
                return float(str(crudo).rstrip("s"))
            except ValueError:
                continue
    m = _ESPERA_EN_MENSAJE.search(str(exc))
    return float(m.group(1)) if m else None


def _con_reintentos(operacion: Callable[[], Any], descripcion: str) -> Any:
    """Reintenta respetando la espera que pide el proveedor.

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
            sugerida = _espera_sugerida(exc)
            if sugerida is not None and sugerida > ESPERA_MAXIMA_S:
                raise
            # El jitter evita que varios hilos del arnes vuelvan a la vez y se
            # atropellen sobre la misma ventana recien liberada.
            espera = (sugerida + 0.5 if sugerida is not None else 2**intento) + random.random()
            log.warning(
                "%s fallo (%s). Reintento %d/%d en %.1fs%s",
                descripcion,
                type(exc).__name__,
                intento + 1,
                REINTENTOS - 1,
                espera,
                " (espera pedida por el proveedor)" if sugerida is not None else "",
            )
            time.sleep(espera)


@dataclass
class Uso:
    """Contabilidad de consumo. Alimenta las metricas obligatorias del README."""

    tokens_entrada: int = 0
    tokens_salida: int = 0
    invocaciones: int = 0
    modelos: list[str] = field(default_factory=list)
    # Los tokens tambien se guardan por modelo. Un turno mezcla el 8B (extraccion
    # y segunda opinion) con el 70B (la respuesta hablada), y entre los dos hay
    # un factor de doce en el precio: cobrar el total a la tarifa del 70B da un
    # costo por llamada que no se sostiene si el jurado rehace la cuenta.
    por_modelo: dict[str, dict[str, int]] = field(default_factory=dict)

    def sumar(self, entrada: int, salida: int, modelo: str) -> None:
        self.tokens_entrada += entrada
        self.tokens_salida += salida
        self.invocaciones += 1
        self.modelos.append(modelo)
        acumulado = self.por_modelo.setdefault(
            modelo, {"entrada": 0, "salida": 0, "invocaciones": 0}
        )
        acumulado["entrada"] += entrada
        acumulado["salida"] += salida
        acumulado["invocaciones"] += 1

    def desglose(self) -> list[tuple[str, int, int]]:
        """(modelo, tokens de entrada, tokens de salida) para tarificar cada uno."""
        return [(m, v["entrada"], v["salida"]) for m, v in self.por_modelo.items()]

    def a_dict(self) -> dict:
        return {
            "tokens_entrada": self.tokens_entrada,
            "tokens_salida": self.tokens_salida,
            "invocaciones_modelo": self.invocaciones,
            "modelos": self.modelos,
            "por_modelo": self.por_modelo,
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
