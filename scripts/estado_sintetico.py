"""Estados de cuenta sintéticos: unos que cuadran y variantes hostiles que no.

Todo es inventado y determinista (sin azar ni fecha de hoy): correrlo dos veces
escribe los mismos bytes. `tests/test_privacy.py` exige que cada CSV y cada PDF
de `tests/fixtures/estados/` sea exactamente lo que genera este script, así que
ahí no puede colarse un estado real. Los PDF imitan la forma del export de
Pichincha y todavía no tienen parser que los lea (`pdfs()`, `CASOS_PDF`).

    python scripts/estado_sintetico.py              # reescribe tests/fixtures/estados/ y los pares
    python scripts/estado_sintetico.py --tabla      # importa cada caso y muestra qué pasó
    python scripts/estado_sintetico.py --base RUTA  # base sintética del bot, para probar el cruce

`--tabla` corre la importación real contra una base en memoria por caso; su
salida es la tabla de `docs/conciliacion.md` (un test las compara).

`--base` crea una base nueva (se niega si el archivo existe) con gastos
inventados y los estados de marzo a julio ya importados.
Algunos gastos calzan con un movimiento el mismo día, otros a uno o tres días,
uno a cuatro, y hay movimientos sin gasto y gastos en efectivo sin movimiento.

`evals/pares_verdaderos.csv` es la verdad para evaluar la conciliación: una fila
por movimiento con el gasto que lo originó, y una por gasto sin movimiento. Sale
de cómo se declararon los datos acá (cada movimiento y cada gasto nacen con su
etiqueta), no de ninguna regla de cruce. No hay azar: la "semilla fija" es que
todo está escrito a mano y en orden.
"""

import csv
import io
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, NamedTuple

from family_expense_bot.conciliacion.bancos import cargar_bancos
from family_expense_bot.conciliacion.contrato import EstadoRechazado, plata
from family_expense_bot.conciliacion.importar import Importacion, importar_estado
from family_expense_bot.extraction.schema import Confianza, GastoExtraido
from family_expense_bot.storage.repository import (
    RepositorioEstados,
    RepositorioGastos,
    crear_engine,
)

RAIZ = Path(__file__).resolve().parents[1]
DESTINO = RAIZ / "tests" / "fixtures" / "estados"
ID_DEMO = 42

Fila = tuple[date, str, int]
"""Fecha, descripción y centavos con signo (negativo si sale plata)."""

SALDO_INICIAL = 100_000

MARZO: list[Fila] = [
    (date(2026, 3, 2), "SUPERMERCADO SINTETICO", -8540),
    (date(2026, 3, 3), "TRANSFERENCIA RECIBIDA NOMINA", 120_000),
    (date(2026, 3, 4), "GASOLINERA SINTETICA", -2200),
    (date(2026, 3, 5), "STREAMING SINTETICO", -999),
    (date(2026, 3, 6), "RESTAURANTE EL SINTETICO", -1850),
    (date(2026, 3, 9), "BUS URBANO", -35),
    (date(2026, 3, 9), "BUS URBANO", -35),
    (date(2026, 3, 10), "PAGO TARJETA VISA", -4500),
    (date(2026, 3, 12), "CINE SINTETICO", -3500),
    (date(2026, 3, 15), "COMISION RETIRO CAJERO", -45),
    (date(2026, 3, 15), "RETIRO CAJERO", -4000),
    (date(2026, 3, 18), "FARMACIA SINTETICA", -1275),
    (date(2026, 3, 20), "TIENDA DE ROPA SINTETICA", -11_000),
    (date(2026, 3, 23), "SUPERMERCADO SINTETICO", -6410),
    (date(2026, 3, 27), "LIBRERIA SINTETICA", -4000),
    (date(2026, 3, 31), "INTERES GANADO", 12),
    (date(2026, 3, 31), "MANTENIMIENTO DE CUENTA", -150),
]

ABRIL: list[Fila] = [
    (date(2026, 4, 3), "SUPERMERCADO SINTETICO", -7215),
    (date(2026, 4, 5), "STREAMING SINTETICO", -999),
    (date(2026, 4, 15), "COMISION RETIRO CAJERO", -45),
    (date(2026, 4, 15), "RETIRO CAJERO", -2000),
    (date(2026, 4, 30), "MANTENIMIENTO DE CUENTA", -150),
]

GASTOS: list[tuple[int, str, str, str]] = [
    # Día de marzo, monto, categoría y descripción, como los habría escrito alguien en el bot.
    (2, "85.40", "alimentacion", "supermercado"),
    (4, "22.00", "transporte", "gasolina"),
    (5, "18.50", "comida_fuera", "almuerzo"),  # el banco lo cobró el 6
    (9, "0.35", "transporte", "bus"),  # el banco trae dos iguales ese día
    (10, "45.00", "deudas_cuotas", "cuota de la tarjeta"),
    (11, "3.00", "transporte", "taxi en efectivo"),  # no pasa por el banco
    (14, "35.00", "entretenimiento", "cine"),  # el banco lo cobró el 12
    (18, "12.75", "personal", "farmacia"),
    (20, "110.00", "ropa", "zapatos"),
    (23, "64.10", "alimentacion", "supermercado"),
    (23, "40.00", "educacion", "libros"),  # el banco lo cobró el 27: a cuatro días
]

Etiqueta = tuple[tuple[int, ...], str]
"""Ids de gasto (vacío: ninguno) y `categoria_caso` de un movimiento."""

SEMANA = "mismo_monto_misma_semana"
DESFASE = "fecha_desfasada"
SIN_GASTO = "movimiento_sin_gasto"
PARTIDO = "pago_partido"
DIARIO = "diario_desfasado"
GEMELOS = "gemelos_otro_comercio"
VACIAS = "palabras_vacias"
CRUZA_MES = "cruza_de_mes"
FALLOS_ESPERADOS = {PARTIDO, GEMELOS}
"""Casos donde no unir es lo esperado: un cargo con varios gastos o al revés (el
uno a uno no lo resuelve), y dos cargos iguales de comercios distintos con un
gasto cuyo texto no dice de cuál es (unir sería adivinar). Solo se marcan las
filas que tienen gasto verdadero; no entran al recall."""

# Etiquetas de cada fila de MARZO y ABRIL, en el mismo orden. Los ids de gasto son
# la posición (desde 1) en GASTOS: así los guarda la base. El gasto 6 (taxi en
# efectivo) no tiene movimiento. Las dos filas del bus son idénticas: la verdad le
# da el gasto a la primera, pero cualquiera de las dos sería un acierto.
ETIQUETAS_MARZO: list[Etiqueta] = [
    ((1,), "exacto"),
    ((), SIN_GASTO),
    ((2,), "exacto"),
    ((), SIN_GASTO),
    ((3,), DESFASE),
    ((4,), SEMANA),
    ((), SEMANA),
    ((5,), "exacto"),
    ((7,), DESFASE),
    ((), "comision"),
    ((), SIN_GASTO),
    ((8,), "exacto"),
    ((9,), "exacto"),
    ((10,), "exacto"),
    ((11,), "fecha_fuera_de_ventana"),
    ((), "interes"),
    ((), "comision"),
]
ETIQUETAS_ABRIL: list[Etiqueta] = [
    ((), SIN_GASTO),
    ((), SIN_GASTO),
    ((), "comision"),
    ((), SIN_GASTO),
    ((), "comision"),
]

MAYO_GASTOS: dict[str, tuple[date, str, str, str]] = {
    # Clave, fecha, monto, categoría y descripción. El orden da los ids (siguen a GASTOS).
    "super": (date(2026, 5, 4), "91.20", "alimentacion", "supermercado"),
    "gasolina": (date(2026, 5, 6), "24.00", "transporte", "gasolina"),
    "farmacia": (date(2026, 5, 8), "18.90", "personal", "farmacia"),
    "camisa": (date(2026, 5, 8), "45.00", "ropa", "camisa"),  # luego la devuelven
    "cafe1": (date(2026, 5, 9), "7.50", "comida_fuera", "cafe"),
    "propina": (date(2026, 5, 10), "5.00", "comida_fuera", "propina en efectivo"),
    "cafe2": (date(2026, 5, 11), "7.50", "comida_fuera", "cafe"),
    "cine": (date(2026, 5, 12), "28.00", "entretenimiento", "cine"),
    "cena": (date(2026, 5, 12), "22.50", "comida_fuera", "cena"),  # el banco: tres días después
    "papeleria": (date(2026, 5, 13), "15.60", "educacion", "papeleria"),
    "bus": (date(2026, 5, 14), "0.45", "transporte", "bus en efectivo"),  # = la comisión
    "chaqueta": (date(2026, 5, 17), "75.00", "ropa", "chaqueta"),
    "taxi": (date(2026, 5, 18), "11.00", "transporte", "taxi"),
    "libro": (date(2026, 5, 19), "33.00", "educacion", "libro"),  # el banco: tres días antes
    "regalo": (date(2026, 5, 19), "100.00", "personal", "regalo"),  # pagado en dos cargos
    "almuerzo1": (date(2026, 5, 20), "12.00", "comida_fuera", "almuerzo"),
    "almuerzo2": (date(2026, 5, 22), "12.00", "comida_fuera", "almuerzo"),
    "mercado": (date(2026, 5, 22), "18.40", "alimentacion", "mercado en efectivo"),
    "parqueadero": (date(2026, 5, 28), "2.50", "transporte", "parqueadero en efectivo"),
    "super2": (date(2026, 5, 29), "60.00", "alimentacion", "supermercado"),  # un cargo, dos gastos
    "farmacia2": (date(2026, 5, 29), "40.00", "personal", "farmacia"),
}

MAYO_ESPEC: list[tuple[Fila, tuple[tuple[str, ...], str]]] = [
    # Movimiento, y la clave de cada gasto que lo originó (vacío: ninguno) con su caso.
    ((date(2026, 5, 3), "TRANSFERENCIA RECIBIDA NOMINA", 120_000), ((), SIN_GASTO)),
    ((date(2026, 5, 4), "SUPERMERCADO SINTETICO", -9120), (("super",), "exacto")),
    ((date(2026, 5, 6), "GASOLINERA SINTETICA", -2400), (("gasolina",), "exacto")),
    ((date(2026, 5, 7), "STREAMING SINTETICO", -999), ((), SIN_GASTO)),
    ((date(2026, 5, 8), "FARMACIA SINTETICA", -1890), (("farmacia",), "exacto")),
    ((date(2026, 5, 8), "TIENDA DE ROPA SINTETICA", -4500), (("camisa",), "exacto")),
    ((date(2026, 5, 9), "CAFE SINTETICO", -750), (("cafe1",), SEMANA)),
    ((date(2026, 5, 12), "CAFE SINTETICO", -750), (("cafe2",), SEMANA)),
    ((date(2026, 5, 12), "CINE SINTETICO", -2800), (("cine",), "exacto")),
    ((date(2026, 5, 13), "PAPELERIA SINTETICA", -1560), (("papeleria",), "exacto")),
    ((date(2026, 5, 14), "COMISION RETIRO CAJERO", -45), ((), "comision")),
    ((date(2026, 5, 14), "RETIRO CAJERO", -3000), ((), SIN_GASTO)),
    ((date(2026, 5, 15), "RESTAURANTE EL SINTETICO", -2250), (("cena",), DESFASE)),
    ((date(2026, 5, 16), "LIBRERIA SINTETICA", -3300), (("libro",), DESFASE)),
    ((date(2026, 5, 17), "DEVOLUCION TIENDA DE ROPA SINTETICA", 4500), ((), "devolucion")),
    ((date(2026, 5, 18), "TIENDA DE ROPA SINTETICA", -7500), (("chaqueta",), DESFASE)),
    ((date(2026, 5, 19), "TIENDA DE REGALOS SINTETICA", -6000), (("regalo",), PARTIDO)),
    ((date(2026, 5, 19), "TIENDA DE REGALOS SINTETICA", -4000), (("regalo",), PARTIDO)),
    ((date(2026, 5, 20), "TAXI SINTETICO", -1100), (("taxi",), DESFASE)),
    ((date(2026, 5, 21), "RESTAURANTE EL SINTETICO", -1200), (("almuerzo1",), SEMANA)),
    ((date(2026, 5, 23), "RESTAURANTE EL SINTETICO", -1200), (("almuerzo2",), SEMANA)),
    ((date(2026, 5, 26), "DEVOLUCION SUPERMERCADO SINTETICO", 1250), ((), "devolucion")),
    ((date(2026, 5, 27), "SEGURO SINTETICO", -2100), ((), SIN_GASTO)),
    ((date(2026, 5, 29), "SUPERMERCADO SINTETICO", -10_000), (("super2", "farmacia2"), PARTIDO)),
    ((date(2026, 5, 31), "INTERES GANADO", 15), ((), "interes")),
    ((date(2026, 5, 31), "MANTENIMIENTO DE CUENTA", -150), ((), "comision")),
]
MAYO: list[Fila] = [fila for fila, _ in MAYO_ESPEC]

# Junio y julio: los cuatro fallos que reprodujo la revisión de las reglas (B5 a B8).
# Entraron al set antes de cambiar las reglas; el criterio está en docs/conciliacion.md.
JUNIO_GASTOS: dict[str, tuple[date, str, str, str]] = {
    # Los ids siguen a los de MAYO_GASTOS.
    # B6: el mismo almuerzo cuatro días seguidos; el banco cobra cada uno al día siguiente.
    "comedor1": (date(2026, 6, 8), "6.50", "comida_fuera", "almuerzo"),
    "comedor2": (date(2026, 6, 9), "6.50", "comida_fuera", "almuerzo"),
    "comedor3": (date(2026, 6, 10), "6.50", "comida_fuera", "almuerzo"),
    "comedor4": (date(2026, 6, 11), "6.50", "comida_fuera", "almuerzo"),
    # B5: fue en la ferretería, pero el texto no lo dice.
    "arreglo": (date(2026, 6, 15), "23.00", "otros", "arreglo de la casa"),
    # B7: en efectivo; solo comparten palabras vacías con un cargo del mismo monto.
    "encomienda": (date(2026, 6, 15), "31.20", "otros", "encomienda a quito"),
    "regalo_hermana": (date(2026, 6, 17), "19.40", "personal", "regalo para mi hermana"),
    "curso": (date(2026, 6, 18), "27.00", "educacion", "curso online, compra de materiales"),
    # B8: el café del 1 de julio. El del 30 de junio no se registró.
    "cafe_julio": (date(2026, 7, 1), "4.80", "comida_fuera", "cafe"),
}

JUNIO_ESPEC: list[tuple[Fila, tuple[tuple[str, ...], str]]] = [
    ((date(2026, 6, 3), "TRANSFERENCIA RECIBIDA NOMINA", 120_000), ((), SIN_GASTO)),
    ((date(2026, 6, 9), "COMEDOR SINTETICO", -650), (("comedor1",), DIARIO)),
    ((date(2026, 6, 10), "COMEDOR SINTETICO", -650), (("comedor2",), DIARIO)),
    ((date(2026, 6, 11), "COMEDOR SINTETICO", -650), (("comedor3",), DIARIO)),
    ((date(2026, 6, 12), "COMEDOR SINTETICO", -650), (("comedor4",), DIARIO)),
    ((date(2026, 6, 15), "FARMACIA SINTETICA", -2300), ((), GEMELOS)),
    ((date(2026, 6, 15), "FERRETERIA SINTETICA", -2300), (("arreglo",), GEMELOS)),
    ((date(2026, 6, 22), "COMPRA ONLINE QUITO", -3120), ((), VACIAS)),
    ((date(2026, 6, 24), "PAGO PARA SERVICIOS", -1940), ((), VACIAS)),
    ((date(2026, 6, 26), "COMPRA ONLINE SINTETICA", -2700), ((), VACIAS)),
    ((date(2026, 6, 30), "CAFETERIA SINTETICA", -480), ((), CRUZA_MES)),
]
JUNIO: list[Fila] = [fila for fila, _ in JUNIO_ESPEC]

JULIO_ESPEC: list[tuple[Fila, tuple[tuple[str, ...], str]]] = [
    ((date(2026, 7, 1), "CAFETERIA SINTETICA", -480), (("cafe_julio",), CRUZA_MES)),
    ((date(2026, 7, 31), "MANTENIMIENTO DE CUENTA", -150), ((), "comision")),
]
JULIO: list[Fila] = [fila for fila, _ in JULIO_ESPEC]

GASTOS_CON_CLAVE = {**MAYO_GASTOS, **JUNIO_GASTOS}
assert len(GASTOS_CON_CLAVE) == len(MAYO_GASTOS) + len(JUNIO_GASTOS), "claves repetidas"

MARZO_DESDE, MARZO_HASTA = date(2026, 3, 1), date(2026, 3, 31)
MAYO_DESDE, MAYO_HASTA = date(2026, 5, 1), date(2026, 5, 31)
ABRIL_DESDE, ABRIL_HASTA = date(2026, 4, 1), date(2026, 4, 30)
JUNIO_DESDE, JUNIO_HASTA = date(2026, 6, 1), date(2026, 6, 30)
JULIO_DESDE, JULIO_HASTA = date(2026, 7, 1), date(2026, 7, 31)

ENCABEZADO = ["Fecha", "Descripción", "Monto", "Saldo"]
ENCABEZADO_PARES = ["estado", "movimiento", "gasto_id", "categoria_caso", "fallo_esperado"]
DESTINO_PARES = Path(__file__).resolve().parents[1] / "evals" / "pares_verdaderos.csv"
ENCABEZADO_COMA = ["Fecha", "Detalle", "Débito", "Crédito"]
TITULO_COMA = [["Banco Sintético", "Estado de cuenta"], ["Cuenta de ahorros", "Marzo 2026"]]

Celdas = list[list[str]]


def _final(filas: list[Fila], anterior: int) -> int:
    return anterior + sum(centavos for _, _, centavos in filas)


def _con_saldo(filas: list[Fila], anterior: int) -> Celdas:
    """Como exporta `banco_sintetico`: dd/mm/aaaa, monto con signo y saldo después de cada fila."""
    celdas, saldo = [], anterior
    for fecha, descripcion, centavos in filas:
        saldo += centavos
        celdas.append([fecha.strftime("%d/%m/%Y"), descripcion, plata(centavos), plata(saldo)])
    return celdas


def _con_coma(filas: list[Fila]) -> Celdas:
    """Como exporta `banco_sintetico_coma`: fecha ISO, débito y crédito aparte, coma decimal."""
    return [
        [
            fecha.isoformat(),
            descripcion,
            plata(-centavos).replace(".", ",") if centavos < 0 else "",
            plata(centavos).replace(".", ",") if centavos > 0 else "",
        ]
        for fecha, descripcion, centavos in filas
    ]


def _csv(filas: Celdas, delimitador: str = ",", fin_de_linea: str = "\n") -> bytes:
    salida = io.StringIO(newline="")
    csv.writer(salida, delimiter=delimitador, lineterminator=fin_de_linea).writerows(filas)
    return salida.getvalue().encode("utf-8")


def _punto(celdas: Celdas, encabezado: list[str] | None = None) -> bytes:
    return _csv([encabezado or ENCABEZADO, *celdas])


def _coma(celdas: Celdas) -> bytes:
    return _csv([*TITULO_COMA, ENCABEZADO_COMA, *celdas], delimitador=";")


def _cambiar(celdas: Celdas, cambios: dict[tuple[int, int], str]) -> Celdas:
    """Una copia con algunas celdas cambiadas: (fila, columna) → texto."""
    copia = [fila.copy() for fila in celdas]
    for (fila, columna), texto in cambios.items():
        copia[fila][columna] = texto
    return copia


def archivos() -> dict[str, bytes]:
    """Nombre → contenido de cada CSV sintético. Es lo único que se versiona."""
    marzo = _con_saldo(MARZO, SALDO_INICIAL)
    abril = _con_saldo(ABRIL, _final(MARZO, SALDO_INICIAL))
    coma = _con_coma(MARZO)
    sin_el_pago = MARZO[:7] + MARZO[8:]
    con_bloque = MARZO[:5] + MARZO[2:5] + MARZO[5:]
    al_reves = [[fecha, detalle, credito, debito] for fecha, detalle, debito, credito in coma]
    formulas = ["=1+1", "+593 99 000 0000", "-DESCUENTO SINTETICO", "@comercio_sintetico"]
    formulas.append('=HYPERLINK("http://ejemplo.invalid";"clic")')
    mes_dia = {(i, 0): fecha.strftime("%m/%d/%Y") for i, (fecha, _, _) in enumerate(MARZO)}
    return {
        # Los que cuadran.
        "marzo.csv": _punto(marzo),
        "abril.csv": _punto(abril),
        "marzo_coma.csv": _coma(coma),
        # Formato del archivo.
        "con_bom.csv": b"\xef\xbb\xbf" + _punto(marzo),
        "reexportado.csv": _csv([ENCABEZADO, *marzo], fin_de_linea="\r\n"),
        "lineas_vacias.csv": _punto([*marzo[:4], [], *marzo[4:9], ["", "", "", ""], *marzo[9:], []])
        + b"\n",
        "encabezado_de_otro_banco.csv": _punto(marzo, ["Date", "Description", "Amount", "Balance"]),
        "sin_movimientos.csv": _punto([]),
        # Filas que no se entienden.
        "fila_a_medias.csv": _punto(_cambiar(marzo, {(6, 2): "", (6, 3): ""})),
        "coma_en_banco_de_punto.csv": _punto(_cambiar(marzo, {(0, 2): "-85,40"})),
        "separador_de_miles.csv": _punto(_cambiar(marzo, {(1, 2): "1,200.00"})),
        "tres_decimales.csv": _punto(_cambiar(marzo, {(0, 2): "-85.400"})),
        "monto_cero.csv": _punto(_cambiar(marzo, {(2, 2): "0.00"})),
        "fecha_imposible.csv": _punto(_cambiar(marzo, {(4, 0): "31/02/2026"})),
        "fecha_mes_dia.csv": _punto(_cambiar(marzo, mes_dia)),
        "descripciones_con_formula.csv": _punto(
            _cambiar(marzo, {(i, 1): texto for i, texto in enumerate(formulas)})
        ),
        # Se entienden, pero no cuadran.
        "fecha_fuera_de_periodo.csv": _punto(_cambiar(marzo, {(16, 0): "01/04/2026"})),
        "primera_fila_corrida.csv": _punto(_cambiar(marzo, {(0, 2): "-85.39"})),
        "errores_compensados.csv": _punto(_cambiar(marzo, {(0, 2): "-85.39", (3, 2): "-10.00"})),
        "errores_compensados_sin_saldo.csv": _coma(
            _cambiar(coma, {(0, 2): "85,39", (3, 2): "10,00"})
        ),
        "fila_perdida.csv": _punto(marzo[:7] + marzo[8:]),
        "fila_perdida_sin_saldo.csv": _coma(_con_coma(sin_el_pago)),
        "bloque_duplicado.csv": _punto(marzo[:5] + marzo[2:5] + marzo[5:]),
        "bloque_duplicado_sin_saldo.csv": _coma(_con_coma(con_bloque)),
        "signo_invertido.csv": _coma(al_reves),
        "abril_otro_saldo.csv": _punto(_con_saldo(ABRIL, _final(MARZO, SALDO_INICIAL) - 100)),
    }


ENORME = "archivo_enorme.csv"
"""No se versiona: pasa del tope de tamaño. `preparar` lo escribe cuando hace falta."""


_SALDOS = (SALDO_INICIAL, _final(MARZO, SALDO_INICIAL))
_SALDOS_ABRIL = (_SALDOS[1], _final(ABRIL, _SALDOS[1]))
_SALDOS_MAYO = (_SALDOS_ABRIL[1], _final(MAYO, _SALDOS_ABRIL[1]))
_SALDOS_JUNIO = (_SALDOS_MAYO[1], _final(JUNIO, _SALDOS_MAYO[1]))
_SALDOS_JULIO = (_SALDOS_JUNIO[1], _final(JULIO, _SALDOS_JUNIO[1]))
MAYO_CSV, JUNIO_CSV, JULIO_CSV = "mayo.csv", "junio.csv", "julio.csv"
"""Solo los usan la base sintética y los pares: no se versionan, `archivos()` no los trae."""

ESTADOS_BASE: list[tuple[str, date, date, tuple[int, int]]] = [
    ("marzo.csv", MARZO_DESDE, MARZO_HASTA, _SALDOS),
    ("abril.csv", ABRIL_DESDE, ABRIL_HASTA, _SALDOS_ABRIL),
    (MAYO_CSV, MAYO_DESDE, MAYO_HASTA, _SALDOS_MAYO),
    (JUNIO_CSV, JUNIO_DESDE, JUNIO_HASTA, _SALDOS_JUNIO),
    (JULIO_CSV, JULIO_DESDE, JULIO_HASTA, _SALDOS_JULIO),
]
"""Los estados de la base sintética, en orden: archivo, periodo y saldos declarados."""


@dataclass(frozen=True)
class Caso:
    nombre: str
    que: str
    """Qué trae el archivo, en una línea."""
    archivo: str
    espera: tuple[str, ...] = ()
    """Códigos de rechazo, sin repetir. Vacío: el estado entra."""
    avisos: int = 0
    banco: str = "banco_sintetico"
    abril: bool = False
    """El periodo es abril, no marzo."""
    saldos: tuple[int, int] = _SALDOS
    """Saldo anterior y final declarados, en centavos. Por defecto, los de marzo."""
    despues_de_marzo: bool = False
    """Se importa en una base que ya tiene `marzo.csv`."""


_COMA = {"banco": "banco_sintetico_coma"}

CASOS = [
    Caso("marzo", "Estado que cuadra, con columna de saldo", "marzo.csv"),
    Caso(
        "abril",
        "El mes siguiente; empata con marzo",
        "abril.csv",
        abril=True,
        saldos=_SALDOS_ABRIL,
    ),
    Caso(
        "marzo_coma",
        "Coma decimal, punto y coma, débito y crédito aparte, sin columna de saldo",
        "marzo_coma.csv",
        **_COMA,
    ),
    Caso(
        "saldo_un_centavo",
        "Saldo final declarado un centavo arriba",
        "marzo_coma.csv",
        ("saldo_no_cuadra",),
        banco="banco_sintetico_coma",
        saldos=(_SALDOS[0], _SALDOS[1] + 1),
    ),
    Caso(
        "fecha_fuera_de_periodo",
        "Un movimiento fechado un día después del periodo",
        "fecha_fuera_de_periodo.csv",
        ("fecha_fuera_de_periodo",),
    ),
    Caso(
        "coma_en_banco_de_punto",
        'Un monto con coma decimal ("-85,40") en un banco de punto',
        "coma_en_banco_de_punto.csv",
        ("monto_invalido",),
    ),
    Caso("lineas_vacias", "Líneas totalmente vacías en medio y al final", "lineas_vacias.csv"),
    Caso(
        "fila_a_medias",
        "Una fila sin monto ni saldo",
        "fila_a_medias.csv",
        ("fila_incompleta",),
    ),
    Caso(
        "archivo_repetido",
        "El mismo archivo por segunda vez",
        "marzo.csv",
        ("archivo_repetido",),
        despues_de_marzo=True,
    ),
    Caso(
        "reexportado",
        "El mismo mes exportado de nuevo (otros bytes: fin de línea de Windows)",
        "reexportado.csv",
        ("periodo_solapado",),
        despues_de_marzo=True,
    ),
    Caso(
        "separador_de_miles",
        'Un monto con separador de miles ("1,200.00")',
        "separador_de_miles.csv",
        ("monto_invalido",),
    ),
    Caso(
        "tres_decimales",
        'Un monto con tres decimales ("-85.400")',
        "tres_decimales.csv",
        ("monto_invalido",),
    ),
    Caso(
        "encabezado_de_otro_banco",
        "Encabezado con las columnas de otro banco",
        "encabezado_de_otro_banco.csv",
        ("encabezado",),
    ),
    Caso(
        "fecha_imposible",
        "Una fecha que no existe (31/02/2026)",
        "fecha_imposible.csv",
        ("fecha_invalida",),
    ),
    Caso(
        "fecha_mes_dia",
        "Fechas en mes/día/año en un banco de día/mes/año",
        "fecha_mes_dia.csv",
        ("fecha_invalida",),
    ),
    Caso(
        "errores_compensados",
        "Dos montos corridos +1 y -1 centavo; el total cuadra",
        "errores_compensados.csv",
        ("saldo_corrido",),
    ),
    Caso(
        "errores_compensados_sin_saldo",
        "Lo mismo en un banco sin columna de saldo (límite conocido)",
        "errores_compensados_sin_saldo.csv",
        **_COMA,
    ),
    Caso(
        "primera_fila_corrida",
        "El monto de la primera fila corrido un centavo",
        "primera_fila_corrida.csv",
        ("saldo_corrido", "saldo_no_cuadra"),
    ),
    Caso(
        "fila_perdida",
        "Falta una fila, con columna de saldo",
        "fila_perdida.csv",
        ("saldo_corrido", "saldo_no_cuadra"),
    ),
    Caso(
        "fila_perdida_sin_saldo",
        "Falta una fila, sin columna de saldo",
        "fila_perdida_sin_saldo.csv",
        ("saldo_no_cuadra",),
        **_COMA,
    ),
    Caso(
        "bloque_duplicado",
        "Tres filas pegadas dos veces, con columna de saldo",
        "bloque_duplicado.csv",
        ("saldo_corrido", "saldo_no_cuadra", "bloque_duplicado"),
    ),
    Caso(
        "bloque_duplicado_sin_saldo",
        "Tres filas pegadas dos veces, sin columna de saldo",
        "bloque_duplicado_sin_saldo.csv",
        ("saldo_no_cuadra", "bloque_duplicado"),
        **_COMA,
    ),
    Caso(
        "descripciones_con_formula",
        "Descripciones que empiezan con = + - @",
        "descripciones_con_formula.csv",
    ),
    Caso("con_bom", "UTF-8 con BOM", "con_bom.csv"),
    Caso(
        "archivo_enorme",
        "Un archivo un byte más grande que el tope",
        ENORME,
        ("archivo_grande",),
    ),
    Caso(
        "signo_invertido",
        "Débitos y créditos cambiados de columna",
        "signo_invertido.csv",
        ("saldo_no_cuadra",),
        **_COMA,
    ),
    Caso("monto_cero", "Un movimiento en cero", "monto_cero.csv", ("monto_cero",)),
    Caso(
        "sin_movimientos",
        "Sin movimientos, pero con saldo final distinto del anterior",
        "sin_movimientos.csv",
        ("saldo_no_cuadra",),
        saldos=(SALDO_INICIAL, SALDO_INICIAL + 500),
    ),
    Caso(
        "continuidad",
        "Abril empieza con un saldo distinto del que cerró marzo",
        "abril_otro_saldo.csv",
        ("continuidad",),
        abril=True,
        saldos=(_SALDOS_ABRIL[0] - 100, _SALDOS_ABRIL[1] - 100),
        despues_de_marzo=True,
    ),
]


def preparar(carpeta: Path, con_enorme: bool = False) -> None:
    """Escribe los CSV y los PDF en `carpeta`. El enorme solo si se pide: no va al repo."""
    carpeta.mkdir(parents=True, exist_ok=True)
    for nombre, contenido in (archivos() | pdfs()).items():
        (carpeta / nombre).write_bytes(contenido)
    if con_enorme:
        marzo = archivos()["marzo.csv"]
        relleno = cargar_bancos().tope_bytes + 1 - len(marzo)
        (carpeta / ENORME).write_bytes(marzo + b"\n" * relleno)


def correr(caso: Caso, carpeta: Path) -> Importacion | EstadoRechazado:
    """Importa el caso de verdad, en una base en memoria nueva. Devuelve lo que pasó."""
    bancos = cargar_bancos()
    repositorio = RepositorioEstados(crear_engine("sqlite://"))

    def importar(archivo: str, un_caso: Caso) -> Importacion:
        anterior, final = un_caso.saldos
        return importar_estado(
            carpeta / archivo,
            bancos.bancos[un_caso.banco],
            repositorio,
            cuenta="ahorros",
            desde=ABRIL_DESDE if un_caso.abril else MARZO_DESDE,
            hasta=ABRIL_HASTA if un_caso.abril else MARZO_HASTA,
            topes=bancos.topes,
            saldo_anterior_centavos=anterior,
            saldo_final_centavos=final,
        )

    if caso.despues_de_marzo:
        importar("marzo.csv", CASOS[0])
    try:
        return importar(caso.archivo, caso)
    except EstadoRechazado as rechazo:
        return rechazo


MAXIMO_MENSAJES = 3


def tabla() -> str:
    """Qué pasa con cada caso, CSV y PDF, y con qué mensaje exacto, en Markdown."""
    from family_expense_bot.conciliacion.importar import importar_pdf

    lineas = ["| Caso | Qué trae | Resultado | Mensaje exacto |", "|---|---|---|---|"]

    def fila(nombre: str, que: str, resultado: Importacion | EstadoRechazado) -> None:
        if isinstance(resultado, EstadoRechazado):
            veredicto = "Rechazado"
            mensajes = [p.mensaje for p in resultado.problemas]
        else:
            veredicto = "Entra con aviso" if resultado.avisos else "Entra"
            mensajes = list(resultado.avisos) or [f"{resultado.movimientos} movimientos"]
        de_mas = len(mensajes) - MAXIMO_MENSAJES
        if de_mas > 0:
            mensajes = [*mensajes[:MAXIMO_MENSAJES], f"(y {de_mas} más)"]
        celda = "<br>".join(mensajes).replace("|", "\\|")
        lineas.append(f"| `{nombre}` | {que} | {veredicto} | {celda} |")

    with tempfile.TemporaryDirectory() as temporal:
        preparar(Path(temporal), con_enorme=True)
        for caso in CASOS:
            fila(caso.nombre, caso.que, correr(caso, Path(temporal)))
        bancos = cargar_bancos()
        for caso_pdf in CASOS_PDF:
            resultado: Importacion | EstadoRechazado
            try:
                resultado = importar_pdf(
                    Path(temporal) / caso_pdf.archivo,
                    bancos.bancos_pdf["pichincha"],
                    RepositorioEstados(crear_engine("sqlite://")),
                    cuentas_locales={_CUENTA: "ahorros"},
                    topes=bancos.topes,
                    saldo_anterior_centavos=caso_pdf.saldos[0],
                    saldo_final_centavos=caso_pdf.saldos[1],
                )
            except EstadoRechazado as rechazo:
                resultado = rechazo
            fila(caso_pdf.nombre, caso_pdf.que, resultado)
    return "\n".join(lineas)


def gastos_sinteticos() -> list[tuple[date, str, str, str]]:
    """Fecha, monto, categoría y descripción de cada gasto de la base, en orden de id (desde 1)."""
    marzo = [(date(2026, 3, dia), monto, cat, desc) for dia, monto, cat, desc in GASTOS]
    return marzo + list(GASTOS_CON_CLAVE.values())


def pares_verdaderos() -> bytes:
    """El CSV de verdad: qué gasto originó cada movimiento, según cómo se declararon los datos.

    Una fila por movimiento (`estado` es el periodo, `movimiento` su línea en el archivo)
    y una por gasto sin movimiento (`estado` y `movimiento` en "ninguno"). `gasto_id` es
    "ninguno", un id o varios con "|" (un cargo que son varios gastos).
    """
    clave_a_id = {
        clave: len(GASTOS) + posicion for posicion, clave in enumerate(GASTOS_CON_CLAVE, start=1)
    }

    def etiquetas(espec: list[tuple[Fila, tuple[tuple[str, ...], str]]]) -> list[Etiqueta]:
        return [(tuple(clave_a_id[c] for c in claves), caso) for _, (claves, caso) in espec]

    estados: list[tuple[str, list[Fila], list[Etiqueta], int]] = [
        ("2026-03", MARZO, ETIQUETAS_MARZO, 0),
        ("2026-04", ABRIL, ETIQUETAS_ABRIL, 0),
        ("2026-05", MAYO, etiquetas(MAYO_ESPEC), 0),
        ("2026-06", JUNIO, etiquetas(JUNIO_ESPEC), 0),
        ("2026-07", JULIO, etiquetas(JULIO_ESPEC), 0),
    ]
    filas: list[list[str]] = [list(ENCABEZADO_PARES)]
    con_movimiento: set[int] = set()
    for periodo, movimientos, etiquetas, _ in estados:
        assert len(movimientos) == len(etiquetas), periodo
        assert [f for f, _, _ in movimientos] == sorted(f for f, _, _ in movimientos), periodo
        for indice, (ids, caso) in enumerate(etiquetas):
            con_movimiento.update(ids)
            fallo = "si" if caso in FALLOS_ESPERADOS and ids else "no"
            gasto = "|".join(map(str, ids)) or "ninguno"
            # La línea 1 es el encabezado del CSV del banco.
            filas.append([periodo, str(indice + 2), gasto, caso, fallo])
    for gasto_id in range(1, len(gastos_sinteticos()) + 1):
        if gasto_id not in con_movimiento:
            filas.append(["ninguno", "ninguno", str(gasto_id), "gasto_sin_movimiento", "no"])
    return _csv(filas)


def base_sintetica(ruta: Path) -> tuple[int, int]:
    """Una base nueva del bot con gastos inventados y los estados de marzo a julio importados.

    Devuelve cuántos gastos y cuántos movimientos quedaron. Nunca escribe sobre
    una base que ya existe.
    """
    if ruta.exists():
        raise SystemExit(f"{ruta} ya existe: no se escribe sobre una base que ya existe.")
    ruta.parent.mkdir(parents=True, exist_ok=True)
    engine = crear_engine(f"sqlite:///{ruta}")
    gastos = RepositorioGastos(engine)
    for esperado, (fecha, monto, categoria, descripcion) in enumerate(gastos_sinteticos(), 1):
        guardado = gastos.guardar(
            GastoExtraido(
                monto=Decimal(monto),
                categoria=categoria,
                fecha=fecha,
                atribuido_a="demian",
                descripcion=descripcion,
                texto_original=f"{monto} {descripcion}",
                remitente="demian",
                confianza_monto=Confianza.ALTA,
                confianza_categoria=Confianza.ALTA,
                confianza_fecha=Confianza.ALTA,
                confianza_atribuido_a=Confianza.ALTA,
            ),
            registrado_por=ID_DEMO,
        )
        assert guardado.id == esperado, "los ids de gasto de pares_verdaderos.csv se desfasaron"
    bancos = cargar_bancos()
    movimientos = 0
    with tempfile.TemporaryDirectory() as temporal:
        preparar(Path(temporal))
        for archivo, filas, saldos in (
            (MAYO_CSV, MAYO, _SALDOS_MAYO),
            (JUNIO_CSV, JUNIO, _SALDOS_JUNIO),
            (JULIO_CSV, JULIO, _SALDOS_JULIO),
        ):
            (Path(temporal) / archivo).write_bytes(_punto(_con_saldo(filas, saldos[0])))
        for archivo, desde, hasta, (anterior, final) in ESTADOS_BASE:
            movimientos += importar_estado(
                Path(temporal) / archivo,
                bancos.bancos["banco_sintetico"],
                RepositorioEstados(engine),
                cuenta="ahorros",
                desde=desde,
                hasta=hasta,
                topes=bancos.topes,
                saldo_anterior_centavos=anterior,
                saldo_final_centavos=final,
            ).movimientos
    return len(gastos_sinteticos()), movimientos


# --- PDF con la forma del de Pichincha (G1b). Todavía no hay parser que los lea. ---
#
# El PDF se escribe a mano, sin dependencias: objetos, un content stream sin
# comprimir y la tabla xref con los offsets reales. Sin /Info ni /ID (son
# opcionales y traerían fecha o azar), así que dos corridas dan los mismos bytes.
# La forma imita al export del banco, medida sobre un estado real en local y sin
# copiar nada de su contenido (ver docs/conciliacion.md): una envoltura que
# dibuja la página como Form XObject y escribe "Página N de M" en Helvetica, y
# adentro un BT/Tm/Tf/rg/Tj/g/ET por celda, en ArialMT sin embeber. Los montos se
# alinean con las mismas métricas (las de Arial son las de Helvetica).

MESES_PICHINCHA = {
    1: "ene.",
    2: "feb.",
    3: "mar.",
    4: "abr.",
    5: "may.",
    6: "jun.",
    7: "jul.",
    8: "ago.",
    9: "sep.",
    10: "oct.",
    11: "nov.",
    12: "dic.",
}
"""Solo `oct.` y `nov.` están vistos en el real; el resto es la abreviatura común."""

_ANCHOS = dict(
    zip(
        " !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`"
        "abcdefghijklmnopqrstuvwxyz{|}~",
        [
            278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
            *[556] * 10,
            278, 278, 584, 584, 584, 556, 1015,
            667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667,
            778, 722, 667, 611, 722, 667, 944, 667, 667, 611,
            278, 278, 278, 469, 556, 333,
            556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556,
            556, 333, 500, 278, 556, 500, 722, 500, 500, 500,
            334, 260, 334, 584,
        ],
        strict=True,
    )
)  # fmt: skip
_ANCHOS |= {"Á": 667, "É": 667, "Í": 278, "Ó": 778, "Ú": 722, "Ñ": 722}
_ANCHOS |= {"á": 556, "é": 556, "í": 222, "ó": 556, "ú": 556, "ñ": 556}
"""Métricas AFM de Helvetica, en milésimas del tamaño de la letra."""


def _ancho(texto: str, tamano: float) -> float:
    return sum(_ANCHOS[c] for c in texto) * tamano / 1000


def _n(x: float) -> str:
    """Un número del content stream, siempre igual: hasta dos decimales y sin ceros de más."""
    return f"{x:.2f}".rstrip("0").rstrip(".")


def _cadena(texto: str) -> bytes:
    """Un string literal de PDF en WinAnsi (cp1252), con \\, ( y ) escapados."""
    crudo = texto.encode("cp1252")
    return b"(" + crudo.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


class Fragmento(NamedTuple):
    """Un texto de la página, en la unidad que lo escribe el banco: un `Tj`."""

    x: float | None
    """None: sigue en la misma línea que el anterior, sin `Tm`."""
    y: float
    tamano: float
    texto: str
    negrita: bool = False
    """El real la simula con `2 Tr` (relleno y trazo)."""
    sigue: bool = False
    """Va en el mismo BT que el anterior, como hace el real con una celda partida."""


def _form(fragmentos: list[Fragmento]) -> bytes:
    """El Form XObject de una página, con los mismos bloques que el real.

    Un BT por celda (`Tm Tf [Tr] rg Tj g [Tr]`); lo que sigue en el mismo BT agrega
    `[Tm] [Tr] rg Tj g [Tr]`. Abajo, una raya bajo cada fila y los dos bloques de
    `Td` sin texto que el real trae en cada página.
    """
    partes = [b"BT\n0 0 Td\nET\n"]
    for k, f in enumerate(fragmentos):
        if not f.sigue:
            partes.append(b"BT\n")
        if f.x is not None:
            partes.append(b"1 0 0 1 %s %s Tm\n" % (_n(f.x).encode(), _n(f.y).encode()))
        if not f.sigue:
            partes.append(b"/F1 %s Tf\n" % _n(f.tamano).encode())
        partes.append(b"2 Tr\n" if f.negrita else b"")
        partes.append(b"0 0 0 rg\n%sTj\n0 g\n" % _cadena(f.texto))
        partes.append(b"0 Tr\n" if f.negrita else b"")
        if k + 1 == len(fragmentos) or not fragmentos[k + 1].sigue:
            partes.append(b"ET\n")
    partes.append(b"BT\n1 0 0 1 50 792 Tm\n0 0 Td\n0 0 Td\nET\n")
    filas = {
        f.y for f in fragmentos if f.tamano == 9 and f.x is not None and f.x < 63.3 and f.y < 620
    }
    for y in sorted(filas, reverse=True):
        y -= 4
        partes.append(
            b"q\n0.5 w\n0.8 G\n17 %s m\n578 %s l\nS\nQ\n" % (_n(y).encode(), _n(y).encode())
        )
    return b"".join(partes)


def _envoltura(pagina: int, de: int) -> bytes:
    """Lo que el real dibuja fuera del Form: el Form y "Página N de M" en Helvetica."""
    pie = _cadena(f"Página {pagina} de {de}")
    return (
        b"q\nBT\n50 792 Td\nET\nQ\nq 1 0 0 1 0 0 cm /Xf1 Do Q\n"
        b"BT\n/F1 10 Tf\n1 0 0 1 507 811 Tm\n" + pie + b"Tj\nET\n"
    )


def _pdf(paginas: list[tuple[bytes, int, int]]) -> bytes:
    """Arma el archivo: cada página es (form, N, M) con el N y el M que dice su pie."""
    anchos = " ".join(str(_ANCHOS.get(chr(c).encode("latin-1").decode("cp1252", "replace"), 556))
                      for c in range(32, 251))  # fmt: skip
    objetos: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # Pages: se llena al final, cuando se saben los ids de cada página
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /TrueType /BaseFont /ArialMT /FirstChar 32 /LastChar 250 "
        b"/Widths [" + anchos.encode() + b"] /Encoding /WinAnsiEncoding /FontDescriptor 5 0 R >>",
        b"<< /Type /FontDescriptor /FontName /ArialMT /Flags 32 /FontBBox [-665 -325 2000 1006] "
        b"/ItalicAngle 0 /Ascent 905 /Descent -212 /CapHeight 716 /StemV 80 >>",
    ]
    kids = []
    for form, pagina, de in paginas:
        base = len(objetos) + 1  # ids: página, envoltura, form
        envoltura = _envoltura(pagina, de)
        objetos.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << "
            b"/F1 3 0 R >> /XObject << /Xf1 %d 0 R >> >> /Contents %d 0 R >>" % (base + 2, base + 1)
        )
        objetos.append(b"<< /Length %d >>\nstream\n%sendstream" % (len(envoltura), envoltura))
        objetos.append(
            b"<< /Type /XObject /Subtype /Form /FormType 1 /BBox [0 0 595 842] "
            b"/Matrix [1 0 0 1 0 0] /Resources << /Font << /F1 4 0 R >> >> /Length %d >>\n"
            b"stream\n%sendstream" % (len(form), form)
        )
        kids.append(b"%d 0 R" % base)
    objetos[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(kids), len(kids))

    salida = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for numero, cuerpo in enumerate(objetos, start=1):
        offsets.append(len(salida))
        salida += b"%d 0 obj\n%s\nendobj\n" % (numero, cuerpo)
    inicio_xref = len(salida)
    salida += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objetos) + 1)
    salida += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    salida += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objetos) + 1,
        inicio_xref,
    )
    return bytes(salida)


FilaPdf = tuple[date, str, str, int]
"""Fecha, N.DOC, descripción y centavos con signo (negativo si sale plata)."""

PICHINCHA_ANTERIOR = 52_310
PICHINCHA_ULTIMO_CORTE, PICHINCHA_ESTE_CORTE = date(2026, 10, 2), date(2026, 11, 2)
PICHINCHA: list[FilaPdf] = [
    (date(2026, 10, 3), "1000101", "TRANSFERENCIA ENVIADA SINTETICA", -2500),
    (date(2026, 10, 5), "1000102", "TRANSFERENCIA RECIBIDA SINTETICA", 15_000),
    (date(2026, 10, 6), "1000103", "PAGO SERVICIO SINTETICO", -1875),
    (date(2026, 10, 8), "1000104", "TRANSFERENCIA ENVIADA SINTETICA", -980),
    (date(2026, 10, 9), "1000105", "COMPRA SUPERMERCADO SINTETICO", -6420),
    (date(2026, 10, 12), "1000106", "TRANSFERENCIA ENVIADA SINTETICA", -350),
    (date(2026, 10, 14), "1000107", "TRANSFERENCIA RECIBIDA SINTETICA", 4000),
    (date(2026, 10, 15), "1000108", "COMISION SINTETICA", -45),
    (date(2026, 10, 15), "1000109", "RETIRO CAJERO SINTETICO", -6000),
    (date(2026, 10, 18), "1000110", "TRANSFERENCIA ENVIADA SINTETICA", -1210),
    (date(2026, 10, 20), "1000111", "COMPRA FARMACIA SINTETICA", -1595),
    (date(2026, 10, 22), "1000112", "TRANSFERENCIA ENVIADA SINTETICA", -700),
    (date(2026, 10, 24), "1000113", "TRANSFERENCIA RECIBIDA SINTETICA", 2550),
    (date(2026, 10, 26), "1000114", "TRANSFERENCIA ENVIADA SINTETICA", -4300),
    (date(2026, 10, 28), "1000115", "PAGO SERVICIO SINTETICO", -2215),
    (date(2026, 10, 30), "1000116", "TRANSFERENCIA ENVIADA SINTETICA", -125),
    (date(2026, 11, 1), "1000117", "TRANSFERENCIA ENVIADA SINTETICA", -1890),
]

# Cada columna empieza donde el real deja una celda vacía de 5 pt bajo la tabla.
_BORDES = (17, 63.3, 109.6, 155.9, 387.5, 452.3, 517.2)
_ENCABEZADO_X = (
    ("FECHA", 22.9),
    ("OFIC.", 72.5),
    ("N.DOC.", 115),
    ("DESCRIPCION", 238.5),
    ("DEBITO", 401.0),
    ("CREDITO", 463.0),
    ("SALDO", 532.0),
)
_FILA, _FILA_PARTIDA, _PRIMERA_FILA, _ULTIMA_Y = 13.0, 22.0, 612.0, 150.0
_TITULAR, _CI, _CUENTA = "TITULAR SINTETICO", "0000000000", "9900000001"


def _fecha_pichincha(fecha: date) -> str:
    return f"{fecha.day:02d}-{MESES_PICHINCHA[fecha.month]}"


def _cabecera(numero_ciclo: int = 1) -> list[Fragmento]:
    """Lo que el real repite arriba de cada página de detalle, con datos inventados."""
    return [
        Fragmento(27, 696, 12, _TITULAR, True),
        Fragmento(420.6, 696, 12, " "),
        Fragmento(None, 696, 12, "C.I./RUC: ", True, sigue=True),
        Fragmento(None, 696, 12, " ", sigue=True),
        Fragmento(None, 696, 12, _CI, sigue=True),
        Fragmento(27, 674, 12, f"CUENTA: {_CUENTA}"),
        Fragmento(420.6, 674, 12, f"CICLO {numero_ciclo}"),
        Fragmento(223.3, 643, 11, "DETALLE DE MOVIMIENTOS"),
        *[Fragmento(x, 625, 9, rotulo, True) for rotulo, x in _ENCABEZADO_X],
    ]


_TITULO = Fragmento(397.3, 784, 17, "ESTADO DE CUENTA")
"""El real lo dibuja al final de cada página."""


_AVISO = "Documento sintético para pruebas del parser: no es un estado de cuenta real."


def _celdas_fila(y: float, celdas: list[str]) -> list[Fragmento]:
    """Una fila como la dibuja el real: fecha centrada, montos a la derecha, saldo centrado."""
    fecha, oficina, doc, descripcion, debito, credito, saldo = celdas
    return [
        Fragmento(37.9 - _ancho(fecha, 9) / 2, y, 9, fecha),
        Fragmento(66, y, 9, oficina),
        Fragmento(111, y, 9, doc),
        Fragmento(159, y, 9, descripcion),
        Fragmento(445.5 - _ancho(debito, 9), y, 9, debito),
        Fragmento(510.5 - _ancho(credito, 9), y, 9, credito),
        Fragmento(547.6 - _ancho(saldo, 9) / 2, y, 9, saldo),
    ]


def _paginas_detalle(
    filas: list[FilaPdf],
    anterior: int,
    *,
    textos: dict[tuple[int, int], str] | None = None,
    correr: dict[tuple[int, int], float] | None = None,
    partir_en: int | None = None,
) -> list[list[Fragmento]]:
    """Las páginas de detalle: una fila por movimiento, con saldo corrido.

    `textos` cambia el texto de una celda, (fila, columna) → texto; un monto se
    vuelve a alinear a la derecha. `correr` mueve una celda en x. `partir_en`
    corta esa fila entre dos páginas: fecha, oficina y N.DOC al pie de una y el
    resto arriba de la siguiente. Un N.DOC de nueve dígitos se parte como en el
    real: el último dígito va 9 pt más abajo, justo después de la celda, y la
    fila ocupa 22 pt en vez de 13.
    """
    textos, correr = textos or {}, correr or {}
    paginas: list[list[Fragmento]] = []
    pagina, y, saldo = _cabecera(), _PRIMERA_FILA, anterior

    def cerrar() -> None:
        nonlocal pagina, y
        paginas.append([*pagina, *_rayas_y_pie(y), _TITULO])
        pagina, y = _cabecera(), _PRIMERA_FILA

    for indice, (fecha, doc, descripcion, centavos) in enumerate(filas):
        saldo += centavos
        alto = _FILA_PARTIDA if len(doc) > 8 else _FILA
        if y - alto < _ULTIMA_Y:
            cerrar()
        celdas = [
            _fecha_pichincha(fecha),
            "99",
            doc[:8],
            descripcion,
            plata(-centavos) if centavos < 0 else "0.00",
            plata(centavos) if centavos > 0 else "0.00",
            plata(saldo),
        ]
        for (fila, columna), texto in textos.items():
            if fila == indice:
                celdas[columna] = texto
        fragmentos = _celdas_fila(y, celdas)
        for (fila, columna), dx in correr.items():
            if fila == indice:
                celda = fragmentos[columna]
                fragmentos[columna] = celda._replace(x=(celda.x or 0) + dx)
        if len(doc) > 8:
            fragmentos.insert(3, Fragmento(111, y - 9, 9, doc[8:], sigue=True))
        if indice == partir_en:
            pagina += fragmentos[:3]
            cerrar()
            pagina += [f._replace(y=y) for f in fragmentos[3:]]
        else:
            pagina += fragmentos
        y -= alto
    cerrar()
    return paginas


def _rayas_y_pie(y: float) -> list[Fragmento]:
    """Bajo la tabla, el real deja una celda vacía de 5 pt al borde de cada columna."""
    return [*[Fragmento(x, y - 9, 5, "") for x in _BORDES], Fragmento(79.9, 125, 7, _AVISO)]


def _resumen(
    ultimo: date,
    este: date,
    anterior: int,
    filas: list[FilaPdf],
    conteos: tuple[int, int] | None = None,
    totales: tuple[int, int] | None = None,
) -> list[Fragmento]:
    """La página 1: cortes y la conciliación del banco. `conteos`/`totales` los fuerzan."""
    creditos = [c for _, _, _, c in filas if c > 0]
    debitos = [-c for _, _, _, c in filas if c < 0]
    n_cred, n_deb = conteos or (len(creditos), len(debitos))
    t_cred, t_deb = totales or (sum(creditos), sum(debitos))
    actual = anterior + t_cred - t_deb
    corte = f"{ultimo:%d-%m-%Y}", f"{este:%d-%m-%Y}"

    def monto(y: float, centavos: int) -> Fragmento:
        texto = plata(centavos)
        return Fragmento(567 - _ancho(texto, 9), y, 9, texto)

    filas_resumen: list[tuple[str, str, int]] = [
        ("SALDO ANTERIOR", "", anterior),
        ("DEPÓSITO / CRÉDITOS", f"({n_cred})", t_cred),
        ("CHEQUES / DÉBITOS", f"({n_deb})", t_deb),
        ("INTERÉS PERIODO", "", 0),
        ("SALDO ACTUAL", "", actual),
        ("SALDO PROMEDIO EFECTIVO", "", (anterior + actual) // 2),
    ]
    fragmentos: list[Fragmento] = [
        Fragmento(23, 704, 10, _TITULAR, True),
        Fragmento(23, 690, 10, f"CUENTA {_CUENTA}", True),
        Fragmento(23, 646, 10, "CICLO 1"),
        Fragmento(23, 632, 10, f"C.I./RUC: {_CI}", True),
        Fragmento(308.4, 703, 10, "FECHA ÚLTIMO CORTE (FACTURA)"),
        Fragmento(521.9, 703, 10, corte[0]),
        Fragmento(308.4, 687, 10, "FECHA ESTE CORTE"),
        Fragmento(521.9, 687, 10, corte[1]),
        Fragmento(313.4, 474.9, 10, "CONCILIACIÓN", True),
        Fragmento(314.4, 452.4, 9, "FECHA ÚLTIMO CORTE (FACTURA)"),
        Fragmento(478.6, 452.4, 9, ""),
        Fragmento(521, 452.4, 9, corte[0]),
        Fragmento(314.4, 434.9, 9, "FECHA ESTE CORTE (FACTURA)"),
        Fragmento(478.6, 434.9, 9, ""),
        Fragmento(521, 434.9, 9, corte[1]),
    ]
    for k, (rotulo, conteo, centavos) in enumerate(filas_resumen):
        y = 417.4 - 17.5 * k
        fragmentos += [Fragmento(314.4, y, 9, rotulo), Fragmento(478.6, y, 9, conteo)]
        fragmentos.append(monto(y, centavos))
    fragmentos += [Fragmento(79.9, 285.9, 7, _AVISO), _TITULO]
    return fragmentos


def _estado_pdf(
    filas: list[FilaPdf],
    *,
    ultimo: date = PICHINCHA_ULTIMO_CORTE,
    este: date = PICHINCHA_ESTE_CORTE,
    anterior: int = PICHINCHA_ANTERIOR,
    resumen_de: list[FilaPdf] | None = None,
    conteos: tuple[int, int] | None = None,
    repetir: int | None = None,
    **detalle: Any,
) -> bytes:
    """Un estado completo: resumen, detalle y una última página solo con título y pie.

    `resumen_de` arma el resumen con otras filas que el detalle. `repetir` vuelve
    a meter esa página (desde 1) justo después de sí misma, con su mismo pie: así
    queda un PDF unido a mano. Lo demás va a `_paginas_detalle`.
    """
    paginas = [_resumen(ultimo, este, anterior, resumen_de or filas, conteos)]
    paginas += _paginas_detalle(filas, anterior, **detalle)
    paginas.append([_TITULO])
    total = len(paginas)
    numeradas = [(_form(p), n, total) for n, p in enumerate(paginas, start=1)]
    if repetir is not None:
        numeradas.insert(repetir, numeradas[repetir - 1])
    return _pdf(numeradas)


def _final_pdf(filas: list[FilaPdf], anterior: int) -> int:
    return anterior + sum(c for _, _, _, c in filas)


@dataclass(frozen=True)
class CasoPdf:
    """Un PDF sintético y lo que debería pasar con él cuando exista el parser.

    `deberia` está anotado a mano, no medido: todavía no hay quien lea el PDF.
    """

    nombre: str
    que: str
    deberia: str
    """"entra" o "rechazo"."""
    ultimo_corte: date = PICHINCHA_ULTIMO_CORTE
    este_corte: date = PICHINCHA_ESTE_CORTE
    saldos: tuple[int, int] = (PICHINCHA_ANTERIOR, _final_pdf(PICHINCHA, PICHINCHA_ANTERIOR))
    """Saldo anterior y actual en centavos, como los declararía quien importa."""

    @property
    def archivo(self) -> str:
        return f"{self.nombre}.pdf"


_DOC_PARTIDO = [
    (f, f"{doc}12" if k in (3, 4, 11) else doc, d, c) for k, (f, doc, d, c) in enumerate(PICHINCHA)
]
_EN_CORTE = [
    (PICHINCHA_ULTIMO_CORTE, "1000100", "TRANSFERENCIA ENVIADA SINTETICA", -500),
    *PICHINCHA,
    (PICHINCHA_ESTE_CORTE, "1000118", "PAGO SERVICIO SINTETICO", -1000),
]
_DIC_ULTIMO, _DIC_ESTE = date(2026, 12, 14), date(2027, 1, 14)
_CRUZA_DICIEMBRE: list[FilaPdf] = [
    (date(2026, 12, 15), "1000201", "TRANSFERENCIA RECIBIDA SINTETICA", 15_000),
    (date(2026, 12, 18), "1000202", "COMPRA SUPERMERCADO SINTETICO", -7350),
    (date(2026, 12, 24), "1000203", "TRANSFERENCIA ENVIADA SINTETICA", -2000),
    (date(2026, 12, 31), "1000204", "PAGO SERVICIO SINTETICO", -1875),
    (date(2027, 1, 3), "1000205", "TRANSFERENCIA ENVIADA SINTETICA", -640),
    (date(2027, 1, 10), "1000206", "COMPRA FARMACIA SINTETICA", -1190),
]
_SALDOS_DIC = (PICHINCHA_ANTERIOR, _final_pdf(_CRUZA_DICIEMBRE, PICHINCHA_ANTERIOR))
_DICIEMBRE = {"ultimo_corte": _DIC_ULTIMO, "este_corte": _DIC_ESTE, "saldos": _SALDOS_DIC}
_CON_MILES = [(f, doc, d, 120_000 if k == 1 else c) for k, (f, doc, d, c) in enumerate(PICHINCHA)]
_SALDOS_MILES = (PICHINCHA_ANTERIOR, _final_pdf(_CON_MILES, PICHINCHA_ANTERIOR))

CASOS_PDF = [
    CasoPdf(
        "pichincha", "Estado que cuadra: resumen, una página de detalle y la última vacía", "entra"
    ),
    CasoPdf(
        "pichincha_doc_partido",
        "Tres N.DOC de nueve dígitos con el último dígito en la línea de abajo, como el real",
        "entra",
    ),
    CasoPdf(
        "pichincha_columna_desplazada",
        "El débito de una fila corrido 65 pt, dentro de la columna de crédito",
        "rechazo",
    ),
    CasoPdf(
        "pichincha_fila_entre_paginas",
        "Una fila con fecha y N.DOC al pie de una página y el resto arriba de la siguiente",
        "rechazo",
    ),
    CasoPdf(
        "pichincha_separador_de_miles",
        'Un crédito con separador de miles ("1,200.00"); el resto cuadra',
        "rechazo",
        saldos=_SALDOS_MILES,
    ),
    CasoPdf(
        "pichincha_pagina_repetida",
        'La página de detalle dos veces, con el mismo pie ("Página 2 de 3")',
        "rechazo",
    ),
    CasoPdf(
        "pichincha_resumen_no_cuadra",
        "El resumen cuenta un débito más de los que trae el detalle",
        "rechazo",
    ),
    CasoPdf(
        "pichincha_corte_contado",
        "Movimientos en las dos fechas de corte, contados en el resumen",
        "entra",
        saldos=(PICHINCHA_ANTERIOR, _final_pdf(_EN_CORTE, PICHINCHA_ANTERIOR)),
    ),
    CasoPdf(
        "pichincha_corte_no_contado",
        "Los mismos movimientos de corte en el detalle, pero no en el resumen",
        "rechazo",
    ),
    CasoPdf(
        "pichincha_cruza_diciembre",
        "Periodo de diciembre a enero: cada fecha cae en un solo año",
        "entra",
        **_DICIEMBRE,
    ),
    CasoPdf(
        "pichincha_mes_fuera_del_periodo",
        'Periodo de diciembre a enero con una fecha "15-jun.", que no cae en ninguno de los dos',
        "rechazo",
        **_DICIEMBRE,
    ),
]


def pdfs() -> dict[str, bytes]:
    """Nombre → bytes de cada PDF sintético. Se versionan junto a los CSV."""
    dic = {"ultimo": _DIC_ULTIMO, "este": _DIC_ESTE}
    creditos = sum(1 for *_, c in PICHINCHA if c > 0)
    debitos = len(PICHINCHA) - creditos
    contenido = {
        "pichincha": _estado_pdf(PICHINCHA),
        "pichincha_doc_partido": _estado_pdf(_DOC_PARTIDO),
        "pichincha_columna_desplazada": _estado_pdf(PICHINCHA, correr={(4, 4): 65.0}),
        "pichincha_fila_entre_paginas": _estado_pdf(PICHINCHA, partir_en=9),
        "pichincha_separador_de_miles": _estado_pdf(_CON_MILES, textos={(1, 5): "1,200.00"}),
        "pichincha_pagina_repetida": _estado_pdf(PICHINCHA, repetir=2),
        "pichincha_resumen_no_cuadra": _estado_pdf(PICHINCHA, conteos=(creditos, debitos + 1)),
        "pichincha_corte_contado": _estado_pdf(_EN_CORTE),
        "pichincha_corte_no_contado": _estado_pdf(_EN_CORTE, resumen_de=PICHINCHA),
        "pichincha_cruza_diciembre": _estado_pdf(_CRUZA_DICIEMBRE, **dic),
        "pichincha_mes_fuera_del_periodo": _estado_pdf(
            _CRUZA_DICIEMBRE, textos={(2, 0): "15-jun."}, **dic
        ),
    }
    assert list(contenido) == [caso.nombre for caso in CASOS_PDF]
    return {f"{nombre}.pdf": datos for nombre, datos in contenido.items()}


def main() -> None:
    if "--tabla" in sys.argv:
        print(tabla())
    elif "--base" in sys.argv:
        if sys.argv[-1] == "--base":
            raise SystemExit("Falta la ruta: --base RUTA")
        ruta = Path(sys.argv[sys.argv.index("--base") + 1])
        gastos, movimientos = base_sintetica(ruta)
        print(f"{ruta}: {gastos} gastos y {movimientos} movimientos sintéticos (marzo a julio)")
    else:
        preparar(DESTINO)
        DESTINO_PARES.write_bytes(pares_verdaderos())
        carpeta = DESTINO.relative_to(RAIZ)
        print(f"{len(archivos())} estados en CSV y {len(pdfs())} en PDF en {carpeta}")
        print(f"pares verdaderos en {DESTINO_PARES.relative_to(RAIZ)}")


if __name__ == "__main__":
    main()
