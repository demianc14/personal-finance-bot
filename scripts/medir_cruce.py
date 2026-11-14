"""Mide las reglas de cruce contra `evals/pares_verdaderos.csv`. Todo es sintético.

    python scripts/medir_cruce.py

Arma la base sintética (`estado_sintetico.base_sintetica`) en una carpeta
temporal, corre `conciliar` sobre todo y compara los pares con la verdad. No
toca la base real ni llama a ningún LLM. Las reglas no se ajustan mirando esta
salida: ver docs/conciliacion.md.
"""

import tempfile
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


def main() -> None:
    with tempfile.TemporaryDirectory() as temporal:
        ruta = Path(temporal) / "sintetica.db"
        gastos, movimientos = estado_sintetico.base_sintetica(ruta)
        engine = crear_engine(f"sqlite:///{ruta}")
        cruce = conciliar(engine, cargar_bancos().genericas)
        with engine.connect() as con:
            hechos = [
                ParHecho(p, linea, g, regla) for p, linea, g, regla, _ in con.execute(_HECHOS)
            ]
            empatados = [tuple(con.execute(_LINEA, {"id": m}).one()) for m in cruce.empatados]
        engine.dispose()

    verdad = leer_pares(PARES)
    medicion = medir(verdad, hechos, [r.nombre for r in REGLAS])
    print(f"Base sintética: {gastos} gastos y {movimientos} movimientos; {len(verdad)} filas.\n")
    print(tabla_reglas(medicion), "\n")
    print(tabla_casos(medicion), "\n")
    print(f"Débitos sin conciliar: {cruce.sin_conciliar}; por empate: {len(empatados)}.")
    for periodo, linea in empatados:
        print(f"  - empate: {periodo} línea {linea}")
    print(f"\nFallos esperados (no entran al recall): {len(medicion.fallos_esperados)}")
    for fila, hecho in medicion.fallos_esperados:
        resultado = f"unido al gasto {hecho.gasto_id} por {hecho.regla}" if hecho else "sin unir"
        print(f"  - {_fila(fila)}: {resultado}")
    print(f"\nPares equivocados: {len(medicion.equivocados)}")
    for hecho, fila in medicion.equivocados:
        print(f"  - gasto {hecho.gasto_id} por {hecho.regla}; {_fila(fila)}")
    print(f"\nPares que faltaron: {len(medicion.perdidos)}")
    for fila in medicion.perdidos:
        print(f"  - {_fila(fila)}")


if __name__ == "__main__":
    main()
