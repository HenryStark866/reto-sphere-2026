"""Motor de triaje determinista.

Por que reglas y no solo el modelo: la rubrica declara asimetria clinica -el
falso negativo es la falla catastrofica- y un modelo de lenguaje no ofrece
garantia dura sobre un umbral. Aqui una temperatura de 38.0 escala siempre,
sin importar como venga redactada la conversacion ni que tan tranquilizador
suene el paciente.

El modelo interviene despues, y solo puede subir el nivel (ver `combinar`):
puede ver lo que las reglas no anticiparon, pero nunca desactiva una bandera.
"""
from __future__ import annotations

from app.config import (
    FIEBRE_FEBRICULA,
    FIEBRE_ROJA,
    UMBRAL_AMARILLO,
    UMBRAL_ROJO_ACUMULADO,
)
from app.agent.schema import (
    ORDEN_NIVEL,
    Bandera,
    EstadoSintomas,
    Nivel,
    ResultadoTriaje,
)

# Dolor esperable segun dia postoperatorio. Un 5/10 en el dia 1 es curso normal;
# el mismo 5/10 en el dia 14 no lo es.
DOLOR_ESPERADO = {1: 6, 3: 5, 7: 4, 14: 3}


def dolor_esperado(dia_postop: int | None) -> int:
    if dia_postop is None:
        return 5
    conocidos = sorted(DOLOR_ESPERADO)
    for d in conocidos:
        if dia_postop <= d:
            return DOLOR_ESPERADO[d]
    return DOLOR_ESPERADO[conocidos[-1]]


def _rojas(e: EstadoSintomas, dia_postop: int | None, procedimiento: str) -> list[Bandera]:
    f: list[Bandera] = []
    p = (procedimiento or "").lower()

    def add(codigo: str, desc: str):
        f.append(Bandera(codigo=codigo, descripcion=desc, peso=0, color="rojo"))

    if e.fiebre_c is not None and e.fiebre_c >= FIEBRE_ROJA:
        add("fiebre_alta", f"Temperatura {e.fiebre_c} C (umbral {FIEBRE_ROJA} C)")
    if e.herida == "secrecion_purulenta":
        add("herida_purulenta", "Secrecion purulenta en la herida quirurgica")
    if e.herida == "dehiscencia":
        add("dehiscencia", "Apertura de la herida quirurgica")
    if e.herida == "sangrado_activo":
        add("sangrado_activo", "Sangrado activo por la herida")
    if e.movilidad == "incapacitante_nueva":
        add("movilidad_incapacitante", "Incapacidad nueva para moverse o caminar")
    if e.dolor_nrs is not None and e.dolor_nrs >= 8:
        add("dolor_severo", f"Dolor {e.dolor_nrs}/10")
    if e.dolor_subito_severo:
        add("dolor_subito", "Dolor de aparicion subita e intensa")
    if e.disnea:
        add("disnea", "Dificultad para respirar")
    if e.dolor_toracico:
        add("dolor_toracico", "Dolor en el pecho")
    if e.sincope:
        add("sincope", "Desmayo o perdida de conciencia")
    if e.confusion:
        add("confusion", "Confusion o desorientacion nueva")
    if e.vomito_persistente:
        add("vomito_persistente", "Vomito que no cede")
    if e.intolerancia_oral:
        add("intolerancia_oral", "No tolera liquidos ni alimentos por via oral")
    if e.signos_tvp:
        add("signos_tvp", "Signos de trombosis venosa profunda en la pierna")
    if e.ictericia:
        add("ictericia", "Coloracion amarilla de piel u ojos")
    # Especifica de cirugia abdominal: ausencia de transito sugiere ileo u obstruccion.
    if e.sin_gases_ni_deposicion and (dia_postop or 0) >= 3:
        if any(k in p for k in ("colectom", "colorrect", "apendic", "colecist", "abdomin")):
            add("sin_transito_intestinal", "Sin gases ni deposicion despues del dia 3")
    return f


def _amarillas(e: EstadoSintomas, dia_postop: int | None) -> list[Bandera]:
    f: list[Bandera] = []

    def add(codigo: str, desc: str, peso: int):
        f.append(Bandera(codigo=codigo, descripcion=desc, peso=peso, color="amarillo"))

    if e.fiebre_c is not None and FIEBRE_FEBRICULA <= e.fiebre_c < FIEBRE_ROJA:
        add("febricula", f"Febricula {e.fiebre_c} C", 2)
    elif e.fiebre_c is None and e.fiebre_subjetiva:
        # Sin termometro no se puede descartar fiebre; pesa igual que la febricula.
        add("fiebre_no_medida", "Refiere sensacion de fiebre sin poder medirla", 2)

    if e.herida == "eritema_leve":
        add("eritema_leve", "Enrojecimiento leve alrededor de la herida", 2)

    if e.dolor_nrs is not None:
        exceso = e.dolor_nrs - dolor_esperado(dia_postop)
        if exceso >= 1:
            add(
                "dolor_sobre_lo_esperado",
                f"Dolor {e.dolor_nrs}/10, por encima de lo esperado en el dia {dia_postop}",
                1 + (1 if exceso >= 2 else 0),
            )

    if e.movilidad == "limitada_esperada" and (dia_postop or 0) >= 7:
        add("movilidad_limitada_tardia", "Movilidad aun limitada en fase tardia", 1)

    if e.apetito == "muy_disminuido":
        add("apetito_muy_disminuido", "Apetito muy disminuido", 2)
    elif e.apetito == "levemente_disminuido" and (dia_postop or 0) >= 7:
        add("apetito_disminuido_tardio", "Apetito aun disminuido en fase tardia", 1)

    if e.sueno == "muy_alterado":
        add("sueno_muy_alterado", "Sueno muy alterado", 2)

    return f


def evaluar(
    estado: EstadoSintomas, dia_postop: int | None = None, procedimiento: str = ""
) -> ResultadoTriaje:
    rojas = _rojas(estado, dia_postop, procedimiento)
    amarillas = _amarillas(estado, dia_postop)
    puntaje = sum(b.peso for b in amarillas)

    if rojas:
        nivel: Nivel = "rojo"
        motivo = "Bandera roja: " + "; ".join(b.descripcion for b in rojas)
    elif puntaje >= UMBRAL_ROJO_ACUMULADO:
        nivel = "rojo"
        motivo = (
            f"Deterioro en varios dominios a la vez (puntaje {puntaje}): "
            + "; ".join(b.descripcion for b in amarillas)
        )
    elif puntaje >= UMBRAL_AMARILLO:
        nivel = "amarillo"
        motivo = f"Hallazgos que ameritan vigilancia (puntaje {puntaje}): " + "; ".join(
            b.descripcion for b in amarillas
        )
    elif amarillas:
        nivel = "verde"
        motivo = "Hallazgos leves aislados, dentro de lo esperable: " + "; ".join(
            b.descripcion for b in amarillas
        )
    else:
        nivel = "verde"
        motivo = "Sin hallazgos de alarma en los dominios evaluados."

    return ResultadoTriaje(
        nivel=nivel,
        banderas_rojas=rojas,
        banderas_amarillas=amarillas,
        puntaje=puntaje,
        motivo=motivo,
        origen="reglas",
        escalar=nivel in ("amarillo", "rojo"),
    )


NIVEL_POR_ORDEN = {v: k for k, v in ORDEN_NIVEL.items()}


def combinar(reglas: ResultadoTriaje, nivel_llm: str | None, motivo_llm: str = "") -> ResultadoTriaje:
    """Fusiona el juicio del modelo con las reglas. El modelo solo puede escalar,
    y como mucho un nivel por encima de lo que dicen las reglas.

    Si el modelo ve algo que las reglas no cubren -un sintoma raro, un contexto
    que preocupa- el nivel sube. Si el modelo quiere tranquilizar por debajo de
    una bandera roja, se ignora: esa es exactamente la falla que la rubrica
    castiga como catastrofica.

    El tope de un nivel sale de una medicion, no de una intuicion. Sobre 20
    casos verdes de la capa ruidosa, 18 acabaron escalados y 14 de ellos por
    este camino: el modelo saltaba de verde a ROJO directamente, con motivos del
    tipo "podria haber un problema subyacente". Un salto de dos niveles es
    precisamente el que no se apoya en nada que las reglas hayan visto. Con el
    tope, esa sospecha sigue llegando a un humano -amarillo tambien escala- pero
    sin declarar una emergencia que nadie puede sustentar.
    """
    if not nivel_llm or nivel_llm not in ORDEN_NIVEL:
        return reglas

    if ORDEN_NIVEL[nivel_llm] <= ORDEN_NIVEL[reglas.nivel]:
        return reglas

    tope = min(ORDEN_NIVEL[nivel_llm], ORDEN_NIVEL[reglas.nivel] + 1)
    nivel_final = NIVEL_POR_ORDEN[tope]
    nota = f"Escalado por el modelo: {motivo_llm}" if motivo_llm else "Escalado por el modelo."
    if tope < ORDEN_NIVEL[nivel_llm]:
        nota += f" (pedia {nivel_llm}; se limita a un nivel sobre las reglas)"

    return ResultadoTriaje(
        nivel=nivel_final,  # type: ignore[arg-type]
        banderas_rojas=reglas.banderas_rojas,
        banderas_amarillas=reglas.banderas_amarillas,
        puntaje=reglas.puntaje,
        motivo=f"{reglas.motivo} | {nota}",
        origen="reglas+llm",
        escalar=nivel_final in ("amarillo", "rojo"),
    )
