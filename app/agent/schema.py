"""Estructuras clinicas que viajan por el sistema."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

Nivel = Literal["verde", "amarillo", "rojo"]

ORDEN_NIVEL: dict[str, int] = {"verde": 0, "amarillo": 1, "rojo": 2}

# Los seis dominios que cubre la llamada, en el orden en que se preguntan.
DOMINIOS = ("dolor", "fiebre", "herida", "movilidad", "apetito", "sueno")

VALORES_HERIDA = ("normal", "eritema_leve", "secrecion_purulenta", "dehiscencia", "sangrado_activo")
VALORES_MOVILIDAD = ("normal", "limitada_esperada", "incapacitante_nueva")
VALORES_APETITO = ("normal", "levemente_disminuido", "muy_disminuido")
VALORES_SUENO = ("normal", "levemente_alterado", "muy_alterado")

# Campos graduados: los cuatro ordinales van de menos a mas grave en su tupla, y
# los dos numericos son peores cuanto mas altos. Se usa para saber, entre dos
# valores del mismo campo, cual es el peor.
ESCALAS_ORDINALES: dict[str, tuple[str, ...]] = {
    "herida": VALORES_HERIDA,
    "movilidad": VALORES_MOVILIDAD,
    "apetito": VALORES_APETITO,
    "sueno": VALORES_SUENO,
}
CAMPOS_NUMERICOS_GRADUADOS = ("dolor_nrs", "fiebre_c")


_CAMPOS_BOOLEANOS = (
    "dolor_subito_severo", "fiebre_subjetiva", "disnea", "dolor_toracico",
    "vomito_persistente", "intolerancia_oral", "sin_gases_ni_deposicion",
    "signos_tvp", "ictericia", "sincope", "confusion",
)
_VERDADERO = {"true", "si", "sí", "yes", "1"}
_FALSO = {"false", "no", "0"}


def _normalizar(campo: str, valor: Any) -> Any:
    """Devuelve el valor con el tipo que el resto del sistema espera, o None.

    El modelo devuelve JSON y a veces manda el numero como cadena: un
    `"dolor_nrs": "8"` hacia estallar el motor de reglas con un TypeError al
    comparar con el umbral, y el turno moria justo cuando habia que escalar.
    Un dato que no se puede interpretar se descarta -que es lo mismo que "no
    se pregunto"- en vez de entrar crudo al estado clinico.
    """
    if campo == "dolor_nrs":
        try:
            return max(0, min(10, int(round(float(valor)))))
        except (TypeError, ValueError):
            return None
    if campo == "fiebre_c":
        try:
            grados = float(valor)
        except (TypeError, ValueError):
            return None
        # Una temperatura corporal fuera de este rango es una transcripcion mal
        # entendida, no un paciente: registrarla dispararia o apagaria banderas.
        return grados if 30.0 <= grados <= 45.0 else None
    if campo in ESCALAS_ORDINALES:
        texto = str(valor).strip().lower()
        return texto if texto in ESCALAS_ORDINALES[campo] else None
    if campo in _CAMPOS_BOOLEANOS:
        if isinstance(valor, bool):
            return valor
        texto = str(valor).strip().lower()
        if texto in _VERDADERO:
            return True
        if texto in _FALSO:
            return False
        return None
    return valor


def _severidad(campo: str, valor: Any) -> float | None:
    """Posicion del valor en su escala. Mayor es peor. None si no aplica."""
    if valor is None:
        return None
    if campo in CAMPOS_NUMERICOS_GRADUADOS:
        return float(valor)
    escala = ESCALAS_ORDINALES.get(campo)
    if escala and valor in escala:
        return float(escala.index(valor))
    return None


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

    # Peor valor visto en la llamada para cada campo graduado. El campo normal
    # guarda lo ultimo que dijo el paciente -que es lo que la conversacion tiene
    # que leer para no repreguntar-; esto guarda lo peor que llego a decir, que
    # es sobre lo que se decide el triaje. Ver `para_triaje`.
    peor_observado: dict[str, Any] = field(default_factory=dict)

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
        ("no, mas bien un ocho"), manda lo ultimo que dijo, y eso es lo que la
        conversacion lee para no repreguntar lo ya respondido.

        En paralelo se anota el peor valor que se llego a ver. Un paciente que
        dice "me duele nueve" y dos turnos despues "ya estoy mejor, como un
        tres" no borra el nueve para efectos de triaje: bajar el nivel por una
        correccion posterior es exactamente el falso negativo que la rubrica
        llama catastrofico.
        """
        cambios: list[str] = []
        for campo, valor in (delta or {}).items():
            if valor is None or not hasattr(self, campo):
                continue
            if campo == "citas_textuales":
                if isinstance(valor, list):
                    self.citas_textuales.extend(str(v) for v in valor)
                continue
            valor = _normalizar(campo, valor)
            if valor is None:
                continue
            if isinstance(getattr(self, campo), bool) and valor is False:
                # Una bandera negativa no borra una positiva ya confirmada.
                continue
            self._anotar_peor(campo, valor)
            if getattr(self, campo) != valor:
                setattr(self, campo, valor)
                cambios.append(campo)
        return cambios

    def _anotar_peor(self, campo: str, valor: Any) -> None:
        nueva = _severidad(campo, valor)
        if nueva is None:
            return
        previa = _severidad(campo, self.peor_observado.get(campo))
        if previa is None or nueva > previa:
            self.peor_observado[campo] = valor

    def para_triaje(self) -> "EstadoSintomas":
        """Copia sobre la que se decide la criticidad: el peor valor de la llamada.

        Las banderas booleanas ya no se apagan (`fusionar` no deja que un false
        pise a un true). Esto extiende la misma garantia a los campos graduados,
        que son los que llevan el dolor, la temperatura y el estado de la herida.
        """
        copia = replace(self, citas_textuales=list(self.citas_textuales))
        copia.peor_observado = dict(self.peor_observado)
        for campo, valor in self.peor_observado.items():
            if _severidad(campo, valor) is not None:
                setattr(copia, campo, valor)
        return copia

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
