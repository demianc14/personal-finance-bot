"""El estado de cuenta cuadra o no entra.

`verificar` recibe un estado ya leído y los estados vigentes de la base, y
devuelve todos los problemas (cualquiera rechaza el estado completo) y los
avisos (no rechazan). Es una función pura: no lee archivos ni toca la base.
Todo en centavos enteros.

Qué alcanza a ver, según lo que traiga el banco:

- **Con columna de saldo** se verifica fila por fila: una fila perdida, repetida
  o con el monto cambiado rompe la cadena en esa línea.
- **Sin columna de saldo** solo se verifica el total. Una fila perdida se nota
  porque el total no cuadra, pero no se sabe cuál; y dos errores que se
  compensan (+1 y -1 centavo) pasan sin que nada los delate.
"""

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, timedelta

from pydantic import BaseModel, ConfigDict

from family_expense_bot.conciliacion.contrato import (
    EstadoLeido,
    EstadoVigente,
    MovimientoLeido,
    Problema,
    plata,
)


class Veredicto(BaseModel):
    model_config = ConfigDict(frozen=True)

    problemas: tuple[Problema, ...] = ()
    """Con uno solo, el estado se rechaza completo."""
    avisos: tuple[str, ...] = ()
    """Para que una persona los mire. No rechazan."""

    @property
    def cuadra(self) -> bool:
        return not self.problemas


def verificar(estado: EstadoLeido, vigentes: Sequence[EstadoVigente] = ()) -> Veredicto:
    repetido = next((v for v in vigentes if v.hash_archivo == estado.hash_archivo), None)
    if repetido is not None:
        # El mismo archivo: no hay nada más que decir de él.
        mensaje = (
            f"Este archivo ya se importó: es el estado #{repetido.id} "
            f"({repetido.banco}/{repetido.cuenta}, {_periodo(repetido)})."
        )
        return Veredicto(problemas=(Problema(codigo="archivo_repetido", mensaje=mensaje),))

    de_la_cuenta = [v for v in vigentes if (v.banco, v.cuenta) == (estado.banco, estado.cuenta)]
    problemas = [
        *_periodos_solapados(estado, de_la_cuenta),
        *_fechas_fuera_del_periodo(estado),
        *_saldo_corrido(estado),
    ]
    no_cuadra = _saldo_no_cuadra(estado)
    if no_cuadra is not None:
        problemas.append(no_cuadra)

    rotas, avisos = _continuidad(estado, de_la_cuenta)
    problemas.extend(rotas)
    problemas.extend(_corte_repetido(estado, de_la_cuenta))
    bloque = _bloque_duplicado(estado.movimientos)
    if bloque is not None:
        primero, segundo = bloque
        repite = (
            f"Las líneas {segundo[0].linea} a {segundo[-1].linea} repiten las líneas "
            f"{primero[0].linea} a {primero[-1].linea}"
        )
        if any(p.codigo.startswith("saldo") for p in problemas):
            suma = sum(m.monto_centavos for m in segundo)
            explica = ", justo la diferencia" if suma == -_diferencia(estado) else ""
            problemas.append(
                Problema(
                    codigo="bloque_duplicado",
                    linea=segundo[0].linea,
                    mensaje=f"{repite} y suman {plata(suma)}{explica}.",
                )
            )
        else:
            avisos.append(
                f"{repite}. El estado cuadra, así que entraron como movimientos reales: "
                "revisa que no sea un bloque pegado dos veces."
            )
    return Veredicto(problemas=tuple(problemas), avisos=tuple(avisos))


def _periodo(estado: EstadoLeido | EstadoVigente) -> str:
    return f"{estado.periodo_desde} a {estado.periodo_hasta}"


def _diferencia(estado: EstadoLeido) -> int:
    """Saldo final menos el que sale de sumar los movimientos al saldo anterior."""
    calculado = estado.saldo_anterior_centavos + sum(m.monto_centavos for m in estado.movimientos)
    return estado.saldo_final_centavos - calculado


def _saldo_no_cuadra(estado: EstadoLeido) -> Problema | None:
    """Saldo anterior + créditos - débitos = saldo final, al centavo."""
    diferencia = _diferencia(estado)
    if diferencia == 0:
        return None
    calculado = estado.saldo_final_centavos - diferencia
    return Problema(
        codigo="saldo_no_cuadra",
        mensaje=f"El estado no cuadra: saldo anterior {plata(estado.saldo_anterior_centavos)} + "
        f"créditos {plata(estado.creditos_centavos)} - débitos {plata(estado.debitos_centavos)} "
        f"= {plata(calculado)}, pero el saldo final es {plata(estado.saldo_final_centavos)} "
        f"(diferencia {plata(diferencia)}).",
    )


def _saldo_corrido(estado: EstadoLeido) -> list[Problema]:
    """Con columna de saldo, cada fila encadena con la anterior y la última da el saldo final."""
    problemas = []
    saldo = estado.saldo_anterior_centavos
    ultimo: MovimientoLeido | None = None
    for m in estado.movimientos:
        if m.saldo_centavos is None:
            continue
        esperado = saldo + m.monto_centavos
        if esperado != m.saldo_centavos:
            operacion = "+" if m.monto_centavos > 0 else "-"
            problemas.append(
                Problema(
                    codigo="saldo_corrido",
                    linea=m.linea,
                    mensaje=f"Línea {m.linea}: el saldo no encadena: {plata(saldo)} {operacion} "
                    f"{plata(abs(m.monto_centavos))} = {plata(esperado)}, pero la fila dice "
                    f"{plata(m.saldo_centavos)}.",
                )
            )
        # Sigue desde lo que dice la fila: así un error se reporta una vez, donde está.
        saldo, ultimo = m.saldo_centavos, m
    if ultimo is not None and ultimo.saldo_centavos != estado.saldo_final_centavos:
        assert ultimo.saldo_centavos is not None
        problemas.append(
            Problema(
                codigo="saldo_final",
                mensaje=f"El saldo final ({plata(estado.saldo_final_centavos)}) no coincide con "
                f"el de la última fila ({plata(ultimo.saldo_centavos)}, línea {ultimo.linea}).",
            )
        )
    return problemas


def _fechas_fuera_del_periodo(estado: EstadoLeido) -> list[Problema]:
    return [
        Problema(
            codigo="fecha_fuera_de_periodo",
            linea=m.linea,
            mensaje=f"Línea {m.linea}: la fecha {m.fecha} está fuera del periodo "
            f"{_periodo(estado)}.",
        )
        for m in estado.movimientos
        if not estado.periodo_desde <= m.fecha <= estado.periodo_hasta
    ]


def _periodos_solapados(estado: EstadoLeido, de_la_cuenta: list[EstadoVigente]) -> list[Problema]:
    """El mismo mes exportado de nuevo trae otro hash: lo delata el periodo."""
    return [
        Problema(
            codigo="periodo_solapado",
            mensaje=f"El periodo {_periodo(estado)} se cruza con el del estado #{v.id} "
            f"({_periodo(v)}) de la misma cuenta.",
        )
        for v in de_la_cuenta
        if v.periodo_desde <= estado.periodo_hasta
        and estado.periodo_desde <= v.periodo_hasta
        and _dia_de_corte(estado, v) is None
    ]


def _dia_de_corte(estado: EstadoLeido, otro: EstadoVigente) -> date | None:
    """El día que comparten si uno empieza justo el día en que termina el otro, y nada más.

    Un banco que cuenta el día de corte en los dos estados (Pichincha) deja dos
    estados seguidos con un día en común. No es un solape: qué movimientos de ese
    día van en cada uno lo dice el resumen de cada estado (decidido el 2026-11-24).
    """
    if otro.periodo_hasta == estado.periodo_desde and otro.periodo_desde < estado.periodo_desde:
        return estado.periodo_desde
    if estado.periodo_hasta == otro.periodo_desde and estado.periodo_desde < otro.periodo_desde:
        return estado.periodo_hasta
    return None


def _corte_repetido(estado: EstadoLeido, de_la_cuenta: list[EstadoVigente]) -> list[Problema]:
    """El mismo movimiento (fecha, monto y descripción) en el día de corte de dos estados."""
    problemas = []
    for v in de_la_cuenta:
        dia = _dia_de_corte(estado, v)
        if dia is None:
            continue
        del_otro = {(m.monto_centavos, m.descripcion) for m in v.de_los_cortes if m.fecha == dia}
        for m in estado.movimientos:
            if m.fecha == dia and (m.monto_centavos, m.descripcion) in del_otro:
                problemas.append(
                    Problema(
                        codigo="corte_repetido",
                        linea=m.linea,
                        mensaje=f"Línea {m.linea}: el movimiento del {dia} por "
                        f"{plata(m.monto_centavos)} ya está en el estado #{v.id} ({_periodo(v)}), "
                        "que también cubre ese día de corte.",
                    )
                )
    return problemas


def _continuidad(
    estado: EstadoLeido, de_la_cuenta: list[EstadoVigente]
) -> tuple[list[Problema], list[str]]:
    """El saldo final de un estado es el anterior del siguiente, o el estado no entra.

    Se compara con el vecino más cercano de cada lado. Si es contiguo y el saldo no
    encadena, se rechaza; si entre los dos faltan días, no hay con qué comparar y
    solo se avisa. Con un periodo solapado no se mira: ya se rechaza por eso.

    No ve un cargo y su reverso inventados (suman cero): ver docs/conciliacion.md.
    """
    problemas: list[Problema] = []
    avisos: list[str] = []
    un_dia = timedelta(days=1)
    # Un vecino que comparte el día de corte también es contiguo: se mira su saldo.
    antes = [
        v
        for v in de_la_cuenta
        if v.periodo_hasta < estado.periodo_desde or _dia_de_corte(estado, v) == v.periodo_hasta
    ]
    despues = [
        v
        for v in de_la_cuenta
        if v.periodo_desde > estado.periodo_hasta or _dia_de_corte(estado, v) == v.periodo_desde
    ]
    if antes:
        v = max(antes, key=lambda e: (e.periodo_hasta, e.id))
        if v.periodo_hasta + un_dia < estado.periodo_desde:
            avisos.append(
                f"Entre el estado #{v.id} ({_periodo(v)}) y este faltan los días "
                f"{v.periodo_hasta + un_dia} a {estado.periodo_desde - un_dia}: la continuidad "
                "del saldo no se pudo verificar."
            )
        elif diferencia := estado.saldo_anterior_centavos - v.saldo_final_centavos:
            problemas.append(
                Problema(
                    codigo="continuidad",
                    mensaje=f"El estado #{v.id} ({_periodo(v)}) cerró con "
                    f"{plata(v.saldo_final_centavos)} y este empieza con "
                    f"{plata(estado.saldo_anterior_centavos)}: hay {plata(diferencia)} de "
                    "diferencia entre los dos.",
                )
            )
    if despues:
        v = min(despues, key=lambda e: (e.periodo_desde, e.id))
        if estado.periodo_hasta + un_dia < v.periodo_desde:
            avisos.append(
                f"Entre este estado y el #{v.id} ({_periodo(v)}) faltan los días "
                f"{estado.periodo_hasta + un_dia} a {v.periodo_desde - un_dia}: la continuidad "
                "del saldo no se pudo verificar."
            )
        elif diferencia := v.saldo_anterior_centavos - estado.saldo_final_centavos:
            problemas.append(
                Problema(
                    codigo="continuidad",
                    mensaje=f"Este estado cierra con {plata(estado.saldo_final_centavos)} y el "
                    f"#{v.id} ({_periodo(v)}) empieza con {plata(v.saldo_anterior_centavos)}: "
                    f"hay {plata(diferencia)} de diferencia entre los dos.",
                )
            )
    return problemas, avisos


_BASE_HASH = 1_000_003
_MODULO_HASH = (1 << 61) - 1


def _bloque_duplicado(
    movimientos: Sequence[MovimientoLeido],
) -> tuple[Sequence[MovimientoLeido], Sequence[MovimientoLeido]] | None:
    """El primer bloque de dos o más filas seguidas que se repite justo después.

    Compara fecha, descripción y monto. Una sola fila repetida no cuenta: dos
    cargos idénticos el mismo día son dos movimientos.

    Un bloque de dos o más filas que se repite empieza con el mismo par de filas
    seguidas: solo se prueban esas posiciones, no todas las de cada fila igual. Y
    dos tramos se comparan por un hash de prefijos, en tiempo constante; si el
    hash coincide se comparan de verdad, así que una colisión no da un falso bloque.
    """
    ids: dict[tuple[object, ...], int] = {}
    filas = [
        ids.setdefault((m.fecha, m.descripcion, m.monto_centavos), len(ids) + 1)
        for m in movimientos
    ]
    n = len(filas)
    prefijo = [0] * (n + 1)
    potencia = [1] * (n + 1)
    for k, fila in enumerate(filas):
        prefijo[k + 1] = (prefijo[k] * _BASE_HASH + fila) % _MODULO_HASH
        potencia[k + 1] = (potencia[k] * _BASE_HASH) % _MODULO_HASH

    def tramo(desde: int, hasta: int) -> int:
        return (prefijo[hasta] - prefijo[desde] * potencia[hasta - desde]) % _MODULO_HASH

    pares: defaultdict[tuple[int, int], list[int]] = defaultdict(list)
    for k in range(n - 1):
        pares[(filas[k], filas[k + 1])].append(k)
    for i in range(n - 1):
        candidatos = pares[(filas[i], filas[i + 1])]
        # El bloque i..j cabe entero si j + (j - i) <= n, o sea j <= (n + i) // 2.
        # De la repetición más lejana a la más cercana: gana el bloque más largo.
        tope = bisect_right(candidatos, (n + i) // 2)
        for j in reversed(candidatos[:tope]):
            largo = j - i
            if largo < 2:
                break
            if tramo(i, j) == tramo(j, j + largo) and filas[i:j] == filas[j : j + largo]:
                return movimientos[i:j], movimientos[j : j + largo]
    return None
