"""Una conciliación no sobrevive a su gasto ni a su estado. Todo con datos inventados.

Los tres primeros tests son los fallos B1, B2 y B3 de la revisión adversarial:
editar un gasto conciliado, borrarlo, y anular un estado para reimportarlo.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from family_expense_bot import cli
from family_expense_bot.conciliacion.bancos import cargar_bancos
from family_expense_bot.conciliacion.importar import Importacion, importar_estado
from family_expense_bot.conciliacion.integridad import (
    IntegridadRota,
    auditar_integridad,
    problemas_de_integridad,
    reparar_integridad,
)
from family_expense_bot.conciliacion.reglas import conciliar
from family_expense_bot.extraction.schema import Confianza, GastoExtraido
from family_expense_bot.storage.repository import (
    GastoRegistrado,
    RepositorioEstados,
    RepositorioGastos,
    crear_engine,
)
from tests.sintetico import cargar

BANCOS = cargar_bancos()
MARZO = (date(2026, 3, 1), date(2026, 3, 31))
ANTERIOR = 100_000
QUIEN = 42

Fila = tuple[date, str, int]
SUPER: Fila = (date(2026, 3, 2), "SUPERMERCADO SINTETICO", -8540)
CINE: Fila = (date(2026, 3, 12), "CINE SINTETICO", -3500)


@pytest.fixture
def engine() -> Engine:
    return crear_engine("sqlite://")


def _extraido(dia: int, monto: str, descripcion: str) -> GastoExtraido:
    return GastoExtraido(
        monto=Decimal(monto),
        categoria="alimentacion",
        fecha=date(2026, 3, dia),
        atribuido_a="demian",
        descripcion=descripcion,
        texto_original=f"{monto} {descripcion}",
        remitente="demian",
        confianza_monto=Confianza.ALTA,
        confianza_categoria=Confianza.ALTA,
        confianza_fecha=Confianza.ALTA,
        confianza_atribuido_a=Confianza.ALTA,
    )


def _gasto(engine: Engine, dia: int, monto: str, descripcion: str = "algo") -> GastoRegistrado:
    return RepositorioGastos(engine).guardar(_extraido(dia, monto, descripcion), QUIEN)


def _csv(carpeta: Path, filas: list[Fila], nombre: str = "marzo.csv") -> Path:
    sintetico = cargar()
    ruta = carpeta / nombre
    ruta.write_bytes(sintetico._punto(sintetico._con_saldo(filas, ANTERIOR)))
    return ruta


def _importar(
    engine: Engine, ruta: Path, filas: list[Fila], cuenta: str = "ahorros"
) -> Importacion:
    return importar_estado(
        ruta,
        BANCOS.bancos["banco_sintetico"],
        RepositorioEstados(engine),
        cuenta=cuenta,
        desde=MARZO[0],
        hasta=MARZO[1],
        tope_bytes=BANCOS.tope_bytes,
        saldo_anterior_centavos=ANTERIOR,
        saldo_final_centavos=ANTERIOR + sum(centavos for _, _, centavos in filas),
    )


def _conciliar(engine: Engine) -> tuple[int, int]:
    """(pares nuevos, débitos sin conciliar)."""
    cruce = conciliar(engine, BANCOS.genericas)
    return len(cruce.pares), cruce.sin_conciliar


def _pares(engine: Engine) -> list[tuple[int, int, str]]:
    """Los pares vigentes: (línea del movimiento, gasto, regla)."""
    with engine.connect() as con:
        filas = con.execute(
            text(
                "SELECT m.linea, c.gasto_id, c.regla FROM conciliaciones c "
                "JOIN movimientos m ON m.id = c.movimiento_id "
                "WHERE c.anulado_en IS NULL ORDER BY m.linea"
            )
        )
        return [(linea, gasto, regla) for linea, gasto, regla in filas]


def _conciliaciones(engine: Engine) -> list[tuple[object, ...]]:
    """La tabla entera, para comprobar que algo no la tocó."""
    with engine.connect() as con:
        return [tuple(f) for f in con.execute(text("SELECT * FROM conciliaciones ORDER BY id"))]


def _motivos(engine: Engine) -> list[str | None]:
    with engine.connect() as con:
        filas = con.execute(text("SELECT motivo_anulacion FROM conciliaciones ORDER BY id"))
        return [motivo for (motivo,) in filas]


def _sql(engine: Engine, sentencia: str) -> None:
    with engine.begin() as con:
        con.execute(text(sentencia))


@pytest.fixture
def conciliada(engine: Engine, tmp_path: Path) -> tuple[Engine, GastoRegistrado, Path]:
    """Un gasto y su cargo ya unidos por `monto_y_fecha`."""
    gasto = _gasto(engine, 2, "85.40", "supermercado")
    ruta = _csv(tmp_path, [SUPER])
    _importar(engine, ruta, [SUPER])
    assert _conciliar(engine) == (1, 0)
    assert _pares(engine) == [(2, gasto.id, "monto_y_fecha")]
    return engine, gasto, ruta


# --- B1: editar un gasto conciliado -----------------------------------------


def test_editar_la_fecha_de_un_gasto_conciliado_anula_el_par_y_el_nuevo_vuelve_a_cruzar(
    conciliada: tuple[Engine, GastoRegistrado, Path],
) -> None:
    """Si no cambian monto ni fecha el par se hereda: ver `test_carreras_conciliacion.py`."""
    engine, gasto, _ = conciliada
    nuevo = RepositorioGastos(engine).reemplazar(
        gasto.id, _extraido(3, "85.40", "supermercado del barrio"), QUIEN, "formulario"
    )
    assert nuevo is not None
    assert _pares(engine) == []
    assert _motivos(engine) == [f"gasto editado → #{nuevo.id}"]
    auditar_integridad(engine)

    assert _conciliar(engine) == (1, 0)
    assert _pares(engine) == [(2, nuevo.id, "monto_3_dias")]


def test_editar_el_monto_de_un_gasto_conciliado_deja_el_cargo_sin_conciliar(
    conciliada: tuple[Engine, GastoRegistrado, Path],
) -> None:
    engine, gasto, _ = conciliada
    RepositorioGastos(engine).reemplazar(gasto.id, _extraido(2, "58.40", "super"), QUIEN, "chat")
    assert _conciliar(engine) == (0, 1)


def test_editar_un_gasto_no_toca_los_pares_de_otros(engine: Engine, tmp_path: Path) -> None:
    super_, cine = _gasto(engine, 2, "85.40"), _gasto(engine, 12, "35.00")
    _importar(engine, _csv(tmp_path, [SUPER, CINE]), [SUPER, CINE])
    _conciliar(engine)
    RepositorioGastos(engine).reemplazar(cine.id, _extraido(12, "53.00", "cine"), QUIEN, "chat")
    assert _pares(engine) == [(2, super_.id, "monto_y_fecha")]


def test_si_editar_se_rechaza_el_par_queda_como_estaba(
    conciliada: tuple[Engine, GastoRegistrado, Path],
) -> None:
    engine, gasto, _ = conciliada
    otra_persona = QUIEN + 1
    assert (
        RepositorioGastos(engine).reemplazar(gasto.id, _extraido(2, "1.00", "x"), otra_persona, "x")
        is None
    )
    assert _pares(engine) == [(2, gasto.id, "monto_y_fecha")]


# --- B2: borrar un gasto conciliado -----------------------------------------


def test_borrar_un_gasto_conciliado_devuelve_el_cargo_a_sin_conciliar(
    conciliada: tuple[Engine, GastoRegistrado, Path],
) -> None:
    engine, gasto, _ = conciliada
    assert RepositorioGastos(engine).anular(gasto.id, "me equivoqué")
    assert _pares(engine) == []
    assert _motivos(engine) == ["gasto anulado: me equivoqué"]
    auditar_integridad(engine)
    assert _conciliar(engine) == (0, 1)


def test_anular_un_gasto_dos_veces_no_cambia_nada_la_segunda(
    conciliada: tuple[Engine, GastoRegistrado, Path],
) -> None:
    engine, gasto, _ = conciliada
    repositorio = RepositorioGastos(engine)
    assert repositorio.anular(gasto.id, "primera")
    antes = _conciliaciones(engine)
    assert not repositorio.anular(gasto.id, "segunda")
    assert _conciliaciones(engine) == antes


# --- B3: anular un estado y reimportarlo ------------------------------------


def test_anular_un_estado_anula_sus_movimientos_y_sus_pares(
    conciliada: tuple[Engine, GastoRegistrado, Path],
) -> None:
    engine, _, _ = conciliada
    estados = RepositorioEstados(engine)
    (estado,) = estados.vigentes()
    assert estados.anular(estado.id, "banco mal configurado")
    assert estados.vigentes() == []
    assert _pares(engine) == []
    assert _motivos(engine) == ["estado anulado: banco mal configurado"]
    with engine.connect() as con:
        movimientos = con.execute(
            text("SELECT anulado_en IS NOT NULL, motivo_anulacion FROM movimientos")
        ).all()
    assert [tuple(m) for m in movimientos] == [(1, "estado anulado: banco mal configurado")]
    auditar_integridad(engine)


def test_reimportar_tras_anular_da_los_mismos_pares(engine: Engine, tmp_path: Path) -> None:
    super_, cine = _gasto(engine, 2, "85.40"), _gasto(engine, 14, "35.00")
    filas = [SUPER, CINE]
    ruta = _csv(tmp_path, filas)
    primero = _importar(engine, ruta, filas)
    assert _conciliar(engine) == (2, 0)
    antes = _pares(engine)
    assert antes == [(2, super_.id, "monto_y_fecha"), (3, cine.id, "monto_3_dias")]

    assert RepositorioEstados(engine).anular(primero.estado.id, "se importó con otro banco")
    segundo = _importar(engine, ruta, filas)
    assert segundo.estado.id != primero.estado.id
    assert _conciliar(engine) == (2, 0)
    assert _pares(engine) == antes
    auditar_integridad(engine)


def test_tras_anular_el_mismo_archivo_vuelve_a_entrar(engine: Engine, tmp_path: Path) -> None:
    """Ni el índice del hash ni el chequeo de solape miran los estados anulados."""
    ruta = _csv(tmp_path, [SUPER])
    primero = _importar(engine, ruta, [SUPER])
    assert RepositorioEstados(engine).anular(primero.estado.id, "prueba")
    segundo = _importar(engine, ruta, [SUPER])
    assert segundo.movimientos == 1
    assert [v.id for v in RepositorioEstados(engine).vigentes()] == [segundo.estado.id]
    assert segundo.estado.hash_archivo == primero.estado.hash_archivo


def test_anular_un_estado_dos_veces_no_cambia_nada_la_segunda(
    conciliada: tuple[Engine, GastoRegistrado, Path],
) -> None:
    engine, _, _ = conciliada
    estados = RepositorioEstados(engine)
    (estado,) = estados.vigentes()
    assert estados.anular(estado.id, "primera")

    def foto() -> list[tuple[object, ...]]:
        with engine.connect() as con:
            return [
                tuple(f)
                for tabla in ("estados_cuenta", "movimientos", "conciliaciones")
                for f in con.execute(text(f"SELECT * FROM {tabla} ORDER BY id"))
            ]

    antes = foto()
    assert not estados.anular(estado.id, "segunda")
    assert foto() == antes


def test_anular_un_estado_que_no_existe_o_sin_motivo(engine: Engine) -> None:
    estados = RepositorioEstados(engine)
    assert not estados.anular(99, "no existe")
    with pytest.raises(ValueError, match="motivo"):
        estados.anular(1, "  ")


def test_anular_un_estado_no_toca_los_de_otro(engine: Engine, tmp_path: Path) -> None:
    _gasto(engine, 2, "85.40")
    cine = _gasto(engine, 12, "35.00")
    ahorros = _importar(engine, _csv(tmp_path, [SUPER]), [SUPER])
    _importar(engine, _csv(tmp_path, [CINE], "visa.csv"), [CINE], cuenta="visa")
    assert _conciliar(engine) == (2, 0)
    assert RepositorioEstados(engine).anular(ahorros.estado.id, "solo ahorros")
    assert [gasto for _, gasto, _ in _pares(engine)] == [cine.id]
    assert len(RepositorioEstados(engine).vigentes()) == 1


# --- auditar_integridad ------------------------------------------------------


def test_una_base_sana_pasa_la_auditoria(conciliada: tuple[Engine, GastoRegistrado, Path]) -> None:
    engine, _, _ = conciliada
    assert problemas_de_integridad(engine) == []
    auditar_integridad(engine)


@pytest.mark.parametrize(
    ("tabla", "problema"),
    [
        ("gastos", "gasto anulado"),
        ("movimientos", "movimiento anulado"),
        ("estados_cuenta", "estado anulado"),
    ],
)
def test_la_auditoria_detecta_una_anulacion_hecha_a_mano(
    conciliada: tuple[Engine, GastoRegistrado, Path], tabla: str, problema: str
) -> None:
    engine, gasto, _ = conciliada
    _sql(engine, f"UPDATE {tabla} SET anulado_en = '2026-04-01', motivo_anulacion = 'a mano'")
    (encontrado,) = problemas_de_integridad(engine)
    assert encontrado.problemas == (problema,)
    assert encontrado.gasto_id == gasto.id
    with pytest.raises(IntegridadRota, match=problema):
        auditar_integridad(engine)


def test_la_auditoria_junta_los_problemas_de_un_mismo_par(
    conciliada: tuple[Engine, GastoRegistrado, Path],
) -> None:
    engine, _, _ = conciliada
    for tabla in ("gastos", "estados_cuenta"):
        _sql(engine, f"UPDATE {tabla} SET anulado_en = '2026-04-01', motivo_anulacion = 'x'")
    (encontrado,) = problemas_de_integridad(engine)
    assert encontrado.problemas == ("gasto anulado", "estado anulado")


@pytest.mark.parametrize(
    ("indice", "columna", "problema"),
    [
        ("uq_conciliaciones_movimiento_vigente", "movimiento_id", "movimiento en más de un par"),
        ("uq_conciliaciones_gasto_vigente", "gasto_id", "gasto en más de un par"),
    ],
)
def test_la_auditoria_detecta_que_se_rompio_el_uno_a_uno(
    engine: Engine, tmp_path: Path, indice: str, columna: str, problema: str
) -> None:
    """Sin el índice único la base acepta el par repetido: la auditoría lo ve igual."""
    _gasto(engine, 2, "85.40")
    _gasto(engine, 2, "85.40")
    _importar(engine, _csv(tmp_path, [SUPER, SUPER]), [SUPER, SUPER])
    assert _conciliar(engine) == (2, 0)
    _sql(engine, f"DROP INDEX {indice}")
    _sql(engine, f"UPDATE conciliaciones SET {columna} = 1 WHERE id = 2")
    encontrados = problemas_de_integridad(engine)
    assert [p.conciliacion_id for p in encontrados] == [1, 2]
    assert [p.problemas for p in encontrados] == [(problema,), (problema,)]


# --- reparar_integridad ------------------------------------------------------


@pytest.fixture
def rota(conciliada: tuple[Engine, GastoRegistrado, Path]) -> Engine:
    """Como quedaba una base antes de este arreglo: el gasto anulado y su par vigente."""
    engine, _, _ = conciliada
    _sql(engine, "UPDATE gastos SET anulado_en = '2026-04-01', motivo_anulacion = 'borrado'")
    return engine


def test_la_reparacion_simulada_lista_y_no_cambia_la_base(rota: Engine) -> None:
    antes = _conciliaciones(rota)
    (por_anular,) = reparar_integridad(rota)
    assert por_anular.problemas == ("gasto anulado",)
    assert _conciliaciones(rota) == antes
    assert len(problemas_de_integridad(rota)) == 1


def test_la_reparacion_confirmada_deja_la_auditoria_limpia(rota: Engine) -> None:
    assert len(reparar_integridad(rota, confirmar=True)) == 1
    auditar_integridad(rota)
    assert _pares(rota) == []
    assert _motivos(rota) == ["reparado: gasto anulado"]
    assert _conciliar(rota) == (0, 1)


def test_reparar_dos_veces_no_cambia_nada_la_segunda(rota: Engine) -> None:
    reparar_integridad(rota, confirmar=True)
    antes = _conciliaciones(rota)
    assert reparar_integridad(rota, confirmar=True) == []
    assert _conciliaciones(rota) == antes


def test_reparar_una_base_sana_no_anula_nada(
    conciliada: tuple[Engine, GastoRegistrado, Path],
) -> None:
    engine, gasto, _ = conciliada
    assert reparar_integridad(engine, confirmar=True) == []
    assert _pares(engine) == [(2, gasto.id, "monto_y_fecha")]


# --- CLI ---------------------------------------------------------------------


@pytest.fixture
def base_rota_en_disco(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:token-de-prueba")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'prueba.db'}")
    engine = crear_engine(f"sqlite:///{tmp_path / 'prueba.db'}")
    _gasto(engine, 2, "85.40", "supermercado")
    _gasto(engine, 12, "35.00", "cine")
    _importar(engine, _csv(tmp_path, [SUPER, CINE]), [SUPER, CINE])
    assert _conciliar(engine) == (2, 0)
    anulado = "anulado_en = '2026-04-01', motivo_anulacion = 'x'"
    _sql(engine, f"UPDATE gastos SET {anulado} WHERE id = 1")  # el par #1 queda huérfano
    _sql(engine, f"UPDATE conciliaciones SET {anulado} WHERE gasto_id = 2")  # este se liberó bien
    return engine


def test_la_cli_no_concilia_sobre_una_base_rota_y_sugiere_repararla(
    base_rota_en_disco: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    antes = _conciliaciones(base_rota_en_disco)
    monkeypatch.setattr("sys.argv", ["family-expense-bot", "--conciliar", "2026-03"])
    with pytest.raises(SystemExit) as salida:
        cli.main()
    mensaje = str(salida.value)
    assert salida.value.code != 0
    assert "No concilio" in mensaje
    assert "par #1" in mensaje and "gasto anulado" in mensaje
    assert "--reparar-integridad" in mensaje
    assert _conciliaciones(base_rota_en_disco) == antes


def test_la_cli_repara_en_simulacion_por_defecto(
    base_rota_en_disco: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    antes = _conciliaciones(base_rota_en_disco)
    monkeypatch.setattr("sys.argv", ["family-expense-bot", "--reparar-integridad"])
    cli.main()
    salida = capsys.readouterr().out
    assert "Simulación" in salida and "par #1" in salida and "--confirmar" in salida
    assert _conciliaciones(base_rota_en_disco) == antes


def test_la_cli_repara_con_confirmar_y_despues_concilia(
    base_rota_en_disco: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["family-expense-bot", "--reparar-integridad", "--confirmar"])
    cli.main()
    assert "1 par anulado" in capsys.readouterr().out
    auditar_integridad(base_rota_en_disco)

    monkeypatch.setattr("sys.argv", ["family-expense-bot", "--conciliar", "2026-03"])
    cli.main()
    assert "1 pares nuevos" in capsys.readouterr().out
    assert _pares(base_rota_en_disco) == [(3, 2, "monto_y_fecha")]


def test_la_cli_dice_que_no_hay_nada_que_reparar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:token-de-prueba")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'prueba.db'}")
    monkeypatch.setattr("sys.argv", ["family-expense-bot", "--reparar-integridad"])
    cli.main()
    assert "nada que reparar" in capsys.readouterr().out


def test_confirmar_solo_va_con_reparar_integridad(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:token-de-prueba")
    monkeypatch.setattr("sys.argv", ["family-expense-bot", "--conciliar", "2026-03", "--confirmar"])
    with pytest.raises(SystemExit) as salida:
        cli.main()
    assert salida.value.code == 2
    assert "--confirmar" in capsys.readouterr().err
