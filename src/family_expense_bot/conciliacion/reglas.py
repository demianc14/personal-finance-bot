"""Reglas de cruce movimiento ↔ gasto: deterministas, en orden y con nombre. Sin LLM.

Cada regla es la misma consulta SQL con otra ventana de días (y, la tercera,
con descripción parecida). Corre solo sobre lo que las anteriores dejaron
libre, y cada par guarda el nombre de la regla que lo unió.

Solo cruzan los movimientos de tipo `debito` contra gastos del mismo monto, al
centavo. Comisiones, intereses, pagos de tarjeta y todo lo que entra (una
devolución) no son un gasto registrado: nunca cruzan.

La conciliación es uno a uno. Cuando varios compiten gana el más cercano en
días y después el que comparte más palabras; si siguen empatados no se une
ninguno, salvo que sean intercambiables (ver `resolver`). Un movimiento sin par
no se guarda como gasto: proponerlo es otra fase.
"""

import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from family_expense_bot.extraction.categorias import normalizar
from family_expense_bot.storage.models import Conciliacion

LARGO_MINIMO = 4
"""Una palabra más corta ("de", "la", "sa") no dice nada del comercio."""


@dataclass(frozen=True)
class Regla:
    nombre: str
    """Lo que queda en `conciliaciones.regla`."""
    dias: int
    """Distancia máxima entre la fecha del movimiento y la del gasto."""
    exige_parecido: bool
    """Si además la descripción del movimiento tiene que parecerse a la del gasto."""


REGLAS: tuple[Regla, ...] = (
    Regla("monto_y_fecha", dias=0, exige_parecido=False),
    Regla("monto_3_dias", dias=3, exige_parecido=False),
    # Más allá de tres días el monto solo ya no alcanza: tiene que coincidir el texto.
    # Diez días cubre un cargo que el banco asienta la semana siguiente; con ±3 la
    # regla anterior ya se habría llevado esos pares y esta no aportaría nada.
    Regla("monto_y_descripcion", dias=10, exige_parecido=True),
)


def palabras(texto: str) -> set[str]:
    """Las palabras que sirven para comparar: sin tildes, sin números y de 4 letras o más."""
    return {p for p in re.findall(r"[a-z]+", normalizar(texto)) if len(p) >= LARGO_MINIMO}


def en_comun(a: str, b: str) -> set[str]:
    return palabras(a) & palabras(b)


def parecidas(a: str, b: str, genericas: frozenset[str]) -> bool:
    """Dos palabras en común, o una sola si es distintiva (no está en `genericas`)."""
    comunes = en_comun(a, b)
    return len(comunes) >= 2 or bool(comunes - genericas)


@dataclass(frozen=True)
class Candidato:
    """Un movimiento y un gasto que una regla permite unir."""

    movimiento_id: int
    orden: tuple[int, int]
    """(estado, línea): el orden en que aparecen los movimientos."""
    fecha_movimiento: date
    gasto_id: int
    fecha_gasto: date
    dias: int
    comunes: int
    """Cuántas palabras comparten las dos descripciones."""


def _mejores(
    candidatos: Iterable[Candidato],
    de: Callable[[Candidato], int],
    hacia: Callable[[Candidato], int],
) -> dict[int, frozenset[int]]:
    """Para cada uno, los del otro lado que más le convienen: menos días y, luego, más palabras."""
    por_clave: dict[int, dict[tuple[int, int], set[int]]] = defaultdict(lambda: defaultdict(set))
    for c in candidatos:
        por_clave[de(c)][(c.dias, -c.comunes)].add(hacia(c))
    return {quien: frozenset(grupos[min(grupos)]) for quien, grupos in por_clave.items()}


def resolver(candidatos: Iterable[Candidato]) -> tuple[list[tuple[int, int]], list[int]]:
    """Decide los pares de una regla. Devuelve (pares, movimientos que quedaron en empate).

    Un par se acepta cuando la elección es mutua: el gasto es el mejor del
    movimiento y el movimiento es el mejor del gasto. Se repite hasta que no
    sale ninguno más, porque cada par libera a los que competían con él.

    Cuando hay empate solo se une si los empatados son intercambiables: un grupo
    de movimientos de la misma fecha y un grupo de gastos de la misma fecha que
    se prefieren entre sí y a nadie más, con al menos tantos movimientos como
    gastos. Se unen en orden (línea ↔ id) y los movimientos que sobran quedan
    sin conciliar. Si sobran gastos, o las fechas difieren, no se une ninguno:
    elegir sería adivinar.
    """
    vivos = list(candidatos)
    orden = {c.movimiento_id: c.orden for c in vivos}
    fecha_m = {c.movimiento_id: c.fecha_movimiento for c in vivos}
    fecha_g = {c.gasto_id: c.fecha_gasto for c in vivos}
    pares: list[tuple[int, int]] = []
    while True:
        mejores_m = _mejores(vivos, lambda c: c.movimiento_id, lambda c: c.gasto_id)
        mejores_g = _mejores(vivos, lambda c: c.gasto_id, lambda c: c.movimiento_id)
        nuevos: list[tuple[int, int]] = []
        vistos: set[int] = set()
        for m in sorted(mejores_m, key=orden.__getitem__):
            gastos = mejores_m[m]
            movimientos = mejores_g[next(iter(gastos))]
            if (
                m in vistos
                or m not in movimientos
                or len(movimientos) < len(gastos)
                or any(mejores_m[x] != gastos for x in movimientos)
                or any(mejores_g[x] != movimientos for x in gastos)
                or len({fecha_m[x] for x in movimientos}) > 1
                or len({fecha_g[x] for x in gastos}) > 1
            ):
                continue
            vistos |= movimientos
            # Si sobran movimientos, los últimos quedan sin conciliar.
            nuevos.extend(
                zip(sorted(movimientos, key=orden.__getitem__), sorted(gastos), strict=False)
            )
        if not nuevos:
            break
        pares.extend(nuevos)
        unidos_m = {m for m, _ in nuevos}
        unidos_g = {g for _, g in nuevos}
        vivos = [c for c in vivos if c.movimiento_id not in unidos_m and c.gasto_id not in unidos_g]
    # Sin empates siempre hay una elección mutua: lo que queda vivo quedó por empate.
    empatados = sorted({c.movimiento_id for c in vivos}, key=orden.__getitem__)
    return pares, empatados


class Par(BaseModel):
    model_config = ConfigDict(frozen=True)

    movimiento_id: int
    gasto_id: int
    regla: str


class Cruce(BaseModel):
    """Lo que hizo una corrida de `conciliar`."""

    model_config = ConfigDict(frozen=True)

    pares: tuple[Par, ...]
    """Los pares nuevos, en el orden de las reglas."""
    por_regla: dict[str, int]
    empatados: tuple[int, ...]
    """Movimientos con candidatos que no se unieron porque hubo empate."""
    sin_conciliar: int
    """Débitos del periodo que quedaron sin par, empatados incluidos."""


_TEXTO_GASTO = "g.descripcion || ' ' || coalesce(g.subcategoria, '') || ' ' || g.texto_original"
_LIBRE_Y_EN_PERIODO = """
    m.tipo = 'debito' AND m.monto_centavos < 0
    AND m.anulado_en IS NULL AND e.anulado_en IS NULL
    AND (:desde IS NULL OR m.fecha >= :desde) AND (:hasta IS NULL OR m.fecha <= :hasta)
    AND NOT EXISTS (
        SELECT 1 FROM conciliaciones c
        WHERE c.movimiento_id = m.id AND c.anulado_en IS NULL
    )
"""
_CANDIDATOS = text(
    f"""
    SELECT m.id, m.estado_id, m.linea, m.fecha, g.id, g.fecha,
           CAST(round(abs(julianday(g.fecha) - julianday(m.fecha))) AS INTEGER),
           palabras_en_comun(m.descripcion, {_TEXTO_GASTO})
    FROM movimientos m
    JOIN estados_cuenta e ON e.id = m.estado_id
    JOIN gastos g ON g.monto_centavos = -m.monto_centavos
    WHERE {_LIBRE_Y_EN_PERIODO}
      AND g.anulado_en IS NULL
      AND NOT EXISTS (
          SELECT 1 FROM conciliaciones c
          WHERE c.gasto_id = g.id AND c.anulado_en IS NULL
      )
      AND abs(julianday(g.fecha) - julianday(m.fecha)) <= :dias
      AND (NOT :exige_parecido OR descripcion_parecida(m.descripcion, {_TEXTO_GASTO}))
    ORDER BY m.estado_id, m.linea, g.id
    """
)
_SIN_CONCILIAR = text(
    f"""
    SELECT count(*) FROM movimientos m
    JOIN estados_cuenta e ON e.id = m.estado_id
    WHERE {_LIBRE_Y_EN_PERIODO}
    """
)


def _registrar_funciones(sesion: Session, genericas: frozenset[str]) -> None:
    """El parecido de descripciones, disponible dentro del SQL de esta conexión."""
    conexion: Any = sesion.connection().connection.driver_connection
    conexion.create_function(
        "palabras_en_comun", 2, lambda a, b: len(en_comun(a, b)), deterministic=True
    )
    conexion.create_function(
        "descripcion_parecida", 2, lambda a, b: int(parecidas(a, b, genericas)), deterministic=True
    )


def conciliar(
    engine: Engine,
    genericas: frozenset[str],
    desde: date | None = None,
    hasta: date | None = None,
) -> Cruce:
    """Une movimientos y gastos libres con las reglas, en orden, y guarda cada par con su regla.

    `desde` y `hasta` acotan los movimientos por fecha; el gasto puede caer fuera
    si la ventana de la regla lo alcanza. Todo va en una transacción: si la base
    rechaza un par (los índices únicos no dejan conciliar algo dos veces), no
    queda ninguno. Correrla de nuevo no duplica nada, y un par anulado libera al
    movimiento y al gasto para volver a cruzarse.
    """
    periodo = {
        "desde": desde.isoformat() if desde else None,
        "hasta": hasta.isoformat() if hasta else None,
    }
    pares: list[Par] = []
    empatados: dict[int, tuple[int, int]] = {}
    with Session(engine) as sesion, sesion.begin():
        _registrar_funciones(sesion, genericas)
        for regla in REGLAS:
            filas = sesion.execute(
                _CANDIDATOS, {**periodo, "dias": regla.dias, "exige_parecido": regla.exige_parecido}
            )
            candidatos = [
                Candidato(
                    movimiento_id=m,
                    orden=(estado, linea),
                    fecha_movimiento=date.fromisoformat(fecha_m),
                    gasto_id=g,
                    fecha_gasto=date.fromisoformat(fecha_g),
                    dias=dias,
                    comunes=comunes,
                )
                for m, estado, linea, fecha_m, g, fecha_g, dias, comunes in filas
            ]
            unidos, en_empate = resolver(candidatos)
            ordenes = {c.movimiento_id: c.orden for c in candidatos}
            empatados.update({m: ordenes[m] for m in en_empate})
            nuevos = [Par(movimiento_id=m, gasto_id=g, regla=regla.nombre) for m, g in unidos]
            sesion.add_all(Conciliacion(**par.model_dump()) for par in nuevos)
            sesion.flush()  # la regla siguiente ya no ve a estos como libres
            pares.extend(nuevos)
        sin_conciliar = sesion.execute(_SIN_CONCILIAR, periodo).scalar_one()
    unidos_al_final = {par.movimiento_id for par in pares}
    return Cruce(
        pares=tuple(pares),
        por_regla={r.nombre: sum(p.regla == r.nombre for p in pares) for r in REGLAS},
        empatados=tuple(
            sorted((m for m in empatados if m not in unidos_al_final), key=empatados.__getitem__)
        ),
        sin_conciliar=sin_conciliar,
    )
