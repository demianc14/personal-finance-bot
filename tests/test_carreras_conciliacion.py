"""Carreras entre lectura y escritura, y huecos de la auditoría. Todo con datos inventados.

Son los hallazgos F2, F4, F5 y F11 de la revisión adversarial de las PR #27 y #29.
Las carreras se prueban sin hilos: un gancho corre al rival justo entre que la
operación lee y escribe, con otra conexión a la misma base que no espera el candado.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.exc import IntegrityError, OperationalError

from family_expense_bot import cli
from family_expense_bot.conciliacion import reglas
from family_expense_bot.conciliacion.integridad import (
    auditar_integridad,
    problemas_de_integridad,
    reparar_integridad,
)
from family_expense_bot.storage.repository import (
    GastoRegistrado,
    IngresoNuevo,
    RepositorioConciliaciones,
    RepositorioEstados,
    RepositorioGastos,
    RepositorioIngresos,
    crear_engine,
    preparar_sqlite,
)
from tests.test_integridad_conciliaciones import (
    QUIEN,
    SUPER,
    _conciliaciones,
    _conciliar,
    _csv,
    _extraido,
    _gasto,
    _importar,
    _motivos,
    _pares,
)


@pytest.fixture
def ruta_base(tmp_path: Path) -> Path:
    return tmp_path / "prueba.db"


@pytest.fixture
def engine(ruta_base: Path) -> Engine:
    """En disco: una base en memoria no se puede abrir desde dos conexiones."""
    return crear_engine(f"sqlite:///{ruta_base}")


@pytest.fixture
def rival(engine: Engine, ruta_base: Path) -> Engine:
    """Otra conexión a la misma base, que no espera el candado: falla en el acto."""
    return preparar_sqlite(create_engine(f"sqlite:///{ruta_base}", connect_args={"timeout": 0}))


@pytest.fixture
def conciliada(engine: Engine, tmp_path: Path) -> tuple[Engine, GastoRegistrado]:
    """Un gasto y su cargo ya unidos por `monto_y_fecha` (par #1)."""
    gasto = _gasto(engine, 2, "85.40", "supermercado")
    _importar(engine, _csv(tmp_path, [SUPER]), [SUPER])
    assert _conciliar(engine) == (1, 0)
    return engine, gasto


@dataclass
class Intento:
    """Lo que le pasó al rival cuando quiso escribir en medio de la operación."""

    corrio: bool = False
    error: OperationalError | None = None

    def correr(self, accion: Callable[[], object]) -> None:
        if self.corrio:
            return
        self.corrio = True
        try:
            accion()
        except OperationalError as e:
            self.error = e

    @property
    def bloqueado(self) -> bool:
        return self.corrio and self.error is not None and "locked" in str(self.error)


def _entre_leer_y_escribir(engine: Engine, accion: Callable[[], object]) -> Intento:
    """Corre `accion` justo antes del primer INSERT o UPDATE que haga `engine`."""
    intento = Intento()

    def gancho(_c: Any, _cursor: Any, sentencia: str, *_resto: Any) -> None:
        if sentencia.lstrip().upper().startswith(("INSERT", "UPDATE")):
            intento.correr(accion)

    event.listen(engine, "before_cursor_execute", gancho)
    return intento


def _a_mano(ruta: Path, sentencia: str) -> None:
    """Como quien abre la base con `sqlite3`: sin llaves foráneas ni repositorios."""
    conexion = sqlite3.connect(ruta)
    conexion.execute(sentencia)
    conexion.commit()
    conexion.close()


def _ingreso() -> IngresoNuevo:
    return IngresoNuevo(
        monto=Decimal("50.00"),
        categoria="regalo",
        subcategoria=None,
        persona="demian",
        fecha=date(2026, 3, 5),
        descripcion="x",
    )


# --- carreras: quien llega segundo espera, no escribe encima ------------------


def test_dos_reemplazar_intercalados_no_dejan_dos_gastos_vigentes(
    engine: Engine, rival: Engine
) -> None:
    gasto = _gasto(engine, 2, "85.40", "super")
    del_rival = RepositorioGastos(rival)
    intento = _entre_leer_y_escribir(
        engine,
        lambda: del_rival.reemplazar(gasto.id, _extraido(2, "85.40", "rival"), QUIEN, "chat"),
    )
    nuevo = RepositorioGastos(engine).reemplazar(
        gasto.id, _extraido(2, "85.40", "primero"), QUIEN, "chat"
    )
    assert nuevo is not None
    assert [g.id for g in RepositorioGastos(engine).listar()] == [nuevo.id]
    assert intento.bloqueado
    # Cuando por fin le toca, el viejo ya no está vigente: no crea otro.
    assert del_rival.reemplazar(gasto.id, _extraido(2, "85.40", "rival"), QUIEN, "chat") is None
    assert [g.id for g in RepositorioGastos(engine).listar()] == [nuevo.id]


def test_dos_anular_intercalados_del_mismo_gasto(engine: Engine, rival: Engine) -> None:
    gasto = _gasto(engine, 2, "85.40")
    intento = _entre_leer_y_escribir(
        engine, lambda: RepositorioGastos(rival).anular(gasto.id, "rival")
    )
    assert RepositorioGastos(engine).anular(gasto.id, "primero")
    assert intento.bloqueado
    assert RepositorioGastos(rival).anular(gasto.id, "rival") is False


def test_dos_anular_intercalados_del_mismo_estado(
    engine: Engine, rival: Engine, tmp_path: Path
) -> None:
    estado = _importar(engine, _csv(tmp_path, [SUPER]), [SUPER]).estado
    intento = _entre_leer_y_escribir(
        engine, lambda: RepositorioEstados(rival).anular(estado.id, "rival")
    )
    assert RepositorioEstados(engine).anular(estado.id, "primero")
    assert intento.bloqueado
    assert RepositorioEstados(rival).anular(estado.id, "rival") is False


def test_anular_y_reemplazar_intercalados_del_mismo_ingreso(engine: Engine, rival: Engine) -> None:
    repo = RepositorioIngresos(engine)
    ingreso = repo.guardar(_ingreso(), QUIEN)
    intento = _entre_leer_y_escribir(
        engine, lambda: RepositorioIngresos(rival).reemplazar(ingreso.id, _ingreso(), QUIEN)
    )
    assert repo.anular(ingreso.id, "primero")
    assert intento.bloqueado
    assert repo.listar() == []


def test_anular_un_par_y_borrar_su_gasto_intercalados(
    conciliada: tuple[Engine, GastoRegistrado], rival: Engine
) -> None:
    engine, gasto = conciliada
    intento = _entre_leer_y_escribir(
        engine, lambda: RepositorioGastos(rival).anular(gasto.id, "rival")
    )
    assert RepositorioConciliaciones(engine).anular(1, "no era este")
    assert intento.bloqueado
    assert _motivos(engine) == ["no era este"]


# --- F2 y F11: --conciliar toma el candado antes de leer ----------------------


@pytest.fixture
def entorno(ruta_base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:token-de-prueba")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{ruta_base}")


def _cli(monkeypatch: pytest.MonkeyPatch, *argumentos: str) -> None:
    monkeypatch.setattr("sys.argv", ["family-expense-bot", *argumentos])
    cli.main()


@pytest.mark.usefixtures("entorno")
def test_conciliar_no_deja_borrar_un_gasto_entre_leer_candidatos_y_guardar_pares(
    engine: Engine, rival: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin el candado, el par se guardaba sobre un gasto que el rival acababa de anular."""
    gasto = _gasto(engine, 2, "85.40", "supermercado")
    _importar(engine, _csv(tmp_path, [SUPER]), [SUPER])
    intento = Intento()
    resolver = reglas.resolver

    def resolver_con_rival(candidatos: Any) -> Any:
        intento.correr(lambda: RepositorioGastos(rival).anular(gasto.id, "rival"))
        return resolver(candidatos)

    monkeypatch.setattr(reglas, "resolver", resolver_con_rival)
    _cli(monkeypatch, "--conciliar", "2026-03")
    assert intento.bloqueado
    assert _pares(engine) == [(2, gasto.id, "monto_y_fecha")]
    assert problemas_de_integridad(engine) == []


@pytest.mark.usefixtures("entorno", "engine")
@pytest.mark.parametrize(
    ("error", "dice"),
    [
        (IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed")), "rechazó un par"),
        (OperationalError("BEGIN IMMEDIATE", {}, Exception("database is locked")), "ocupada"),
    ],
)
def test_la_cli_explica_por_que_no_concilio(
    monkeypatch: pytest.MonkeyPatch, error: Exception, dice: str
) -> None:
    def falla(*_a: Any, **_k: Any) -> None:
        raise error

    monkeypatch.setattr(cli, "conciliar", falla)
    with pytest.raises(SystemExit) as salida:
        _cli(monkeypatch, "--conciliar", "2026-03")
    mensaje = str(salida.value)
    assert salida.value.code != 0
    assert "No concilio" in mensaje and dice in mensaje and "no se guardó nada" in mensaje
    assert "Traceback" not in mensaje


# --- F4: la auditoría ve pares sin gasto o sin movimiento, y pares que no calzan


@pytest.mark.parametrize(
    ("sentencia", "problemas"),
    [
        ("DELETE FROM gastos", ("gasto inexistente",)),
        ("DELETE FROM movimientos", ("movimiento inexistente",)),
        ("DELETE FROM estados_cuenta", ("estado inexistente",)),
        ("UPDATE gastos SET monto_centavos = 8541", ("monto distinto",)),
        ("UPDATE movimientos SET tipo = 'comision'", ("movimiento no es débito",)),
        (
            "UPDATE movimientos SET monto_centavos = 8540",
            ("monto distinto", "movimiento no es débito"),
        ),
        ("UPDATE gastos SET fecha = '2026-03-03'", ("fecha fuera de la ventana",)),
    ],
)
def test_la_auditoria_detecta_y_la_reparacion_anula(
    conciliada: tuple[Engine, GastoRegistrado],
    ruta_base: Path,
    sentencia: str,
    problemas: tuple[str, ...],
) -> None:
    engine, _ = conciliada
    _a_mano(ruta_base, sentencia)
    (roto,) = problemas_de_integridad(engine)
    assert roto.problemas == problemas
    assert roto.conciliacion_id == 1

    antes = _conciliaciones(engine)
    assert reparar_integridad(engine) == [roto]
    assert _conciliaciones(engine) == antes
    assert reparar_integridad(engine, confirmar=True) == [roto]
    auditar_integridad(engine)
    assert _motivos(engine) == [f"reparado: {', '.join(problemas)}"]


@pytest.mark.parametrize(
    ("regla", "dia_del_gasto", "roto"),
    [
        ("monto_y_fecha", 2, False),
        ("monto_y_fecha", 3, True),
        ("monto_3_dias", 5, False),
        ("monto_3_dias", 6, True),
        ("monto_y_descripcion", 12, False),
        ("monto_y_descripcion", 13, True),
        # Una regla que la auditoría no conoce usa la ventana más ancha.
        ("a_mano", 12, False),
        ("a_mano", 13, True),
    ],
)
def test_la_ventana_de_fecha_es_la_de_la_regla_que_unio_el_par(
    conciliada: tuple[Engine, GastoRegistrado],
    ruta_base: Path,
    regla: str,
    dia_del_gasto: int,
    roto: bool,
) -> None:
    engine, _ = conciliada
    _a_mano(ruta_base, f"UPDATE conciliaciones SET regla = '{regla}'")
    _a_mano(ruta_base, f"UPDATE gastos SET fecha = '2026-03-{dia_del_gasto:02d}'")
    esperado = [("fecha fuera de la ventana",)] if roto else []
    assert [p.problemas for p in problemas_de_integridad(engine)] == esperado


def test_la_reparacion_espera_el_candado_como_cualquier_escritura(
    conciliada: tuple[Engine, GastoRegistrado], rival: Engine, ruta_base: Path
) -> None:
    engine, gasto = conciliada
    _a_mano(ruta_base, "UPDATE gastos SET monto_centavos = 1")
    intento = _entre_leer_y_escribir(
        engine, lambda: RepositorioGastos(rival).anular(gasto.id, "rival")
    )
    assert len(reparar_integridad(engine, confirmar=True)) == 1
    assert intento.bloqueado


# --- F5: editar sin tocar monto ni fecha conserva el par ----------------------


@pytest.mark.parametrize("cambio", ["descripcion", "categoria"])
def test_editar_solo_la_categoria_o_la_descripcion_pasa_el_par_al_gasto_nuevo(
    conciliada: tuple[Engine, GastoRegistrado], cambio: str
) -> None:
    engine, gasto = conciliada
    editado = _extraido(2, "85.40", "supermercado")
    editado = editado.model_copy(
        update={"descripcion": "super del barrio"}
        if cambio == "descripcion"
        else {"categoria": "hogar"}
    )
    nuevo = RepositorioGastos(engine).reemplazar(gasto.id, editado, QUIEN, "formulario")
    assert nuevo is not None
    assert _pares(engine) == [(2, nuevo.id, "monto_y_fecha")]
    assert _motivos(engine) == [f"par heredado por el gasto #{nuevo.id}", None]
    auditar_integridad(engine)
    assert _conciliar(engine) == (0, 0)


@pytest.mark.parametrize(("dia", "monto"), [(2, "58.40"), (3, "85.40"), (3, "58.40")])
def test_editar_el_monto_o_la_fecha_anula_el_par_y_no_lo_hereda(
    conciliada: tuple[Engine, GastoRegistrado], dia: int, monto: str
) -> None:
    engine, gasto = conciliada
    nuevo = RepositorioGastos(engine).reemplazar(
        gasto.id, _extraido(dia, monto, "supermercado"), QUIEN, "formulario"
    )
    assert nuevo is not None
    assert _pares(engine) == []
    assert _motivos(engine) == [f"gasto editado → #{nuevo.id}"]
    auditar_integridad(engine)


def test_editar_un_gasto_sin_par_no_inventa_uno(engine: Engine) -> None:
    gasto = _gasto(engine, 2, "85.40")
    RepositorioGastos(engine).reemplazar(gasto.id, _extraido(2, "85.40", "otro"), QUIEN, "chat")
    assert _conciliaciones(engine) == []


# --- anular un par ------------------------------------------------------------


def test_anular_un_par_libera_al_movimiento_y_al_gasto_sin_anularlos(
    conciliada: tuple[Engine, GastoRegistrado],
) -> None:
    engine, gasto = conciliada
    repo = RepositorioConciliaciones(engine)
    par = repo.obtener(1)
    assert par is not None
    assert (par.gasto_id, par.regla, par.anulado) == (gasto.id, "monto_y_fecha", False)

    assert repo.anular(1, "  no era este cargo  ")
    assert _pares(engine) == []
    assert _motivos(engine) == ["no era este cargo"]
    assert [g.id for g in RepositorioGastos(engine).listar()] == [gasto.id]
    auditar_integridad(engine)
    anulado = repo.obtener(1)
    assert anulado is not None and anulado.anulado


def test_anular_un_par_dos_veces_o_uno_que_no_existe_no_cambia_nada(
    conciliada: tuple[Engine, GastoRegistrado],
) -> None:
    engine, _ = conciliada
    repo = RepositorioConciliaciones(engine)
    assert repo.anular(1, "primero")
    antes = _conciliaciones(engine)
    assert repo.anular(1, "segundo") is False
    assert repo.anular(99, "no existe") is False
    assert repo.obtener(99) is None
    assert _conciliaciones(engine) == antes


def test_anular_un_par_exige_un_motivo(conciliada: tuple[Engine, GastoRegistrado]) -> None:
    engine, _ = conciliada
    with pytest.raises(ValueError, match="motivo"):
        RepositorioConciliaciones(engine).anular(1, "   ")
    assert len(_pares(engine)) == 1


@pytest.mark.usefixtures("entorno")
def test_la_cli_anula_un_par_en_simulacion_por_defecto(
    conciliada: tuple[Engine, GastoRegistrado],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine, gasto = conciliada
    antes = _conciliaciones(engine)
    _cli(monkeypatch, "--anular-par", "1", "--motivo", "no era este")
    salida = capsys.readouterr().out
    assert "Simulación" in salida and "par #1" in salida and f"gasto #{gasto.id}" in salida
    assert "--confirmar" in salida
    assert _conciliaciones(engine) == antes


@pytest.mark.usefixtures("entorno")
def test_la_cli_anula_un_par_con_confirmar(
    conciliada: tuple[Engine, GastoRegistrado],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine, _ = conciliada
    _cli(monkeypatch, "--anular-par", "1", "--motivo", "no era este", "--confirmar")
    salida = capsys.readouterr().out
    assert "Anulado" in salida and "par #1" in salida
    # El límite conocido se dice en el momento: las reglas no recuerdan el rechazo.
    assert "--conciliar" in salida and "volver a unir" in salida
    assert _pares(engine) == []
    assert _motivos(engine) == ["no era este"]


@pytest.mark.usefixtures("entorno")
@pytest.mark.parametrize("par", ["1", "99"])
def test_la_cli_no_anula_un_par_que_no_existe_o_ya_estaba_anulado(
    conciliada: tuple[Engine, GastoRegistrado], monkeypatch: pytest.MonkeyPatch, par: str
) -> None:
    engine, _ = conciliada
    RepositorioConciliaciones(engine).anular(1, "antes")
    antes = _conciliaciones(engine)
    with pytest.raises(SystemExit) as salida:
        _cli(monkeypatch, "--anular-par", par, "--motivo", "x", "--confirmar")
    assert salida.value.code != 0
    assert f"par #{par}" in str(salida.value)
    assert _conciliaciones(engine) == antes


@pytest.mark.usefixtures("entorno")
@pytest.mark.parametrize(
    ("argumentos", "falta"),
    [
        (["--anular-par", "1"], "--motivo"),
        (["--anular-par", "1", "--motivo", "  "], "--motivo"),
        (["--conciliar", "2026-03", "--motivo", "x"], "--motivo"),
        (["--anular-par", "uno", "--motivo", "x"], "--anular-par"),
    ],
)
def test_la_cli_exige_el_motivo_y_un_id_entero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argumentos: list[str],
    falta: str,
) -> None:
    with pytest.raises(SystemExit) as salida:
        _cli(monkeypatch, *argumentos)
    assert salida.value.code == 2
    assert falta in capsys.readouterr().err
