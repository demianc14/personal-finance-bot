"""Auditoría de las conciliaciones: un par vigente une un débito y un gasto que existen,
están vigentes y calzan, uno a uno.

Los repositorios anulan el par cuando se anula su gasto o su estado, y los
índices únicos impiden conciliar algo dos veces. Esto revisa que de verdad sea
así: una base tocada a mano, o una anterior a ese arreglo, puede tener pares
huérfanos, que esconden un cargo sin registrar o retienen un gasto.

También revisa que el par siga siendo uno que las reglas habrían hecho: mismo
monto, movimiento débito y fechas dentro de la ventana de la regla que lo unió.
Un par cuyo gasto o movimiento ya no existe (alguien borró la fila con las
llaves foráneas apagadas) sale igual: la consulta usa LEFT JOIN.

`--conciliar` audita antes de cruzar y no sigue si algo está roto. Reparar es
anular los pares rotos (nada se borra): el siguiente `--conciliar` vuelve a
cruzar lo que quedó libre.
"""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, Engine, text, update

from family_expense_bot.conciliacion.reglas import REGLAS
from family_expense_bot.storage.models import Conciliacion
from family_expense_bot.storage.repository import para_escribir

_VENTANA = "CASE c.regla {} ELSE {} END".format(
    " ".join(f"WHEN '{regla.nombre}' THEN {regla.dias}" for regla in REGLAS),
    max(regla.dias for regla in REGLAS),
)
"""Días que admite la regla que unió el par. Una regla que este módulo no
conoce (un par hecho por otra vía) usa la ventana más ancha."""

_REVISIONES: tuple[tuple[str, str], ...] = (
    ("gasto inexistente", "g.id IS NULL"),
    ("movimiento inexistente", "m.id IS NULL"),
    ("estado inexistente", "m.id IS NOT NULL AND e.id IS NULL"),
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
    ("monto distinto", "g.monto_centavos <> -m.monto_centavos"),
    ("movimiento no es débito", "m.tipo <> 'debito' OR m.monto_centavos >= 0"),
    (
        "fecha fuera de la ventana",
        f"abs(julianday(g.fecha) - julianday(m.fecha)) > {_VENTANA}",
    ),
)
"""Nombre de cada problema y la condición SQL que lo delata en un par vigente.

Si falta el gasto o el movimiento, las condiciones que los comparan dan NULL y
no cuentan: el par sale solo como inexistente.
"""

_PARES_ROTOS = text(
    "SELECT c.id, c.movimiento_id, c.gasto_id, {} ".format(
        ", ".join(condicion for _, condicion in _REVISIONES)
    )
    + """
    FROM conciliaciones c
    LEFT JOIN movimientos m ON m.id = c.movimiento_id
    LEFT JOIN estados_cuenta e ON e.id = m.estado_id
    LEFT JOIN gastos g ON g.id = c.gasto_id
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
    """Lanza `IntegridadRota` si algún par vigente tiene alguno de los problemas de arriba."""
    pares = problemas_de_integridad(engine)
    if pares:
        raise IntegridadRota(pares)


def reparar_integridad(engine: Engine, confirmar: bool = False) -> list[ParRoto]:
    """Los pares rotos que hay que anular; solo los anula con `confirmar`.

    Sin `confirmar` es una simulación: no escribe nada. Con él, anula todos en
    una transacción que toma el candado antes de buscarlos, con el motivo
    `reparado: <problema>`. De un movimiento o un
    gasto que quedó en varios pares se anulan todos: elegir uno sería adivinar,
    y el siguiente `--conciliar` los vuelve a cruzar con las reglas.
    """
    if not confirmar:
        return problemas_de_integridad(engine)
    with para_escribir(engine).begin() as conexion:
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
