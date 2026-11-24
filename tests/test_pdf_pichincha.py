"""El parser del PDF de Pichincha (G1b), contra los PDF sintéticos de `scripts/estado_sintetico.py`.

Cada PDF de `tests/fixtures/estados/` con su "debería" es un test: lo que cuadra
entra y lo hostil se rechaza entero, con la página y la fila en el mensaje. Los
rechazos que no tienen fixture se arman acá con las mismas piezas del generador.
Esto prueba el parser contra nuestro generador, no contra el banco: ver
`docs/conciliacion.md`.
"""

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from family_expense_bot.conciliacion.bancos import BancoPdf, cargar_bancos
from family_expense_bot.conciliacion.contrato import EstadoRechazado
from family_expense_bot.conciliacion.importar import Importacion, importar_pdf
from family_expense_bot.conciliacion.pdf import leer_pdf
from family_expense_bot.storage.repository import RepositorioEstados, crear_engine
from tests.sintetico import cargar

sintetico = cargar()
NUMERO = sintetico._CUENTA
CUENTAS = {NUMERO: "ahorros"}
BANCOS = cargar_bancos()
PICHINCHA: BancoPdf = BANCOS.bancos_pdf["pichincha"]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "estados"

ESPERA = {
    "pichincha": (),
    "pichincha_doc_partido": (),
    "pichincha_columna_desplazada": ("fuera_de_columna",),
    "pichincha_fila_entre_paginas": ("fila_incompleta", "texto_sin_fila"),
    "pichincha_separador_de_miles": ("monto_invalido",),
    "pichincha_pagina_repetida": ("numeracion",),
    "pichincha_resumen_no_cuadra": ("resumen_no_cuadra",),
    "pichincha_corte_contado": (),
    "pichincha_corte_no_contado": ("resumen_no_cuadra",),
    "pichincha_cruza_diciembre": (),
    "pichincha_mes_fuera_del_periodo": ("mes_fuera_del_periodo",),
}


def _repositorio() -> RepositorioEstados:
    return RepositorioEstados(crear_engine("sqlite://"))


def _importar(
    ruta: Path,
    saldos: tuple[int, int],
    repositorio: RepositorioEstados | None = None,
    cuentas: dict[str, str] | None = None,
) -> Importacion:
    return importar_pdf(
        ruta,
        PICHINCHA,
        repositorio or _repositorio(),
        cuentas_locales=CUENTAS if cuentas is None else cuentas,
        topes=BANCOS.topes,
        saldo_anterior_centavos=saldos[0],
        saldo_final_centavos=saldos[1],
    )


def _rechazo(ruta: Path, saldos: tuple[int, int], **kwargs: Any) -> list[tuple[str, str]]:
    with pytest.raises(EstadoRechazado) as error:
        _importar(ruta, saldos, **kwargs)
    return [(p.codigo, p.mensaje) for p in error.value.problemas]


def _escribir(tmp_path: Path, datos: bytes, nombre: str = "variante.pdf") -> Path:
    ruta = tmp_path / nombre
    ruta.write_bytes(datos)
    return ruta


SALDOS = sintetico.CASOS_PDF[0].saldos


# --- Cada fixture con su "debería" ---


def test_cada_fixture_tiene_su_resultado_esperado() -> None:
    assert list(ESPERA) == [caso.nombre for caso in sintetico.CASOS_PDF]
    for caso in sintetico.CASOS_PDF:
        assert (ESPERA[caso.nombre] == ()) == (caso.deberia == "entra"), caso.nombre


def test_los_fixtures_versionados_son_los_que_escribe_el_generador() -> None:
    for nombre, datos in sintetico.pdfs().items():
        assert (FIXTURES / nombre).read_bytes() == datos, nombre


@pytest.mark.parametrize("caso", sintetico.CASOS_PDF, ids=lambda caso: caso.nombre)
def test_cada_pdf_termina_como_dice_el_fixture(caso: Any) -> None:
    ruta = FIXTURES / caso.archivo
    if caso.deberia == "entra":
        resultado = _importar(ruta, caso.saldos)
        assert resultado.estado.periodo_desde == caso.ultimo_corte
        assert resultado.estado.periodo_hasta == caso.este_corte
        assert resultado.estado.cuenta == "ahorros"
        assert resultado.estado.banco == "pichincha"
        return
    with pytest.raises(EstadoRechazado) as error:
        _importar(ruta, caso.saldos)
    codigos = tuple(dict.fromkeys(p.codigo for p in error.value.problemas))
    assert codigos == ESPERA[caso.nombre], [p.mensaje for p in error.value.problemas]


def test_el_estado_base_trae_cada_movimiento_con_su_signo_saldo_y_tipo() -> None:
    estado = leer_pdf(
        FIXTURES / "pichincha.pdf",
        PICHINCHA,
        cuentas_locales=CUENTAS,
        topes=BANCOS.topes,
        saldo_anterior_centavos=SALDOS[0],
        saldo_final_centavos=SALDOS[1],
    )
    esperado = [(f, d, c) for f, _, d, c in sintetico.PICHINCHA]
    assert [(m.fecha, m.descripcion, m.monto_centavos) for m in estado.movimientos] == esperado
    assert [m.linea for m in estado.movimientos] == list(range(1, len(esperado) + 1))
    saldo = SALDOS[0]
    for m in estado.movimientos:
        saldo += m.monto_centavos
        assert m.saldo_centavos == saldo
    assert {m.tipo for m in estado.movimientos} == {"debito", "credito", "comision"}


def test_el_digito_suelto_del_ndoc_no_abre_una_fila() -> None:
    estado = _importar(FIXTURES / "pichincha_doc_partido.pdf", SALDOS)
    assert estado.movimientos == len(sintetico.PICHINCHA)


def test_cruza_diciembre_le_pone_a_cada_fecha_su_anio() -> None:
    caso = next(c for c in sintetico.CASOS_PDF if c.nombre == "pichincha_cruza_diciembre")
    estado = leer_pdf(
        FIXTURES / caso.archivo,
        PICHINCHA,
        cuentas_locales=CUENTAS,
        topes=BANCOS.topes,
        saldo_anterior_centavos=caso.saldos[0],
        saldo_final_centavos=caso.saldos[1],
    )
    assert [m.fecha for m in estado.movimientos] == [f for f, *_ in sintetico._CRUZA_DICIEMBRE]


# --- Mensajes exactos de los fixtures hostiles ---


def test_columna_desplazada_dice_pagina_y_fila() -> None:
    assert _rechazo(FIXTURES / "pichincha_columna_desplazada.pdf", SALDOS) == [
        (
            "fuera_de_columna",
            "Página 2, fila 5: la columna CREDITO trae dos textos y DEBITO ninguno: "
            "un texto quedó fuera de su columna.",
        )
    ]


def test_fila_entre_paginas_se_rechaza_en_las_dos_paginas() -> None:
    assert _rechazo(FIXTURES / "pichincha_fila_entre_paginas.pdf", SALDOS) == [
        ("fila_incompleta", "Página 2, fila 10: falta DESCRIPCION, DEBITO, CREDITO y SALDO."),
        ("texto_sin_fila", "Página 3: hay texto arriba de la primera fila, sin fecha."),
    ]


def test_separador_de_miles() -> None:
    caso = next(c for c in sintetico.CASOS_PDF if c.nombre == "pichincha_separador_de_miles")
    assert _rechazo(FIXTURES / caso.archivo, caso.saldos) == [
        (
            "monto_invalido",
            "Página 2, fila 2: el CREDITO '1,200.00' no es válido: van solo dígitos y dos "
            "decimales con punto, sin separador de miles.",
        )
    ]


def test_pagina_repetida() -> None:
    assert _rechazo(FIXTURES / "pichincha_pagina_repetida.pdf", SALDOS) == [
        (
            "numeracion",
            "Las páginas no van de 1 a 4: el archivo trae 4 y sus pies dicen 1 de 3, 2 de 3, "
            "2 de 3 y 3 de 3.",
        )
    ]


def test_resumen_no_cuadra() -> None:
    assert _rechazo(FIXTURES / "pichincha_resumen_no_cuadra.pdf", SALDOS) == [
        (
            "resumen_no_cuadra",
            "El resumen dice 15 débitos por 302.05 y el detalle trae 14 por 302.05.",
        )
    ]


def test_mes_fuera_del_periodo() -> None:
    caso = next(c for c in sintetico.CASOS_PDF if c.nombre == "pichincha_mes_fuera_del_periodo")
    assert _rechazo(FIXTURES / caso.archivo, caso.saldos) == [
        (
            "mes_fuera_del_periodo",
            "Página 2, fila 3: la fecha '15-jun.' no cae en el periodo 2026-12-14 a 2027-01-14.",
        )
    ]


# --- Rechazos sin fixture, armados con las piezas del generador ---


@pytest.fixture
def parche() -> Iterator[pytest.MonkeyPatch]:
    with pytest.MonkeyPatch.context() as mp:
        yield mp


def _con_resumen(cambio: dict[str, str]) -> bytes:
    """El estado base con textos del resumen (página 1) cambiados: texto viejo → nuevo."""
    original = sintetico._resumen

    def resumen(*args: Any, **kwargs: Any) -> list[Any]:
        return [f._replace(texto=cambio.get(f.texto, f.texto)) for f in original(*args, **kwargs)]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sintetico, "_resumen", resumen)
        datos: bytes = sintetico._estado_pdf(sintetico.PICHINCHA)
    return datos


def test_interes_distinto_de_cero(tmp_path: Path) -> None:
    datos = _con_resumen({"0.00": "0.12"})
    assert _rechazo(_escribir(tmp_path, datos), SALDOS) == [
        (
            "interes",
            "El resumen trae 0.12 de INTERÉS PERIODO: un estado con interés no se importa todavía.",
        )
    ]


def test_saldos_declarados_distintos_del_resumen(tmp_path: Path) -> None:
    ruta = FIXTURES / "pichincha.pdf"
    assert _rechazo(ruta, (SALDOS[0] + 1, SALDOS[1])) == [
        (
            "saldo_declarado",
            "Declaraste un saldo anterior de 523.11 y el resumen dice 523.10.",
        )
    ]


def test_cuenta_distinta_entre_paginas(tmp_path: Path) -> None:
    original = sintetico._cabecera

    def cabecera(*args: Any, **kwargs: Any) -> list[Any]:
        return [
            f._replace(texto="CUENTA: 9900000002") if f.texto.startswith("CUENTA:") else f
            for f in original(*args, **kwargs)
        ]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sintetico, "_cabecera", cabecera)
        datos = sintetico._estado_pdf(sintetico.PICHINCHA)
    problemas = _rechazo(_escribir(tmp_path, datos), SALDOS)
    assert problemas == [
        ("cuenta_distinta", "La página 2 trae otro número de cuenta que la página 1."),
    ]
    assert not any("99000" in mensaje for _, mensaje in problemas)


def test_cuenta_sin_etiqueta_no_muestra_el_numero() -> None:
    problemas = _rechazo(FIXTURES / "pichincha.pdf", SALDOS, cuentas={})
    assert problemas == [
        (
            "cuenta_sin_etiqueta",
            "El número de cuenta de este estado no está en CUENTAS_LOCALES del .env para "
            "pichincha: agrégalo como pichincha:<número>:<etiqueta>.",
        )
    ]
    assert not any(NUMERO in mensaje for _, mensaje in problemas)


def test_la_etiqueta_del_mapa_tiene_que_estar_en_el_yaml() -> None:
    problemas = _rechazo(FIXTURES / "pichincha.pdf", SALDOS, cuentas={NUMERO: "corriente"})
    assert [codigo for codigo, _ in problemas] == ["cuenta_desconocida"]


def test_el_numero_de_cuenta_no_se_guarda() -> None:
    repositorio = _repositorio()
    resultado = _importar(FIXTURES / "pichincha.pdf", SALDOS, repositorio)
    assert NUMERO not in repr(resultado)
    assert NUMERO not in repr(repositorio.vigentes())


def _con_fila(tmp_path: Path, **cambios: Any) -> Path:
    return _escribir(tmp_path, sintetico._estado_pdf(sintetico.PICHINCHA, **cambios))


def test_debito_y_credito_a_la_vez(tmp_path: Path) -> None:
    ruta = _con_fila(tmp_path, textos={(0, 5): "1.00"})
    assert _rechazo(ruta, SALDOS) == [
        (
            "debito_y_credito",
            "Página 2, fila 1: trae débito 25.00 y crédito 1.00; va uno solo distinto de cero.",
        )
    ]


def test_debito_y_credito_en_cero(tmp_path: Path) -> None:
    ruta = _con_fila(tmp_path, textos={(0, 4): "0.00"})
    assert _rechazo(ruta, SALDOS) == [
        (
            "debito_y_credito",
            "Página 2, fila 1: trae débito 0.00 y crédito 0.00; va uno solo distinto de cero.",
        )
    ]


@pytest.mark.parametrize("monto", ["25.000", "25,00", "25.0", "$25.00"])
def test_montos_mal_escritos(tmp_path: Path, monto: str) -> None:
    ruta = _con_fila(tmp_path, textos={(0, 4): monto})
    assert _rechazo(ruta, SALDOS) == [
        (
            "monto_invalido",
            f"Página 2, fila 1: el DEBITO '{monto}' no es válido: van solo dígitos y dos "
            "decimales con punto, sin separador de miles.",
        )
    ]


def test_pagina_sin_pie(tmp_path: Path) -> None:
    original = sintetico._envoltura

    def envoltura(pagina: int, de: int) -> bytes:
        datos: bytes = original(pagina, de)
        return datos.split(b"BT\n/F1 10 Tf")[0] if pagina == 2 else datos

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sintetico, "_envoltura", envoltura)
        datos = sintetico._estado_pdf(sintetico.PICHINCHA)
    assert _rechazo(_escribir(tmp_path, datos), SALDOS) == [
        ("sin_pie", 'La página 2 no trae su pie ("Página N de M").'),
    ]


def test_pagina_sin_encabezado(tmp_path: Path) -> None:
    original = sintetico._cabecera

    def cabecera(*args: Any, **kwargs: Any) -> list[Any]:
        return [f for f in original(*args, **kwargs) if f.texto != "SALDO"]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sintetico, "_cabecera", cabecera)
        datos = sintetico._estado_pdf(sintetico.PICHINCHA)
    assert _rechazo(_escribir(tmp_path, datos), SALDOS) == [
        (
            "sin_encabezado",
            "La página 2 no trae el encabezado del detalle: falta SALDO.",
        )
    ]


def test_un_archivo_que_no_es_pdf(tmp_path: Path) -> None:
    ruta = _escribir(tmp_path, b"no es un pdf")
    assert [c for c, _ in _rechazo(ruta, SALDOS)] == ["pdf_ilegible"]


def test_un_pdf_de_mas_del_tope_no_se_abre(tmp_path: Path) -> None:
    ruta = _escribir(tmp_path, b"%PDF-1.4\n" + b"0" * BANCOS.tope_bytes)
    assert [c for c, _ in _rechazo(ruta, SALDOS)] == ["archivo_grande"]


# --- El día de corte entre dos estados seguidos ---


def _siguiente(filas: list[Any], anterior: int) -> bytes:
    datos: bytes = sintetico._estado_pdf(
        filas, ultimo=date(2026, 11, 2), este=date(2026, 12, 2), anterior=anterior
    )
    return datos


NOVIEMBRE = [
    (date(2026, 11, 2), "1000301", "PAGO SERVICIO SINTETICO", -1000),
    (date(2026, 11, 10), "1000302", "TRANSFERENCIA RECIBIDA SINTETICA", 5000),
]


def test_dos_estados_que_comparten_el_dia_de_corte_entran(tmp_path: Path) -> None:
    repositorio = _repositorio()
    _importar(FIXTURES / "pichincha.pdf", SALDOS, repositorio)
    final = SALDOS[1] + sum(c for *_, c in NOVIEMBRE)
    siguiente = _importar(
        _escribir(tmp_path, _siguiente(NOVIEMBRE, SALDOS[1])), (SALDOS[1], final), repositorio
    )
    assert siguiente.avisos == ()


def test_el_mismo_movimiento_en_el_corte_de_los_dos_estados_se_rechaza(tmp_path: Path) -> None:
    repositorio = _repositorio()
    contado = next(c for c in sintetico.CASOS_PDF if c.nombre == "pichincha_corte_contado")
    _importar(FIXTURES / contado.archivo, contado.saldos, repositorio)
    en_corte = sintetico._EN_CORTE[-1]  # (2026-11-02, ..., PAGO SERVICIO SINTETICO, -1000)
    filas = [en_corte, *NOVIEMBRE[1:]]
    final = contado.saldos[1] + sum(c for *_, c in filas)
    problemas = _rechazo(
        _escribir(tmp_path, _siguiente(filas, contado.saldos[1])),
        (contado.saldos[1], final),
        repositorio=repositorio,
    )
    assert problemas == [
        (
            "corte_repetido",
            "Línea 1: el movimiento del 2026-11-02 por -10.00 ya está en el estado #1 "
            "(2026-10-02 a 2026-11-02), que también cubre ese día de corte.",
        )
    ]


def test_la_continuidad_se_mira_a_traves_del_dia_de_corte(tmp_path: Path) -> None:
    repositorio = _repositorio()
    _importar(FIXTURES / "pichincha.pdf", SALDOS, repositorio)
    anterior = SALDOS[1] - 100
    final = anterior + sum(c for *_, c in NOVIEMBRE)
    problemas = _rechazo(
        _escribir(tmp_path, _siguiente(NOVIEMBRE, anterior)),
        (anterior, final),
        repositorio=repositorio,
    )
    assert [codigo for codigo, _ in problemas] == ["continuidad"]
