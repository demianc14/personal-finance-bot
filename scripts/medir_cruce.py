"""Mide las reglas de cruce contra `evals/pares_verdaderos.csv`. Todo es sintético.

    python scripts/medir_cruce.py

Arma la base sintética (`estado_sintetico.base_sintetica`) en una carpeta
temporal, concilia mes por mes, del más viejo al más nuevo (como se usa
`--conciliar`), y compara los pares con la verdad. Después repite todo en una
base nueva con los meses al revés y dice si salieron los mismos pares. No toca
la base real ni llama a ningún LLM. Las reglas no se ajustan mirando esta
salida: ver docs/conciliacion.md.
"""

import tempfile
from datetime import date
from pathlib import Path

import estado_sintetico
from sqlalchemy import text

from family_expense_bot.conciliacion.bancos import cargar_bancos
from family_expense_bot.conciliacion.medicion import (
    FilaVerdad,
    ParHecho,
    leer_pares,
    medir,
    tabla_casos,
    tabla_reglas,
)
from family_expense_bot.conciliacion.reglas import REGLAS, conciliar
from family_expense_bot.storage.repository import crear_engine

PARES = Path(__file__).resolve().parents[1] / "evals" / "pares_verdaderos.csv"

_HECHOS = text(
    """
    SELECT strftime('%Y-%m', e.periodo_desde), m.linea, c.gasto_id, c.regla, m.id
    FROM conciliaciones c
    JOIN movimientos m ON m.id = c.movimiento_id
    JOIN estados_cuenta e ON e.id = m.estado_id
    WHERE c.anulado_en IS NULL
    ORDER BY e.periodo_desde, m.linea
    """
)
_SIN_CONCILIAR = text(
    """
    SELECT count(*) FROM movimientos m
    WHERE m.tipo = 'debito' AND m.monto_centavos < 0 AND m.anulado_en IS NULL
      AND NOT EXISTS (
          SELECT 1 FROM conciliaciones c
          WHERE c.movimiento_id = m.id AND c.anulado_en IS NULL
      )
    """
)
_LINEA = text(
    """
    SELECT strftime('%Y-%m', e.periodo_desde), m.linea
    FROM movimientos m JOIN estados_cuenta e ON e.id = m.estado_id WHERE m.id = :id
    """
)


def _fila(fila: FilaVerdad | None) -> str:
    if fila is None:
        return "sin fila en la verdad"
    gastos = "|".join(map(str, fila.gastos)) or "ninguno"
    return f"{fila.periodo} línea {fila.linea} ({fila.caso}), gasto verdadero {gastos}"


def _corrida(
    meses: list[tuple[date, date]],
) -> tuple[int, int, list[ParHecho], list[tuple[str, int]], int]:
    """Base nueva, un `conciliar` por mes en el orden dado. Devuelve gastos, movimientos,
    los pares que quedaron, los movimientos en empate y los débitos sin conciliar."""
    with tempfile.TemporaryDirectory() as temporal:
        ruta = Path(temporal) / "sintetica.db"
        gastos, movimientos = estado_sintetico.base_sintetica(ruta)
        engine = crear_engine(f"sqlite:///{ruta}")
        en_empate: dict[int, None] = {}
        for desde, hasta in meses:
            cruce = conciliar(engine, cargar_bancos().genericas, desde, hasta)
            en_empate.update(dict.fromkeys(cruce.empatados))
        with engine.connect() as con:
            hechos = [
                ParHecho(p, linea, g, regla) for p, linea, g, regla, _ in con.execute(_HECHOS)
            ]
            unidos = {fila[4] for fila in con.execute(_HECHOS)}
            empatados = [
                (str(periodo), int(linea))
                for m in en_empate
                if m not in unidos
                for periodo, linea in [con.execute(_LINEA, {"id": m}).one()]
            ]
            sin_conciliar = con.execute(_SIN_CONCILIAR).scalar_one()
        engine.dispose()
    return gastos, movimientos, hechos, sorted(empatados), sin_conciliar


def main() -> None:
    meses = [(desde, hasta) for _, desde, hasta, _ in estado_sintetico.ESTADOS_BASE]
    gastos, movimientos, hechos, empatados, sin_conciliar = _corrida(meses)
    _, _, al_reves, _, _ = _corrida(meses[::-1])

    verdad = leer_pares(PARES)
    medicion = medir(verdad, hechos, [r.nombre for r in REGLAS])
    print(f"Base sintética: {gastos} gastos y {movimientos} movimientos; {len(verdad)} filas.")
    print(f"Conciliado mes por mes, de {meses[0][0]:%Y-%m} a {meses[-1][0]:%Y-%m}.\n")
    print(tabla_reglas(medicion) + "\n")
    print(tabla_casos(medicion) + "\n")
    print(f"Débitos sin conciliar: {sin_conciliar}; por empate: {len(empatados)}.")
    for periodo, linea in empatados:
        print(f"  - empate: {periodo} línea {linea}")
    print(
        f"\nAbstenciones y fallos esperados (no entran al recall): {len(medicion.fallos_esperados)}"
    )
    for fila, hecho in medicion.fallos_esperados:
        resultado = f"unido al gasto {hecho.gasto_id} por {hecho.regla}" if hecho else "sin unir"
        print(f"  - {_fila(fila)}: {resultado}")
    print(f"\nPares equivocados: {len(medicion.equivocados)}")
    for hecho, fila in medicion.equivocados:
        print(f"  - gasto {hecho.gasto_id} por {hecho.regla}; {_fila(fila)}")
    print(f"\nPares que faltaron: {len(medicion.perdidos)}")
    for fila in medicion.perdidos:
        print(f"  - {_fila(fila)}")
    distintos = set(hechos) ^ set(al_reves)
    print(f"\nCon los meses al revés: {len(distintos)} pares distintos.")
    for hecho in sorted(distintos, key=lambda h: (h.periodo, h.linea, h.gasto_id)):
        donde = "solo del más viejo al más nuevo" if hecho in hechos else "solo al revés"
        print(f"  - {hecho.periodo} línea {hecho.linea} con el gasto {hecho.gasto_id}: {donde}")


if __name__ == "__main__":
    main()
