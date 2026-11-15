# Roadmap

Objetivo: que quien llegue al repo vea en dos minutos un sistema que funciona, está medido y hace algo útil sin que se lo pidan. Tesis: *el LLM propone, el código verifica*.

El repo cuenta una sola historia de producto: **captura verificada → conciliación en SQL → análisis en Power BI**.

> Los plazos son estimaciones, no compromisos. El tiempo es poco (Movmo, universidad, cursos): una tarea a la vez, y cada una termina con una PR y una evidencia (tabla, test o captura).

## Estado al 2026-11-14 (para retomar)

- **En `main` (9189681, 12-nov):** Fases A y B completas; Fase C con la evaluación, flash descartado, `--desde-corrida` y las mejoras 1 y 2; Mini App con coma decimal, subcategoría, bolsillo para `otros`, editar y borrar, e ingresos extra (PRs #13 a #15); alertas de bolsillo D1 (PR #7); renombre a `personal-finance-bot` (PR #19).
- **Verificado el 12-nov sobre `main` más la corrida de los 20 casos:** `pytest` 573 pasan; `ruff check .`, `ruff format --check .` y `mypy` sin errores.
- **Mejora 2 (PR #20, mergeada el 12-nov):** "me devolvieron 20 de una camisa" ya no entra como gasto sin preguntar. Solo código: marcadores `no_es_gasto` en el yaml, con paridad en n8n. El prompt no cambió.
- **Evaluación** (`docs/evaluacion.md`): 104 casos con flash-lite → 0 errores silenciosos en monto y fecha, 2 de 82 en categoría y 2 de 99 en de quién es; dudó cuando debía en 32 de 34.
- **G1, esquema (PR #22, rama `fase-g1-esquema`, rebaseada sobre `main` el 13-nov):** tablas `estados_cuenta`, `movimientos` y `conciliaciones` con sus restricciones, llaves foráneas activas en cada conexión y verificación al arrancar. `pytest` 616 pasan en la rama; `ruff check .`, `ruff format --check .` y `mypy` sin errores. Mergeada el 13-nov (3ad2863).
- **G1, ingesta (PR #23, rama `fase-g1-ingesta`, 14-nov, sin mergear):** `config/bancos.yaml`, `conciliacion/` (`contrato.py`, `bancos.py`, `ingesta.py`, `verificacion.py`, `importar.py`), `--importar-estado` y `scripts/estado_sintetico.py` con 29 casos (8 entran, uno con aviso; 21 se rechazan). Solo CSV. `pytest` 814 pasan en la rama; `ruff check .`, `ruff format --check .` y `mypy` sin errores. Los saldos declarados son obligatorios siempre. Detalle, límites y la tabla de mensajes en `docs/conciliacion.md`.

- **G2, reglas de cruce (rama `fase-g2-reglas`, 14-nov, sin mergear):** `conciliacion/reglas.py` con tres reglas en orden y con nombre, `--conciliar <AAAA-MM>` y `scripts/medir_cruce.py`. Congeladas en `c7694f0` y medidas una vez: 20 pares, 20 correctos (precisión 100 %), recall 20 de 24 (83 %); la regla 3 no hizo pares y su precisión no quedó medida. Detalle y los 4 pares que faltaron en `docs/conciliacion.md`. La rama trae también los pares verdaderos de la PR #24, que se mergeó a `fase-g1-ingesta` y no llegó a `main`.
- **Revisión de las reglas de cruce (rama `fix-reglas-cruce`, 14-nov):** cuatro fallos reproducidos (B5 a B8). Los casos entraron al set (junio y julio; 69 filas) y el criterio se congeló en `99a42c4`, antes de implementar. Ahora el cruce mira todos los movimientos libres y el mes solo filtra lo que se muestra; `monto_y_fecha` y `monto_3_dias` se asignan juntas por menor distancia total; dos cargos iguales de comercios distintos no son gemelos; `monto_y_descripcion` queda apagada por defecto (`regla_descripcion` en `config/bancos.yaml`). Medidas una vez en `e703909`: 27 pares, 27 correctos, recall 27 de 29 (93 %), mismos pares en cualquier orden de meses. Las reglas de la PR #25 sobre el mismo set: 28 pares, 8 equivocados. **El set es de desarrollo; no hay uno sin ver.** Detalle en `docs/conciliacion.md` y `docs/decisiones.md`.
- **Carreras y huecos de la auditoría (rama `fix-carreras-auditoria`, 15-nov):** las escrituras que leen antes de escribir (`--conciliar`, anular, editar, anular estado, reparar) abren con `BEGIN IMMEDIATE`; la auditoría usa LEFT JOIN y revisa monto, débito y ventana de fecha; editar un gasto sin tocar monto ni fecha hereda el par; `RepositorioConciliaciones.anular` y `--anular-par`. Detalle en `docs/conciliacion.md` y `docs/decisiones.md`.

### Qué sigue

- Tarea 4: conciliación de estados de cuenta (Fase G, fases 0 a 2). Las tareas 1 a 3 están cerradas, salvo los enlaces de fuera del repo (tarea 1).
- Dentro de la tarea 4, después de las reglas de cruce: las vistas SQL, la lista "Sin registrar" y `--exportar-bi` (G2). La entrada del banco real en `config/bancos.yaml` espera a G0.

### Qué decide Demian

- **Revisar las etiquetas de oro de los 20 casos nuevos** del banco (`no_gasto` y `parece_no_gasto` en `evals/mensajes.yaml`): las puso Claude.
- **Quien recibe la plata sale como quien asume el gasto.** "Le devolví 30 a mi hermana" queda atribuido a la hermana con confianza alta (2 errores silenciosos de 99, `docs/evaluacion.md`). No viene de la mejora 2. ¿Se ataca antes de la conciliación o después?
- **"Devolución de la camisa 20"** sigue entrando como gasto de ropa con confianza alta. Sumar sustantivos a los marcadores frena también "pagué 5 por la devolución del paquete". ¿Se deja como límite?
- **Mejora 5** ("me pagaron" por prompt): cambia el prompt y pide una corrida nueva de 104 llamadas.
- **Cruce: ¿el pago de la tarjeta cruza con un gasto?** Hoy no (solo débitos), y por eso faltó un par que la verdad cuenta como `exacto`. Si cruza, el pago y los consumos de la tarjeta pueden contarse dos veces.
- **Cruce: ¿hace falta un set sin ver?** Todo el set sintético es de desarrollo desde la revisión del 14-nov. Un set nuevo, escrito sin mirar las reglas, daría una cifra que sí se pueda citar como medición.
- **Cruce: ¿se enciende `monto_y_descripcion`?** Está apagada: 0 pares medidos y 3 falsos positivos. Encenderla pide un set con pares que le toquen y decidir si compara raíces ("librería" contra "libros") y si entran "para" o "quito" a `palabras_genericas`.
- **Cruce: ¿se espera al estado siguiente?** Si abril no está importado, el cargo del 30 de marzo se lleva el gasto del 1 de abril. Se puede dejar como límite (hoy) o no unir los cargos de los últimos 3 días del último estado importado hasta que entre el siguiente.
- **Cruce: ¿las reglas recuerdan un par anulado a mano?** Hoy no: tras `--anular-par`, el siguiente `--conciliar` puede volver a unir el mismo movimiento con el mismo gasto. Recordarlo pide cambiar `reglas.py`.
- **Cruce: `CLAUDE.md` todavía describe las tres reglas "en orden".** Es un principio no negociable y no se tocó: dice "monto exacto y misma fecha, monto exacto a ±3 días, y monto exacto con descripción parecida", y hoy las dos primeras se deciden juntas y la tercera está apagada.
- **G0 (fuentes de la conciliación):** Pichincha exporta solo PDF con texto; Diners, PDF y quizás CSV (lo verifica Demian). Faltan desde qué mes hay estados, el porcentaje en efectivo y dónde corre Power BI.
- **G1b (PDF de Pichincha):** diseño aprobado el 14-nov y fixtures sintéticos hechos. El parser espera a que termine el cambio de la ingesta (anulación en cascada, identidad de cuenta, solape en transacción).
- **Decisión de rumbo del 10-nov:** el repo se renombra, entra la conciliación de estados de cuenta (Fase G) y la Fase F sale de este repo.

### Operación

- Corre con `docker compose up -d`; la Mini App sale por `tailscale funnel --bg 8080` (URL fija, vuelve sola cuando la Mac despierta). Si cambia `APP_URL`: `docker compose up -d bot` (`restart` no relee el `.env`).
- **Aplicado de verdad recién el 2026-11-09.** Hasta ese día `APP_URL` seguía en cloudflared y la app no abría. La puesta en marcha, con una comprobación por paso, está en `docs/mini-app.md#puesta-en-marcha-una-sola-vez-por-mac`. Desde la PR #11, el bot verifica `APP_URL` al arrancar y avisa por Telegram si no carga.
- Respaldo de la base previo a la migración: `data/processed/gastos.respaldo-20261021-133136.db` (local, nunca en el repo).

## Hecho (Fases A y B)

- README de una pantalla, detalle en `docs/`, n8n en `n8n/README.md`; historial sin datos reales (`tests/test_privacy.py`); topics del repo.
- CI con badge (`pytest` + `ruff` + `mypy`, Gemini mockeado), `Dockerfile` + `docker-compose`, `.env.example`, `pre-commit`.
- Rediseño riso de la Mini App y presupuesto solo personal (PR #2).

## Pedidos de uso real (10-nov, fuera de fase)

- [x] Montos con coma decimal en la Mini App.
- [x] Subcategoría libre, con sugerencias de las ya usadas.
- [x] "Otros" pide de qué bolsillo sale (`marcadores_otros`, con paridad en n8n).
- [x] Pestaña Gastos: editar (anular + crear) y borrar con confirmación.
- [x] Ingresos extra: se suman al fijo y se reparten 30/30/20/20; mismo flujo que los gastos.
- [ ] En la Mac: respaldo, `--migrar` y `docker compose up -d --build` (ver `docs/mini-app.md`).

## Tareas, en orden

Una rama y una PR por tarea. Lo marcado **DECIDE DEMIAN** no se hace sin su respuesta. Las reglas de trabajo completas están en `CLAUDE.md`.

### 1. Renombrar el repo a `personal-finance-bot`

- [x] Demian eligió el nombre (11-nov).
- [x] Enlaces al día dentro del repo: título del README, badge de CI, enlace al release, `CLAUDE.md` (Objetivo y Rol) y `description` de `pyproject.toml` (PR #19).
- [x] El paquete `family_expense_bot` y el comando `family-expense-bot` **no** se renombran: cambio grande sin valor visible. Decisión en `docs/decisiones.md` (PR #19).
- [x] Demian renombra en GitHub (Settings → General) y después se mergea la PR #19.
- [ ] Demian cambia los enlaces de fuera del repo: CV, LinkedIn, README del perfil de GitHub y brief del portafolio.

### 2. Mergear D1 (alertas al 80 % y 100 %)

- [x] Rebase sobre `main`.
- [x] Test: la alerta y la Mini App usan el mismo presupuesto con ingresos extra.
- [x] Test: un reinicio no reenvía una alerta ya enviada.
- [x] Editar un gasto no repite el aviso (el "antes" incluye el gasto reemplazado).
- [x] Subir la rama rebaseada (`git push --force-with-lease`, lo corre Demian), PR #7 verde y mergeada.

### 3. Mejora 2 de la Fase C: devoluciones e ingresos no son gasto

- [x] Decidir dónde se resuelve: solo código, con marcadores de "no es gasto" en el yaml (decidido por Demian el 12-nov). El prompt no se toca.
- [x] Medir antes y después contra el banco de 84 casos: 24 → 25 de 26, 0 llamadas.
- [x] Fila en la tabla "antes → después" de `docs/evaluacion.md`, con los falsos positivos medidos aparte.

### 4. Conciliación de estados de cuenta (Fase G, fases 0 a 2)

Es la **prioridad del repo** después de las tareas 1 a 3. Ver [Fase G](#fase-g-conciliación-de-estados-de-cuenta).

### 5. Después de la conciliación (si hay tiempo, en este orden)

- [ ] D2: resumen semanal automático (JobQueue, sin LLM, idempotente, tests con reloj falso). Puede incluir "movimientos del banco sin registrar".
- [ ] `/estado`: latencia p50/p90 y tasa de error de 7 días, desde la tabla `extracciones`.
- [ ] README: sección corta "Qué hace sin que se lo pidas".

## Fase G. Conciliación de estados de cuenta

El bot sabe lo que Demian le escribe; el banco y la tarjeta saben lo que de verdad salió. La conciliación cruza las dos fuentes y responde: qué gasté y no registré, qué pago cada mes sin pensarlo y cuánto me cuesta el banco. Métrica nueva: **cobertura**, el porcentaje del gasto real que capturó el bot.

Principios, además de los de `CLAUDE.md`:

- **El estado de cuenta cuadra o no entra.** Saldo anterior + créditos − débitos = saldo final, al centavo y en enteros. Si no cuadra, se rechaza el estado completo.
- **La conciliación la deciden reglas en SQL, no el LLM.** En orden y con nombre; cada par guarda qué regla lo unió.
- **Nada se guarda como gasto sin confirmación**, y nada se borra: anulación con fecha y motivo.
- **Los estados de cuenta reales nunca entran al repo ni van a un LLM** sin que Demian lo decida. Tests, capturas y el `.pbix` público usan datos sintéticos.

Limitación conocida (decidida el 12-nov): la conciliación es **uno a uno y solo contra gastos**. Un movimiento se une a un solo gasto y un gasto a un solo movimiento, así que no entran los pagos partidos (un cargo del banco que son varios gastos, o un gasto pagado en dos cargos) ni los créditos del banco contra la tabla `ingresos`. Esos movimientos quedan como "sin registrar".

### G0. Fuentes (DECIDE DEMIAN)

- [x] Banco y tarjeta; si exportan CSV o Excel; si el PDF trae texto. Pichincha: solo PDF con texto. Diners: PDF, el CSV del portal sin confirmar.
- [ ] Desde qué mes hay estados de cuenta y cuánto se paga en efectivo.
- [ ] Dónde corre Power BI (Desktop no corre en Mac).
- [x] Respuestas en `docs/conciliacion.md` (las que hay; lo demás figura como pendiente).

### G1. Ingesta y modelo

- [x] Esquema con migración (rama `fase-g1-esquema`, tests en `tests/test_esquema_conciliacion.py`):
  - `estados_cuenta`: `hash_archivo` único entre los vigentes (índice parcial), así un estado anulado se puede reimportar; saldos en centavos enteros con signo.
  - `movimientos`: centavos enteros con signo y distintos de cero, `tipo` (`debito`, `credito`, `comision`, `interes`, `pago`), línea única por estado y anulación.
  - `conciliaciones`: qué regla unió cada par; un movimiento y un gasto solo pueden estar en un par vigente.
  - `--migrar` crea las tres tablas sin tocar las demás; el arranque falla si les falta una columna o un índice único. `PRAGMA foreign_keys=ON` en cada conexión.
- [x] Reglas por banco en `config/bancos.yaml` que asignan `movimientos.tipo` al importar, sin LLM.
- [x] `conciliacion/ingesta.py` (CSV → contrato Pydantic) y `verificacion.py` (invariante de saldo, saldo corrido, fechas en el periodo, duplicados, continuidad como aviso).
- [x] `config/bancos.yaml`: un banco nuevo es una entrada en el yaml, no código. Hoy solo trae dos bancos inventados.
- [x] CLI: `--importar-estado`.
- [x] `scripts/estado_sintetico.py`: estados sintéticos que cuadran y variantes que no, y una base sintética del bot (`--base`).
- [x] `.gitignore` con una sola excepción (`tests/fixtures/estados/*.csv`), y `tests/test_privacy.py` ampliado a CSV, xlsx y pbix.
- [x] **Cierre:** importar un estado sintético funciona, uno que no cuadra se rechaza con un mensaje exacto y el mismo archivo dos veces no duplica nada (`docs/conciliacion.md`).
- [ ] Excel (`openpyxl`): solo si G0 dice que el banco lo exporta.
- [ ] Entrada del banco real en `config/bancos.yaml` (depende de G0).
- [x] `conciliacion/reglas.py` y CLI `--conciliar <AAAA-MM>` (rama `fase-g2-reglas`).
- [ ] `conciliacion/recurrentes.py`.
- [ ] CLI: `--exportar-bi`.

### G1b. PDF (G0: Pichincha solo exporta PDF)

- [x] PDF sintéticos con la forma del real, reproducibles byte a byte (`scripts/estado_sintetico.py`, `tests/test_pdf_sintetico.py`); el contraste con el real fue una corrida local única (`docs/conciliacion.md`).
- [ ] Parser con pypdf, después del cambio de la ingesta.
- [ ] Texto con una librería de PDF y reglas; el LLM solo si las reglas no alcanzan y con la misma verificación (cada monto aparece literal en la página y el saldo cuadra).

### G2. Conciliación, SQL y Power BI

- [ ] Vistas en `sql/vistas.sql`: `v_cobertura_mensual`, `v_no_registrados`, `v_recurrentes`, `v_costo_banco` y `v_gasto_real_bolsillo`.
- [ ] Tests de SQL: conteos, totales contra el estado de cuenta y ningún movimiento conciliado dos veces.
- [ ] Mini App: lista "Sin registrar" con Registrar o Ignorar, por el flujo de confirmación de siempre.
- [ ] `--exportar` (gastos) y `--exportar-bi` neutralizan las celdas de texto que empiezan con `=`, `+`, `-` o `@`, en una PR aparte de la ingesta (decisión en `docs/conciliacion.md`).
- [ ] `--exportar-bi` escribe las vistas a CSV; dashboard de 3 páginas (Cobertura, Gasto real, Costo del banco y suscripciones) con datos sintéticos.
- [x] Medición: precisión y recall por regla y por `categoria_caso` sobre `evals/pares_verdaderos.csv` (`python scripts/medir_cruce.py`, una sola corrida con las reglas congeladas). Tabla en `docs/conciliacion.md`.
- [ ] **Cierre:** vistas con tests, "Sin registrar" funcionando, dashboard en PNG en `docs/img/` (no PDF: decidido el 14-nov), tabla de precisión y recall en `docs/conciliacion.md` y una línea en el README.

## Fase C. Evaluación (cerrada salvo la mejora 2)

- [x] Banco de 84 casos y `--evaluar` con errores silenciosos separados.
- [x] flash-lite medido; flash descartado por cuota y latencia.
- [x] Mejora 1: `otros` nunca con confianza alta. Medida antes y después.
- [x] Mejora 2: devoluciones no son gasto (tarea 3). Los ingresos por "me pagaron" quedaron fuera: chocan con "mi papá me pagó el semestre".
- Mejoras 3 (separar `personal` de `educacion`) y 4 (timeout más corto con un reintento): si hay tiempo.
- [x] Los 20 casos nuevos del banco (`no_gasto` y `parece_no_gasto`), corridos una vez contra Gemini el 12-nov. La regla no se ajustó con el resultado; los límites están en `docs/evaluacion.md`.
- Mejora 5, para después: ingresos por "me pagaron" resueltos en el prompt. Por marcadores chocan con la atribución (`docs/evaluacion.md`). Cambia el prompt, así que pide una corrida nueva contra Gemini y el OK de Demian.

Regla: cada mejora es una PR chica; si el número empeora, se descarta y se documenta.

## Fase D. Automatización que actúa sola

- [x] D1. Alertas al 80 % y 100 % (tarea 2).
- [ ] D2. Resumen semanal (tarea 5).
- D3. Gastos recurrentes: si hay tiempo. La conciliación detecta suscripciones (`v_recurrentes`) y puede reemplazarla; se evalúa al llegar a G2.
- Requisitos: tests con reloj falso, idempotencia y envío solo a IDs de la allowlist.

## Fase E. Operación mínima

- [x] Hosting estable: Tailscale Funnel (2026-10-26, ver `docs/decisiones.md`).
- [x] Demo corta (GIF de la Mini App en el README).
- [ ] `/estado` (tarea 5).

## Fase F. Consultas con herramientas: fuera de este repo

Decidido el 10-nov: tool use, RAG y agentes van al proyecto del asistente académico, para que cada repo muestre algo distinto. Aquí no se hace.

## Fuera de alcance

- Fase F (ver arriba).
- Conexión directa con el banco (APIs, open banking) y scraping de la banca web.
- Categorización de movimientos con LLM: primero reglas; el LLM solo si la medición muestra que hace falta y con OK de Demian.
- OCR de fotos, fine-tuning, importar Excel de gastos, multi-moneda, multiusuario, WhatsApp.
- Más trabajo en la versión n8n (queda congelada como segunda implementación).
- Hosting en la nube: Funnel basta para una persona.

## Criterio de "terminado"

- [x] Corre con un comando (Docker).
- [x] CI verde con badge.
- [x] Tabla de evaluación con números reales.
- [x] Historial sin datos personales.
- [ ] Repo renombrado y enlaces al día.
- [x] Tabla "antes → después" de las mejoras de la Fase C, con la mejora 2.
- [x] Al menos una automatización que actúa sin que se pida (D1).
- [ ] Conciliación con invariante de saldo probado y precisión/recall medidos.
- [ ] Dashboard Power BI con datos sintéticos en el repo (PNG en `docs/img/`).
- [ ] `/estado` con latencia y errores.
- [ ] README de una pantalla y limitaciones honestas, al día con lo anterior.
