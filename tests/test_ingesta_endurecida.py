"""Ingesta endurecida: cuentas permitidas, solape bajo candado, tarjeta con el signo al
revés, topes de filas y de monto, y caracteres de control. Todo es sintético.

Las carreras se prueban sin hilos, como en `test_carreras_conciliacion.py`: un gancho
corre al rival en medio de la importación, con otra conexión que no espera el candado.
"""

import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import OperationalError

from family_expense_bot.conciliacion.bancos import Banco, cargar_bancos
from family_expense_bot.conciliacion.contrato import (
    EstadoLeido,
    EstadoRechazado,
    EstadoVigente,
    MovimientoLeido,
)
from family_expense_bot.conciliacion.importar import importar_estado
from family_expense_bot.conciliacion.ingesta import leer_estado
from family_expense_bot.conciliacion.verificacion import verificar
from family_expense_bot.storage.repository import RepositorioEstados, crear_engine, preparar_sqlite

BANCOS = cargar_bancos()
CON_SALDO = BANCOS.bancos["banco_sintetico"]
TARJETA = CON_SALDO.model_copy(update={"signo": "positivo_sale"})

MARZO = """\
Fecha,Descripción,Monto,Saldo
02/03/2026,SUPERMERCADO SINTETICO,-15.50,984.50
05/03/2026,TRANSFERENCIA RECIBIDA,250.00,1234.50
09/03/2026,COMISION RETIRO CAJERO,-0.45,1234.05
"""


def _archivo(tmp_path: Path, contenido: str, nombre: str = "estado.csv") -> Path:
    ruta = tmp_path / nombre
    ruta.write_text(contenido, encoding="utf-8")
    return ruta


def _argumentos(**cambios: Any) -> dict[str, Any]:
    return {
        "cuenta": "ahorros",
        "desde": date(2026, 3, 1),
        "hasta": date(2026, 3, 31),
        "topes": BANCOS.topes,
        "saldo_anterior_centavos": 100_000,
        "saldo_final_centavos": 123_405,
    } | cambios


def _leer(tmp_path: Path, contenido: str, banco: Banco = CON_SALDO, **cambios: Any) -> EstadoLeido:
    return leer_estado(_archivo(tmp_path, contenido), banco, **_argumentos(**cambios))


def _rechazo(
    tmp_path: Path, contenido: str, banco: Banco = CON_SALDO, **cambios: Any
) -> list[tuple[str, str]]:
    with pytest.raises(EstadoRechazado) as error:
        _leer(tmp_path, contenido, banco, **cambios)
    return [(p.codigo, p.mensaje) for p in error.value.problemas]


# --- Cuentas: salen de una lista del yaml ------------------------------------


def test_cada_banco_del_yaml_lista_sus_cuentas() -> None:
    assert CON_SALDO.cuentas == ["ahorros", "visa"]
    assert all(banco.cuentas for banco in BANCOS.bancos.values())


def test_una_cuenta_que_no_esta_en_el_yaml_se_rechaza(tmp_path: Path) -> None:
    assert _rechazo(tmp_path, MARZO, cuenta="corriente") == [
        (
            "cuenta_desconocida",
            "La cuenta 'corriente' no está entre las de Banco Sintético en config/bancos.yaml "
            "(ahorros y visa). Si es una cuenta nueva, agrégala ahí.",
        )
    ]


def _banco(**cambios: Any) -> Banco:
    base: dict[str, Any] = {
        "nombre": "Banco de prueba",
        "columnas": {"fecha": "Fecha", "descripcion": "Detalle", "monto": "Monto"},
        "formato_fecha": "%d/%m/%Y",
        "decimal": "punto",
        "cuentas": ["ahorros"],
    }
    return Banco.model_validate(base | cambios)


@pytest.mark.parametrize(
    "cuentas", [[], ["ahorros 1234"], ["Ahorros"], ["2200123456"], ["ahorros", "ahorros"]]
)
def test_el_yaml_no_acepta_cuentas_vacias_repetidas_o_con_numero(cuentas: list[str]) -> None:
    with pytest.raises(ValidationError):
        _banco(cuentas=cuentas)


# --- El solape se decide con el candado tomado --------------------------------


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


def _importar(engine: Engine, ruta: Path) -> Any:
    return importar_estado(ruta, CON_SALDO, RepositorioEstados(engine), **_argumentos())


def test_dos_importaciones_del_mismo_mes_a_la_vez_dejan_un_solo_estado(
    engine: Engine, rival: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El rival importa marzo (otro archivo, otro hash) justo después de que revisamos.

    Sin volver a revisar con el candado tomado, quedarían dos estados de marzo.
    """
    nuestro = _archivo(tmp_path, MARZO, "nuestro.csv")
    del_rival = _archivo(tmp_path, MARZO + "\n", "rival.csv")
    leer = RepositorioEstados.vigentes
    rival_importo: list[int] = []

    def vigentes_y_rival(repositorio: RepositorioEstados) -> list[EstadoVigente]:
        leidos = leer(repositorio)
        if not rival_importo:
            rival_importo.append(1)
            monkeypatch.setattr(RepositorioEstados, "vigentes", leer)
            _importar(rival, del_rival)
        return leidos

    monkeypatch.setattr(RepositorioEstados, "vigentes", vigentes_y_rival)
    with pytest.raises(EstadoRechazado) as error:
        _importar(engine, nuestro)
    assert rival_importo
    assert [p.codigo for p in error.value.problemas] == ["periodo_solapado"]
    assert len(RepositorioEstados(engine).vigentes()) == 1


def test_guardar_revisa_con_el_candado_tomado(
    engine: Engine, rival: Engine, tmp_path: Path
) -> None:
    """Mientras `guardar` revisa los vigentes, nadie más puede escribir."""
    estado = _leer(tmp_path, MARZO)
    del_rival = _archivo(tmp_path, MARZO + "\n", "rival.csv")
    errores: list[Exception] = []

    def revisar(vigentes: list[EstadoVigente]) -> None:
        assert vigentes == []
        try:
            _importar(rival, del_rival)
        except OperationalError as e:
            errores.append(e)

    RepositorioEstados(engine).guardar(estado, revisar=revisar)
    assert len(errores) == 1 and "locked" in str(errores[0])
    assert len(RepositorioEstados(engine).vigentes()) == 1


def test_si_la_revision_rechaza_no_se_guarda_nada(engine: Engine, tmp_path: Path) -> None:
    estado = _leer(tmp_path, MARZO)

    def revisar(_vigentes: list[EstadoVigente]) -> None:
        raise EstadoRechazado([])

    with pytest.raises(EstadoRechazado):
        RepositorioEstados(engine).guardar(estado, revisar=revisar)
    assert RepositorioEstados(engine).vigentes() == []


# --- Tarjeta: si entra más de lo que sale, el signo está al revés --------------

TARJETA_AL_REVES = """\
Fecha,Descripción,Monto,Saldo
02/03/2026,MERCADO,-15.50,84.50
03/03/2026,FARMACIA,-20.00,64.50
04/03/2026,PAGO RECIBIDO,5.00,69.50
"""


def test_una_tarjeta_con_mas_creditos_que_debitos_se_rechaza(tmp_path: Path) -> None:
    assert _rechazo(tmp_path, TARJETA_AL_REVES, TARJETA) == [
        (
            "signo_tarjeta",
            "Banco Sintético es una tarjeta y este estado trae 2 créditos y 1 débito: en una "
            "tarjeta casi todo es consumo. ¿Signo invertido? Revisa `signo` en "
            "config/bancos.yaml.",
        )
    ]


def test_una_tarjeta_con_tantos_creditos_como_debitos_entra(tmp_path: Path) -> None:
    contenido = "Fecha,Descripción,Monto,Saldo\n02/03/2026,MERCADO,15.50,115.50\n"
    contenido += "03/03/2026,PAGO RECIBIDO,-15.50,100.00\n"
    estado = _leer(tmp_path, contenido, TARJETA)
    assert [m.monto_centavos for m in estado.movimientos] == [-1550, 1550]


def test_una_cuenta_de_ahorros_puede_tener_mas_creditos(tmp_path: Path) -> None:
    assert len(_leer(tmp_path, TARJETA_AL_REVES).movimientos) == 3


# --- Topes de filas y de monto, del yaml -----------------------------------------


def test_el_yaml_trae_topes_de_filas_y_de_monto() -> None:
    assert BANCOS.topes.filas == 2000
    assert BANCOS.topes.monto_centavos == 10_000_000


def test_un_archivo_con_mas_filas_que_el_tope_se_rechaza(tmp_path: Path) -> None:
    topes = BANCOS.topes.model_copy(update={"filas": 2})
    assert _rechazo(tmp_path, MARZO, topes=topes) == [
        ("demasiadas_filas", "El archivo trae 3 movimientos y el tope es 2: no se leyó.")
    ]


@pytest.mark.parametrize("monto", ["-100000.01", "100000.01", "999999999.00"])
def test_un_monto_sobre_el_tope_se_rechaza(tmp_path: Path, monto: str) -> None:
    contenido = f"Fecha,Descripción,Monto,Saldo\n02/03/2026,MERCADO,{monto},1.00\n"
    assert _rechazo(tmp_path, contenido) == [
        (
            "monto_tope",
            "Línea 2: el monto pasa del tope de 100000.00 de config/bancos.yaml. "
            "Si es real, sube `tope_monto` ahí.",
        )
    ]


def test_un_monto_igual_al_tope_entra(tmp_path: Path) -> None:
    contenido = "Fecha,Descripción,Monto,Saldo\n02/03/2026,MERCADO,-100000.00,1.00\n"
    assert _leer(tmp_path, contenido).movimientos[0].monto_centavos == -10_000_000


# --- Caracteres de control en la descripción -------------------------------------


@pytest.mark.parametrize(
    "descripcion",
    ["MERCADO\x00", "MER\x1b[31mCADO", "MERCADO‮ODACREM", "MER\tCADO", "MER​CADO"],
)
def test_una_descripcion_con_caracteres_de_control_se_rechaza(
    tmp_path: Path, descripcion: str
) -> None:
    contenido = f"Fecha,Descripción,Monto,Saldo\n02/03/2026,{descripcion},-15.50,984.50\n"
    assert _rechazo(tmp_path, contenido) == [
        (
            "descripcion_control",
            "Línea 2: la descripción trae caracteres de control (invisibles o que cambian "
            "cómo se ve el texto).",
        )
    ]


def test_tildes_y_enes_no_son_caracteres_de_control(tmp_path: Path) -> None:
    contenido = "Fecha,Descripción,Monto,Saldo\n02/03/2026,PANADERÍA ÑUÑOA,-15.50,984.50\n"
    assert _leer(tmp_path, contenido).movimientos[0].descripcion == "PANADERÍA ÑUÑOA"


# --- El encabezado no repite celdas del archivo --------------------------------


def test_el_mensaje_de_encabezado_no_repite_celdas_del_archivo(tmp_path: Path) -> None:
    contenido = 'Fecha,Descripción,Monto,=HYPERLINK("http://x")\n02/03/2026,MERCADO,-1.00,1\n'
    problemas = _rechazo(tmp_path, contenido)
    assert problemas == [
        (
            "encabezado",
            "Línea 1: al encabezado le faltan las columnas 'Saldo'; trae 4 columnas. "
            "¿Es un estado de Banco Sintético?",
        )
    ]
    assert "HYPERLINK" not in problemas[0][1]


# --- El bloque duplicado no se vuelve cuadrático -------------------------------


def _mismo_dia(descripciones: list[str]) -> EstadoLeido:
    movimientos = tuple(
        MovimientoLeido(
            linea=i + 2,
            fecha=date(2026, 3, 2),
            descripcion=d,
            monto_centavos=-100,
            tipo="debito",
        )
        for i, d in enumerate(descripciones)
    )
    return EstadoLeido(
        banco="banco_sintetico",
        cuenta="ahorros",
        periodo_desde=date(2026, 3, 1),
        periodo_hasta=date(2026, 3, 31),
        saldo_anterior_centavos=1_000_000,
        saldo_final_centavos=1_000_000 - 100 * len(movimientos),
        hash_archivo="a" * 64,
        movimientos=movimientos,
    )


@pytest.mark.parametrize(
    ("nombre", "patron", "filas"),
    [
        # Una fila igual intercalada con filas únicas: tardaba 16,8 s.
        ("alternadas", lambda i: "MERCADO" if i % 2 else f"UNICO {i}", 8000),
        # Dos filas iguales seguidas y una única: el peor caso del índice por pares.
        ("pares", lambda i: ("MERCADO", "FARMACIA", f"UNICO {i}")[i % 3], BANCOS.topes.filas),
    ],
)
def test_el_bloque_duplicado_tarda_menos_de_un_segundo(
    nombre: str, patron: Callable[[int], str], filas: int
) -> None:
    estado = _mismo_dia([patron(i) for i in range(filas)])
    inicio = time.perf_counter()
    veredicto = verificar(estado)
    assert time.perf_counter() - inicio < 1, nombre
    assert veredicto.avisos == ()


def test_el_bloque_duplicado_se_sigue_detectando_entre_miles_de_filas() -> None:
    unicas = [f"UNICO {i}" for i in range(3000)]
    bloque = ["MERCADO", "FARMACIA", "CINE"]
    estado = _mismo_dia(unicas + bloque + bloque + unicas[:5])
    assert verificar(estado).avisos == (
        "Las líneas 3005 a 3007 repiten las líneas 3002 a 3004. El estado cuadra, así que "
        "entraron como movimientos reales: revisa que no sea un bloque pegado dos veces.",
    )
