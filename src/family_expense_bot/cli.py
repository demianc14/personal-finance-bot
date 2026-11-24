"""Punto de entrada: `family-expense-bot` levanta el bot en modo polling; `--app`, la Mini App."""

import argparse
import asyncio
import calendar
import functools
import logging
from datetime import date
from pathlib import Path
from typing import NoReturn

import uvicorn
from sqlalchemy.exc import IntegrityError, OperationalError

from family_expense_bot.app.api import ServiciosApp, crear_app
from family_expense_bot.bot.envio import enviar
from family_expense_bot.bot.handlers import Servicios, build_application, obtener_identidad
from family_expense_bot.conciliacion.bancos import cargar_bancos
from family_expense_bot.conciliacion.contrato import EstadoRechazado, plata
from family_expense_bot.conciliacion.importar import (
    Revision,
    guardar_revision,
    revisar_estado,
    revisar_pdf,
)
from family_expense_bot.conciliacion.ingesta import centavos_declarados
from family_expense_bot.conciliacion.integridad import (
    IntegridadRota,
    auditar_integridad,
    reparar_integridad,
)
from family_expense_bot.conciliacion.reglas import conciliar
from family_expense_bot.config import Settings
from family_expense_bot.dashboard.report import exportar_csv
from family_expense_bot.evaluacion import (
    agregar_resultado,
    cargar_casos,
    correr,
    iniciar_corrida,
    leer_corrida,
    resumir,
    reverificar,
    tabla_markdown,
    version_prompt,
)
from family_expense_bot.extraction.categorias import cargar_taxonomia
from family_expense_bot.extraction.gemini import GeminiCliente
from family_expense_bot.extraction.parser import extraer
from family_expense_bot.storage.repository import (
    EsquemaDesactualizado,
    RepositorioConciliaciones,
    RepositorioEstados,
    RepositorioExtracciones,
    RepositorioGastos,
    RepositorioIngresos,
    crear_engine,
    migrar,
    para_escribir,
)


def _configurar_logging() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
    )
    # httpx loguea la URL completa de cada request, que incluye el token del bot.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="family-expense-bot")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verifica el token contra la API de Telegram y sale. No levanta el bot.",
    )
    parser.add_argument(
        "--exportar",
        metavar="RUTA",
        nargs="?",
        const="data/processed/gastos.csv",
        help="Exporta los gastos a CSV para Power BI. El archivo lleva montos reales "
        "y vive en data/processed/, que está fuera de git.",
    )
    parser.add_argument(
        "--extraer",
        metavar="MENSAJE",
        help="Extrae un gasto de un mensaje usando el LLM real y muestra el resultado. "
        "Prueba manual del parser; no guarda nada. Requiere --como.",
    )
    parser.add_argument(
        "--como",
        metavar="ROL",
        help="Con --extraer: el rol de quien escribe (papa, mama, hermana, demian).",
    )
    parser.add_argument(
        "--evaluar",
        metavar="BANCO",
        nargs="?",
        const="evals/mensajes.yaml",
        help="Pasa el banco de mensajes etiquetados por el LLM real, guarda la corrida en "
        "evals/corridas/ e imprime la tabla. Gasta cuota de Gemini.",
    )
    parser.add_argument("--modelo", help="Con --evaluar: modelo de Gemini (el de .env si no).")
    parser.add_argument(
        "--limite", type=int, help="Con --evaluar: solo los primeros N casos, para probar."
    )
    parser.add_argument(
        "--pausa",
        type=float,
        default=4.0,
        help="Con --evaluar: segundos entre llamadas (4); el plan gratuito limita por minuto.",
    )
    parser.add_argument(
        "--desde-corrida",
        metavar="JSONL",
        help="Con --evaluar: recalcula la tabla de una corrida guardada, sin llamar al LLM.",
    )
    parser.add_argument(
        "--app",
        action="store_true",
        help="Levanta la Mini App en 127.0.0.1 (exponla con cloudflared y pon la URL en "
        "APP_URL). Corre aparte del bot; comparten la base.",
    )
    parser.add_argument("--puerto", type=int, default=8080, help="Con --app: puerto local (8080).")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Con --app: interfaz donde escucha (127.0.0.1). En Docker va 0.0.0.0 y el "
        "compose publica el puerto solo en 127.0.0.1 del equipo.",
    )
    parser.add_argument(
        "--migrar",
        action="store_true",
        help="Agrega a una base existente las tablas y columnas nuevas del esquema. "
        "Respáldala antes.",
    )
    parser.add_argument(
        "--importar-estado",
        metavar="ARCHIVO",
        help="Importa un estado de cuenta en CSV o PDF. Entra completo o no entra: si no "
        "cuadra, dice por qué y no guarda nada. Un CSV requiere --banco, --cuenta, --desde y "
        "--hasta; un PDF solo --banco (el periodo y la cuenta salen del archivo y de "
        "CUENTAS_LOCALES).",
    )
    parser.add_argument(
        "--banco", help="Con --importar-estado: clave del banco en config/bancos.yaml."
    )
    parser.add_argument(
        "--cuenta",
        help='Con --importar-estado: etiqueta de la cuenta ("ahorros", "visa"). Nunca el número.',
    )
    parser.add_argument(
        "--desde",
        type=date.fromisoformat,
        metavar="AAAA-MM-DD",
        help="Con --importar-estado: primer día del periodo del estado.",
    )
    parser.add_argument(
        "--hasta",
        type=date.fromisoformat,
        metavar="AAAA-MM-DD",
        help="Con --importar-estado: último día del periodo del estado.",
    )
    for saldo in ("anterior", "final"):
        parser.add_argument(
            f"--saldo-{saldo}",
            type=_saldo,
            metavar="MONTO",
            help=f"Con --importar-estado: saldo {saldo} como lo muestra el banco, con punto "
            "decimal (1234.05). Obligatorio.",
        )
    parser.add_argument(
        "--conciliar",
        type=_mes,
        metavar="AAAA-MM",
        help="Cruza los movimientos de ese mes con los gastos registrados, con reglas "
        "deterministas y sin LLM. Guarda cada par con la regla que lo unió; los movimientos "
        "sin par no se guardan como gasto.",
    )
    parser.add_argument(
        "--reparar-integridad",
        action="store_true",
        help="Lista las conciliaciones vigentes rotas (sobre un gasto o un estado anulado, o "
        "repetidas) y no cambia nada. Con --confirmar las anula.",
    )
    parser.add_argument(
        "--confirmar",
        action="store_true",
        help="Con --importar-estado: guarda el estado después de mostrar el resumen. Con "
        "--reparar-integridad o --anular-par: anula de verdad, en una transacción.",
    )
    parser.add_argument(
        "--anular-par",
        type=int,
        metavar="ID",
        help="Anula una conciliación por su id y deja libres a su movimiento y a su gasto. "
        "Exige --motivo. Sin --confirmar solo muestra qué anularía.",
    )
    parser.add_argument(
        "--motivo",
        metavar="TEXTO",
        help="Con --anular-par: por qué se anula el par. Queda guardado con la anulación.",
    )
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if args.confirmar and not (
        args.importar_estado or args.reparar_integridad or args.anular_par is not None
    ):
        parser.error(
            "--confirmar solo va con --importar-estado, --reparar-integridad o --anular-par"
        )
    if args.motivo is not None and args.anular_par is None:
        parser.error("--motivo solo va con --anular-par")
    if args.anular_par is not None and not (args.motivo or "").strip():
        parser.error("--anular-par exige --motivo con el porqué")

    _configurar_logging()
    if args.evaluar and args.desde_corrida:
        # Sin .env: recalcular una corrida guardada no necesita ningún secreto.
        _recalcular_evaluacion(Path(args.evaluar), Path(args.desde_corrida))
        return

    settings = Settings()  # valores vienen del entorno
    token = settings.telegram_bot_token.get_secret_value()

    if args.check:
        username = asyncio.run(obtener_identidad(token))
        print(f"Token válido. Conectado como @{username}")
        return

    if args.exportar:
        _exportar(Path(args.exportar), settings)
        return

    if args.migrar:
        agregadas = migrar(settings.database_url)
        print(f"Agregadas: {', '.join(agregadas)}" if agregadas else "La base ya estaba al día")
        return

    if args.importar_estado:
        _importar_estado(parser, args, settings)
        return

    if args.reparar_integridad:
        _reparar_integridad(args.confirmar, settings)
        return

    if args.anular_par is not None:
        _anular_par(args.anular_par, args.motivo.strip(), args.confirmar, settings)
        return

    if args.conciliar:
        desde, hasta = args.conciliar
        _conciliar(desde, hasta, settings)
        return

    if args.evaluar:
        modelo = args.modelo or settings.gemini_modelo
        _evaluar(Path(args.evaluar), modelo, args.limite, args.pausa, settings)
        return

    if args.app:
        _levantar_app(settings, token, args.host, args.puerto)
        return

    if args.extraer:
        if not args.como:
            parser.error("--extraer necesita --como ROL: los parentescos dependen de quién escribe")
        _mostrar_extraccion(args.extraer, args.como, settings)
        return

    try:
        servicios = Servicios(
            repositorio=RepositorioGastos(engine := crear_engine(settings.database_url)),
            extracciones=RepositorioExtracciones(engine),
            cliente=_cliente_gemini(settings),
            taxonomia=cargar_taxonomia(),
            usuarios=settings.usuarios,
            ingresos=settings.ingresos,
            ingresos_extra=RepositorioIngresos(engine),
        )
    except (ValueError, EsquemaDesactualizado) as e:
        # Errores de configuración: se explican en una línea, sin traceback.
        raise SystemExit(f"No arranco: {e}") from e

    if not servicios.usuarios:
        # Sin usuarios el bot no le responde a nadie: es la falla segura para
        # un bot que cualquiera en Telegram puede encontrar.
        logging.getLogger(__name__).warning(
            "TELEGRAM_USUARIOS está vacío: el bot va a ignorar todos los mensajes. "
            "Escríbele a @userinfobot para saber tu ID y agrégalo en .env como id:rol"
        )

    build_application(token, servicios, settings.app_url).run_polling()


def _levantar_app(settings: Settings, token: str, host: str, puerto: int) -> None:
    try:
        servicios = ServiciosApp(
            repositorio=RepositorioGastos(engine := crear_engine(settings.database_url)),
            ingresos_extra=RepositorioIngresos(engine),
            taxonomia=cargar_taxonomia(),
            bot_token=token,
            usuarios=settings.usuarios,
            ingresos=settings.ingresos,
            notificar=functools.partial(enviar, token),
        )
    except (ValueError, EsquemaDesactualizado) as e:
        raise SystemExit(f"No arranco: {e}") from e
    # Solo local: lo que llega de afuera entra por el túnel, que termina el https.
    uvicorn.run(crear_app(servicios), host=host, port=puerto)


def _exportar(ruta: Path, settings: Settings) -> None:
    repositorio = RepositorioGastos(crear_engine(settings.database_url))
    gastos = repositorio.listar()
    exportar_csv(gastos, ruta)
    print(f"{len(gastos)} gasto(s) exportados a {ruta}")


def _saldo(texto: str) -> int:
    try:
        return centavos_declarados(texto)
    except ValueError as e:
        # argparse solo muestra el mensaje si el error es suyo.
        raise argparse.ArgumentTypeError(str(e)) from e


def _importar_estado(
    parser: argparse.ArgumentParser, args: argparse.Namespace, settings: Settings
) -> None:
    bancos = cargar_bancos()
    if args.banco is None or args.saldo_anterior is None or args.saldo_final is None:
        parser.error("--importar-estado necesita --banco, --saldo-anterior y --saldo-final")
    es_pdf = args.banco in bancos.bancos_pdf
    if es_pdf and (args.cuenta, args.desde, args.hasta) != (None, None, None):
        parser.error(
            f"{args.banco} exporta PDF: el periodo sale del resumen y la cuenta de "
            "CUENTAS_LOCALES, así que no van --cuenta, --desde ni --hasta"
        )
    if not es_pdf and None in (args.cuenta, args.desde, args.hasta):
        parser.error("Un estado en CSV necesita también --cuenta, --desde y --hasta")
    if not es_pdf and args.banco not in bancos.bancos:
        todos = sorted([*bancos.bancos, *bancos.bancos_pdf])
        parser.error(
            f"No existe el banco {args.banco!r} en config/bancos.yaml; hay: {', '.join(todos)}"
        )
    try:
        repositorio = RepositorioEstados(crear_engine(settings.database_url))
    except EsquemaDesactualizado as e:
        raise SystemExit(f"No importo: {e}") from e
    revision: Revision
    try:
        if es_pdf:
            try:
                cuentas = settings.mapa_cuentas.get(args.banco, {})
            except ValueError as e:
                raise SystemExit(f"No importo: {e}") from None
            revision = revisar_pdf(
                Path(args.importar_estado),
                bancos.bancos_pdf[args.banco],
                repositorio,
                cuentas_locales=cuentas,
                topes=bancos.topes,
                saldo_anterior_centavos=args.saldo_anterior,
                saldo_final_centavos=args.saldo_final,
            )
        else:
            revision = revisar_estado(
                Path(args.importar_estado),
                bancos.bancos[args.banco],
                repositorio,
                cuenta=args.cuenta,
                desde=args.desde,
                hasta=args.hasta,
                topes=bancos.topes,
                saldo_anterior_centavos=args.saldo_anterior,
                saldo_final_centavos=args.saldo_final,
            )
    except EstadoRechazado as e:
        _rechazado(e)
    # El resumen va siempre antes de guardar: sin --confirmar no se escribe nada.
    estado = revision.estado
    tipos = ", ".join(f"{cuantos} {tipo}" for tipo, cuantos in revision.por_tipo.items())
    print(
        f"Estado a importar: {estado.banco}/{estado.cuenta}, "
        f"{estado.periodo_desde} a {estado.periodo_hasta}."
    )
    print(
        f"  Saldo {plata(estado.saldo_anterior_centavos)} → {plata(estado.saldo_final_centavos)} "
        f"con {revision.movimientos} movimientos{': ' + tipos if tipos else ''}."
    )
    print(
        f"  Entran {plata(estado.creditos_centavos)} en "
        f"{revision.creditos} movimiento{'' if revision.creditos == 1 else 's'} "
        f"y salen {plata(estado.debitos_centavos)} en {revision.debitos}."
    )
    for aviso in revision.avisos:
        print(f"  Aviso: {aviso}")
    if not args.confirmar:
        print("No se guardó nada. Si el resumen está bien, repite el comando con --confirmar.")
        return
    try:
        resultado = guardar_revision(revision, repositorio)
    except EstadoRechazado as e:
        _rechazado(e)
    # Un aviso nuevo solo puede aparecer si otro proceso importó entre la revisión y el guardado.
    for aviso in resultado.avisos:
        if aviso not in revision.avisos:
            print(f"  Aviso: {aviso}")
    print(f"Estado importado (#{resultado.estado.id}).")


def _rechazado(error: EstadoRechazado) -> NoReturn:
    problemas = "\n".join(f"  - {p.mensaje}" for p in error.problemas)
    raise SystemExit(f"Estado rechazado; no se guardó nada:\n{problemas}") from error


def _mes(texto: str) -> tuple[date, date]:
    """El primer y el último día de un mes escrito como AAAA-MM."""
    try:
        primero = date.fromisoformat(f"{texto}-01")
        if len(texto) != 7:
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError(f"{texto!r} no es un mes: va como AAAA-MM") from None
    return primero, primero.replace(day=calendar.monthrange(primero.year, primero.month)[1])


def _conciliar(desde: date, hasta: date, settings: Settings) -> None:
    try:
        engine = crear_engine(settings.database_url)
    except EsquemaDesactualizado as e:
        raise SystemExit(f"No concilio: {e}") from e
    try:
        auditar_integridad(engine)
    except IntegridadRota as e:
        rotos = "\n".join(f"  - {par.mensaje}" for par in e.pares)
        raise SystemExit(
            f"No concilio: hay {len(e.pares)} conciliaciones vigentes rotas y no se guardó nada.\n"
            f"{rotos}\n"
            "Corre `family-expense-bot --reparar-integridad` para ver qué anularía, y con "
            "--confirmar para anularlas."
        ) from e
    bancos = cargar_bancos()
    try:
        # Con el candado tomado antes de leer candidatos: nadie anula un gasto ni
        # hace otro cruce entre que se eligen los pares y se guardan.
        cruce = conciliar(
            para_escribir(engine),
            bancos.genericas,
            desde,
            hasta,
            con_descripcion=bancos.regla_descripcion,
        )
    except IntegrityError as e:
        raise SystemExit(
            "No concilio: la base rechazó un par (un movimiento o un gasto ya estaba en otro "
            f"par vigente) y no se guardó nada.\n  {e.orig}\n"
            "Corre `family-expense-bot --reparar-integridad` y vuelve a intentar."
        ) from e
    except OperationalError as e:
        raise SystemExit(
            "No concilio: la base está ocupada por otra escritura o no se pudo usar, y no se "
            f"guardó nada.\n  {e.orig}\nVuelve a intentar en unos segundos."
        ) from e
    reglas = ", ".join(f"{regla} {cuantos}" for regla, cuantos in cruce.por_regla.items())
    print(f"Conciliación de {desde:%Y-%m}: {len(cruce.pares)} pares nuevos ({reglas}).")
    print(
        f"  Quedan {cruce.sin_conciliar} débitos sin conciliar; {len(cruce.empatados)} de ellos "
        "por empate entre candidatos iguales."
    )
    if cruce.pares_fuera_del_periodo:
        print(
            f"  Además se guardaron {cruce.pares_fuera_del_periodo} pares de otros meses: "
            "el cruce mira todos los movimientos libres, no solo los de este mes."
        )


def _anular_par(id_par: int, motivo: str, confirmar: bool, settings: Settings) -> None:
    try:
        engine = crear_engine(settings.database_url)
    except EsquemaDesactualizado as e:
        raise SystemExit(f"No anulo: {e}") from e
    repositorio = RepositorioConciliaciones(engine)
    par = repositorio.obtener(id_par)
    if par is None or par.anulado:
        estado = "no existe" if par is None else "ya estaba anulado"
        raise SystemExit(f"No anulo: el par #{id_par} {estado}. No se cambió nada.")
    descrito = (
        f"par #{par.id} (movimiento #{par.movimiento_id}, gasto #{par.gasto_id}, regla {par.regla})"
    )
    if not confirmar:
        print(f"Simulación: se anularía el {descrito} con el motivo {motivo!r}.")
        print("  No se cambió nada. Para anularlo, repite el comando con --confirmar.")
        return
    try:
        anulado = repositorio.anular(id_par, motivo)
    except OperationalError as e:
        raise SystemExit(
            f"No anulo: la base está ocupada por otra escritura.\n  {e.orig}\n"
            "Vuelve a intentar en unos segundos."
        ) from e
    if not anulado:  # otro proceso lo anuló entre la consulta y ahora
        raise SystemExit(f"No anulo: el par #{id_par} ya estaba anulado. No se cambió nada.")
    print(f"Anulado el {descrito}. El movimiento y el gasto siguen vigentes y quedan libres.")
    print(
        "  Ojo: las reglas no recuerdan este rechazo. Si el monto y la fecha siguen calzando, "
        "el siguiente --conciliar los puede volver a unir."
    )


def _reparar_integridad(confirmar: bool, settings: Settings) -> None:
    try:
        engine = crear_engine(settings.database_url)
    except EsquemaDesactualizado as e:
        raise SystemExit(f"No reparo: {e}") from e
    pares = reparar_integridad(engine, confirmar=confirmar)
    if not pares:
        print("Las conciliaciones están íntegras: no hay nada que reparar.")
        return
    cuantos = f"{len(pares)} par" if len(pares) == 1 else f"{len(pares)} pares"
    if confirmar:
        print(f"Reparado: {cuantos} anulado{'' if len(pares) == 1 else 's'}.")
    else:
        print(f"Simulación: se anularía {cuantos}. No se cambió nada.")
    for par in pares:
        print(f"  - {par.mensaje}")
    if confirmar:
        print("  Corre --conciliar de nuevo: lo que quedó libre vuelve a cruzarse.")
    else:
        print("  Para anularlos: family-expense-bot --reparar-integridad --confirmar")


def _cliente_gemini(settings: Settings, modelo: str | None = None) -> GeminiCliente:
    if settings.gemini_api_key is None:
        raise SystemExit("Falta GEMINI_API_KEY en .env")
    return GeminiCliente(
        settings.gemini_api_key.get_secret_value(), modelo=modelo or settings.gemini_modelo
    )


def _evaluar(
    banco: Path, modelo: str, limite: int | None, pausa: float, settings: Settings
) -> None:
    taxonomia = cargar_taxonomia()
    hoy, casos = cargar_casos(banco, taxonomia)
    casos = casos[:limite] if limite else casos
    ruta = banco.parent / "corridas" / f"{modelo}-{version_prompt()}.jsonl"
    resultados = iniciar_corrida(ruta, modelo, hoy)
    hechos = {r.id for r in resultados}
    pendientes = [c for c in casos if c.id not in hechos]
    if hechos:
        print(f"  Retomando {ruta}: {len(hechos)} casos ya corridos, faltan {len(pendientes)}")
    cliente = _cliente_gemini(settings, modelo)
    for r in correr(pendientes, cliente, taxonomia, hoy, pausa):
        agregar_resultado(ruta, r)
        resultados.append(r)
        print(f"  {len(resultados):>3}/{len(casos)} {r.id} ({r.segundos:.1f} s)", flush=True)
    # Con --limite, la corrida puede traer casos de más: la tabla es de estos casos.
    en_banco = {c.id for c in casos}
    resultados = [r for r in resultados if r.id in en_banco]
    if faltan := len(casos) - len(resultados):
        print(f"\n  Cuota agotada: quedan {faltan} casos; vuelve a correrlo para retomar.")
    print(f"\n  Corrida guardada en {ruta}\n")
    print(tabla_markdown(resumir(casos, resultados), modelo))


def _recalcular_evaluacion(banco: Path, corrida: Path) -> None:
    taxonomia = cargar_taxonomia()
    _, casos = cargar_casos(banco, taxonomia)
    meta, guardados = leer_corrida(corrida)
    # Las respuestas del LLM son las guardadas; la verificación es la de hoy.
    resultados = reverificar(casos, guardados, taxonomia, date.fromisoformat(meta["hoy"]))
    if meta["prompt"] != version_prompt():
        print(
            f"  Ojo: la corrida es del prompt {meta['prompt']}, el actual es {version_prompt()}\n"
        )
    print(tabla_markdown(resumir(casos, resultados), meta["modelo"], meta["prompt"]))


def _mostrar_extraccion(mensaje: str, remitente: str, settings: Settings) -> None:
    cliente = _cliente_gemini(settings)
    gasto = extraer(mensaje, cliente, cargar_taxonomia(), remitente.strip().lower())

    print(f"  monto:       {gasto.monto} ({gasto.confianza_monto})")
    print(f"  categoría:   {gasto.categoria} ({gasto.confianza_categoria})")
    print(f"  fecha:       {gasto.fecha} ({gasto.confianza_fecha})")
    print(f"  de:          {gasto.atribuido_a} ({gasto.confianza_atribuido_a})")
    print(f"  descripción: {gasto.descripcion}")
    for nota in gasto.notas:
        print(f"  ⚠ [{nota.campo}] {nota.texto}")
    print(f"\n  ¿Sin dudas? {'sí' if gasto.listo_para_guardar else 'no'}")


if __name__ == "__main__":
    main()
