"""Orquestacion de una llamada de seguimiento.

Un turno recorre: transcripcion -> extraccion estructurada y recuperacion (en
paralelo) -> triaje determinista -> segunda opinion del modelo -> respuesta
hablada. La extraccion y la recuperacion se lanzan a la vez porque ninguna
depende de la otra: ambas parten del texto crudo del paciente, y solaparlas
ahorra un tramo de silencio que en voz se nota.
"""
from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterator

from app.agent import prompts, triage
from app.agent.schema import EstadoSintomas, ResultadoTriaje
from app.config import MAX_TOKENS_DIALOGO, MODEL_DIALOGO, MODEL_EXTRACCION
from app.llm import groq_client
from app.metrics import Cronometro, costo_usd, registrar_turno
from app.rag import service as rag
from app.storage import db

log = logging.getLogger(__name__)

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="turno")

# Cuantos turnos del historial se envian al modelo. La llamada es corta por
# diseno; mas contexto no mejora la respuesta y si encarece cada turno.
VENTANA_HISTORIAL = 12

INTERROGATIVAS = (
    "que ", "qué ", "como ", "cómo ", "cuando ", "cuándo ", "cuanto ", "cuánto ",
    "por que", "por qué", "puedo", "debo", "es normal", "es malo", "tengo que",
    "sirve", "conviene", "peligroso", "grave",
)


def _es_pregunta(texto: str) -> bool:
    t = texto.lower().strip()
    return "?" in t or t.startswith(INTERROGATIVAS) or any(f" {i}" in t for i in INTERROGATIVAS)


def _primera_frase(texto: str) -> tuple[str, str]:
    """Parte el texto en (primera frase completa, resto)."""
    m = re.search(r"^(.{12,}?[.!?])(\s|$)", texto, re.S)
    if m:
        return m.group(1).strip(), texto[m.end() :]
    return "", texto


@dataclass
class Llamada:
    llamada_id: str
    paciente: dict
    dia_postop: int | None
    estado: EstadoSintomas = field(default_factory=EstadoSintomas)
    historial: list[dict] = field(default_factory=list)
    triaje: ResultadoTriaje | None = None
    turno_idx: int = 0
    consultas_rag: int = 0
    alerta_id: str | None = None
    finalizada: bool = False
    inicio: float = field(default_factory=time.time)
    citas_acumuladas: list[dict] = field(default_factory=list)

    # ------------------------------------------------------------------ ayudas
    @property
    def escenario(self) -> str:
        return self.paciente.get("escenario", "general")

    def _nivel_actual(self) -> str:
        return self.triaje.nivel if self.triaje else "verde"

    def _mensajes(self, contexto: str) -> list[dict]:
        sistema = "\n\n".join(
            [
                prompts.SISTEMA_DIALOGO,
                prompts.bloque_paciente(self.paciente, self.dia_postop),
                contexto,
                prompts.bloque_estado(
                    self.estado,
                    self.triaje.motivo if self.triaje else "sin datos aun",
                    self._nivel_actual(),
                ),
            ]
        )
        mensajes = [{"role": "system", "content": sistema}]
        for t in self.historial[-VENTANA_HISTORIAL:]:
            mensajes.append(
                {"role": "assistant" if t["hablante"] == "agente" else "user", "content": t["texto"]}
            )
        return mensajes

    # ------------------------------------------------------------------ saludo
    def saludo(self) -> str:
        nombre = ((self.paciente.get("nombre_completo") or "").split() or [""])[0]
        procedimiento = (self.paciente.get("procedimiento") or "su cirugia").lower()
        # Concordancia de genero: en una llamada de voz un "lo" por un "la" se
        # oye de inmediato y arruina la credibilidad del agente en la primera frase.
        pronombre = "La" if str(self.paciente.get("genero", "")).upper().startswith("F") else "Lo"
        saludo_inicial = f"Buenos dias, {nombre}." if nombre else "Buenos dias."
        texto = (
            f"{saludo_inicial} Le habla {prompts.NOMBRE_AGENTE}, del equipo de "
            f"seguimiento de la clinica. {pronombre} llamo para saber como ha seguido "
            f"despues de {procedimiento}. Cuenteme, como se ha sentido?"
        )
        self.historial.append({"hablante": "agente", "texto": texto})
        db.registrar_turno(self.llamada_id, self.turno_idx, "agente", texto, "verde")
        self.turno_idx += 1
        return texto

    # ------------------------------------------------------------------ turno
    def _extraer(self, texto: str, uso: groq_client.Uso) -> dict:
        # Se busca hacia atras la ultima intervencion del agente: para cuando se
        # extrae, el turno del paciente ya esta en el historial, asi que el
        # ultimo elemento es su propia frase y no la pregunta que la motivo.
        # Sin la pregunta, una respuesta como "un tres" no se puede interpretar.
        pregunta = next(
            (t["texto"] for t in reversed(self.historial) if t["hablante"] == "agente"),
            "(ninguna)",
        )
        entrada = (
            f"Procedimiento: {self.paciente.get('procedimiento')}. "
            f"Dia postoperatorio: {self.dia_postop}.\n"
            f"Ultima pregunta del agente: {pregunta}\n"
            f"Dijo el paciente: \"{texto}\""
        )
        try:
            return groq_client.completar_json(
                prompts.SISTEMA_EXTRACCION, entrada, modelo=MODEL_EXTRACCION, uso=uso
            )
        except Exception as exc:
            log.error("Fallo la extraccion: %s", exc)
            return {}

    def _recuperar(self, texto: str) -> dict:
        """Consulta el corpus solo cuando el turno lo amerita.

        Recuperar en cada turno gastaria latencia y tokens en frases que no
        contienen ninguna duda clinica ("si", "ahi vamos"). Se consulta cuando
        el paciente pregunta algo o cuando hay una bandera que sustentar.
        """
        nivel = self._nivel_actual()
        banderas = []
        if self.triaje:
            banderas = [b.descripcion for b in self.triaje.banderas_rojas + self.triaje.banderas_amarillas]

        if _es_pregunta(texto):
            consulta = f"{self.paciente.get('procedimiento', '')}: {texto}"
        elif nivel in ("amarillo", "rojo") and banderas:
            consulta = (
                f"{self.paciente.get('procedimiento', '')} postoperatorio: "
                f"{'; '.join(banderas[:3])}. Signos de alarma y conducta."
            )
        else:
            return {"pasajes": [], "hay_evidencia": False, "mejor_similitud": 0.0}

        self.consultas_rag += 1
        return rag.consultar(consulta, escenario=self.escenario)

    def _segunda_opinion(self, uso: groq_client.Uso) -> tuple[str | None, str]:
        """El modelo revisa el veredicto de las reglas. Solo puede subir el nivel."""
        if not self.triaje or self.triaje.nivel == "rojo":
            return None, ""  # ya esta en el maximo: no hay a donde escalar
        entrada = (
            f"Procedimiento: {self.paciente.get('procedimiento')} | "
            f"Dia postoperatorio: {self.dia_postop} | Edad: {self.paciente.get('edad')} | "
            f"Comorbilidades: {', '.join(self.paciente.get('comorbilidades') or []) or 'ninguna'}\n"
            f"Estado clinico recogido: {self.estado.resumen_legible()}\n"
            f"Frases del paciente: {' | '.join(self.estado.citas_textuales[-6:])}\n"
            f"Nivel asignado por las reglas: {self.triaje.nivel} ({self.triaje.motivo})"
        )
        try:
            datos = groq_client.completar_json(
                prompts.SISTEMA_JUICIO, entrada, modelo=MODEL_EXTRACCION, uso=uso, max_tokens=200
            )
            return datos.get("nivel"), str(datos.get("motivo", ""))
        except Exception as exc:
            log.error("Fallo la segunda opinion: %s", exc)
            return None, ""

    def procesar(self, texto_paciente: str, ms_stt: float = 0.0, seg_audio: float = 0.0) -> Iterator[dict]:
        """Procesa un turno y emite eventos para el cliente."""
        crono = Cronometro()
        crono.fijar("stt", ms_stt)
        uso = groq_client.Uso()
        t0 = time.perf_counter()

        self.historial.append({"hablante": "paciente", "texto": texto_paciente})
        db.registrar_turno(self.llamada_id, self.turno_idx, "paciente", texto_paciente, None)
        self.turno_idx += 1

        # --- extraccion y recuperacion en paralelo -------------------------
        inicio_par = time.perf_counter()
        f_extraccion = _pool.submit(self._extraer, texto_paciente, uso)
        f_recuperacion = _pool.submit(self._recuperar, texto_paciente)
        delta = f_extraccion.result()
        recuperado = f_recuperacion.result()
        crono.fijar("extraccion_y_recuperacion", (time.perf_counter() - inicio_par) * 1000)

        cambios = self.estado.fusionar(delta)

        # --- triaje --------------------------------------------------------
        with crono.medir("triaje"):
            resultado = triage.evaluar(
                self.estado, self.dia_postop, self.paciente.get("procedimiento", "")
            )
            self.triaje = resultado
        nivel_llm, motivo_llm = (None, "")
        if cambios:
            nivel_llm, motivo_llm = self._segunda_opinion(uso)
            self.triaje = triage.combinar(resultado, nivel_llm, motivo_llm)

        nivel = self.triaje.nivel
        yield {
            "tipo": "estado",
            "nivel": nivel,
            "motivo": self.triaje.motivo,
            "estado_clinico": self.estado.a_dict(),
            "banderas": self.triaje.a_dict(),
            "pendientes": self.estado.dominios_pendientes(),
        }

        # --- alerta persistente -------------------------------------------
        if nivel == "rojo" and not self.alerta_id:
            self.alerta_id = db.crear_alerta(
                self.llamada_id,
                nivel,
                self.triaje.motivo,
                [b.__dict__ for b in self.triaje.banderas_rojas + self.triaje.banderas_amarillas],
                self.estado.a_dict(),
            )
            yield {"tipo": "alerta", "alerta_id": self.alerta_id, "nivel": nivel}

        # --- citas ---------------------------------------------------------
        pasajes = recuperado.get("pasajes", [])
        if pasajes:
            db.registrar_citas(self.llamada_id, self.turno_idx, pasajes)
            citas = [
                {
                    "documento": p["documento"],
                    "pagina": p["pagina"],
                    "chunk_id": p["chunk_id"],
                    "similitud": p["similitud"],
                }
                for p in pasajes
            ]
            self.citas_acumuladas.extend(citas)
            yield {
                "tipo": "citas",
                "citas": citas,
                "hay_evidencia": recuperado.get("hay_evidencia", False),
            }

        # --- respuesta hablada ---------------------------------------------
        contexto = prompts.bloque_contexto(pasajes, recuperado.get("hay_evidencia", False))
        mensajes = self._mensajes(contexto)

        inicio_llm = time.perf_counter()
        primer_token = None
        acumulado = ""
        pendiente = ""
        try:
            for trozo in groq_client.completar_stream(
                mensajes, modelo=MODEL_DIALOGO, max_tokens=MAX_TOKENS_DIALOGO, uso=uso
            ):
                if primer_token is None:
                    primer_token = time.perf_counter()
                    crono.fijar("llm_ttft", (primer_token - inicio_llm) * 1000)
                acumulado += trozo
                pendiente += trozo
                # Se emite por frases completas: el navegador empieza a hablar
                # con la primera sin esperar a que termine de generarse el resto.
                frase, resto = _primera_frase(pendiente)
                if frase:
                    pendiente = resto
                    yield {"tipo": "frase", "texto": frase}
        except Exception as exc:
            log.error("Fallo la generacion: %s", exc)
            acumulado = (
                "Disculpe, tuve un problema tecnico. Podria repetirme lo ultimo que me dijo?"
            )
            yield {"tipo": "frase", "texto": acumulado}
            pendiente = ""

        if pendiente.strip():
            yield {"tipo": "frase", "texto": pendiente.strip()}

        crono.fijar("llm_total", (time.perf_counter() - inicio_llm) * 1000)
        crono.fijar("servidor_total", (time.perf_counter() - t0) * 1000 + ms_stt)

        texto_agente = acumulado.strip()
        self.historial.append({"hablante": "agente", "texto": texto_agente})
        db.registrar_turno(self.llamada_id, self.turno_idx, "agente", texto_agente, nivel)
        turno_registrado = self.turno_idx
        self.turno_idx += 1

        costo = costo_usd(
            [(MODEL_EXTRACCION, 0, 0), (MODEL_DIALOGO, uso.tokens_entrada, uso.tokens_salida)],
            segundos_audio=seg_audio,
        )
        registrar_turno(
            {
                "llamada_id": self.llamada_id,
                "turno": turno_registrado,
                "paciente_id": self.paciente.get("paciente_id"),
                "ms": crono.etapas,
                "tokens": {"entrada": uso.tokens_entrada, "salida": uso.tokens_salida},
                "invocaciones_modelo": uso.invocaciones,
                "consultas_rag": 1 if pasajes else 0,
                "citas": len(pasajes),
                "nivel": nivel,
                "costo_usd": round(costo, 6),
                "modelos": uso.modelos,
            }
        )

        yield {
            "tipo": "fin",
            "turno": turno_registrado,
            "texto_completo": texto_agente,
            "nivel": nivel,
            "metricas": {
                "ms": crono.etapas,
                "tokens": uso.a_dict(),
                "costo_usd": round(costo, 6),
            },
        }

    # ------------------------------------------------------------------ cierre
    def resumen(self) -> dict:
        """Resumen estructurado de la llamada. Se persiste y queda auditable."""
        uso = groq_client.Uso()
        transcripcion = "\n".join(
            f"{'AGENTE' if t['hablante'] == 'agente' else 'PACIENTE'}: {t['texto']}"
            for t in self.historial
        )
        entrada = (
            f"Paciente: {self.paciente.get('nombre_completo')} | "
            f"Documento: {self.paciente.get('documento_cc')} | "
            f"Procedimiento: {self.paciente.get('procedimiento')} | "
            f"Dia postoperatorio: {self.dia_postop}\n"
            f"Nivel asignado: {self._nivel_actual()}\n"
            f"Motivo del nivel: {self.triaje.motivo if self.triaje else 'sin evaluacion'}\n"
            f"Estado clinico recogido: {self.estado.resumen_legible()}\n"
            f"Dominios sin respuesta: {', '.join(self.estado.dominios_pendientes()) or 'ninguno'}\n\n"
            f"TRANSCRIPCION\n{transcripcion}"
        )
        try:
            narrativa = groq_client.completar_json(
                prompts.SISTEMA_RESUMEN, entrada, modelo=MODEL_DIALOGO, uso=uso, max_tokens=800
            )
        except Exception as exc:
            log.error("Fallo el resumen: %s", exc)
            narrativa = {}

        datos = {
            "llamada_id": self.llamada_id,
            "paciente": {
                "paciente_id": self.paciente.get("paciente_id"),
                "nombre": self.paciente.get("nombre_completo"),
                "documento": self.paciente.get("documento_cc"),
                "eps": self.paciente.get("eps"),
                "procedimiento": self.paciente.get("procedimiento"),
                "fecha_cirugia": self.paciente.get("fecha_cirugia"),
                "dia_postop": self.dia_postop,
                "edad": self.paciente.get("edad"),
                "comorbilidades": self.paciente.get("comorbilidades"),
            },
            "decision": {
                "nivel": self._nivel_actual(),
                "escalar_a_humano": self._nivel_actual() in ("amarillo", "rojo"),
                "motivo": self.triaje.motivo if self.triaje else "",
                "origen": self.triaje.origen if self.triaje else "",
                "banderas_rojas": [b.__dict__ for b in (self.triaje.banderas_rojas if self.triaje else [])],
                "banderas_amarillas": [
                    b.__dict__ for b in (self.triaje.banderas_amarillas if self.triaje else [])
                ],
                "alerta_id": self.alerta_id,
            },
            "estado_clinico": self.estado.a_dict(),
            "narrativa": narrativa,
            "referencias": self.citas_acumuladas,
            "conversacion": self.historial,
            "duracion_s": round(time.time() - self.inicio, 1),
            "turnos": len(self.historial),
        }
        db.guardar_resumen(self.llamada_id, datos)
        db.cerrar_llamada(self.llamada_id, self._nivel_actual())
        self.finalizada = True
        return datos


# --------------------------------------------------------------------- registro
_llamadas: dict[str, Llamada] = {}


def iniciar(paciente: dict, dia_postop: int | None) -> Llamada:
    llamada_id = db.crear_llamada(paciente, dia_postop)
    llamada = Llamada(llamada_id=llamada_id, paciente=paciente, dia_postop=dia_postop)
    _llamadas[llamada_id] = llamada
    return llamada


def obtener(llamada_id: str) -> Llamada | None:
    return _llamadas.get(llamada_id)
