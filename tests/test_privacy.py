"""Guardas de privacidad: el repo es público y maneja dinero real de terceros."""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.sintetico import cargar

ROOT = Path(__file__).resolve().parents[1]

DEBEN_IGNORARSE = [
    "data/processed/gastos.db",
    "data/processed/export.csv",
    "gastos.sqlite",
    "reporte.xlsx",
    ".env",
    "n8n/.env",
    "n8n/credenciales.json",
    "n8n/.datos/database.sqlite",
    "n8n/e2e/.datos/database.sqlite",
    # Estados de cuenta y lo que sale de ellos: en ninguna carpeta, con ningún formato.
    "estado-marzo.csv",
    "estado-marzo.xlsx",
    "estado-marzo.xls",
    "data/raw/estado-marzo.csv",
    "data/raw/estado-marzo.pdf",
    "config/estado-marzo.csv",
    "tests/fixtures/estado-marzo.csv",
    "tests/fixtures/estados/marzo.xlsx",
    "tests/fixtures/estados/marzo.pbix",
    "tests/fixtures/estados/real/marzo.csv",
    "tests/fixtures/estados/real/marzo.pdf",
    "estado-marzo.pdf",
    "docs/estado-marzo.pdf",
    "scripts/estado-marzo.pdf",
    "powerbi/conciliacion.pbix",
    "powerbi/cobertura.csv",
    "exports/vistas.csv",
    # El texto de un PDF real (lo que va a volcar el parser de G1b) y las copias de la base.
    "tests/fixtures/estados/volcado.txt",
    "evals/corridas/estado-real.txt",
    "data/processed/gastos.db-wal",
    "data/processed/gastos.db-shm",
    "gastos.sqlite-wal",
    "gastos.db.bak",
    "respaldo.sql",
]


@pytest.mark.parametrize("ruta", DEBEN_IGNORARSE)
def test_gitignore_excluye_datos_reales_y_secretos(ruta: str) -> None:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", ruta], cwd=ROOT, check=False
    )
    assert result.returncode == 0, f"{ruta} NO está ignorado por .gitignore"


def test_env_example_si_se_versiona() -> None:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", ".env.example"], cwd=ROOT, check=False
    )
    assert result.returncode == 1


SECRETOS = ("TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY", "TELEGRAM_USUARIOS", "INGRESOS_MENSUALES")
"""Variables que en `.env.example` tienen que ir vacías: son secretos o IDs personales."""


SECRETOS_N8N = (
    "N8N_ENCRYPTION_KEY",
    "N8N_WEBHOOK_URL",
    "TELEGRAM_USUARIOS",
    "TELEGRAM_ADMIN_CHAT_ID",
    "INGRESOS_MENSUALES",
)


@pytest.mark.parametrize(
    ("archivo", "secretos"), [(".env.example", SECRETOS), ("n8n/.env.example", SECRETOS_N8N)]
)
def test_env_example_no_trae_valores_secretos(archivo: str, secretos: tuple[str, ...]) -> None:
    valores = dict(
        linea.split("=", 1)
        for linea in (ROOT / archivo).read_text(encoding="utf-8").splitlines()
        if "=" in linea and not linea.lstrip().startswith("#")
    )
    assert set(secretos) <= valores.keys()
    con_valor = [clave for clave in secretos if valores[clave].strip()]
    assert not con_valor, f"{archivo} trae valores en {con_valor}"


def test_credenciales_de_ejemplo_n8n_solo_tienen_marcadores() -> None:
    credenciales = json.loads((ROOT / "n8n" / "credenciales.ejemplo.json").read_text())
    for credencial in credenciales:
        for campo in ("accessToken", "apiKey"):
            if campo in credencial["data"]:
                assert re.fullmatch(r"<[A-Z_]+>", credencial["data"][campo]), campo


PATRONES_DE_SECRETOS = {
    "token de bot de Telegram": re.compile(r"\b\d{8,10}:AA[\w-]{30,}"),
    "API key de Google": re.compile(r"\bAIza[\w-]{30,}"),
    "API key de Google (AQ.)": re.compile(r"\bAQ\.[\w-]{30,}"),
}


def test_ningun_archivo_versionado_contiene_secretos() -> None:
    """`.env.example` llegó a versionarse con un token real: esto lo habría atajado."""
    archivos = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.split("\0")
    hallazgos = []
    for nombre in filter(None, archivos):
        ruta = ROOT / nombre
        if not ruta.is_file():
            continue
        texto = ruta.read_text(encoding="utf-8", errors="ignore")
        hallazgos += [
            f"{nombre}: {tipo}" for tipo, p in PATRONES_DE_SECRETOS.items() if p.search(texto)
        ]
    assert not hallazgos, f"Posibles secretos versionados: {hallazgos}"


def test_ningun_commit_del_historial_contiene_secretos() -> None:
    """Revisa cada objeto de git, no solo la versión actual: un push publica todo el historial.

    El historial se limpió con `git filter-repo --replace-text` después de que
    `.env.example` llegara a versionarse con un token real.
    """
    objetos = subprocess.run(
        ["git", "cat-file", "--batch-all-objects", "--batch"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8", errors="ignore")
    hallazgos = [tipo for tipo, p in PATRONES_DE_SECRETOS.items() if p.search(objetos)]
    assert not hallazgos, f"El historial de git contiene: {hallazgos}"


def test_ningun_commit_del_historial_versiono_un_archivo_ignorado() -> None:
    """Una base o un export que se versionó y después se ignoró sigue en el historial.

    Toma cada ruta que alguna vez entró a un commit y le pregunta al `.gitignore`
    actual si la habría excluido.
    """
    rutas = subprocess.run(
        ["git", "log", *_refs(), "--name-only", "--format="],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    ignoradas = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        cwd=ROOT,
        input="\n".join(sorted(set(rutas.splitlines()) - {""})),
        check=False,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert not ignoradas, f"El historial de git versionó archivos ignorados: {ignoradas}"


# --- Estados de cuenta: lo único tabular que se versiona es sintético ---

ESTADOS_SINTETICOS = "tests/fixtures/estados"
PARES_VERDADEROS = "evals/pares_verdaderos.csv"
CORRIDAS_DE_CRUCE = "evals/corridas/cruce-"
"""Salidas guardadas de `scripts/medir_cruce.py` (`cruce-<commit>.txt`), sobre la base sintética."""
DATOS = (
    ".csv",
    ".tsv",
    ".txt",
    ".xlsx",
    ".xls",
    ".xlsm",
    ".xlsb",
    ".ods",
    ".numbers",
    ".ofx",
    ".qif",
    ".pbix",
    ".pdf",
    ".parquet",
    ".db",
    ".sqlite",
    ".sqlite3",
    "-wal",
    "-shm",
    ".sql",
    ".bak",
    ".zip",
    ".gz",
)
"""Formatos en los que podría colarse un estado de cuenta, su texto o una copia de la base.

`.gitignore` ignora todos (lo exige un test de abajo). Las imágenes de `docs/img/`
y las corridas `.jsonl` de `evals/` no están: no hay generador contra el cual
compararlas byte a byte (límite en docs/decisiones.md)."""

BLOBS_SINTETICOS = ROOT / "tests" / "blobs_sinteticos"
"""Versiones viejas de los archivos sintéticos: `<blob> <ruta> <commit del generador>`."""


@pytest.mark.parametrize("extension", DATOS)
@pytest.mark.parametrize("carpeta", ["", "evals/corridas/", "docs/img/", "tests/fixtures/"])
def test_gitignore_ignora_cada_formato_de_datos(carpeta: str, extension: str) -> None:
    """La tupla `DATOS` y `.gitignore` son la misma lista: si una crece, la otra también."""
    ruta = f"{carpeta}estado-marzo{extension}"
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", ruta], cwd=ROOT, check=False
    )
    assert result.returncode == 0, f"{ruta} NO está ignorado por .gitignore"


def _es_dato(ruta: str) -> bool:
    return ruta.lower().endswith(DATOS) or ruta.startswith(f"{ESTADOS_SINTETICOS}/")


def _es_corrida_de_cruce(ruta: str) -> bool:
    """`evals/corridas/cruce-<commit>.txt`, con sufijos opcionales (`cruce-99a42c4-antes.txt`).

    El contenido no se compara con nada: es la salida de una corrida, no de un generador.
    """
    patron = r"evals/corridas/cruce-[0-9a-f]{7,40}(-[a-z0-9]+)*\.txt"
    return re.fullmatch(patron, ruta) is not None


def _refs() -> list[str]:
    """Qué historial se revisa. Por defecto todas las ramas y tags, como los ve un push.

    El hook local pasa `PRIVACIDAD_REFS=HEAD`: una rama local de otra tarea no
    debe frenar los commits de esta. CI no lo pasa y revisa todo.
    """
    refs = os.environ.get("PRIVACIDAD_REFS", "--all").split()
    opcion = r"--(all|branches|tags|remotes)(=[\w./*-]+)?"
    nombre = r"[\w./^~][\w./^~-]*"  # un ref, nunca una opción de git log
    if not refs or not all(re.fullmatch(f"{opcion}|{nombre}", r) for r in refs):
        raise ValueError(f"PRIVACIDAD_REFS no es una lista de refs: {refs!r}")
    return refs


def _versiones_de_datos(raiz: Path, refs: list[str]) -> list[tuple[str, str, str]]:
    """(blob, ruta, commit) de cada versión de un archivo de datos que entró a algún commit.

    `-m` muestra también lo que agrega un merge; `-z` deja las rutas tal cual, sin
    comillas, aunque traigan tildes, comillas o un tabulador.
    """
    salida = subprocess.run(
        [
            "git",
            "log",
            *refs,
            "-m",
            "--raw",
            "--no-abbrev",
            "--no-renames",
            "-z",
            "--format=%x00%H",
        ],
        cwd=raiz,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8", errors="surrogateescape")
    partes = salida.split("\0")
    versiones = []
    commit, i = "", 0
    while i < len(partes):
        parte = partes[i].lstrip("\n")
        if parte.startswith(":"):
            # ":<modo> <modo> <blob viejo> <blob nuevo> <estado>" y después la ruta.
            blob, ruta = parte.split()[3], partes[i + 1]
            if set(blob) != {"0"} and _es_dato(ruta):
                versiones.append((blob, ruta, commit))
            i += 2
            continue
        if re.fullmatch(r"[0-9a-f]{40}", parte):
            commit = parte
        i += 1
    return versiones


def _no_sinteticas(raiz: Path, permitidos: set[str], refs: list[str] | None = None) -> list[str]:
    """Las versiones de datos del historial que no son ni sintéticas ni una corrida de cruce."""
    vistas: dict[tuple[str, str], str] = {}
    for blob, ruta, commit in _versiones_de_datos(raiz, refs or ["--all"]):
        if blob not in permitidos and not _es_corrida_de_cruce(ruta):
            vistas.setdefault((blob, ruta), commit)
    return [f"{blob} {ruta} ({commit[:7]})" for (blob, ruta), commit in sorted(vistas.items())]


def _blob(datos: bytes) -> str:
    """El id que git le da a un archivo con estos bytes (`git hash-object`)."""
    return hashlib.sha1(b"blob %d\0" % len(datos) + datos).hexdigest()


def _permitidos() -> set[str]:
    """Blobs de lo que genera hoy el script, más los de `tests/blobs_sinteticos`."""
    hoy = {_blob(datos) for datos in [*_sinteticos().values(), cargar().pares_verdaderos()]}
    viejos = {
        linea.split()[0]
        for linea in BLOBS_SINTETICOS.read_text(encoding="utf-8").splitlines()
        if linea.strip() and not linea.startswith("#")
    }
    return hoy | viejos


def _git(raiz: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=prueba",
            "-c",
            "user.email=prueba@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=raiz,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo_con_un_estado_colado(tmp_path: Path) -> Path:
    """Un repo que versiona un estado y lo borra en el commit siguiente: lo que ve un push."""
    _git(tmp_path, "init", "-q")
    estados = tmp_path / "tests" / "fixtures" / "estados"
    estados.mkdir(parents=True)
    (estados / "marzo.csv").write_bytes(b"sintetico\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "sintetico")
    (estados / "real.csv").write_bytes(b"Fecha,Descripcion\n05/03/2026,INVENTADO\n")
    (estados / "marzo.csv").write_bytes(b"cambiado\n")
    (tmp_path / "volcado.txt").write_bytes(b"texto de un pdf\n")
    (tmp_path / "CON TAB\t.CSV").write_bytes(b"x\n")
    (tmp_path / "evals" / "corridas").mkdir(parents=True)
    (tmp_path / "evals" / "corridas" / "cruce-abc1234.txt").write_bytes(b"corrida\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "cuela")
    _git(tmp_path, "rm", "-q", "tests/fixtures/estados/real.csv", "volcado.txt", "CON TAB\t.CSV")
    (estados / "marzo.csv").write_bytes(b"sintetico\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "limpia")
    return tmp_path


def test_un_estado_que_entro_y_se_borro_se_detecta_en_el_historial(
    repo_con_un_estado_colado: Path,
) -> None:
    colados = _no_sinteticas(repo_con_un_estado_colado, {_blob(b"sintetico\n")})
    rutas = sorted(c.split(" ", 1)[1].split(" (", 1)[0] for c in colados)
    assert rutas == [
        "CON TAB\t.CSV",
        "tests/fixtures/estados/marzo.csv",
        "tests/fixtures/estados/real.csv",
        "volcado.txt",
    ]


def test_ninguna_version_del_historial_es_un_dato_que_no_sea_sintetico() -> None:
    """Revisa el contenido de cada versión, no solo la ruta: un push publica todo el historial.

    Si falla tras regenerar los fixtures, el mensaje trae las líneas para
    `tests/blobs_sinteticos`: son las versiones que generaba el script antes.
    """
    colados = _no_sinteticas(ROOT, _permitidos(), _refs())
    assert not colados, "Versiones de datos en el historial que no son sintéticas:\n" + "\n".join(
        colados
    )


def test_el_hook_de_privacidad_corre_antes_del_commit_y_del_push() -> None:
    """CI llega tarde: cuando corre, el push ya publicó el dato."""
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = [h for repo in config["repos"] if repo["repo"] == "local" for h in repo["hooks"]]
    privacidad = [h for h in hooks if "tests/test_privacy.py" in h["entry"]]
    assert len(privacidad) == 1, "falta el hook local que corre tests/test_privacy.py"
    hook = privacidad[0]
    assert hook["always_run"] is True and hook["pass_filenames"] is False
    assert set(hook["stages"]) == {"pre-commit", "pre-push"}


def _sinteticos() -> dict[str, bytes]:
    """Lo que genera `scripts/estado_sintetico.py` para la carpeta: los CSV y los PDF."""
    modulo = cargar()
    return {**modulo.archivos(), **modulo.pdfs()}


@pytest.mark.parametrize("nombre", ["marzo.csv", "pichincha.pdf"])
def test_los_estados_sinteticos_si_se_versionan(nombre: str) -> None:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", f"{ESTADOS_SINTETICOS}/{nombre}"],
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 1


def test_ningun_pdf_versionado_fuera_de_los_estados_sinteticos() -> None:
    """Un estado de cuenta real llega en PDF: fuera de la carpeta sintética no se versiona ninguno.

    Los de la carpeta los cubre el test byte a byte de abajo.
    """
    archivos = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.split("\0")
    colados = [
        nombre
        for nombre in filter(None, archivos)
        if nombre.lower().endswith(".pdf") and Path(nombre).parent.as_posix() != ESTADOS_SINTETICOS
    ]
    assert not colados, f"PDF versionados fuera de {ESTADOS_SINTETICOS}/: {colados}"


def test_lo_unico_tabular_versionado_son_los_estados_sinteticos() -> None:
    """Un CSV, un Excel o un .pbix en cualquier otra parte del repo hace fallar esto."""
    archivos = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.split("\0")
    sinteticos = {f"{ESTADOS_SINTETICOS}/{nombre}" for nombre in _sinteticos()}
    sinteticos.add(PARES_VERDADEROS)
    colados = [
        nombre
        for nombre in filter(None, archivos)
        if _es_dato(nombre) and nombre not in sinteticos and not _es_corrida_de_cruce(nombre)
    ]
    assert not colados, f"Archivos de datos versionados que no son sintéticos: {colados}"


def test_cada_estado_de_la_carpeta_es_byte_a_byte_el_que_genera_el_script() -> None:
    """Mira el disco, no solo git: un estado real copiado a la carpeta falla antes del commit.

    Si esto falla por un cambio en el generador: `python scripts/estado_sintetico.py`.
    """
    esperados = _sinteticos()
    en_disco = {ruta.name: ruta.read_bytes() for ruta in (ROOT / ESTADOS_SINTETICOS).iterdir()}
    assert en_disco.keys() == esperados.keys(), "sobran o faltan archivos en la carpeta"
    distintos = [nombre for nombre in esperados if en_disco[nombre] != esperados[nombre]]
    assert not distintos, f"No son lo que genera el script: {distintos}"


def test_los_estados_sinteticos_solo_nombran_bancos_y_comercios_inventados() -> None:
    """Toda descripción lleva la marca de sintética o es un movimiento genérico del banco."""
    modulo = cargar()
    genericas = {
        "TRANSFERENCIA RECIBIDA NOMINA",
        "BUS URBANO",
        "PAGO TARJETA VISA",
        "COMISION RETIRO CAJERO",
        "RETIRO CAJERO",
        "INTERES GANADO",
        "MANTENIMIENTO DE CUENTA",
    }
    for _, descripcion, _ in [*modulo.MARZO, *modulo.ABRIL, *modulo.MAYO]:
        assert "SINTETIC" in descripcion or descripcion in genericas, descripcion
    for _, _, descripcion, _ in [*modulo.PICHINCHA, *modulo._CRUZA_DICIEMBRE]:
        assert "SINTETIC" in descripcion, descripcion
    assert "SINTETICO" in modulo._TITULAR


def test_los_pares_verdaderos_se_versionan_y_son_lo_que_genera_el_script() -> None:
    ignorado = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", PARES_VERDADEROS], cwd=ROOT, check=False
    )
    assert ignorado.returncode == 1
    assert (ROOT / PARES_VERDADEROS).read_bytes() == cargar().pares_verdaderos()
