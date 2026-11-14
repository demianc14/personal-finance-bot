"""Reglas de cruce movimiento ↔ gasto: deterministas, en orden y con nombre. Sin LLM.

Cada test arma su propia base en memoria con datos inventados. Ninguno usa
`evals/pares_verdaderos.csv`: ese set solo se mira al medir.
"""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from family_expense_bot import cli
from family_expense_bot.conciliacion.bancos import cargar_bancos
from family_expense_bot.conciliacion.reglas import (
    REGLAS,
    Candidato,
    conciliar,
    en_comun,
    palabras,
    parecidas,
    resolver,
)
from family_expense_bot.storage.models import Conciliacion, EstadoCuenta, Gasto, Movimiento
from family_expense_bot.storage.repository import crear_engine

GENERICAS = frozenset({"compra", "pago", "debito", "tienda", "tarjeta"})


@pytest.fixture
def engine() -> Engine:
    return crear_engine("sqlite://")


def _estado(engine: Engine, numero: int = 1) -> int:
    with Session(engine) as sesion, sesion.begin():
        fila = EstadoCuenta(
            banco="banco_sintetico",
            cuenta="ahorros",
            periodo_desde=date(2026, 1, 1),
            periodo_hasta=date(2026, 12, 31),
            saldo_anterior_centavos=0,
            saldo_final_centavos=0,
            hash_archivo=f"{numero:064d}",
        )
        sesion.add(fila)
        sesion.flush()
        return fila.id


class Base:
    """Atajos para sembrar una base de prueba con un estado ya creado."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.estado = _estado(engine)
        self._linea = 1

    def mov(
        self, dia: int, centavos: int, descripcion: str = "MOVIMIENTO", tipo: str = "debito"
    ) -> int:
        self._linea += 1
        with Session(self.engine) as sesion, sesion.begin():
            fila = Movimiento(
                estado_id=self.estado,
                linea=self._linea,
                fecha=date(2026, 3, dia),
                descripcion=descripcion,
                monto_centavos=centavos,
                tipo=tipo,
            )
            sesion.add(fila)
            sesion.flush()
            return fila.id

    def gasto(
        self,
        dia: int,
        centavos: int,
        descripcion: str = "algo",
        subcategoria: str | None = None,
        texto: str | None = None,
    ) -> int:
        with Session(self.engine) as sesion, sesion.begin():
            fila = Gasto(
                monto_centavos=centavos,
                categoria="otros",
                atribuido_a="demian",
                fecha=date(2026, 3, dia),
                descripcion=descripcion,
                subcategoria=subcategoria,
                texto_original=texto if texto is not None else descripcion,
                registrado_por=1,
                confianza_monto="alta",
                confianza_categoria="alta",
                confianza_fecha="alta",
                confianza_atribuido_a="alta",
            )
            sesion.add(fila)
            sesion.flush()
            return fila.id

    def anular(self, tabla: str, id_fila: int) -> None:
        with self.engine.begin() as con:
            con.execute(
                text(
                    f"UPDATE {tabla} SET anulado_en = '2026-04-01', motivo_anulacion = 'prueba' "
                    "WHERE id = :id"
                ),
                {"id": id_fila},
            )

    def pares(self) -> list[tuple[int, int, str]]:
        """Los pares vigentes guardados: (movimiento, gasto, regla)."""
        with self.engine.connect() as con:
            filas = con.execute(
                text(
                    "SELECT movimiento_id, gasto_id, regla FROM conciliaciones "
                    "WHERE anulado_en IS NULL ORDER BY movimiento_id"
                )
            )
            return [(m, g, r) for m, g, r in filas]

    def conciliar(self, **kwargs: date) -> tuple[list[tuple[int, int, str]], tuple[int, ...]]:
        cruce = conciliar(self.engine, GENERICAS, **kwargs)
        nuevos = [(p.movimiento_id, p.gasto_id, p.regla) for p in cruce.pares]
        return nuevos, cruce.empatados


@pytest.fixture
def base(engine: Engine) -> Base:
    return Base(engine)


def test_las_reglas_van_en_orden_y_con_nombre() -> None:
    assert [(r.nombre, r.dias, r.exige_parecido) for r in REGLAS] == [
        ("monto_y_fecha", 0, False),
        ("monto_3_dias", 3, False),
        ("monto_y_descripcion", 10, True),
    ]


# --- Regla 1: monto exacto y misma fecha ----------------------------------


def test_mismo_monto_y_misma_fecha_se_unen_y_queda_la_regla(base: Base) -> None:
    m, g = base.mov(5, -1550), base.gasto(5, 1550)
    nuevos, empatados = base.conciliar()
    assert nuevos == [(m, g, "monto_y_fecha")]
    assert base.pares() == nuevos
    assert empatados == ()


def test_un_centavo_de_diferencia_no_cruza(base: Base) -> None:
    base.mov(5, -1550)
    base.gasto(5, 1551)
    assert base.conciliar() == ([], ())


@pytest.mark.parametrize(
    ("tipo", "centavos"),
    [("comision", -1550), ("interes", -1550), ("pago", -1550), ("credito", 1550)],
)
def test_solo_cruzan_los_debitos(base: Base, tipo: str, centavos: int) -> None:
    """Comisión, interés, pago de tarjeta y lo que entra (una devolución) no son un gasto."""
    base.mov(5, centavos, tipo=tipo)
    base.gasto(5, 1550)
    assert base.conciliar() == ([], ())


def test_un_interes_ganado_tampoco_cruza(base: Base) -> None:
    base.mov(5, 1550, tipo="interes")
    base.gasto(5, 1550)
    assert base.conciliar() == ([], ())


def test_una_devolucion_no_deshace_el_par_de_la_compra(base: Base) -> None:
    m, g = base.mov(5, -2000, "TIENDA ROPA"), base.gasto(5, 2000, "camisa")
    base.mov(8, 2000, "DEVOLUCION TIENDA ROPA", tipo="credito")
    assert base.conciliar()[0] == [(m, g, "monto_y_fecha")]
    assert base.conciliar() == ([], ())
    assert base.pares() == [(m, g, "monto_y_fecha")]


def test_lo_anulado_no_cruza(base: Base) -> None:
    m1, g1 = base.mov(5, -1000), base.gasto(5, 1000)
    base.anular("movimientos", m1)
    assert base.conciliar() == ([], ())
    m2 = base.mov(5, -1000)
    base.anular("gastos", g1)
    assert base.conciliar() == ([], ())
    assert m2  # sigue libre: su único candidato estaba anulado


def test_los_movimientos_de_un_estado_anulado_no_cruzan(base: Base) -> None:
    base.mov(5, -1000)
    base.gasto(5, 1000)
    base.anular("estados_cuenta", base.estado)
    assert base.conciliar() == ([], ())


def test_dos_movimientos_iguales_el_mismo_dia_y_un_solo_gasto(base: Base) -> None:
    """Se une uno (el de la línea más baja) y el otro queda sin conciliar."""
    m1, m2 = base.mov(5, -450, "CAFE"), base.mov(5, -450, "CAFE")
    g = base.gasto(5, 450, "cafe")
    nuevos, empatados = base.conciliar()
    assert nuevos == [(m1, g, "monto_y_fecha")]
    assert empatados == ()  # el segundo no quedó por empate: ya no tiene con quién
    assert m2 not in {m for m, _, _ in base.pares()}


def test_dos_gastos_compiten_por_un_movimiento_y_empatan_no_se_une_ninguno(base: Base) -> None:
    m = base.mov(5, -450)
    base.gasto(5, 450, "uno")
    base.gasto(5, 450, "otro")
    assert base.conciliar() == ([], (m,))
    assert base.pares() == []


def test_la_descripcion_desempata_entre_dos_gastos(base: Base) -> None:
    m = base.mov(5, -450, "CAFETERIA LUNA")
    base.gasto(5, 450, "taxi")
    g = base.gasto(5, 450, "desayuno en la cafeteria")
    assert base.conciliar() == ([(m, g, "monto_y_fecha")], ())


def test_gemelos_exactos_se_unen_en_orden(base: Base) -> None:
    """N movimientos y N gastos con el mismo monto, fecha y parecido: línea ↔ id."""
    m1, m2 = base.mov(5, -450, "CAFE"), base.mov(5, -450, "CAFE")
    g1, g2 = base.gasto(5, 450, "cafe"), base.gasto(5, 450, "cafe")
    nuevos, empatados = base.conciliar()
    assert nuevos == [(m1, g1, "monto_y_fecha"), (m2, g2, "monto_y_fecha")]
    assert empatados == ()


def test_tres_movimientos_gemelos_y_dos_gastos_gemelos(base: Base) -> None:
    m1, m2, m3 = (base.mov(5, -450) for _ in range(3))
    g1, g2 = base.gasto(5, 450), base.gasto(5, 450)
    nuevos, empatados = base.conciliar()
    assert nuevos == [(m1, g1, "monto_y_fecha"), (m2, g2, "monto_y_fecha")]
    assert empatados == ()
    assert m3


def test_conciliar_dos_veces_no_duplica_nada(base: Base) -> None:
    base.mov(5, -1550)
    base.gasto(5, 1550)
    assert len(base.conciliar()[0]) == 1
    assert base.conciliar() == ([], ())
    assert len(base.pares()) == 1


def test_un_par_anulado_puede_volver_a_cruzarse(base: Base, engine: Engine) -> None:
    m, g = base.mov(5, -1550), base.gasto(5, 1550)
    base.conciliar()
    with Session(engine) as sesion:
        (par,) = sesion.query(Conciliacion).all()
    base.anular("conciliaciones", par.id)
    assert base.conciliar() == ([(m, g, "monto_y_fecha")], ())
    with engine.connect() as con:
        assert con.execute(text("SELECT count(*) FROM conciliaciones")).scalar_one() == 2
    assert base.pares() == [(m, g, "monto_y_fecha")]


def test_un_par_vigente_no_se_toca(base: Base, engine: Engine) -> None:
    """Un gasto ya conciliado (a mano, con otra regla) no compite por otro movimiento."""
    m1, g = base.mov(9, -1550), base.gasto(5, 1550)
    with Session(engine) as sesion, sesion.begin():
        sesion.add(Conciliacion(movimiento_id=m1, gasto_id=g, regla="manual"))
    base.mov(5, -1550)
    assert base.conciliar() == ([], ())
    assert base.pares() == [(m1, g, "manual")]


def test_sin_gasto_o_sin_movimiento_no_se_guarda_nada(base: Base, engine: Engine) -> None:
    base.mov(5, -999)  # movimiento sin gasto: G3 lo propondrá, acá no se crea nada
    base.gasto(6, 123)  # gasto en efectivo: sin movimiento
    assert base.conciliar() == ([], ())
    with engine.connect() as con:
        assert con.execute(text("SELECT count(*) FROM gastos")).scalar_one() == 1
        assert con.execute(text("SELECT count(*) FROM conciliaciones")).scalar_one() == 0


# --- Regla 2: monto exacto a ±3 días --------------------------------------


@pytest.mark.parametrize("dia_gasto", [2, 3, 4, 6, 7, 8])
def test_hasta_tres_dias_antes_o_despues(base: Base, dia_gasto: int) -> None:
    m, g = base.mov(5, -1550), base.gasto(dia_gasto, 1550)
    assert base.conciliar() == ([(m, g, "monto_3_dias")], ())


@pytest.mark.parametrize("dia_gasto", [1, 9])
def test_a_cuatro_dias_sin_descripcion_parecida_no_cruza(base: Base, dia_gasto: int) -> None:
    base.mov(5, -1550, "FARMACIA")
    base.gasto(dia_gasto, 1550, "taxi")
    assert base.conciliar() == ([], ())


def test_la_misma_fecha_gana_antes_que_la_ventana(base: Base) -> None:
    m = base.mov(5, -1550)
    g_cerca, g_mismo = base.gasto(6, 1550), base.gasto(5, 1550)
    nuevos, _ = base.conciliar()
    assert nuevos == [(m, g_mismo, "monto_y_fecha")]
    assert g_cerca not in {g for _, g, _ in base.pares()}


def test_mismo_monto_dos_veces_en_la_semana(base: Base) -> None:
    """Cada movimiento se queda con el gasto más cercano, y la elección es mutua."""
    m1, m2 = base.mov(3, -450), base.mov(6, -450)
    g1, g2 = base.gasto(4, 450), base.gasto(5, 450)
    assert base.conciliar() == ([(m1, g1, "monto_3_dias"), (m2, g2, "monto_3_dias")], ())


def test_mismo_monto_dos_veces_en_la_semana_uno_el_mismo_dia(base: Base) -> None:
    m1, m2 = base.mov(3, -450), base.mov(5, -450)
    g_tarde, g_mismo = base.gasto(6, 450), base.gasto(3, 450)
    nuevos, empatados = base.conciliar()
    assert nuevos == [(m1, g_mismo, "monto_y_fecha"), (m2, g_tarde, "monto_3_dias")]
    assert empatados == ()


def test_gana_el_gasto_mas_cercano_y_el_otro_queda_libre(base: Base) -> None:
    m = base.mov(5, -450)
    g_lejos, g_cerca = base.gasto(2, 450), base.gasto(4, 450)
    assert base.conciliar() == ([(m, g_cerca, "monto_3_dias")], ())
    assert g_lejos not in {g for _, g, _ in base.pares()}


def test_dos_gastos_a_la_misma_distancia_empatan(base: Base) -> None:
    m = base.mov(5, -450)
    base.gasto(4, 450)
    base.gasto(6, 450)
    assert base.conciliar() == ([], (m,))


def test_dos_movimientos_de_dias_distintos_empatan_por_un_gasto(base: Base) -> None:
    """No son gemelos (fechas distintas): unir uno sería adivinar cuál."""
    m1, m2 = base.mov(4, -450), base.mov(6, -450)
    base.gasto(5, 450)
    assert base.conciliar() == ([], (m1, m2))


def test_la_descripcion_desempata_a_la_misma_distancia(base: Base) -> None:
    m = base.mov(5, -450, "LIBRERIA CENTRAL")
    base.gasto(4, 450, "almuerzo")
    g = base.gasto(6, 450, "cuaderno de la libreria")
    assert base.conciliar() == ([(m, g, "monto_3_dias")], ())


# --- Regla 3: monto exacto con descripción parecida, a ±10 días -----------


@pytest.mark.parametrize("dia_gasto", [11, 17, 25, 31])
def test_hasta_diez_dias_con_una_palabra_distintiva(base: Base, dia_gasto: int) -> None:
    m = base.mov(21, -3500, "COMPRA GIMNASIO ATLAS")
    g = base.gasto(dia_gasto, 3500, "mensualidad del gimnasio")
    assert base.conciliar() == ([(m, g, "monto_y_descripcion")], ())


def test_a_once_dias_no_cruza_ni_con_la_misma_descripcion(base: Base) -> None:
    base.mov(21, -3500, "GIMNASIO ATLAS")
    base.gasto(10, 3500, "gimnasio atlas")
    assert base.conciliar() == ([], ())


def test_una_sola_palabra_generica_en_comun_no_alcanza(base: Base) -> None:
    base.mov(15, -3500, "COMPRA EN COMERCIO 123")
    base.gasto(8, 3500, "compra de zapatos")
    assert base.conciliar() == ([], ())


def test_dos_palabras_en_comun_alcanzan_aunque_sean_genericas(base: Base) -> None:
    m = base.mov(15, -3500, "PAGO TARJETA TIENDA")
    g = base.gasto(8, 3500, "pago en la tienda")
    assert base.conciliar() == ([(m, g, "monto_y_descripcion")], ())


def test_sin_palabras_en_comun_no_cruza(base: Base) -> None:
    base.mov(15, -3500, "FARMACIA SALUD")
    base.gasto(8, 3500, "taxi al aeropuerto")
    assert base.conciliar() == ([], ())


def test_la_subcategoria_y_el_texto_original_tambien_cuentan(base: Base) -> None:
    m1 = base.mov(15, -3500, "TIPTI ECUADOR")
    g1 = base.gasto(8, 3500, "mercado", subcategoria="Tipti")
    m2 = base.mov(15, -4200, "NETFLIX.COM")
    g2 = base.gasto(9, 4200, "suscripción", texto="42 netflix del mes")
    assert base.conciliar() == (
        [(m1, g1, "monto_y_descripcion"), (m2, g2, "monto_y_descripcion")],
        (),
    )


def test_en_la_regla_3_gana_el_mas_cercano_y_luego_el_mas_parecido(base: Base) -> None:
    m = base.mov(20, -3500, "GIMNASIO ATLAS CENTRO")
    base.gasto(12, 3500, "gimnasio")
    g_cerca = base.gasto(14, 3500, "gimnasio")
    assert base.conciliar() == ([(m, g_cerca, "monto_y_descripcion")], ())

    m2 = base.mov(20, -900, "GIMNASIO ATLAS CENTRO")
    base.gasto(14, 900, "gimnasio")
    g_parecido = base.gasto(26, 900, "gimnasio atlas")
    assert base.conciliar() == ([(m2, g_parecido, "monto_y_descripcion")], ())


def test_empate_total_en_la_regla_3(base: Base) -> None:
    m = base.mov(20, -3500, "GIMNASIO ATLAS")
    base.gasto(14, 3500, "gimnasio")
    base.gasto(26, 3500, "gimnasio")
    assert base.conciliar() == ([], (m,))


def test_un_empate_que_resuelve_una_regla_posterior_no_se_reporta(base: Base) -> None:
    """Dos gastos a la misma distancia empatan, salvo que otro movimiento se lleve a uno."""
    m1 = base.mov(5, -450)
    g1, g2 = base.gasto(4, 450), base.gasto(6, 450)
    m2 = base.mov(6, -450)
    nuevos, empatados = base.conciliar()
    assert nuevos == [(m2, g2, "monto_y_fecha"), (m1, g1, "monto_3_dias")]
    assert empatados == ()


# --- Periodo ---------------------------------------------------------------


def test_el_periodo_filtra_movimientos_pero_el_gasto_puede_caer_fuera(base: Base) -> None:
    m_marzo = base.mov(1, -1550)
    with Session(base.engine) as sesion, sesion.begin():
        gasto = sesion.get(Gasto, base.gasto(1, 1550))
        assert gasto is not None
        gasto.fecha = date(2026, 2, 27)  # dos días antes, en el mes anterior
        g = gasto.id
    base.mov(2, -700)
    base.gasto(2, 700)
    nuevos, _ = base.conciliar(desde=date(2026, 3, 1), hasta=date(2026, 3, 1))
    assert nuevos == [(m_marzo, g, "monto_3_dias")]


def test_el_cruce_cuenta_los_debitos_que_quedaron_sin_conciliar(base: Base) -> None:
    base.mov(5, -1550)
    base.gasto(5, 1550)
    base.mov(6, -999)
    base.mov(7, -45, tipo="comision")
    cruce = conciliar(base.engine, GENERICAS)
    assert (len(cruce.pares), cruce.sin_conciliar) == (1, 1)
    assert cruce.por_regla == {"monto_y_fecha": 1, "monto_3_dias": 0, "monto_y_descripcion": 0}


# --- Palabras --------------------------------------------------------------


def test_palabras_sin_tildes_ni_mayusculas_ni_numeros_ni_cortas() -> None:
    assert palabras("Café 123 del Día en la PANADERÍA x2") == {"cafe", "panaderia"}
    assert palabras("NETFLIX.COM 4521") == {"netflix"}
    assert palabras("") == set()


def test_en_comun_y_parecidas() -> None:
    assert en_comun("COMPRA GIMNASIO ATLAS", "gimnasio atlas del mes") == {"gimnasio", "atlas"}
    assert parecidas("COMPRA GIMNASIO", "gimnasio", GENERICAS)  # una distintiva
    assert not parecidas("COMPRA SUPER", "compra", GENERICAS)  # una genérica
    assert parecidas("PAGO TIENDA", "pago tienda", GENERICAS)  # dos, aunque genéricas
    assert not parecidas("FARMACIA", "taxi", GENERICAS)


def test_la_lista_de_genericas_sale_del_yaml_y_ya_normalizada() -> None:
    genericas = cargar_bancos().genericas
    assert {"compra", "pago", "debito", "tienda"} <= genericas
    assert all(p == p.lower() and palabras(p) == {p} for p in genericas)


# --- Desempate, sin base ---------------------------------------------------


def _candidato(mov: int, gasto: int, dias: int = 0, comunes: int = 0) -> Candidato:
    fecha = date(2026, 3, 10)
    return Candidato(
        movimiento_id=mov,
        orden=(1, mov),
        fecha_movimiento=fecha,
        gasto_id=gasto,
        fecha_gasto=fecha,
        dias=dias,
        comunes=comunes,
    )


def test_resolver_es_determinista_sin_importar_el_orden_de_entrada() -> None:
    candidatos = [_candidato(1, 10, 1), _candidato(1, 11, 2), _candidato(2, 10, 2)]
    esperado = ([(1, 10), (2, 11)], [])
    # 2 solo puede ir con 10, pero 10 prefiere a 1; al salir 10, 2 se queda sin candidatos.
    assert resolver(candidatos) == ([(1, 10)], [])
    candidatos.append(_candidato(2, 11, 3))
    assert resolver(candidatos) == esperado
    assert resolver(list(reversed(candidatos))) == esperado


def test_resolver_devuelve_los_movimientos_que_quedaron_en_empate() -> None:
    assert resolver([_candidato(1, 10), _candidato(1, 11)]) == ([], [1])


# --- CLI -------------------------------------------------------------------


def test_la_cli_concilia_un_mes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:token-de-prueba")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'prueba.db'}")
    base = Base(crear_engine(f"sqlite:///{tmp_path / 'prueba.db'}"))
    m, g = base.mov(5, -1550), base.gasto(5, 1550)
    base.mov(9, -700)
    monkeypatch.setattr("sys.argv", ["family-expense-bot", "--conciliar", "2026-03"])
    cli.main()
    assert capsys.readouterr().out == (
        "Conciliación de 2026-03: 1 pares nuevos "
        "(monto_y_fecha 1, monto_3_dias 0, monto_y_descripcion 0).\n"
        "  Quedan 1 débitos sin conciliar; 0 de ellos por empate entre candidatos iguales.\n"
    )
    assert base.pares() == [(m, g, "monto_y_fecha")]


@pytest.mark.parametrize("mes", ["2026-3", "2026-13", "marzo", "2026-03-01"])
def test_la_cli_rechaza_un_mes_mal_escrito(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mes: str
) -> None:
    monkeypatch.setattr("sys.argv", ["family-expense-bot", "--conciliar", mes])
    with pytest.raises(SystemExit) as salida:
        cli.main()
    assert salida.value.code == 2
    assert "va como AAAA-MM" in capsys.readouterr().err


def test_el_mes_llega_hasta_su_ultimo_dia() -> None:
    assert cli._mes("2028-02") == (date(2028, 2, 1), date(2028, 2, 29))
