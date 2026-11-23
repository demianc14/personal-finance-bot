"""Importar un estado de cuenta: leer, verificar, mostrar y guardar. Entra completo o no entra.

Son dos pasos para que el resumen se vea antes de guardar: `revisar_estado` lee y
verifica sin escribir nada, y `guardar_revision` vuelve a verificar con el candado
de escritura tomado y guarda. La segunda verificación no es de más: entre los dos
pasos otro proceso pudo importar un periodo que se cruza.
"""

from datetime import date
from pathlib import Path
from typing import get_args

from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError

from family_expense_bot.conciliacion.bancos import Banco, Topes
from family_expense_bot.conciliacion.contrato import (
    EstadoLeido,
    EstadoRechazado,
    EstadoVigente,
    Problema,
    TipoMovimiento,
)
from family_expense_bot.conciliacion.ingesta import leer_estado
from family_expense_bot.conciliacion.verificacion import Veredicto, verificar
from family_expense_bot.storage.repository import RepositorioEstados


class Revision(BaseModel):
    """Un estado leído que cuadra, todavía sin guardar: lo que se muestra antes de confirmar."""

    model_config = ConfigDict(frozen=True)

    estado: EstadoLeido
    avisos: tuple[str, ...]

    @property
    def movimientos(self) -> int:
        return len(self.estado.movimientos)

    @property
    def por_tipo(self) -> dict[str, int]:
        """Cuántos movimientos de cada tipo, en el orden de los tipos y sin los que no hay."""
        tipos = [m.tipo for m in self.estado.movimientos]
        return {t: tipos.count(t) for t in get_args(TipoMovimiento) if t in tipos}

    @property
    def creditos(self) -> int:
        """Cuántos movimientos entran."""
        return sum(1 for m in self.estado.movimientos if m.monto_centavos > 0)

    @property
    def debitos(self) -> int:
        """Cuántos movimientos salen."""
        return self.movimientos - self.creditos


class Importacion(BaseModel):
    model_config = ConfigDict(frozen=True)

    estado: EstadoVigente
    movimientos: int
    por_tipo: dict[str, int]
    """Cuántos movimientos de cada tipo, en el orden de los tipos y sin los que no hay."""
    avisos: tuple[str, ...]


def revisar_estado(
    ruta: Path,
    banco: Banco,
    repositorio: RepositorioEstados,
    *,
    cuenta: str,
    desde: date,
    hasta: date,
    topes: Topes,
    saldo_anterior_centavos: int,
    saldo_final_centavos: int,
) -> Revision:
    """Lee y verifica sin guardar nada. Si no cuadra, lanza `EstadoRechazado`."""
    estado = leer_estado(
        ruta,
        banco,
        cuenta=cuenta,
        desde=desde,
        hasta=hasta,
        topes=topes,
        saldo_anterior_centavos=saldo_anterior_centavos,
        saldo_final_centavos=saldo_final_centavos,
    )
    veredicto = _exigir(verificar(estado, repositorio.vigentes()))
    return Revision(estado=estado, avisos=veredicto.avisos)


def guardar_revision(revision: Revision, repositorio: RepositorioEstados) -> Importacion:
    """Verifica de nuevo con el candado tomado y guarda; si ya no cuadra, `EstadoRechazado`."""
    estado = revision.estado
    avisos: tuple[str, ...] = ()

    def revisar(vigentes: list[EstadoVigente]) -> None:
        nonlocal avisos
        avisos = _exigir(verificar(estado, vigentes)).avisos

    try:
        guardado = repositorio.guardar(estado, revisar=revisar)
    except IntegrityError:
        # Última defensa: el índice único del hash, por si la revisión no lo vio.
        raise EstadoRechazado(
            [
                Problema(
                    codigo="archivo_repetido",
                    mensaje="Este archivo ya se importó: la base lo rechazó y no se guardó nada.",
                )
            ]
        ) from None
    return Importacion(
        estado=guardado,
        movimientos=revision.movimientos,
        por_tipo=revision.por_tipo,
        avisos=avisos,
    )


def importar_estado(
    ruta: Path,
    banco: Banco,
    repositorio: RepositorioEstados,
    *,
    cuenta: str,
    desde: date,
    hasta: date,
    topes: Topes,
    saldo_anterior_centavos: int,
    saldo_final_centavos: int,
) -> Importacion:
    """Revisar y guardar de una vez, sin mostrar nada: para scripts y tests con datos sintéticos."""
    revision = revisar_estado(
        ruta,
        banco,
        repositorio,
        cuenta=cuenta,
        desde=desde,
        hasta=hasta,
        topes=topes,
        saldo_anterior_centavos=saldo_anterior_centavos,
        saldo_final_centavos=saldo_final_centavos,
    )
    return guardar_revision(revision, repositorio)


def _exigir(veredicto: Veredicto) -> Veredicto:
    if not veredicto.cuadra:
        raise EstadoRechazado(veredicto.problemas)
    return veredicto
