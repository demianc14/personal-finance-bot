"""Reglas de cruce movimiento ↔ gasto: deterministas y con nombre. Sin LLM.

Solo cruzan los movimientos de tipo `debito` contra gastos del mismo monto, al
centavo. Comisiones, intereses, pagos de tarjeta y todo lo que entra (una
devolución) no son un gasto registrado: nunca cruzan.

`monto_y_fecha` y `monto_3_dias` se deciden juntas. Los candidatos (mismo monto,
hasta tres días de diferencia) salen de SQL y se parten en grupos que no se
tocan entre sí; en cada grupo se busca la asignación uno a uno con más pares y,
entre esas, la de menor distancia total de fechas (ver `resolver`). El nombre de
la regla sale después, de la distancia de cada par: cero días es
`monto_y_fecha`; de uno a tres, `monto_3_dias`. Decidirlas en orden, primero la
misma fecha, era un fallo: el cargo de hoy se llevaba el gasto de hoy aunque
fuera el de ayer, y todos los pares quedaban corridos.

`monto_y_descripcion` (hasta diez días y descripción parecida) está apagada por
defecto: se enciende con `regla_descripcion` en `config/bancos.yaml`. Corre
después, sobre lo que quedó libre.

Se cruzan siempre todos los movimientos libres de la base, no los de un mes: así
el resultado no depende de qué mes se concilia primero. Si la mejor asignación de
un grupo no es única, no se une nadie del grupo. Un movimiento sin par no se
guarda como gasto: proponerlo es otra fase.
"""

import math
import re
from collections import defaultdict
from collections.abc import Iterable
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
TOPE_DISTINTIVAS = 20
"""Más palabras distintivas en común que estas no cuentan más."""


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
    # Apagada por defecto: en la medición de la PR #25 no hizo ningún par y la
    # revisión reprodujo tres falsos positivos por palabras vacías.
    Regla("monto_y_descripcion", dias=10, exige_parecido=True),
)
MISMA_FECHA, POR_MONTO, POR_DESCRIPCION = REGLAS


def palabras(texto: str) -> set[str]:
    """Las palabras que sirven para comparar: sin tildes, sin números y de 4 letras o más."""
    return {p for p in re.findall(r"[a-z]+", normalizar(texto)) if len(p) >= LARGO_MINIMO}


def en_comun(a: str, b: str) -> set[str]:
    return palabras(a) & palabras(b)


def distintivas(a: str, b: str, genericas: frozenset[str]) -> set[str]:
    """Las palabras en común que sí dicen algo del comercio (no están en `genericas`)."""
    return en_comun(a, b) - genericas


def parecidas(a: str, b: str, genericas: frozenset[str]) -> bool:
    """Dos palabras en común, o una sola si es distintiva (no está en `genericas`)."""
    return len(en_comun(a, b)) >= 2 or bool(distintivas(a, b, genericas))


@dataclass(frozen=True)
class Candidato:
    """Un movimiento y un gasto que una regla permite unir."""

    movimiento_id: int
    orden: tuple[int, int]
    """(estado, línea): el orden en que aparecen los movimientos."""
    fecha_movimiento: date
    descripcion: str
    """La del movimiento, tal como la trae el banco."""
    gasto_id: int
    fecha_gasto: date
    dias: int
    distintivas: int
    """Cuántas palabras distintivas comparten las dos descripciones."""


def _asignar(costo: list[list[int]]) -> list[int]:
    """La columna de cada fila en la asignación de costo total mínimo (filas ≤ columnas).

    Es el algoritmo húngaro con potenciales, O(filas² por columnas), en enteros.
    Está escrito acá para no sumar una dependencia: los grupos son chicos (los
    cargos de un mismo monto en días vecinos) y el algoritmo entra en una
    función.
    """
    filas, columnas = len(costo), len(costo[0])
    # Los potenciales solo suman enteros: `math.inf` nunca llega a ellos, porque
    # siempre queda una columna libre con un mínimo finito.
    u: list[float] = [0] * (filas + 1)
    v: list[float] = [0] * (columnas + 1)
    de, camino = [0] * (columnas + 1), [0] * (columnas + 1)
    for fila in range(1, filas + 1):
        de[0] = fila
        j0 = 0
        minimo: list[float] = [math.inf] * (columnas + 1)
        usada = [False] * (columnas + 1)
        while True:
            usada[j0] = True
            i0, j1 = de[j0], 0
            delta = math.inf
            for j in range(1, columnas + 1):
                if usada[j]:
                    continue
                actual = costo[i0 - 1][j - 1] - u[i0] - v[j]
                if actual < minimo[j]:
                    minimo[j], camino[j] = actual, j0
                if minimo[j] < delta:
                    delta, j1 = minimo[j], j
            for j in range(columnas + 1):
                if usada[j]:
                    u[de[j]] += delta
                    v[j] -= delta
                else:
                    minimo[j] -= delta
            j0 = j1
            if de[j0] == 0:
                break
        while j0:
            j1 = camino[j0]
            de[j0] = de[j1]
            j0 = j1
    resultado = [0] * filas
    for j in range(1, columnas + 1):
        if de[j]:
            resultado[de[j] - 1] = j - 1
    return resultado


def _gemelo(c: Candidato) -> tuple[date, str]:
    """Dos movimientos del mismo monto son intercambiables solo con la misma fecha y descripción."""
    return c.fecha_movimiento, " ".join(normalizar(c.descripcion).split())


def _grupos(candidatos: list[Candidato]) -> list[list[Candidato]]:
    """Los candidatos partidos en grupos que no comparten ni movimientos ni gastos."""
    padre: dict[tuple[str, int], tuple[str, int]] = {}

    def raiz(nodo: tuple[str, int]) -> tuple[str, int]:
        while padre.setdefault(nodo, nodo) != nodo:
            padre[nodo] = padre[padre[nodo]]
            nodo = padre[nodo]
        return nodo

    for c in candidatos:
        padre[raiz(("m", c.movimiento_id))] = raiz(("g", c.gasto_id))
    grupos: dict[tuple[str, int], list[Candidato]] = defaultdict(list)
    for c in candidatos:
        grupos[raiz(("m", c.movimiento_id))].append(c)
    return list(grupos.values())


def _resolver_grupo(grupo: list[Candidato]) -> list[Candidato] | None:
    """Los pares de un grupo, o None si la mejor asignación no es única."""
    de_movimiento = {c.movimiento_id: c for c in grupo}
    movimientos = sorted(de_movimiento, key=lambda m: de_movimiento[m].orden)
    gastos = sorted({c.gasto_id for c in grupo})
    candidato = {(c.movimiento_id, c.gasto_id): c for c in grupo}
    n = len(movimientos)

    # Un solo número por par que ordena igual que (días, -distintivas, días²):
    # cada peso es mayor que todo lo que pueden sumar los criterios que le siguen.
    peso_palabras = max(c.dias for c in grupo) ** 2 * n + 1
    peso_dias = peso_palabras * (TOPE_DISTINTIVAS * n + 1)

    def peso(c: Candidato) -> int:
        sin_parecido = TOPE_DISTINTIVAS - min(c.distintivas, TOPE_DISTINTIVAS)
        return c.dias * peso_dias + sin_parecido * peso_palabras + c.dias**2

    # Dejar un movimiento sin par cuesta más que cualquier suma de pares: primero
    # se maximiza cuántos se unen. Cada movimiento tiene su columna "sin par".
    sin_par = sum(peso(c) for c in grupo) + 1
    prohibido = sin_par * n + 1
    costo = [
        [peso(candidato[m, g]) if (m, g) in candidato else prohibido for g in gastos]
        + [sin_par] * n
        for m in movimientos
    ]

    def total(matriz: list[list[int]]) -> tuple[int, list[int]]:
        asignacion = _asignar(matriz)
        return sum(matriz[i][j] for i, j in enumerate(asignacion)), asignacion

    optimo, mejor = total(costo)
    gemelos: dict[tuple[date, str], list[int]] = defaultdict(list)
    for i, m in enumerate(movimientos):
        gemelos[_gemelo(de_movimiento[m])].append(i)
    unidos = [(i, j) for i, j in enumerate(mejor) if j < len(gastos)]

    # ¿Es la única? Para cada par se le prohíbe ese gasto a ese movimiento y a
    # sus gemelos y se resuelve de nuevo: si sale otra asignación igual de buena,
    # hay dos respuestas distintas y elegir sería adivinar.
    for clave, j in sorted({(_gemelo(de_movimiento[movimientos[i]]), j) for i, j in unidos}):
        sin_ese_par = [list(fila) for fila in costo]
        for i in gemelos[clave]:
            sin_ese_par[i][j] = prohibido
        if total(sin_ese_par)[0] == optimo:
            return None

    # Entre gemelos da igual cuál se queda con cada gasto: van en orden (línea ↔
    # fecha e id del gasto), y si sobran, los últimos quedan sin conciliar.
    pares: list[Candidato] = []
    for indices in gemelos.values():
        primero = movimientos[indices[0]]
        suyos = sorted(
            (gastos[j] for i, j in unidos if i in indices),
            key=lambda g: (candidato[primero, g].fecha_gasto, g),
        )
        pares.extend(candidato[movimientos[i], g] for i, g in zip(indices, suyos, strict=False))
    return pares


def resolver(candidatos: Iterable[Candidato]) -> tuple[list[Candidato], list[int]]:
    """Decide los pares. Devuelve (pares, movimientos de los grupos que quedaron en empate).

    Los candidatos se parten en grupos que no comparten movimientos ni gastos
    (en la práctica, los cargos y gastos de un mismo monto en días vecinos). En
    cada grupo se elige la asignación uno a uno que, en este orden:

    1. une más pares;
    2. suma menos días entre cada movimiento y su gasto;
    3. suma más palabras distintivas en común (la descripción desempata);
    4. suma menos días al cuadrado: entre dos asignaciones con la misma suma
       gana la que respeta el orden de las fechas. Dos gastos del lunes y el
       martes cobrados el miércoles y el jueves suman cuatro días en orden o
       cruzados; en orden son 2² + 2² = 8 y cruzados 3² + 1² = 10. Con una
       función estrictamente convexa, descruzar dos pares siempre baja la suma.

    Si otra asignación distinta empata en los cuatro, no se une nadie del grupo:
    un par mal unido esconde un gasto sin registrar, y uno sin unir solo queda
    pendiente. Dos movimientos de la misma fecha y la misma descripción son
    gemelos: intercambiarlos no cuenta como otra asignación. Dos cargos iguales
    de comercios distintos no lo son.

    Es un problema de asignación de costo mínimo y se resuelve exacto con el
    algoritmo húngaro (`_asignar`). Ir par por par, cada uno con su más cercano,
    no alcanza: con el mismo gasto todos los días, el más cercano de cada cargo
    es el del mismo día y lo correcto es el de la víspera.
    """
    pares: list[Candidato] = []
    empatados: list[Candidato] = []
    for grupo in _grupos(list(candidatos)):
        resueltos = _resolver_grupo(grupo)
        if resueltos is None:
            empatados.extend(grupo)
        else:
            pares.extend(resueltos)
    orden = {c.movimiento_id: c.orden for c in empatados}
    return sorted(pares, key=lambda c: c.orden), sorted(orden, key=orden.__getitem__)


class Par(BaseModel):
    model_config = ConfigDict(frozen=True)

    movimiento_id: int
    gasto_id: int
    regla: str


class Cruce(BaseModel):
    """Lo que hizo una corrida de `conciliar`, visto desde el periodo pedido."""

    model_config = ConfigDict(frozen=True)

    pares: tuple[Par, ...]
    """Los pares nuevos con el movimiento dentro del periodo, en el orden de las reglas."""
    por_regla: dict[str, int]
    """Cuántos de esos pares hizo cada regla activa."""
    pares_fuera_del_periodo: int
    """Pares nuevos que también se guardaron, con el movimiento fuera del periodo."""
    empatados: tuple[int, ...]
    """Movimientos del periodo con candidatos que no se unieron porque hubo empate."""
    sin_conciliar: int
    """Débitos del periodo que quedaron sin par, empatados incluidos."""


_TEXTO_GASTO = "g.descripcion || ' ' || coalesce(g.subcategoria, '') || ' ' || g.texto_original"
_LIBRE = """
    m.tipo = 'debito' AND m.monto_centavos < 0
    AND m.anulado_en IS NULL AND e.anulado_en IS NULL
    AND NOT EXISTS (
        SELECT 1 FROM conciliaciones c
        WHERE c.movimiento_id = m.id AND c.anulado_en IS NULL
    )
"""
_CANDIDATOS = text(
    f"""
    SELECT m.id, m.estado_id, m.linea, m.fecha, m.descripcion, g.id, g.fecha,
           CAST(round(abs(julianday(g.fecha) - julianday(m.fecha))) AS INTEGER),
           palabras_distintivas(m.descripcion, {_TEXTO_GASTO})
    FROM movimientos m
    JOIN estados_cuenta e ON e.id = m.estado_id
    JOIN gastos g ON g.monto_centavos = -m.monto_centavos
    WHERE {_LIBRE}
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
    WHERE {_LIBRE}
      AND (:desde IS NULL OR m.fecha >= :desde) AND (:hasta IS NULL OR m.fecha <= :hasta)
    """
)


def _registrar_funciones(sesion: Session, genericas: frozenset[str]) -> None:
    """El parecido de descripciones, disponible dentro del SQL de esta conexión."""
    conexion: Any = sesion.connection().connection.driver_connection
    conexion.create_function(
        "palabras_distintivas",
        2,
        lambda a, b: len(distintivas(a, b, genericas)),
        deterministic=True,
    )
    conexion.create_function(
        "descripcion_parecida", 2, lambda a, b: int(parecidas(a, b, genericas)), deterministic=True
    )


def conciliar(
    engine: Engine,
    genericas: frozenset[str],
    desde: date | None = None,
    hasta: date | None = None,
    *,
    con_descripcion: bool = False,
) -> Cruce:
    """Une movimientos y gastos libres con las reglas y guarda cada par con su regla.

    Cruza **todos** los movimientos libres de la base, sin mirar el periodo: un
    cargo del 30 de marzo compite por el gasto del 1 de abril con el cargo del 1
    de abril, se concilie el mes que se concilie. `desde` y `hasta` solo acotan
    lo que cuenta el resultado (pares, empates y débitos sin conciliar); los
    pares de otros meses se guardan igual y salen en `pares_fuera_del_periodo`.

    `con_descripcion` enciende `monto_y_descripcion`, que corre después sobre lo
    que quedó libre. Todo va en una transacción: si la base rechaza un par (los
    índices únicos no dejan conciliar algo dos veces), no queda ninguno. Correrla
    de nuevo no duplica nada, y un par anulado libera al movimiento y al gasto
    para volver a cruzarse.
    """

    def en_periodo(c: Candidato) -> bool:
        fecha = c.fecha_movimiento
        return (desde is None or fecha >= desde) and (hasta is None or fecha <= hasta)

    def nombre(fase: Regla, c: Candidato) -> str:
        if fase.exige_parecido:
            return fase.nombre
        return MISMA_FECHA.nombre if c.dias == 0 else POR_MONTO.nombre

    fases = [POR_MONTO, *([POR_DESCRIPCION] if con_descripcion else [])]
    hechos: list[tuple[Par, Candidato]] = []
    empatados: dict[int, tuple[int, int]] = {}
    with Session(engine) as sesion, sesion.begin():
        _registrar_funciones(sesion, genericas)
        for fase in fases:
            filas = sesion.execute(
                _CANDIDATOS, {"dias": fase.dias, "exige_parecido": fase.exige_parecido}
            )
            candidatos = [
                Candidato(
                    movimiento_id=m,
                    orden=(estado, linea),
                    fecha_movimiento=date.fromisoformat(fecha_m),
                    descripcion=descripcion,
                    gasto_id=g,
                    fecha_gasto=date.fromisoformat(fecha_g),
                    dias=dias,
                    distintivas=comunes,
                )
                for m, estado, linea, fecha_m, descripcion, g, fecha_g, dias, comunes in filas
            ]
            unidos, en_empate = resolver(candidatos)
            de_movimiento = {c.movimiento_id: c for c in candidatos}
            empatados.update(
                {m: de_movimiento[m].orden for m in en_empate if en_periodo(de_movimiento[m])}
            )
            nuevos = [
                (Par(movimiento_id=c.movimiento_id, gasto_id=c.gasto_id, regla=nombre(fase, c)), c)
                for c in unidos
            ]
            sesion.add_all(Conciliacion(**par.model_dump()) for par, _ in nuevos)
            sesion.flush()  # la fase siguiente ya no ve a estos como libres
            hechos.extend(nuevos)
        sin_conciliar = sesion.execute(
            _SIN_CONCILIAR,
            {
                "desde": desde.isoformat() if desde else None,
                "hasta": hasta.isoformat() if hasta else None,
            },
        ).scalar_one()
    posicion = {regla.nombre: i for i, regla in enumerate(REGLAS)}
    hechos.sort(key=lambda hecho: (posicion[hecho[0].regla], hecho[1].orden))
    pares = tuple(par for par, c in hechos if en_periodo(c))
    unidos_al_final = {par.movimiento_id for par, _ in hechos}
    return Cruce(
        pares=pares,
        por_regla={
            regla.nombre: sum(par.regla == regla.nombre for par in pares)
            for regla in (MISMA_FECHA, *fases)
        },
        pares_fuera_del_periodo=len(hechos) - len(pares),
        empatados=tuple(
            sorted((m for m in empatados if m not in unidos_al_final), key=empatados.__getitem__)
        ),
        sin_conciliar=sin_conciliar,
    )
