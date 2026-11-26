"""PDF del banco → `EstadoLeido`. Estricto como el CSV: lo que no se entiende se rechaza.

El PDF no trae filas, trae textos sueltos con su posición. Acá se leen con pypdf
(cada `Tj` con su x, su y y su ancho), y las filas se arman con reglas fijas:

- **Columnas por el nombre del encabezado**, con su x leída en cada página. Cada
  columna va desde donde termina el rótulo de su vecina de la izquierda hasta
  donde empieza el de la derecha. Un texto tiene que caber entero en una sola
  columna; si cae en ninguna o en dos, se rechaza.
- **Cada fecha abre una fila**: lo que está a su misma altura es de esa fila.
- **El dígito suelto del N.DOC.** solo se une si está en su columna, debajo de la
  fila y antes de la siguiente. Cualquier otro texto sin fecha se rechaza.

Después el estado tiene que cuadrar con el propio PDF: los saldos que declara
quien importa son los del resumen, y los conteos y totales del resumen son los
del detalle. Lo demás (saldo corrido, invariante, solape, continuidad) lo decide
`verificacion.verificar`, igual que con un CSV.

El número de cuenta se compara entre páginas y se cambia por su etiqueta de
`CUENTAS_LOCALES`: no sale de esta función ni aparece en un mensaje.

pypdf es un extra opcional (`pip install -e '.[pdf]'`): solo se importa al leer.
"""

import hashlib
import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from family_expense_bot.conciliacion.bancos import BancoPdf, Topes, asignar_tipo
from family_expense_bot.conciliacion.contrato import (
    EstadoLeido,
    EstadoRechazado,
    MovimientoLeido,
    Problema,
    plata,
)
from family_expense_bot.conciliacion.ingesta import MAXIMO_DESCRIPCION

TOLERANCIA_Y = 0.5
"""Dos textos a menos de media unidad de altura están en la misma línea."""

_MONTO = re.compile(r"(\d{1,15})\.(\d{2})")
_SALDO = re.compile(r"(-?)(\d{1,15})\.(\d{2})")
_PIE = re.compile(r"Página (\d{1,4}) de (\d{1,4})")
_CONTEO = re.compile(r"\((\d{1,6})\)")
_FECHA = re.compile(r"(\d{2})-(\S+)")

Matriz = tuple[float, float, float, float, float, float]
_IDENTIDAD: Matriz = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


@dataclass(frozen=True)
class Texto:
    """Un `Tj` de la página: lo que escribe, dónde empieza y cuánto ocupa."""

    x: float
    y: float
    tamano: float
    texto: str
    ancho: float | None
    """None si la fuente no trae anchos: no se sabe dónde termina."""

    @property
    def fin(self) -> float | None:
        return None if self.ancho is None else self.x + self.ancho


# --- Del PDF a textos con posición --------------------------------------------


class _Ilegible(Exception):
    """El PDF no se puede leer como texto con posición."""


def _mult(a: Matriz, b: Matriz) -> Matriz:
    """a por b, como las multiplica PDF (vectores fila)."""
    return (
        a[0] * b[0] + a[1] * b[2],
        a[0] * b[1] + a[1] * b[3],
        a[2] * b[0] + a[3] * b[2],
        a[2] * b[1] + a[3] * b[3],
        a[4] * b[0] + a[5] * b[2] + b[4],
        a[4] * b[1] + a[5] * b[3] + b[5],
    )


def _numeros(operandos: Sequence[Any], cuantos: int) -> list[float]:
    if len(operandos) != cuantos:
        raise _Ilegible("un operador con operandos de más o de menos")
    return [float(o) for o in operandos]


class _Fuente:
    """Lo que hace falta de una fuente: decodificar WinAnsi y medir cada carácter."""

    def __init__(self, fuente: Any) -> None:
        codificacion = fuente.get("/Encoding")
        if codificacion != "/WinAnsiEncoding":
            raise _Ilegible("una fuente que no está en WinAnsi")
        anchos = fuente.get("/Widths")
        self.primero = int(fuente.get("/FirstChar", 0))
        self.anchos = [float(a) for a in anchos] if anchos is not None else None

    def texto(self, crudo: bytes) -> str:
        return crudo.decode("cp1252", errors="strict")

    def ancho(self, crudo: bytes, tamano: float) -> float | None:
        if self.anchos is None:
            return None
        total = 0.0
        for byte in crudo:
            indice = byte - self.primero
            if not 0 <= indice < len(self.anchos):
                return None
            total += self.anchos[indice]
        return total * tamano / 1000


def _bytes(operando: Any) -> bytes:
    original = getattr(operando, "get_original_bytes", None)
    if original is not None:
        datos: bytes = original()
        return datos
    if isinstance(operando, bytes):
        return operando
    raise _Ilegible("un texto que no es un string de PDF")


def _textos(contenido: Any, recursos: Any, lector: Any, ctm: Matriz, nivel: int) -> Iterator[Texto]:
    """Recorre un content stream (y los Form XObject que dibuja) y da cada `Tj`."""
    from pypdf.generic import ContentStream

    if nivel > 4:
        raise _Ilegible("Form XObject anidados de más")
    pila: list[Matriz] = []
    tm = tlm = _IDENTIDAD
    fuente: _Fuente | None = None
    tamano = 0.0
    interlineado = 0.0
    fuentes = recursos.get("/Font", {}) if recursos is not None else {}
    xobjetos = recursos.get("/XObject", {}) if recursos is not None else {}

    def emitir(operando: Any) -> Iterator[Texto]:
        nonlocal tm
        if fuente is None:
            raise _Ilegible("un texto sin fuente")
        crudo = _bytes(operando)
        posicion = _mult(tm, ctm)
        if abs(posicion[1]) > 1e-6 or abs(posicion[2]) > 1e-6:
            raise _Ilegible("texto girado")
        ancho = fuente.ancho(crudo, tamano)
        yield Texto(
            x=posicion[4],
            y=posicion[5],
            tamano=tamano * posicion[0],
            texto=fuente.texto(crudo),
            ancho=None if ancho is None else ancho * posicion[0],
        )
        if ancho is not None:
            tm = _mult((1.0, 0.0, 0.0, 1.0, ancho, 0.0), tm)

    for operandos, operador in contenido.operations:
        if operador == b"q":
            pila.append(ctm)
        elif operador == b"Q":
            if not pila:
                raise _Ilegible("un Q sin su q")
            ctm = pila.pop()
        elif operador == b"cm":
            a, b, c, d, e, f = _numeros(operandos, 6)
            ctm = _mult((a, b, c, d, e, f), ctm)
        elif operador == b"BT":
            tm = tlm = _IDENTIDAD
        elif operador == b"Tm":
            a, b, c, d, e, f = _numeros(operandos, 6)
            tm = tlm = (a, b, c, d, e, f)
        elif operador in (b"Td", b"TD"):
            tx, ty = _numeros(operandos, 2)
            if operador == b"TD":
                interlineado = -ty
            tm = tlm = _mult((1.0, 0.0, 0.0, 1.0, tx, ty), tlm)
        elif operador == b"TL":
            (interlineado,) = _numeros(operandos, 1)
        elif operador == b"T*":
            tm = tlm = _mult((1.0, 0.0, 0.0, 1.0, 0.0, -interlineado), tlm)
        elif operador == b"Tf":
            if len(operandos) != 2:
                raise _Ilegible("Tf mal formado")
            nombre, tamano = operandos[0], float(operandos[1])
            if nombre not in fuentes:
                raise _Ilegible("una fuente que no está en los recursos")
            fuente = _Fuente(fuentes[nombre].get_object())
        elif operador in (b"Tc", b"Tw", b"Tz", b"Ts"):
            # El banco no los usa; con ellos el ancho medido dejaría de ser el real.
            valor = _numeros(operandos, 1)[0]
            if valor != (100.0 if operador == b"Tz" else 0.0):
                raise _Ilegible("espaciado de caracteres o de palabras")
        elif operador == b"Tj":
            if len(operandos) != 1:
                raise _Ilegible("Tj mal formado")
            yield from emitir(operandos[0])
        elif operador in (b"'", b'"'):
            tm = tlm = _mult((1.0, 0.0, 0.0, 1.0, 0.0, -interlineado), tlm)
            yield from emitir(operandos[-1])
        elif operador == b"TJ":
            # El banco escribe solo Tj: un TJ trae kerning y partiría una celda.
            raise _Ilegible("texto con TJ")
        elif operador == b"Do":
            if len(operandos) != 1 or operandos[0] not in xobjetos:
                raise _Ilegible("un XObject que no está en los recursos")
            xobjeto = xobjetos[operandos[0]].get_object()
            if xobjeto.get("/Subtype") != "/Form":
                continue  # una imagen (el logo): no trae texto
            matriz = xobjeto.get("/Matrix", list(_IDENTIDAD))
            m = _numeros(matriz, 6)
            interior = ContentStream(xobjeto, lector)
            propios = xobjeto.get("/Resources")
            yield from _textos(
                interior,
                propios.get_object() if propios is not None else recursos,
                lector,
                _mult((m[0], m[1], m[2], m[3], m[4], m[5]), ctm),
                nivel + 1,
            )


def textos_por_pagina(datos: bytes) -> list[list[Texto]]:
    """Los textos de cada página, en el orden en que el PDF los dibuja."""
    import io

    try:
        from pypdf import PdfReader
        from pypdf.errors import PyPdfError
    except ImportError as e:  # pragma: no cover - depende del entorno
        raise RuntimeError("Leer un PDF necesita pypdf: pip install -e '.[pdf]'") from e

    try:
        lector = PdfReader(io.BytesIO(datos), strict=False)
        if lector.is_encrypted:
            raise _Ilegible("el PDF está cifrado")
        paginas: list[list[Texto]] = []
        for pagina in lector.pages:
            contenido = pagina.get_contents()
            if contenido is None:
                paginas.append([])
                continue
            recursos = pagina.get("/Resources")
            recursos = recursos.get_object() if recursos is not None else None
            paginas.append(list(_textos(contenido, recursos, lector, _IDENTIDAD, 0)))
        return paginas
    except (_Ilegible, PyPdfError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise EstadoRechazado(
            [
                Problema(
                    codigo="pdf_ilegible",
                    mensaje=f"El PDF no se pudo leer como texto ({_razon(e)}): no se importó nada.",
                )
            ]
        ) from None


def _razon(error: Exception) -> str:
    if isinstance(error, _Ilegible):
        return str(error)
    if isinstance(error, UnicodeDecodeError):
        return "un texto que no está en WinAnsi"
    return "archivo dañado o que no es un PDF"


# --- De textos a un estado ------------------------------------------------------


def _leer_bytes(ruta: Path, tope_bytes: int) -> bytes:
    """Los bytes del archivo, o un rechazo sin abrirlo si pasa del tope."""
    try:
        tamano = ruta.stat().st_size
    except OSError:
        raise EstadoRechazado(
            [Problema(codigo="archivo_no_existe", mensaje=f"No existe el archivo {ruta}.")]
        ) from None
    if tamano <= tope_bytes:
        with ruta.open("rb") as archivo:
            datos = archivo.read(tope_bytes + 1)
        if len(datos) <= tope_bytes:
            return datos
        tamano = len(datos)
    raise EstadoRechazado(
        [
            Problema(
                codigo="archivo_grande",
                mensaje=f"El archivo pesa {tamano} bytes y el tope es {tope_bytes}: no se leyó.",
            )
        ]
    )


def _misma_linea(a: float, b: float) -> bool:
    return abs(a - b) <= TOLERANCIA_Y


def _lista(partes: Sequence[str]) -> str:
    return partes[0] if len(partes) == 1 else f"{', '.join(partes[:-1])} y {partes[-1]}"


def _sin_vacios(textos: list[Texto]) -> list[Texto]:
    return [t for t in textos if t.texto.strip()]


@dataclass(frozen=True)
class _Resumen:
    ultimo_corte: date
    este_corte: date
    saldo_anterior: int
    creditos: tuple[int, int]
    """(cuántos, centavos)."""
    debitos: tuple[int, int]
    interes: int
    saldo_final: int


def _a_la_derecha(rotulo: Texto, textos: list[Texto]) -> list[Texto]:
    return sorted(
        (t for t in textos if _misma_linea(t.y, rotulo.y) and t.x > rotulo.x and t is not rotulo),
        key=lambda t: t.x,
    )


def _resumen(textos: list[Texto], banco: BancoPdf) -> tuple[_Resumen | None, list[Problema]]:
    """Los datos de la página 1, cada uno al lado de su rótulo."""
    problemas: list[Problema] = []
    rotulos = banco.resumen
    textos = _sin_vacios(textos)

    def problema(mensaje: str) -> None:
        problemas.append(Problema(codigo="resumen_incompleto", pagina=1, mensaje=mensaje))

    def valores(rotulo: str) -> list[list[Texto]]:
        return [_a_la_derecha(t, textos) for t in textos if t.texto.strip() == rotulo]

    def corte(rotulo: str) -> date | None:
        vistos = set()
        for derecha in valores(rotulo):
            for t in derecha:
                try:
                    vistos.add(datetime.strptime(t.texto.strip(), banco.formato_corte).date())
                except ValueError:
                    continue
        if len(vistos) != 1:
            falta = "no trae" if not vistos else "trae dos fechas distintas en"
            problema(f"El resumen {falta} {rotulo}.")
            return None
        return vistos.pop()

    def monto(rotulo: str, con_conteo: bool = False) -> tuple[int, int] | None:
        encontrados = valores(rotulo)
        if len(encontrados) != 1:
            problema(f"El resumen {'no trae' if not encontrados else 'repite'} {rotulo}.")
            return None
        derecha = encontrados[0]
        if not derecha:
            problema(f"El resumen no trae el monto de {rotulo}.")
            return None
        coincide = _SALDO.fullmatch(derecha[-1].texto.strip())
        if coincide is None:
            problemas.append(
                Problema(
                    codigo="monto_invalido",
                    pagina=1,
                    mensaje=f"Página 1: el monto de {rotulo} {derecha[-1].texto.strip()!r} no es "
                    "válido: van solo dígitos y dos decimales con punto, sin separador de miles.",
                )
            )
            return None
        negativo, entero, decimales = coincide.groups()
        centavos = (int(entero) * 100 + int(decimales)) * (-1 if negativo else 1)
        if not con_conteo:
            return 0, centavos
        conteos = [_CONTEO.fullmatch(t.texto.strip()) for t in derecha[:-1]]
        hallados = [c for c in conteos if c is not None]
        if len(hallados) != 1:
            problema(f"El resumen no trae cuántos movimientos son {rotulo}.")
            return None
        return int(hallados[0].group(1)), centavos

    ultimo, este = corte(rotulos.ultimo_corte), corte(rotulos.este_corte)
    anterior, creditos = monto(rotulos.saldo_anterior), monto(rotulos.creditos, True)
    debitos, interes = monto(rotulos.debitos, True), monto(rotulos.interes)
    final = monto(rotulos.saldo_final)
    if (
        ultimo is None
        or este is None
        or anterior is None
        or creditos is None
        or debitos is None
        or interes is None
        or final is None
    ):
        return None, problemas
    if not ultimo < este:
        problema(f"El resumen dice que el periodo va de {ultimo} a {este}.")
        return None, problemas
    return (
        _Resumen(ultimo, este, anterior[1], creditos, debitos, interes[1], final[1]),
        problemas,
    )


def _numero_de_cuenta(textos: list[Texto], banco: BancoPdf) -> list[str]:
    patron = re.compile(rf"{re.escape(banco.rotulo_cuenta)}:?\s*(\d{{4,30}})")
    return [m.group(1) for t in textos if (m := patron.fullmatch(t.texto.strip()))]


@dataclass(frozen=True)
class _Columna:
    campo: str
    rotulo: str
    desde: float
    hasta: float


def _columnas(textos: list[Texto], banco: BancoPdf) -> tuple[list[_Columna], float] | list[str]:
    """Las columnas del detalle por su encabezado, o los rótulos que faltan."""
    rotulos = banco.encabezados.model_dump()
    hallados: dict[str, Texto] = {}
    faltan = []
    for campo, rotulo in rotulos.items():
        candidatos = [t for t in textos if t.texto.strip() == rotulo and t.ancho is not None]
        if len(candidatos) != 1:
            faltan.append(rotulo)
        else:
            hallados[campo] = candidatos[0]
    if faltan:
        return faltan
    altura = next(iter(hallados.values())).y
    if not all(_misma_linea(t.y, altura) for t in hallados.values()):
        return ["los rótulos en una misma línea"]
    orden = sorted(hallados.items(), key=lambda par: par[1].x)
    columnas = []
    for k, (campo, _rotulo) in enumerate(orden):
        anterior = orden[k - 1][1].fin if k > 0 else float("-inf")
        siguiente = orden[k + 1][1].x if k + 1 < len(orden) else float("inf")
        assert anterior is not None
        columnas.append(_Columna(campo, rotulos[campo], anterior, siguiente))
    return columnas, altura


def _columna_de(texto: Texto, columnas: list[_Columna]) -> _Columna | None:
    """La única columna en la que el texto cabe entero, o None."""
    if texto.fin is None:
        return None
    caben = [c for c in columnas if c.desde <= texto.x and texto.fin <= c.hasta]
    return caben[0] if len(caben) == 1 else None


@dataclass
class _Fila:
    pagina: int
    numero: int
    """Fila dentro de su página, desde 1."""
    y: float
    celdas: dict[str, list[str]]
    doc_partido: bool = False


def _filas_de_pagina(
    numero: int, textos: list[Texto], columnas: list[_Columna], altura: float
) -> tuple[list[_Fila], list[Problema]]:
    problemas: list[Problema] = []
    tamano = next(t.tamano for t in textos if _misma_linea(t.y, altura))
    debajo = [t for t in _sin_vacios(textos) if t.y < altura - TOLERANCIA_Y]
    tabla = [t for t in debajo if abs(t.tamano - tamano) < 0.01]
    if tabla:
        piso = min(t.y for t in tabla) - TOLERANCIA_Y
        intrusos = [t for t in debajo if t not in tabla and t.y > piso]
        tabla += intrusos  # otro tamaño en medio de la tabla: se rechaza abajo
    lineas: list[list[Texto]] = []
    for t in sorted(tabla, key=lambda t: (-t.y, t.x)):
        if lineas and _misma_linea(lineas[-1][0].y, t.y):
            lineas[-1].append(t)
        else:
            lineas.append([t])
    filas: list[_Fila] = []
    fecha = next(c for c in columnas if c.campo == "fecha")
    documento = next(c for c in columnas if c.campo == "documento")
    for linea in lineas:
        ubicados = [(t, _columna_de(t, columnas)) for t in linea]
        abre = any(c is fecha for _, c in ubicados)
        donde = (
            f"Página {numero}, fila {len(filas) + 1}"
            if abre
            else (
                f"Página {numero}, debajo de la fila {filas[-1].numero}"
                if filas
                else f"Página {numero}"
            )
        )
        if any(c is None for _, c in ubicados) or any(
            abs(t.tamano - tamano) >= 0.01 for t in linea
        ):
            problemas.append(
                Problema(
                    codigo="fuera_de_columna",
                    pagina=numero,
                    mensaje=f"{donde}: hay un texto que no cabe en una sola columna.",
                )
            )
            if abre:
                filas.append(_Fila(numero, len(filas) + 1, linea[0].y, {}))
            continue
        if abre:
            celdas: dict[str, list[str]] = {}
            for t, c in ubicados:
                assert c is not None
                celdas.setdefault(c.campo, []).append(t.texto.strip())
            filas.append(_Fila(numero, len(filas) + 1, linea[0].y, celdas))
            continue
        digito = (
            len(linea) == 1
            and ubicados[0][1] is documento
            and re.fullmatch(r"\d", linea[0].texto.strip()) is not None
        )
        if digito and filas and not filas[-1].doc_partido and "documento" in filas[-1].celdas:
            filas[-1].doc_partido = True
            continue
        mensaje = (
            f"Página {numero}, debajo de la fila {filas[-1].numero}: hay texto sin fecha que no es "
            "el dígito que le falta al N.DOC."
            if filas
            else f"Página {numero}: hay texto arriba de la primera fila, sin fecha."
        )
        problemas.append(Problema(codigo="texto_sin_fila", pagina=numero, mensaje=mensaje))
    return filas, problemas


def _centavos(texto: str) -> int | None:
    coincide = _MONTO.fullmatch(texto)
    if coincide is None:
        return None
    return int(coincide.group(1)) * 100 + int(coincide.group(2))


def _fecha(texto: str, banco: BancoPdf, desde: date, hasta: date) -> tuple[date | None, str]:
    """La fecha de la fila con el año del periodo, o el código de por qué no."""
    coincide = _FECHA.fullmatch(texto)
    if coincide is None or coincide.group(2) not in banco.meses:
        return None, "fecha_invalida"
    dia, mes = int(coincide.group(1)), banco.meses.index(coincide.group(2)) + 1
    posibles = set()
    for anio in {desde.year, hasta.year}:
        try:
            candidata = date(anio, mes, dia)
        except ValueError:
            continue
        if desde <= candidata <= hasta:
            posibles.add(candidata)
    if len(posibles) != 1:
        return None, "mes_fuera_del_periodo"
    return posibles.pop(), ""


def _con_control(texto: str) -> bool:
    """Caracteres de control o de formato (Cc, Cf): ningún banco los escribe."""
    return any(unicodedata.category(c) in ("Cc", "Cf") for c in texto)


def _movimiento(
    fila: _Fila, linea: int, banco: BancoPdf, desde: date, hasta: date, topes: Topes
) -> tuple[MovimientoLeido | None, list[Problema]]:
    donde = f"Página {fila.pagina}, fila {fila.numero}"
    rotulos = banco.encabezados.model_dump()
    problemas: list[Problema] = []

    def problema(codigo: str, mensaje: str) -> None:
        problemas.append(Problema(codigo=codigo, pagina=fila.pagina, mensaje=f"{donde}: {mensaje}"))

    faltan = [rotulos[c] for c in rotulos if not fila.celdas.get(c)]
    dobles = [rotulos[c] for c in rotulos if len(fila.celdas.get(c, [])) > 1]
    if dobles and faltan:
        problema(
            "fuera_de_columna",
            f"la columna {_lista(dobles)} trae dos textos y {_lista(faltan)} ninguno: "
            "un texto quedó fuera de su columna.",
        )
    elif faltan:
        problema("fila_incompleta", f"falta {_lista(faltan)}.")
    elif dobles:
        problema("celda_doble", f"la columna {_lista(dobles)} trae dos textos.")
    if problemas:
        return None, problemas
    celda = {c: v[0] for c, v in fila.celdas.items()}

    fecha, error = _fecha(celda["fecha"], banco, desde, hasta)
    if error == "fecha_invalida":
        problema(error, f"la fecha {celda['fecha']!r} no es día-mes con los meses del banco.")
    elif error:
        problema(error, f"la fecha {celda['fecha']!r} no cae en el periodo {desde} a {hasta}.")

    montos: dict[str, int] = {}
    for campo in ("debito", "credito"):
        centavos = _centavos(celda[campo])
        if centavos is None:
            problema(
                "monto_invalido",
                f"el {rotulos[campo]} {celda[campo]!r} no es válido: van solo dígitos y dos "
                "decimales con punto, sin separador de miles.",
            )
        else:
            montos[campo] = centavos
    saldo = _SALDO.fullmatch(celda["saldo"])
    if saldo is None:
        problema(
            "monto_invalido",
            f"el {rotulos['saldo']} {celda['saldo']!r} no es válido: van solo dígitos y dos "
            "decimales con punto, sin separador de miles.",
        )
    if len(montos) == 2 and (montos["debito"] > 0) == (montos["credito"] > 0):
        problema(
            "debito_y_credito",
            f"trae débito {plata(montos['debito'])} y crédito {plata(montos['credito'])}; va uno "
            "solo distinto de cero.",
        )
    descripcion = celda["descripcion"]
    if len(descripcion) > MAXIMO_DESCRIPCION:
        problema("descripcion_larga", f"la descripción pasa de {MAXIMO_DESCRIPCION} caracteres.")
    if _con_control(descripcion):
        problema("descripcion_con_control", "la descripción trae caracteres de control.")
    if problemas or fecha is None or saldo is None:
        return None, problemas

    monto = montos["credito"] - montos["debito"]
    if abs(monto) > topes.monto_centavos:
        problema(
            "monto_tope",
            f"el monto {plata(monto)} pasa del tope de {plata(topes.monto_centavos)} por "
            "movimiento (tope_monto en config/bancos.yaml).",
        )
        return None, problemas
    negativo, entero, decimales = saldo.groups()
    saldo_centavos = (int(entero) * 100 + int(decimales)) * (-1 if negativo else 1)
    return (
        MovimientoLeido(
            linea=linea,
            fecha=fecha,
            descripcion=descripcion,
            monto_centavos=monto,
            saldo_centavos=saldo_centavos,
            tipo=asignar_tipo(descripcion, monto, banco.tipos),
        ),
        [],
    )


def _numeracion(paginas: list[list[Texto]]) -> list[Problema]:
    problemas = []
    pies: list[tuple[int, int]] = []
    for n, textos in enumerate(paginas, start=1):
        hallados = [m for t in textos if (m := _PIE.fullmatch(t.texto.strip()))]
        if len(hallados) != 1:
            problemas.append(
                Problema(
                    codigo="sin_pie",
                    pagina=n,
                    mensaje=f'La página {n} no trae su pie ("Página N de M").'
                    if not hallados
                    else f"La página {n} trae {len(hallados)} pies.",
                )
            )
        else:
            pies.append((int(hallados[0].group(1)), int(hallados[0].group(2))))
    total = len(paginas)
    if not problemas and pies != [(n, total) for n in range(1, total + 1)]:
        dicen = _lista([f"{n} de {m}" for n, m in pies])
        problemas.append(
            Problema(
                codigo="numeracion",
                mensaje=f"Las páginas no van de 1 a {total}: el archivo trae {total} y sus pies "
                f"dicen {dicen}.",
            )
        )
    return problemas


def _es_vacia(textos: list[Texto], banco: BancoPdf) -> bool:
    return all(
        t.texto.strip() == banco.titulo or _PIE.fullmatch(t.texto.strip())
        for t in _sin_vacios(textos)
    )


def leer_pdf(
    ruta: Path,
    banco: BancoPdf,
    *,
    cuentas_locales: Mapping[str, str],
    topes: Topes,
    saldo_anterior_centavos: int,
    saldo_final_centavos: int,
) -> EstadoLeido:
    """Lee el PDF completo o lanza `EstadoRechazado` con todos los problemas.

    `cuentas_locales` es número de cuenta → etiqueta, solo de este banco (sale de
    `CUENTAS_LOCALES`). El periodo sale de los cortes del resumen, con los dos días
    de corte adentro: qué movimientos de esos días son de este estado lo dice el
    resumen, que tiene que cuadrar con el detalle.
    """
    datos = _leer_bytes(ruta, topes.bytes)
    hash_archivo = hashlib.sha256(datos).hexdigest()
    paginas = textos_por_pagina(datos)
    if not paginas:
        raise EstadoRechazado([Problema(codigo="pdf_ilegible", mensaje="El PDF no trae páginas.")])

    problemas = _numeracion(paginas)
    resumen, del_resumen = _resumen(paginas[0], banco)
    problemas += del_resumen

    numeros = {1: _numero_de_cuenta(paginas[0], banco)}
    detalle: list[tuple[int, list[_Columna], float]] = []
    for n, textos in enumerate(paginas[1:], start=2):
        encabezado = _columnas(textos, banco)
        if isinstance(encabezado, tuple):
            detalle.append((n, *encabezado))
            numeros[n] = _numero_de_cuenta(textos, banco)
        elif not (n == len(paginas) and _es_vacia(textos, banco)):
            problemas.append(
                Problema(
                    codigo="sin_encabezado",
                    pagina=n,
                    mensaje=f"La página {n} no trae el encabezado del detalle: falta "
                    f"{_lista(encabezado)}.",
                )
            )
    if not detalle and not problemas:
        problemas.append(
            Problema(codigo="sin_encabezado", mensaje="El PDF no trae ninguna página de detalle.")
        )

    etiqueta = None
    for n, hallados in numeros.items():
        if len(set(hallados)) != 1:
            problemas.append(
                Problema(
                    codigo="cuenta_falta",
                    pagina=n,
                    mensaje=f"La página {n} no trae un solo número de cuenta.",
                )
            )
    vistos = {n: hallados[0] for n, hallados in numeros.items() if len(set(hallados)) == 1}
    if 1 in vistos:
        for n, numero in vistos.items():
            if numero != vistos[1]:
                problemas.append(
                    Problema(
                        codigo="cuenta_distinta",
                        pagina=n,
                        mensaje=f"La página {n} trae otro número de cuenta que la página 1.",
                    )
                )
        etiqueta = cuentas_locales.get(vistos[1])
        if etiqueta is None:
            problemas.append(
                Problema(
                    codigo="cuenta_sin_etiqueta",
                    mensaje="El número de cuenta de este estado no está en CUENTAS_LOCALES del "
                    f".env para {banco.clave}: agrégalo como {banco.clave}:<número>:<etiqueta>.",
                )
            )
        elif etiqueta not in banco.cuentas:
            problemas.append(
                Problema(
                    codigo="cuenta_desconocida",
                    mensaje=f"La cuenta {etiqueta!r} de CUENTAS_LOCALES no está entre las de "
                    f"{banco.nombre} en config/bancos.yaml ({', '.join(banco.cuentas)}). Si es una "
                    "cuenta nueva, agrégala ahí.",
                )
            )
    del numeros, vistos  # el número de cuenta no sigue de acá

    if resumen is None or problemas:
        raise EstadoRechazado(problemas)

    movimientos: list[MovimientoLeido] = []
    de_las_filas: list[Problema] = []
    en_el_detalle = 0  # la "línea" de cada movimiento: su número en el detalle, desde 1
    for n, columnas, altura in detalle:
        filas, sueltos = _filas_de_pagina(n, paginas[n - 1], columnas, altura)
        de_las_filas += sueltos
        for fila in filas:
            en_el_detalle += 1
            if not fila.celdas:
                continue  # ya rechazada por un texto fuera de columna
            movimiento, malos = _movimiento(
                fila,
                en_el_detalle,
                banco,
                resumen.ultimo_corte,
                resumen.este_corte,
                topes,
            )
            de_las_filas += malos
            if movimiento is not None:
                movimientos.append(movimiento)
    problemas += de_las_filas
    if len(movimientos) > topes.filas:
        problemas.append(
            Problema(
                codigo="tope_filas",
                mensaje=f"El estado trae {len(movimientos)} movimientos y el tope es {topes.filas} "
                "(tope_filas en config/bancos.yaml).",
            )
        )

    if resumen.interes != 0:
        problemas.append(
            Problema(
                codigo="interes",
                mensaje=f"El resumen trae {plata(resumen.interes)} de {banco.resumen.interes}: un "
                "estado con interés no se importa todavía.",
            )
        )
    for nombre, declarado, del_resumen_centavos in (
        ("anterior", saldo_anterior_centavos, resumen.saldo_anterior),
        ("final", saldo_final_centavos, resumen.saldo_final),
    ):
        if declarado != del_resumen_centavos:
            problemas.append(
                Problema(
                    codigo="saldo_declarado",
                    mensaje=f"Declaraste un saldo {nombre} de {plata(declarado)} y el resumen "
                    f"dice {plata(del_resumen_centavos)}.",
                )
            )
    if not de_las_filas:
        creditos = [m.monto_centavos for m in movimientos if m.monto_centavos > 0]
        debitos = [-m.monto_centavos for m in movimientos if m.monto_centavos < 0]
        for nombre, (cuantos, total), detalle_montos in (
            ("créditos", resumen.creditos, creditos),
            ("débitos", resumen.debitos, debitos),
        ):
            if (cuantos, total) != (len(detalle_montos), sum(detalle_montos)):
                problemas.append(
                    Problema(
                        codigo="resumen_no_cuadra",
                        mensaje=f"El resumen dice {cuantos} {nombre} por {plata(total)} y el "
                        f"detalle trae {len(detalle_montos)} por {plata(sum(detalle_montos))}.",
                    )
                )
    if problemas or etiqueta is None:
        raise EstadoRechazado(problemas)
    return EstadoLeido(
        banco=banco.clave,
        cuenta=etiqueta,
        periodo_desde=resumen.ultimo_corte,
        periodo_hasta=resumen.este_corte,
        saldo_anterior_centavos=saldo_anterior_centavos,
        saldo_final_centavos=saldo_final_centavos,
        hash_archivo=hash_archivo,
        movimientos=tuple(movimientos),
    )
