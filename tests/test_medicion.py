"""La cuenta de precisión y recall, con una verdad inventada acá mismo.

No usa `evals/pares_verdaderos.csv` ni corre las reglas: solo prueba que se
cuenta bien.
"""

from pathlib import Path

from family_expense_bot.conciliacion.medicion import (
    FilaVerdad,
    ParHecho,
    leer_pares,
    medir,
    tabla_casos,
    tabla_reglas,
)

REGLAS = ["uno", "dos"]
VERDAD = [
    FilaVerdad("2026-03", 2, (1,), "mismo_dia", False),
    FilaVerdad("2026-03", 3, (2,), "mismo_dia", False),
    FilaVerdad("2026-03", 4, (3,), "a_dos_dias", False),
    FilaVerdad("2026-03", 5, (), "sin_gasto", False),
    FilaVerdad("2026-03", 6, (4, 5), "partido", True),
    FilaVerdad(None, None, (6,), "gasto_sin_movimiento", False),
]


def test_leer_pares(tmp_path: Path) -> None:
    ruta = tmp_path / "pares.csv"
    ruta.write_text(
        "estado,movimiento,gasto_id,categoria_caso,fallo_esperado\n"
        "2026-03,2,1,mismo_dia,no\n"
        "2026-03,5,ninguno,sin_gasto,no\n"
        "2026-03,6,4|5,partido,si\n"
        "ninguno,ninguno,6,gasto_sin_movimiento,no\n",
        encoding="utf-8",
    )
    assert leer_pares(ruta) == [VERDAD[0], VERDAD[3], VERDAD[4], VERDAD[5]]


def test_todo_bien() -> None:
    hechos = [
        ParHecho("2026-03", 2, 1, "uno"),
        ParHecho("2026-03", 3, 2, "uno"),
        ParHecho("2026-03", 4, 3, "dos"),
    ]
    m = medir(VERDAD, hechos, REGLAS)
    assert [(c.unidos, c.correctos, c.precision, c.recall) for c in m.por_regla] == [
        (2, 2, 1.0, 2 / 3),
        (1, 1, 1.0, 1 / 3),
    ]
    assert (m.total.esperados, m.total.unidos, m.total.correctos) == (3, 3, 3)
    assert (m.total.precision, m.total.recall) == (1.0, 1.0)
    assert m.equivocados == () and m.perdidos == ()
    assert m.fallos_esperados == ((VERDAD[4], None),)


def test_un_par_equivocado_baja_la_precision_y_uno_perdido_el_recall() -> None:
    hechos = [
        ParHecho("2026-03", 2, 1, "uno"),
        ParHecho("2026-03", 4, 6, "dos"),  # se llevó al gasto sin movimiento
        ParHecho("2026-03", 5, 2, "dos"),  # un movimiento sin gasto, unido
    ]
    m = medir(VERDAD, hechos, REGLAS)
    assert (m.total.unidos, m.total.correctos) == (3, 1)
    assert m.total.precision == 1 / 3 and m.total.recall == 1 / 3
    assert [f.linea for f in m.perdidos] == [3, 4]
    assert [h.linea for h, _ in m.equivocados] == [4, 5]
    casos = {c.nombre: (c.filas, c.esperados, c.unidos, c.correctos) for c in m.por_caso}
    assert casos == {
        "mismo_dia": (2, 2, 1, 1),
        "a_dos_dias": (1, 1, 1, 0),
        "sin_gasto": (1, 0, 1, 0),
        "partido": (1, 0, 0, 0),
        "gasto_sin_movimiento": (1, 0, 1, 0),
    }


def test_unir_un_pago_partido_con_uno_de_sus_gastos_no_es_correcto() -> None:
    m = medir(VERDAD, [ParHecho("2026-03", 6, 4, "dos")], REGLAS)
    assert m.total.correctos == 0
    assert m.fallos_esperados == ((VERDAD[4], ParHecho("2026-03", 6, 4, "dos")),)


def test_las_tablas_muestran_raya_cuando_no_hay_denominador() -> None:
    m = medir(VERDAD, [ParHecho("2026-03", 2, 1, "uno")], REGLAS)
    assert "| `uno` | 1 | 1 | 100% | 1 de 3 (33%) |" in tabla_reglas(m)
    assert "| `dos` | 0 | 0 | — | 0 de 3 (0%) |" in tabla_reglas(m)
    assert "| Total | 1 | 1 | 100% | 1 de 3 (33%) |" in tabla_reglas(m)
    assert "| `sin_gasto` | 1 | 0 | 0 | 0 | — | — |" in tabla_casos(m)
