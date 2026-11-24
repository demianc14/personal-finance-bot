"""Importar un estado de punta a punta: leer, verificar y guardar todo o nada.

Sobre una base en memoria o temporal, nunca la real. Todo es sintético.
"""

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from family_expense_bot import cli
from family_expense_bot.conciliacion.bancos import cargar_bancos
from family_expense_bot.conciliacion.contrato import EstadoLeido, EstadoRechazado, MovimientoLeido
from family_expense_bot.conciliacion.importar import importar_estado
from family_expense_bot.storage.repository import RepositorioEstados, crear_engine

BANCOS = cargar_bancos()
BANCO = BANCOS.bancos["banco_sintetico"]

MARZO = """\
Fecha,Descripción,Monto,Saldo
02/03/2026,SUPERMERCADO SINTETICO,-15.50,984.50
05/03/2026,TRANSFERENCIA RECIBIDA,250.00,1234.50
09/03/2026,COMISION RETIRO CAJERO,-0.45,1234.05
"""
ABRIL = """\
Fecha,Descripción,Monto,Saldo
03/04/2026,FARMACIA SINTETICA,-30.00,1204.05
"""


@pytest.fixture
def engine() -> Engine:
    return crear_engine("sqlite://")


def _archivo(tmp_path: Path, contenido: str, nombre: str = "marzo.csv") -> Path:
    ruta = tmp_path / nombre
    ruta.write_text(contenido, encoding="utf-8")
    return ruta


def _importar(engine: Engine, ruta: Path, mes: int = 3, **cambios: Any) -> Any:
    argumentos: dict[str, Any] = {
        "cuenta": "ahorros",
        "desde": date(2026, mes, 1),
        "hasta": date(2026, mes, 30),
        "topes": BANCOS.topes,
        "saldo_anterior_centavos": 100_000 if mes == 3 else 123_405,
        "saldo_final_centavos": 123_405 if mes == 3 else 120_405,
    } | cambios
    return importar_estado(ruta, BANCO, RepositorioEstados(engine), **argumentos)


def _contar(engine: Engine) -> tuple[int, int]:
    with engine.connect() as con:
        return (
            con.execute(text("SELECT count(*) FROM estados_cuenta")).scalar_one(),
            con.execute(text("SELECT count(*) FROM movimientos")).scalar_one(),
        )


# --- Lo que entra ---


def test_importa_un_estado_que_cuadra(engine: Engine, tmp_path: Path) -> None:
    resultado = _importar(engine, _archivo(tmp_path, MARZO))
    assert resultado.estado.id == 1
    assert resultado.movimientos == 3
    assert resultado.por_tipo == {"debito": 1, "credito": 1, "comision": 1}
    assert resultado.avisos == ()
    with engine.connect() as con:
        estado = con.execute(text("SELECT * FROM estados_cuenta")).mappings().one()
        movimientos = con.execute(
            text("SELECT linea, fecha, descripcion, monto_centavos, tipo FROM movimientos")
        ).all()
    assert (estado["banco"], estado["cuenta"]) == ("banco_sintetico", "ahorros")
    assert (estado["saldo_anterior_centavos"], estado["saldo_final_centavos"]) == (100_000, 123_405)
    assert estado["anulado_en"] is None
    assert movimientos == [
        (2, "2026-03-02", "SUPERMERCADO SINTETICO", -1550, "debito"),
        (3, "2026-03-05", "TRANSFERENCIA RECIBIDA", 25_000, "credito"),
        (4, "2026-03-09", "COMISION RETIRO CAJERO", -45, "comision"),
    ]


def test_el_repositorio_devuelve_los_estados_vigentes(engine: Engine, tmp_path: Path) -> None:
    _importar(engine, _archivo(tmp_path, MARZO))
    (vigente,) = RepositorioEstados(engine).vigentes()
    assert (vigente.id, vigente.periodo_desde, vigente.saldo_final_centavos) == (
        1,
        date(2026, 3, 1),
        123_405,
    )


# --- Lo que no entra no deja nada ---


def test_un_estado_que_no_cuadra_se_rechaza_completo(engine: Engine, tmp_path: Path) -> None:
    ruta = _archivo(tmp_path, MARZO)
    with pytest.raises(EstadoRechazado) as error:
        _importar(engine, ruta, saldo_final_centavos=123_406)
    assert [p.codigo for p in error.value.problemas] == ["saldo_final", "saldo_no_cuadra"]
    assert _contar(engine) == (0, 0)


def test_una_fila_mala_rechaza_el_archivo_completo(engine: Engine, tmp_path: Path) -> None:
    ruta = _archivo(tmp_path, MARZO.replace("09/03/2026", "31/02/2026"))
    with pytest.raises(EstadoRechazado, match="Línea 4: la fecha '31/02/2026'"):
        _importar(engine, ruta)
    assert _contar(engine) == (0, 0)


def test_el_mismo_archivo_dos_veces_no_duplica_nada(engine: Engine, tmp_path: Path) -> None:
    ruta = _archivo(tmp_path, MARZO)
    _importar(engine, ruta)
    with pytest.raises(EstadoRechazado) as error:
        _importar(engine, ruta)
    assert [p.mensaje for p in error.value.problemas] == [
        "Este archivo ya se importó: es el estado #1 (banco_sintetico/ahorros, "
        "2026-03-01 a 2026-03-30)."
    ]
    assert _contar(engine) == (1, 3)


def test_el_mismo_mes_exportado_de_nuevo_se_rechaza_por_periodo(
    engine: Engine, tmp_path: Path
) -> None:
    _importar(engine, _archivo(tmp_path, MARZO))
    otra_vez = _archivo(tmp_path, MARZO.replace("\n", "\r\n"), "marzo-windows.csv")
    with pytest.raises(EstadoRechazado) as error:
        _importar(engine, otra_vez)
    assert [p.codigo for p in error.value.problemas] == ["periodo_solapado"]
    assert _contar(engine) == (1, 3)


def test_un_estado_anulado_se_puede_reimportar(engine: Engine, tmp_path: Path) -> None:
    ruta = _archivo(tmp_path, MARZO)
    _importar(engine, ruta)
    with engine.begin() as con:
        con.execute(
            text(
                "UPDATE estados_cuenta SET anulado_en = '2026-04-02 10:00:00', "
                "motivo_anulacion = 'banco mal configurado'"
            )
        )
    assert RepositorioEstados(engine).vigentes() == []
    assert _importar(engine, ruta).estado.id == 2


def test_guardar_es_todo_o_nada(engine: Engine) -> None:
    """Si la base rechaza un movimiento, tampoco queda el estado."""
    malo = MovimientoLeido.model_construct(
        linea=3, fecha=date(2026, 3, 5), descripcion="CERO", monto_centavos=0, tipo="debito"
    )
    bueno = MovimientoLeido(
        linea=2, fecha=date(2026, 3, 2), descripcion="MERCADO", monto_centavos=-1550, tipo="debito"
    )
    estado = EstadoLeido.model_construct(
        banco="banco_sintetico",
        cuenta="ahorros",
        periodo_desde=date(2026, 3, 1),
        periodo_hasta=date(2026, 3, 31),
        saldo_anterior_centavos=100_000,
        saldo_final_centavos=98_450,
        hash_archivo="a" * 64,
        movimientos=(bueno, malo),
    )
    with pytest.raises(IntegrityError):
        RepositorioEstados(engine).guardar(estado, revisar=lambda _vigentes: None)
    assert _contar(engine) == (0, 0)


def test_la_base_es_la_ultima_defensa_contra_el_archivo_repetido(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si la revisión no viera el estado ya guardado, el índice único del hash lo frena igual."""
    ruta = _archivo(tmp_path, MARZO)
    _importar(engine, ruta)
    monkeypatch.setattr(RepositorioEstados, "vigentes", lambda _: [])
    monkeypatch.setattr(RepositorioEstados, "_vigentes", staticmethod(lambda _sesion: []))
    with pytest.raises(EstadoRechazado) as error:
        _importar(engine, ruta)
    assert [(p.codigo, p.mensaje) for p in error.value.problemas] == [
        ("archivo_repetido", "Este archivo ya se importó: la base lo rechazó y no se guardó nada.")
    ]
    assert _contar(engine) == (1, 3)


# --- Continuidad: un saldo que no encadena rechaza; un hueco avisa ---


def test_la_continuidad_rota_rechaza_y_no_guarda(engine: Engine, tmp_path: Path) -> None:
    _importar(engine, _archivo(tmp_path, MARZO), hasta=date(2026, 3, 31))
    # Con un peso menos en la columna de saldo, abril empieza en 1233.05 y no en 1234.05.
    abril = _archivo(tmp_path, ABRIL.replace("1204.05", "1203.05"), "abril.csv")
    with pytest.raises(EstadoRechazado) as error:
        _importar(
            engine, abril, mes=4, saldo_anterior_centavos=123_305, saldo_final_centavos=120_305
        )
    assert [(p.codigo, p.mensaje) for p in error.value.problemas] == [
        (
            "continuidad",
            "El estado #1 (2026-03-01 a 2026-03-31) cerró con 1234.05 y este empieza con "
            "1233.05: hay -1.00 de diferencia entre los dos.",
        )
    ]
    assert _contar(engine) == (1, 3)


def test_dos_meses_seguidos_que_empatan_no_avisan(engine: Engine, tmp_path: Path) -> None:
    _importar(engine, _archivo(tmp_path, MARZO), hasta=date(2026, 3, 31))
    assert _importar(engine, _archivo(tmp_path, ABRIL, "abril.csv"), mes=4).avisos == ()


def test_un_hueco_entre_meses_avisa_y_entra(engine: Engine, tmp_path: Path) -> None:
    _importar(engine, _archivo(tmp_path, MARZO))  # termina el 30: falta el 31
    resultado = _importar(engine, _archivo(tmp_path, ABRIL, "abril.csv"), mes=4)
    assert resultado.avisos == (
        "Entre el estado #1 (2026-03-01 a 2026-03-30) y este faltan los días 2026-03-31 a "
        "2026-03-31: la continuidad del saldo no se pudo verificar.",
    )
    assert _contar(engine) == (2, 4)


# --- La línea de comandos ---


@pytest.fixture
def base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """Una base temporal y un directorio sin `.env`: la CLI nunca ve la base real."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:token-de-prueba")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'prueba.db'}")
    return crear_engine(f"sqlite:///{tmp_path / 'prueba.db'}")


def _cli(monkeypatch: pytest.MonkeyPatch, *argumentos: str) -> None:
    monkeypatch.setattr("sys.argv", ["family-expense-bot", *argumentos])
    cli.main()


ARGUMENTOS = ("--banco", "banco_sintetico", "--cuenta", "ahorros")
PERIODO = ("--desde", "2026-03-01", "--hasta", "2026-03-31")
SALDOS = ("--saldo-anterior", "1000", "--saldo-final", "1234.05")


RESUMEN = (
    "Estado a importar: banco_sintetico/ahorros, 2026-03-01 a 2026-03-31.\n"
    "  Saldo 1000.00 → 1234.05 con 3 movimientos: 1 debito, 1 credito, 1 comision.\n"
    "  Entran 250.00 en 1 movimiento y salen 15.95 en 2.\n"
)


def test_la_cli_sin_confirmar_muestra_el_resumen_y_no_guarda(
    base: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ruta = _archivo(tmp_path, MARZO)
    _cli(monkeypatch, "--importar-estado", str(ruta), *ARGUMENTOS, *PERIODO, *SALDOS)
    assert capsys.readouterr().out == RESUMEN + (
        "No se guardó nada. Si el resumen está bien, repite el comando con --confirmar.\n"
    )
    assert _contar(base) == (0, 0)


def test_la_cli_con_confirmar_muestra_el_resumen_y_guarda(
    base: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ruta = _archivo(tmp_path, MARZO)
    _cli(monkeypatch, "--importar-estado", str(ruta), *ARGUMENTOS, *PERIODO, *SALDOS, "--confirmar")
    assert capsys.readouterr().out == RESUMEN + "Estado importado (#1).\n"
    assert _contar(base) == (1, 3)


def test_la_cli_rechaza_con_el_mensaje_exacto_y_no_guarda(
    base: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ruta = _archivo(tmp_path, MARZO)
    with pytest.raises(SystemExit) as salida:
        _cli(
            monkeypatch,
            "--importar-estado",
            str(ruta),
            *ARGUMENTOS,
            *PERIODO,
            "--saldo-anterior",
            "1000",
            "--saldo-final",
            "1234.06",
        )
    assert salida.value.code == (
        "Estado rechazado; no se guardó nada:\n"
        "  - El saldo final (1234.06) no coincide con el de la última fila (1234.05, línea 4).\n"
        "  - El estado no cuadra: saldo anterior 1000.00 + créditos 250.00 - débitos 15.95 = "
        "1234.05, pero el saldo final es 1234.06 (diferencia 0.01)."
    )
    assert _contar(base) == (0, 0)


def test_la_cli_muestra_los_avisos_antes_de_guardar(
    base: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    marzo = _archivo(tmp_path, MARZO)
    _cli(
        monkeypatch, "--importar-estado", str(marzo), *ARGUMENTOS, *PERIODO, *SALDOS, "--confirmar"
    )
    abril = _archivo(tmp_path, ABRIL, "abril.csv")
    capsys.readouterr()
    _cli(
        monkeypatch,
        "--importar-estado",
        str(abril),
        *ARGUMENTOS,
        "--desde",
        "2026-04-02",
        "--hasta",
        "2026-04-30",
        "--saldo-anterior",
        "1234.05",
        "--saldo-final",
        "1204.05",
    )
    assert capsys.readouterr().out.splitlines()[3] == (
        "  Aviso: Entre el estado #1 (2026-03-01 a 2026-03-31) y este faltan los días "
        "2026-04-01 a 2026-04-01: la continuidad del saldo no se pudo verificar."
    )
    assert _contar(base) == (1, 3)


def test_la_cli_con_confirmar_rechaza_una_cuenta_desconocida(
    base: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ruta = _archivo(tmp_path, MARZO)
    otra = ("--banco", "banco_sintetico", "--cuenta", "corriente")
    with pytest.raises(SystemExit) as salida:
        _cli(monkeypatch, "--importar-estado", str(ruta), *otra, *PERIODO, *SALDOS, "--confirmar")
    assert salida.value.code == (
        "Estado rechazado; no se guardó nada:\n"
        "  - La cuenta 'corriente' no está entre las de Banco Sintético en config/bancos.yaml "
        "(ahorros y visa). Si es una cuenta nueva, agrégala ahí."
    )
    assert _contar(base) == (0, 0)


@pytest.mark.parametrize(
    ("argumentos", "mensaje"),
    [
        (("--banco", "banco_sintetico"), "--importar-estado necesita --banco, --saldo-anterior"),
        (("--banco", "banco_sintetico", *SALDOS), "CSV necesita también --cuenta, --desde"),
        # Los saldos son obligatorios aunque el banco traiga columna de saldo.
        ((*ARGUMENTOS, *PERIODO), "--saldo-anterior y --saldo-final"),
        ((*ARGUMENTOS, *PERIODO, "--saldo-anterior", "1000"), "--saldo-anterior y --saldo-final"),
        ((*ARGUMENTOS, *PERIODO, "--saldo-final", "1234.05"), "--saldo-anterior y --saldo-final"),
        ((*ARGUMENTOS, "--desde", "01/03/2026", "--hasta", "2026-03-31", *SALDOS), "--desde"),
        ((*ARGUMENTOS, *PERIODO, "--saldo-final", "1,234.05"), "'1,234.05' no es un saldo"),
        (
            ("--banco", "banco_inventado", "--cuenta", "ahorros", *PERIODO, *SALDOS),
            "No existe el banco 'banco_inventado' en config/bancos.yaml",
        ),
        (
            ("--banco", "pichincha", "--cuenta", "ahorros", *SALDOS),
            "pichincha exporta PDF: el periodo sale del resumen",
        ),
    ],
)
def test_la_cli_explica_los_argumentos_que_faltan_o_estan_mal(
    base: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argumentos: tuple[str, ...],
    mensaje: str,
) -> None:
    ruta = _archivo(tmp_path, MARZO)
    with pytest.raises(SystemExit) as salida:
        _cli(monkeypatch, "--importar-estado", str(ruta), *argumentos)
    assert salida.value.code == 2
    assert mensaje in capsys.readouterr().err
    assert _contar(base) == (0, 0)


# --- PDF por la CLI ---

PDF = Path(__file__).resolve().parent / "fixtures" / "estados" / "pichincha.pdf"
SALDOS_PDF = ("--saldo-anterior", "523.10", "--saldo-final", "436.55")


def test_la_cli_importa_un_pdf_con_la_cuenta_del_mapa_local(
    base: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CUENTAS_LOCALES", "pichincha:9900000001:ahorros")
    _cli(monkeypatch, "--importar-estado", str(PDF), "--banco", "pichincha", *SALDOS_PDF)
    salida = capsys.readouterr().out
    assert salida.startswith("Estado a importar: pichincha/ahorros, 2026-10-02 a 2026-11-02.\n")
    assert "No se guardó nada" in salida
    assert _contar(base) == (0, 0)
    _cli(
        monkeypatch,
        "--importar-estado",
        str(PDF),
        "--banco",
        "pichincha",
        *SALDOS_PDF,
        "--confirmar",
    )
    salida = capsys.readouterr().out
    assert "Estado importado (#1)." in salida
    assert "9900000001" not in salida
    assert _contar(base) == (1, 17)


def test_la_cli_rechaza_un_pdf_sin_la_cuenta_en_el_mapa(
    base: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CUENTAS_LOCALES", raising=False)
    with pytest.raises(SystemExit) as salida:
        _cli(monkeypatch, "--importar-estado", str(PDF), "--banco", "pichincha", *SALDOS_PDF)
    assert "CUENTAS_LOCALES" in str(salida.value.code)
    assert "9900000001" not in str(salida.value.code)
    assert _contar(base) == (0, 0)
