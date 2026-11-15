"""Auditoría de las conciliaciones: un par vigente solo une cosas vigentes, uno a uno.

Los repositorios anulan el par cuando se anula su gasto o su estado, y los
índices únicos impiden conciliar algo dos veces. Esto revisa que de verdad sea
así: una base tocada a mano, o una anterior a ese arreglo, puede tener pares
huérfanos, que esconden un cargo sin registrar o retienen un gasto.

`--conciliar` audita antes de cruzar y no sigue si algo está roto. Reparar es
anular los pares rotos (nada se borra): el siguiente `--conciliar` vuelve a
cruzar lo que quedó libre.
"""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, Engine, text, update

from family_expense_bot.storage.models import Conciliacion

_REVISIONES: tuple[tuple[str, str], ...] = (
    ("gasto anulado", "g.anulado_en IS NOT NULL"),
    ("movimiento anulado", "m.anulado_en IS NOT NULL"),
    ("estado anulado", "e.anulado_en IS NOT NULL"),
    (
        "movimiento en más de un par",
        "(SELECT count(*) FROM conciliaciones o "
        "WHERE o.movimiento_id = c.movimiento_id AND o.anulado_en IS NULL) > 1",
    ),
    (
        "gasto en más de un par",
        "(SELECT count(*) FROM conciliaciones o "
        "WHERE o.gasto_id = c.gasto_id AND o.anulado_en IS NULL) > 1",
    ),
)
"""Nombre de cada problema y la condición SQL que lo delata en un par vigente."""

_PARES_ROTOS = text(
    "SELECT c.id, c.movimiento_id, c.gasto_id, {} ".format(
        ", ".join(condicion for _, condicion in _REVISIONES)
    )
    + """
    FROM conciliaciones c
    JOIN movimientos m ON m.id = c.movimiento_id
    JOIN estados_cuenta e ON e.id = m.estado_id
    JOIN gastos g ON g.id = c.gasto_id
    WHERE c.anulado_en IS NULL
    ORDER BY c.id
    """
)


class ParRoto(BaseModel):
    """Una conciliación vigente que no debería estarlo."""

    model_config = ConfigDict(frozen=True)

    conciliacion_id: int
    movimiento_id: int
    gasto_id: int
    problemas: tuple[str, ...]

    @property
    def mensaje(self) -> str:
        return (
            f"par #{self.conciliacion_id} (movimiento #{self.movimiento_id}, "
            f"gasto #{self.gasto_id}): {', '.join(self.problemas)}"
        )


class IntegridadRota(RuntimeError):
    """Hay conciliaciones vigentes rotas. Trae todas, no solo la primera."""

    def __init__(self, pares: list[ParRoto]) -> None:
        self.pares = tuple(pares)
        super().__init__("\n".join(par.mensaje for par in self.pares))


def _pares_rotos(conexion: Connection) -> list[ParRoto]:
    return [
        ParRoto(
            conciliacion_id=conciliacion,
            movimiento_id=movimiento,
            gasto_id=gasto,
            problemas=tuple(
                nombre for (nombre, _), hay in zip(_REVISIONES, banderas, strict=True) if hay
            ),
        )
        for conciliacion, movimiento, gasto, *banderas in conexion.execute(_PARES_ROTOS)
        if any(banderas)
    ]


def problemas_de_integridad(engine: Engine) -> list[ParRoto]:
    """Los pares vigentes rotos, por id. Solo lee."""
    with engine.connect() as conexion:
        return _pares_rotos(conexion)


def auditar_integridad(engine: Engine) -> None:
    """Lanza `IntegridadRota` si algún par vigente une algo anulado o rompe el uno a uno."""
    pares = problemas_de_integridad(engine)
    if pares:
        raise IntegridadRota(pares)


def reparar_integridad(engine: Engine, confirmar: bool = False) -> list[ParRoto]:
    """Los pares rotos que hay que anular; solo los anula con `confirmar`.

    Sin `confirmar` es una simulación: no escribe nada. Con él, anula todos en
    una transacción, con el motivo `reparado: <problema>`. De un movimiento o un
    gasto que quedó en varios pares se anulan todos: elegir uno sería adivinar,
    y el siguiente `--conciliar` los vuelve a cruzar con las reglas.
    """
    if not confirmar:
        return problemas_de_integridad(engine)
    with engine.begin() as conexion:
        pares = _pares_rotos(conexion)
        ahora = datetime.now(UTC)
        for par in pares:
            motivo = f"reparado: {', '.join(par.problemas)}"
            conexion.execute(
                update(Conciliacion)
                .where(Conciliacion.id == par.conciliacion_id, Conciliacion.anulado_en.is_(None))
                .values(anulado_en=ahora, motivo_anulacion=motivo[:200])
            )
    return pares
