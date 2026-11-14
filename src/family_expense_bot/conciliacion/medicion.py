"""Precisión y recall del cruce contra pares verdaderos. Solo cuenta: no decide ningún par.

La verdad es un CSV (`evals/pares_verdaderos.csv`) con una fila por movimiento
y el gasto que lo originó, y una fila por gasto sin movimiento. Un par hecho es
correcto solo si el movimiento tenía exactamente ese gasto.

- **Precisión:** de los pares que se hicieron, cuántos son correctos.
- **Recall:** de los pares uno a uno que había que encontrar, cuántos se
  encontraron. Las filas con `fallo_esperado` (un pago partido, que el uno a
  uno no puede resolver) se cuentan aparte y no entran al recall.
"""

import csv
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

NINGUNO = "ninguno"


@dataclass(frozen=True)
class FilaVerdad:
    periodo: str | None
    """Periodo del estado ("2026-03"); None si es un gasto sin movimiento."""
    linea: int | None
    gastos: tuple[int, ...]
    """Vacío si el movimiento no tiene gasto; varios si el cargo son varios gastos."""
    caso: str
    fallo_esperado: bool


@dataclass(frozen=True)
class ParHecho:
    periodo: str
    linea: int
    gasto_id: int
    regla: str


@dataclass(frozen=True)
class Conteo:
    nombre: str
    filas: int = 0
    esperados: int = 0
    """Pares uno a uno que había que encontrar."""
    unidos: int = 0
    correctos: int = 0

    @property
    def precision(self) -> float | None:
        return self.correctos / self.unidos if self.unidos else None

    @property
    def recall(self) -> float | None:
        return self.correctos / self.esperados if self.esperados else None


@dataclass(frozen=True)
class Medicion:
    por_regla: tuple[Conteo, ...]
    """`esperados` es el total de pares a encontrar: el recall de cada regla es su aporte."""
    por_caso: tuple[Conteo, ...]
    total: Conteo
    fallos_esperados: tuple[tuple[FilaVerdad, ParHecho | None], ...]
    """Cada fila con `fallo_esperado` y el par que se hizo con ese movimiento, si hubo."""
    equivocados: tuple[tuple[ParHecho, FilaVerdad | None], ...]
    """Pares hechos que no son verdad, con la fila del movimiento."""
    perdidos: tuple[FilaVerdad, ...]
    """Pares a encontrar que no se hicieron (o se hicieron con otro gasto)."""


def leer_pares(ruta: Path) -> list[FilaVerdad]:
    with ruta.open(encoding="utf-8", newline="") as archivo:
        return [
            FilaVerdad(
                periodo=None if fila["estado"] == NINGUNO else fila["estado"],
                linea=None if fila["movimiento"] == NINGUNO else int(fila["movimiento"]),
                gastos=()
                if fila["gasto_id"] == NINGUNO
                else tuple(int(g) for g in fila["gasto_id"].split("|")),
                caso=fila["categoria_caso"],
                fallo_esperado=fila["fallo_esperado"] == "si",
            )
            for fila in csv.DictReader(archivo)
        ]


def medir(
    verdad: Sequence[FilaVerdad], hechos: Iterable[ParHecho], reglas: Sequence[str]
) -> Medicion:
    hechos = list(hechos)
    clave = tuple[str | None, int | None]
    por_movimiento: dict[clave, FilaVerdad] = {
        (f.periodo, f.linea): f for f in verdad if f.linea is not None
    }
    hecho_de: dict[clave, ParHecho] = {(h.periodo, h.linea): h for h in hechos}

    def a_encontrar(fila: FilaVerdad) -> bool:
        return fila.linea is not None and len(fila.gastos) == 1 and not fila.fallo_esperado

    def correcto(hecho: ParHecho) -> bool:
        fila = por_movimiento.get((hecho.periodo, hecho.linea))
        return fila is not None and fila.gastos == (hecho.gasto_id,)

    esperados = sum(a_encontrar(f) for f in verdad)
    por_regla = tuple(
        Conteo(
            nombre=regla,
            filas=len(verdad),
            esperados=esperados,
            unidos=sum(h.regla == regla for h in hechos),
            correctos=sum(h.regla == regla and correcto(h) for h in hechos),
        )
        for regla in reglas
    )

    por_caso = []
    for caso in dict.fromkeys(f.caso for f in verdad):
        filas = [f for f in verdad if f.caso == caso]
        # Un gasto sin movimiento se cuenta como "unido" si algún par se lo llevó.
        unidos = [
            hecho_de.get((f.periodo, f.linea))
            if f.linea is not None
            else next((h for h in hechos if h.gasto_id in f.gastos), None)
            for f in filas
        ]
        por_caso.append(
            Conteo(
                nombre=caso,
                filas=len(filas),
                esperados=sum(a_encontrar(f) for f in filas),
                unidos=sum(h is not None for h in unidos),
                correctos=sum(
                    h is not None and f.linea is not None and correcto(h)
                    for f, h in zip(filas, unidos, strict=True)
                ),
            )
        )

    return Medicion(
        por_regla=por_regla,
        por_caso=tuple(por_caso),
        total=Conteo(
            nombre="Total",
            filas=len(verdad),
            esperados=esperados,
            unidos=len(hechos),
            correctos=sum(correcto(h) for h in hechos),
        ),
        fallos_esperados=tuple(
            (f, hecho_de.get((f.periodo, f.linea))) for f in verdad if f.fallo_esperado
        ),
        equivocados=tuple(
            (h, por_movimiento.get((h.periodo, h.linea))) for h in hechos if not correcto(h)
        ),
        perdidos=tuple(
            f
            for f in verdad
            if a_encontrar(f)
            and ((hecho := hecho_de.get((f.periodo, f.linea))) is None or not correcto(hecho))
        ),
    )


def _pct(valor: float | None) -> str:
    return "—" if valor is None else f"{valor:.0%}"


def tabla_reglas(medicion: Medicion) -> str:
    lineas = [
        "| Regla | Pares hechos | Correctos | Precisión | Recall (aporte) |",
        "|---|---|---|---|---|",
    ]
    for c in (*medicion.por_regla, medicion.total):
        lineas.append(
            f"| {c.nombre if c is medicion.total else f'`{c.nombre}`'} | {c.unidos} | "
            f"{c.correctos} | {_pct(c.precision)} | "
            f"{c.correctos} de {c.esperados} ({_pct(c.recall)}) |"
        )
    return "\n".join(lineas)


def tabla_casos(medicion: Medicion) -> str:
    lineas = [
        "| categoria_caso | Filas | Pares a encontrar | Unidos | Correctos | Precisión | Recall |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in medicion.por_caso:
        lineas.append(
            f"| `{c.nombre}` | {c.filas} | {c.esperados} | {c.unidos} | {c.correctos} | "
            f"{_pct(c.precision)} | {_pct(c.recall)} |"
        )
    return "\n".join(lineas)
