"""Estructuras clinicas que viajan por el sistema."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Nivel = Literal["verde", "amarillo", "rojo"]

ORDEN_NIVEL: dict[str, int] = {"verde": 0, "amarillo": 1, "rojo": 2}

# Los seis dominios que cubre la llamada, en el orden en que se preguntan.
DOMINIOS = ("dolor", "fiebre", "herida", "movilidad", "apetito", "sueno")

VALORES_HERIDA = ("normal", "eritema_leve", "secrecion_purulenta", "dehiscencia", "sangrado_activo")
VALORES_MOVILIDAD = ("normal", "limitada_esperada", "incapacitante_nueva")
VALORES_APETITO = ("normal", "levemente_disminuido", "muy_disminuido")
VALORES_SUENO = ("normal", "levemente_alterado", "muy_alterado")


@dataclass
class EstadoSintomas:
    """Lo que el agente ha logrado averiguar hasta ahora.

    Todo campo en None significa "todavia no se pregunto o el paciente no
    respondio", que es distinto de "esta normal". Esa diferencia importa: el
    agente no puede cerrar la llamada dando por sano un dominio que nunca
    consulto.
    """

    dolor_nrs: int | None = None
    dolor_subito_severo: bool = False
    fiebre_c: float | None = None
    fiebre_subjetiva: bool | None = None  # dice sentir calentura, sin termometro
    herida: str | None = None
    movilidad: str | None = None
    apetito: str | None = None
    sueno: str | None = None

    # Banderas transversales que ningun dominio de la lista captura por si solo.
    disnea: bool = False
    dolor_toracico: bool = False
    vomito_persistente: bool = False
    intolerancia_oral: bool = False
    sin_gases_ni_deposicion: bool = False
    signos_tvp: bool = False
    ictericia: bool = False
    sincope: bool = False
    confusion: bool = False

    citas_textuales: list[str] = field(default_factory=list)

    def dominios_cubiertos(self) -> set[str]:
        cubiertos = set()
        if self.dolor_nrs is not None:
            cubiertos.add("dolor")
        if self.fiebre_c is not None or self.fiebre_subjetiva is not None:
            cubiertos.add("fiebre")
        for dominio in ("herida", "movilidad", "apetito", "sueno"):
            if getattr(self, dominio) is not None:
                cubiertos.add(dominio)
        return cubiertos

    def dominios_pendientes(self) -> list[str]:
        cubiertos = self.dominios_cubiertos()
        return [d for d in DOMINIOS if d not in cubiertos]

    def fusionar(self, delta: dict[str, Any]) -> list[str]:
        """Incorpora lo extraido de un turno. Devuelve los campos que cambiaron.

        Un valor nuevo pisa al anterior a proposito: si el paciente se corrige
        ("no, mas bien un ocho"), manda lo ultimo que dijo.
        """
        cambios: list[str] = []
        for campo, valor in (delta or {}).items():
            if valor is None or not hasattr(self, campo):
                continue
            if campo == "citas_textuales":
                if isinstance(valor, list):
                    self.citas_textuales.extend(str(v) for v in valor)
                continue
            if isinstance(getattr(self, campo), bool) and valor is False:
                # Una bandera negativa no borra una positiva ya confirmada.
                continue
            if getattr(self, campo) != valor:
                setattr(self, campo, valor)
                cambios.append(campo)
        return cambios

    def a_dict(self) -> dict:
        return asdict(self)

    def resumen_legible(self) -> str:
        partes = []
        if self.dolor_nrs is not None:
            partes.append(f"dolor {self.dolor_nrs}/10")
        if self.fiebre_c is not None:
            partes.append(f"temperatura {self.fiebre_c} C")
        elif self.fiebre_subjetiva:
            partes.append("sensacion de fiebre sin termometro")
        for campo in ("herida", "movilidad", "apetito", "sueno"):
            valor = getattr(self, campo)
            if valor:
                partes.append(f"{campo}: {valor}")
        for bandera in (
            "disnea", "dolor_toracico", "vomito_persistente", "intolerancia_oral",
            "sin_gases_ni_deposicion", "signos_tvp", "ictericia", "sincope", "confusion",
        ):
            if getattr(self, bandera):
                partes.append(bandera.replace("_", " "))
        return "; ".join(partes) if partes else "sin datos aun"


@dataclass
class Bandera:
    codigo: str
    descripcion: str
    peso: int
    color: Nivel


@dataclass
class ResultadoTriaje:
    nivel: Nivel
    banderas_rojas: list[Bandera] = field(default_factory=list)
    banderas_amarillas: list[Bandera] = field(default_factory=list)
    puntaje: int = 0
    motivo: str = ""
    origen: str = "reglas"  # "reglas" | "llm" | "reglas+llm"
    escalar: bool = False

    def a_dict(self) -> dict:
        return {
            "nivel": self.nivel,
            "escalar": self.escalar,
            "puntaje": self.puntaje,
            "motivo": self.motivo,
            "origen": self.origen,
            "banderas_rojas": [asdict(b) for b in self.banderas_rojas],
            "banderas_amarillas": [asdict(b) for b in self.banderas_amarillas],
        }
