"""Los PDF sintéticos con la forma del de Pichincha: reproducibles y bien armados.

Todavía no hay parser que los lea (G1b). Acá se prueba el archivo en sí, con
bytes y sin librerías de PDF: que no cambie entre corridas, que no traiga fecha
ni azar, que la tabla xref apunte a cada objeto y que cada página lleve su pie.
Que los fixtures sirvan para el banco de verdad no lo prueba esto: ver
`docs/conciliacion.md`.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from family_expense_bot.conciliacion.contrato import plata
from tests.sintetico import cargar

sintetico = cargar()
RAIZ = Path(__file__).resolve().parents[1]
PDFS = sintetico.pdfs()


def test_cada_caso_tiene_su_pdf_y_cada_pdf_su_caso() -> None:
    assert [caso.archivo for caso in sintetico.CASOS_PDF] == list(PDFS)
    assert {caso.deberia for caso in sintetico.CASOS_PDF} == {"entra", "rechazo"}


def test_hay_la_base_la_fila_partida_y_las_cinco_variantes_hostiles() -> None:
    nombres = {caso.nombre for caso in sintetico.CASOS_PDF}
    assert {
        "pichincha",
        "pichincha_doc_partido",
        "pichincha_columna_desplazada",
        "pichincha_fila_entre_paginas",
        "pichincha_separador_de_miles",
        "pichincha_pagina_repetida",
        "pichincha_resumen_no_cuadra",
    } <= nombres


def test_dos_llamadas_dan_los_mismos_bytes() -> None:
    assert sintetico.pdfs() == PDFS


def test_otro_proceso_da_los_mismos_bytes(tmp_path: Path) -> None:
    """Sin estado escondido en el módulo: un intérprete nuevo escribe lo mismo."""
    codigo = (
        "import sys; from tests.sintetico import cargar; from pathlib import Path\n"
        "for n, b in cargar().pdfs().items(): (Path(sys.argv[1]) / n).write_bytes(b)"
    )
    subprocess.run([sys.executable, "-c", codigo, str(tmp_path)], cwd=RAIZ, check=True)
    assert {ruta.name: ruta.read_bytes() for ruta in tmp_path.iterdir()} == PDFS


@pytest.mark.parametrize("nombre", list(PDFS))
def test_no_trae_fecha_ni_identificador_ni_compresion(nombre: str) -> None:
    datos = PDFS[nombre]
    assert datos.startswith(b"%PDF-1.4\n")
    assert datos.endswith(b"%%EOF\n")
    for marca in (b"/Info", b"/CreationDate", b"/ModDate", b"/ID", b"/Producer", b"/Filter"):
        assert marca not in datos, marca


@pytest.mark.parametrize("nombre", list(PDFS))
def test_la_tabla_xref_apunta_a_cada_objeto(nombre: str) -> None:
    datos = PDFS[nombre]
    inicio = int(re.search(rb"startxref\n(\d+)\n%%EOF\n$", datos).group(1))  # type: ignore[union-attr]
    assert datos[inicio:].startswith(b"xref\n0 ")
    tamano = int(re.search(rb"/Size (\d+)", datos[inicio:]).group(1))  # type: ignore[union-attr]
    offsets = [int(o) for o in re.findall(rb"(\d{10}) 00000 n ", datos[inicio:])]
    assert len(offsets) == tamano - 1
    for numero, offset in enumerate(offsets, start=1):
        assert datos[offset:].startswith(b"%d 0 obj\n" % numero), numero


@pytest.mark.parametrize("nombre", list(PDFS))
def test_cada_stream_declara_su_largo(nombre: str) -> None:
    streams = re.findall(rb"/Length (\d+) >>\nstream\n", PDFS[nombre])
    cuerpos = re.findall(rb"\nstream\n(.*?)endstream", PDFS[nombre], re.DOTALL)
    assert streams and [int(n) for n in streams] == [len(c) for c in cuerpos]


def _pies(datos: bytes) -> list[tuple[int, int]]:
    return [(int(n), int(m)) for n, m in re.findall(rb"\(P\xe1gina (\d+) de (\d+)\)Tj", datos)]


@pytest.mark.parametrize("nombre", [n for n in PDFS if n != "pichincha_pagina_repetida.pdf"])
def test_cada_pagina_lleva_su_pie_de_1_a_m(nombre: str) -> None:
    datos = PDFS[nombre]
    paginas = int(re.search(rb"/Type /Pages /Kids \[.*?\] /Count (\d+)", datos).group(1))  # type: ignore[union-attr]
    assert _pies(datos) == [(n, paginas) for n in range(1, paginas + 1)]


def test_la_pagina_repetida_repite_el_pie_y_sobra_una_pagina() -> None:
    assert _pies(PDFS["pichincha_pagina_repetida.pdf"]) == [(1, 3), (2, 3), (2, 3), (3, 3)]


def _textos(datos: bytes) -> list[str]:
    """Cada string que se dibuja con Tj, en el orden del archivo."""
    crudos = re.findall(rb"\(((?:[^\\()]|\\.)*)\)Tj", datos)
    return [re.sub(rb"\\(.)", rb"\1", c).decode("cp1252") for c in crudos]


@pytest.mark.parametrize(
    ("nombre", "filas"),
    [("pichincha.pdf", "PICHINCHA"), ("pichincha_cruza_diciembre.pdf", "_CRUZA_DICIEMBRE")],
)
def test_cada_monto_y_cada_saldo_estan_literales(nombre: str, filas: str) -> None:
    textos = _textos(PDFS[nombre])
    saldo = sintetico.PICHINCHA_ANTERIOR
    for _, _, _, centavos in getattr(sintetico, filas):
        saldo += centavos
        assert plata(abs(centavos)) in textos
        assert plata(saldo) in textos
    assert textos.count("SALDO ACTUAL") == 1


def test_el_doc_partido_deja_el_ultimo_digito_en_el_mismo_bloque() -> None:
    """Como el real: el dígito que no cabe va con su propio Tm, sin cerrar el BT."""
    datos = PDFS["pichincha_doc_partido.pdf"]
    bloques = re.findall(rb"BT\n(.*?)ET\n", datos, re.DOTALL)
    partidos = [b for b in bloques if b.count(b"Tm") == 2 and b.count(b"Tj") == 2]
    assert len(partidos) == 3


def test_las_variantes_cambian_lo_que_dicen_y_nada_mas_salta_a_la_vista() -> None:
    base = _textos(PDFS["pichincha.pdf"])
    miles = _textos(PDFS["pichincha_separador_de_miles.pdf"])
    assert "1,200.00" in miles and not any("," in t and t[:1].isdigit() for t in base)
    junio = _textos(PDFS["pichincha_mes_fuera_del_periodo.pdf"])
    assert "15-jun." in junio
    no_cuadra = _textos(PDFS["pichincha_resumen_no_cuadra.pdf"])
    debitos = sum(1 for *_, c in sintetico.PICHINCHA if c < 0)
    assert f"({debitos + 1})" in no_cuadra and f"({debitos})" in base


def test_preparar_escribe_los_pdf_junto_a_los_csv(tmp_path: Path) -> None:
    sintetico.preparar(tmp_path)
    assert {r.name for r in tmp_path.iterdir()} == set(sintetico.archivos()) | set(PDFS)
