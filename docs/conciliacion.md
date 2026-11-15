# Conciliación de estados de cuenta

[← README](../README.md) · [Decisiones](decisiones.md) · [Presupuesto](presupuesto.md) · [Calidad](calidad.md) · [Evaluación](evaluacion.md) · [Mini App](mini-app.md) · [n8n](../n8n/README.md)

El bot sabe lo que le escribo; el banco sabe lo que de verdad salió. Esta parte cruza las dos fuentes. Hoy están hechas la **ingesta** (un estado de cuenta en CSV entra a la base completo y verificado, o no entra) y las **reglas de cruce** contra los gastos. Las vistas SQL, la lista "Sin registrar" y Power BI son lo siguiente en [`ROADMAP.md`](../ROADMAP.md).

Todo lo de esta página se hizo con datos inventados. Ningún estado real entra al repo ni va a un LLM.

## G0. Fuentes

| Fuente | Qué exporta | Estado |
|---|---|---|
| Pichincha (cuenta) | PDF con texto seleccionable (3 páginas y unos 3.200 caracteres en el estado de ejemplo) | Confirmado |
| Diners (tarjeta) | PDF; puede ofrecer CSV en su portal, lo verifica Demian | Sin confirmar |

- Desde qué mes hay estados de cuenta: pendiente de Demian.
- Porcentaje que se paga en efectivo: pendiente de Demian.
- Dónde corre Power BI: pendiente de Demian.

Pichincha no exporta CSV ni Excel, así que entra la subfase del PDF (G1b en [`ROADMAP.md`](../ROADMAP.md)). Hasta que esté, `config/bancos.yaml` solo trae dos bancos inventados. Excel no se lee todavía: pide una dependencia nueva (`openpyxl`) y no se agrega hasta saber si hace falta.

## PDF de Pichincha (G1b): fixtures, todavía sin parser

`scripts/estado_sintetico.py` genera 11 PDF sintéticos en `tests/fixtures/estados/`, con la forma del export de Pichincha. **Todavía no hay parser que los lea**: espera a que cambie la ingesta (anulación en cascada, identidad de cuenta, solape en transacción) para no chocar con ese trabajo.

**Qué prueban y qué no.** Cuando exista el parser, estos fixtures lo prueban **contra nuestro generador, no contra el banco**. Que el generador se parezca al export real se comprobó **una sola vez, en local**, comparando la forma de un estado real con la del sintético (sin copiar su contenido; abajo están las diferencias). Ese contraste no está en los tests ni en el repo. Si el banco cambia el formato, los tests siguen pasando y solo una corrida nueva contra un estado real lo nota.

| Archivo | Qué trae | Debería |
|---|---|---|
| `pichincha` | Resumen, una página de detalle y la última vacía, como el real | Entrar |
| `pichincha_doc_partido` | Tres N.DOC de nueve dígitos con el último en la línea de abajo, como el real | Entrar |
| `pichincha_columna_desplazada` | El débito de una fila corrido 65 pt, dentro de la columna de crédito | Rechazarse |
| `pichincha_fila_entre_paginas` | Fecha, oficina y N.DOC al pie de una página; descripción y montos arriba de la siguiente | Rechazarse |
| `pichincha_separador_de_miles` | Un crédito "1,200.00"; el resto cuadra | Rechazarse |
| `pichincha_pagina_repetida` | La página de detalle dos veces con el mismo pie ("Página 2 de 3"): cuatro páginas | Rechazarse |
| `pichincha_resumen_no_cuadra` | El resumen cuenta un débito más de los que trae el detalle | Rechazarse |
| `pichincha_corte_contado` | Movimientos en las dos fechas de corte, contados en el resumen | Entrar |
| `pichincha_corte_no_contado` | Los mismos movimientos de corte, pero el resumen no los cuenta | Rechazarse |
| `pichincha_cruza_diciembre` | Periodo de diciembre a enero; cada fecha cae en un solo año | Entrar |
| `pichincha_mes_fuera_del_periodo` | El mismo periodo con una fecha "15-jun.", que no cae en ninguno de los dos años | Rechazarse |

La columna "Debería" está **anotada a mano, no medida**: sale de `CASOS_PDF` y la medirá el parser.

**Decisiones de Demian para el parser** (14-nov):

- **Día de corte:** no hay regla fija. Entra el estado cuyo detalle cuadra con los conteos y totales del resumen; si no cuadra, se rechaza (`corte_contado` y `corte_no_contado`).
- **Año:** la fecha trae día y mes; el año sale del periodo. Si una fecha no cae en exactamente un día del periodo, se rechaza y no se deduce.
- **Número de cuenta:** se compara entre páginas, pero no se guarda ni se hashea (diez dígitos se sacan por fuerza bruta). La etiqueta de cuenta sale de un mapa local, fuera del repo.
- **Dashboard:** se publica como PNG en `docs/img/`. La guarda de PDF no tiene excepciones fuera de esta carpeta.

**Cómo se escriben.** A mano, sin dependencias: objetos PDF, content streams sin comprimir y la tabla xref con los offsets reales. No llevan `/Info` ni `/ID`, así que dos corridas dan los mismos bytes (`tests/test_pdf_sintetico.py`, con bytes y sin librería de PDF). El titular, la cédula, la cuenta y los comercios son inventados y llevan la marca SINTETICO.

**Igual que el real** (comparado con pypdf sobre la forma: operadores, orden y troceo, nunca el texto):

- PDF 1.4. Cada página es una envoltura que dibuja la página como Form XObject y escribe "Página N de M" arriba a la derecha, en Helvetica de 10 pt.
- Adentro, ArialMT TrueType sin embeber, en WinAnsi. Un `BT` por celda con `Tm Tf rg Tj g`, y `Tr 2` antes y `Tr 0` después en las negritas.
- Solo `Tj`, nunca `TJ` ni kerning: el texto de una celda, con sus espacios, sale en un solo fragmento.
- N.DOC de nueve dígitos: el último dígito va en el mismo `BT`, con su propio `Tm` 9 pt más abajo, y la fila ocupa 22 pt en vez de 13. En texto plano, esa fila sale en tres líneas, igual que en el real.
- Orden: filas de arriba abajo y, dentro de cada una, de izquierda a derecha; el título "ESTADO DE CUENTA" va al final de cada página.
- Bajo la tabla, una celda vacía de 5 pt al borde de cada columna, y en cada página los dos bloques de `Td` sin texto que trae el real.
- Posición de cada columna, alineación (fecha y saldo centrados, débito y crédito a la derecha) y rótulos del resumen y del encabezado.

**Distinto del real:**

- **Sin compresión.** El real usa FlateDecode. La salida de zlib puede cambiar entre versiones de la librería y rompería el byte a byte.
- **Sin `/Info` ni `/ID`.** El real los trae, con fechas.
- **Sin imágenes** (los logos del banco) y sin el bloque de contacto de la página 1, que en el real mezcla varios `Tj` en un `BT`. La página 1 tiene menos fragmentos.
- **Menos movimientos** (17 en el base, contra unos 30 en el real) y rayas de la tabla más simples (el real dibuja curvas y rellenos de color).
- **Posiciones redondeadas** a dos decimales; los montos se alinean con las métricas de Helvetica, que son las de Arial.
- **Supuestos sin ver en el real:** las abreviaturas de mes que no son `oct.` y `nov.`, cómo escribe el banco un monto de 1.000 o más (el real no traía ninguno) y si el día de corte entra al periodo (el real no tenía movimientos ese día).

## Importar un estado

```bash
family-expense-bot --importar-estado marzo.csv --banco banco_sintetico --cuenta ahorros --desde 2026-03-01 --hasta 2026-03-31 --saldo-anterior 1000.00 --saldo-final 1714.73
```

- `--banco` es la clave del banco en `config/bancos.yaml`.
- `--cuenta` es una etiqueta ("ahorros", "visa"). Se rechaza cualquier cosa con cuatro dígitos seguidos: el número de cuenta nunca se guarda.
- `--saldo-anterior` y `--saldo-final` van como los muestra el banco, con punto decimal. Son **obligatorios siempre**, aunque el archivo traiga columna de saldo (ver [decisiones](#decisiones)).

Si entra, dice cuántos movimientos guardó y de qué tipo. Si no, lista **todos** los problemas, termina con código 1 y no guarda nada.

Para probar sin tocar la base real:

```bash
python scripts/estado_sintetico.py --base /tmp/sintetica.db
```

Crea una base nueva con 41 gastos inventados y los estados de marzo a julio ya importados (61 movimientos). Se niega a escribir sobre un archivo que ya existe.

## Agregar un banco

Es una entrada en [`config/bancos.yaml`](../config/bancos.yaml), no código: nombre de cada columna, formato de fecha, separador decimal, delimitador, líneas de título antes del encabezado, convención de signo y las reglas de `tipo`. El archivo explica cada campo. Un yaml mal escrito no carga (campos desconocidos, monto en dos lugares, formato de fecha sin año de cuatro dígitos).

`movimientos.tipo` lo asignan esas reglas, en orden y sin LLM: gana la primera cuya frase aparece en la descripción como palabras completas, sin distinguir tildes ni mayúsculas ("comisionista" no es una comisión). Una regla puede exigir signo: la devolución de una comisión es plata que entra, no costo del banco. Si ninguna coincide, lo que sale es `debito` y lo que entra, `credito`.

## Qué se verifica

**Al leer** (`conciliacion/ingesta.py`). Nada se adivina; una sola fila mala rechaza el archivo:

- El archivo no pasa del tope (`tope_bytes`, 1 MiB). Se mira el tamaño antes de abrirlo.
- Está en la codificación del banco. UTF-8 entra con o sin BOM.
- El encabezado trae todas las columnas del yaml, sin repetir. Las columnas de más se ignoran.
- Cada fila tiene las mismas columnas que el encabezado, con fecha, descripción, monto y saldo (si el banco lo trae).
- La fecha existe y está en el formato del banco.
- El monto son dígitos y hasta dos decimales con el separador del banco. Sin separador de miles, sin símbolo de moneda, sin paréntesis, y distinto de cero.
- La descripción cabe en 500 caracteres. Si no, se rechaza: cortarla sería adivinar.

**Después de leer** (`conciliacion/verificacion.py`), todo en centavos enteros:

1. **Archivo repetido:** el mismo SHA-256 que un estado vigente.
2. **Periodo solapado:** se cruza con otro estado vigente del mismo banco y cuenta. Atrapa el mismo mes exportado de nuevo, que trae otros bytes y otro hash.
3. **Fechas:** cada movimiento cae dentro del periodo.
4. **Saldo corrido** (solo con columna de saldo): cada fila encadena con la anterior, y la última da el saldo final.
5. **Invariante:** saldo anterior + créditos − débitos = saldo final.
6. **Bloque duplicado:** dos o más filas seguidas que se repiten justo después. Si el estado no cuadra, el mensaje dice qué líneas son y si suman la diferencia.

**Avisos** (no rechazan; se muestran al importar y no se guardan):

- **Continuidad:** si hay un estado vigente de la misma cuenta que termina el día anterior (o empieza el día siguiente) y su saldo no empata con el de este.
- **Bloque repetido en un estado que cuadra:** el saldo confirma que los movimientos son reales, pero conviene mirarlo.

**Al guardar**, el estado y sus movimientos van en una sola transacción. El índice único del hash es la última defensa si dos importaciones corren a la vez.

## Cruce con los gastos

```bash
family-expense-bot --conciliar 2026-03
```

Cruza los movimientos con los gastos registrados y guarda cada par en `conciliaciones` con la regla que lo unió. Dice cuántos pares hizo cada regla, cuántos débitos quedaron sin conciliar y cuántos de ellos por empate. Correrlo dos veces no duplica nada, y un par anulado puede volver a cruzarse. Un movimiento sin par **no** se guarda como gasto: proponerlo es otra fase.

**El mes solo filtra lo que se muestra.** El cruce mira siempre todos los movimientos libres de la base, y guarda también los pares de otros meses (dice cuántos fueron). Así el resultado no depende de qué mes se concilia primero: el cargo del 30 de marzo compite por el gasto del 1 de abril con el cargo del 1 de abril, que es el suyo.

Antes de cruzar, `--conciliar` audita las conciliaciones que ya hay. Si alguna está rota, no cruza nada y dice cuáles son (ver [Integridad](#integridad-de-las-conciliaciones)).

Las reglas son deterministas y sin LLM:

| Regla | Qué pide | Estado |
|---|---|---|
| `monto_y_fecha` | Monto exacto y la misma fecha | Activa |
| `monto_3_dias` | Monto exacto y de 1 a 3 días de diferencia | Activa |
| `monto_y_descripcion` | Monto exacto, hasta 10 días y descripción parecida | **Apagada por defecto** |

- **Solo cruzan los débitos.** Comisiones, intereses, pagos de tarjeta y todo lo que entra (una devolución) nunca se unen a un gasto.
- **Las dos primeras se deciden juntas.** Los cargos y gastos del mismo monto a tres días o menos forman un grupo, y en cada grupo se elige la asignación uno a uno que une más pares y, entre esas, la que suma menos días. El nombre de la regla sale después, de la distancia de cada par. Decidir primero "misma fecha" era un fallo: con el mismo almuerzo todos los días, el cargo de hoy se llevaba el gasto de hoy aunque fuera el de ayer.
- **Desempates, en orden:** más palabras distintivas en común entre la descripción del banco y el texto del gasto, y después el orden de las fechas (el primer gasto con el primer cargo). Una palabra es distintiva si no está en `palabras_genericas` de [`config/bancos.yaml`](../config/bancos.yaml). Las palabras se comparan sin tildes ni mayúsculas, sin números y con cuatro letras o más. Del gasto cuentan la descripción, la subcategoría y el texto original.
- **Si la mejor asignación no es única, no se une nadie del grupo.** Un par mal unido esconde un gasto sin registrar; uno sin unir solo queda pendiente.
- **Gemelos:** dos cargos del mismo monto son intercambiables solo si tienen la misma fecha y la misma descripción. Dos cargos iguales de comercios distintos no lo son: si el texto del gasto no nombra a uno, no se une ninguno.
- **La regla por descripción se enciende con `regla_descripcion: true`** en `config/bancos.yaml`, y corre después sobre lo que quedó libre. Está apagada porque no tiene ningún par correcto medido (0 pares en la medición de la PR #25) y unió tres pares falsos por palabras vacías: "quito", "para" y "compra online". No conviene encenderla sin una medición nueva.

El algoritmo y sus motivos están en [decisiones](decisiones.md#reglas-de-cruce-revisión-del-14-nov-2026).

### Medición

```bash
python scripts/medir_cruce.py
```

Arma la base sintética, concilia mes por mes de marzo a julio (como se usa `--conciliar`), compara con `evals/pares_verdaderos.csv` (69 filas que salen del generador sintético, no de las reglas) y repite todo con los meses al revés. La salida completa está en [`evals/corridas/cruce-e703909.txt`](../evals/corridas/cruce-e703909.txt).

**Este set es de desarrollo.** Las reglas se corrigieron después de ver la medición de la PR #25 y los cuatro fallos de la revisión, que ahora son parte del set. Las cifras de abajo dicen que las reglas hacen lo que se les pidió en los casos conocidos; no son una medición sobre datos sin ver, y hoy no hay un set aparte para eso.

Un par es correcto solo si el movimiento tenía exactamente ese gasto. El recall es sobre los 29 pares uno a uno que había que encontrar; los pagos partidos y la abstención esperada se cuentan aparte.

| Regla | Pares hechos | Correctos | Precisión | Recall (aporte) |
|---|---|---|---|---|
| `monto_y_fecha` | 14 | 14 | 100% | 14 de 29 (48%) |
| `monto_3_dias` | 13 | 13 | 100% | 13 de 29 (45%) |
| `monto_y_descripcion` | 0 | 0 | — | 0 de 29 (0%) |
| Total | 27 | 27 | 100% | 27 de 29 (93%) |

| categoria_caso | Filas | Pares a encontrar | Unidos | Correctos | Precisión | Recall |
|---|---|---|---|---|---|---|
| `exacto` | 12 | 12 | 11 | 11 | 100% | 92% |
| `movimiento_sin_gasto` | 11 | 0 | 0 | 0 | — | — |
| `fecha_desfasada` | 6 | 6 | 6 | 6 | 100% | 100% |
| `mismo_monto_misma_semana` | 6 | 5 | 5 | 5 | 100% | 100% |
| `comision` | 7 | 0 | 0 | 0 | — | — |
| `fecha_fuera_de_ventana` | 1 | 1 | 0 | 0 | — | 0% |
| `interes` | 2 | 0 | 0 | 0 | — | — |
| `devolucion` | 2 | 0 | 0 | 0 | — | — |
| `pago_partido` | 3 | 0 | 0 | 0 | — | — |
| `diario_desfasado` | 4 | 4 | 4 | 4 | 100% | 100% |
| `gemelos_otro_comercio` | 2 | 0 | 0 | 0 | — | — |
| `palabras_vacias` | 3 | 0 | 0 | 0 | — | — |
| `cruza_de_mes` | 2 | 1 | 1 | 1 | 100% | 100% |
| `gasto_sin_movimiento` | 8 | 0 | 0 | 0 | — | — |

Quedaron 19 débitos sin conciliar, 2 de ellos por empate (los dos cargos de `gemelos_otro_comercio`, que es la abstención esperada). Ningún par salió equivocado, y con los meses al revés salieron los mismos 27 pares.

**Pagos partidos (fallo esperado, 3 filas):** los tres quedaron sin unir, que es el límite declarado del uno a uno. Son un gasto de 100.00 pagado en dos cargos (60.00 y 40.00) y un cargo de 100.00 que eran dos gastos.

**Los 2 pares que faltaron:**

| Movimiento | Gasto | Por qué no se unió |
|---|---|---|
| Marzo, línea 9: `PAGO TARJETA VISA`, 45.00 | "cuota de la tarjeta", mismo día | El movimiento es de tipo `pago` y solo cruzan los débitos. La verdad lo cuenta como `exacto`. |
| Marzo, línea 16: `LIBRERIA SINTETICA`, 40.00 | "libros", 4 días antes | Fuera de ±3 días, y la regla por descripción está apagada. Encendida tampoco lo uniría: "libreria" y "libros" no son la misma palabra. |

Los dos almuerzos de mayo en días alternados, que en la medición anterior quedaron en empate, ahora se unen: la asignación de menor distancia total es única.

### Revisión de las reglas: criterio congelado

Una revisión adversarial reprodujo cuatro fallos en las reglas de la PR #25. Antes de cambiarlas se agregaron los cuatro casos al set sintético (los estados de junio y julio, 16 filas nuevas en `evals/pares_verdaderos.csv`) y se escribió este criterio, en el commit `99a42c4`, que no toca `conciliacion/reglas.py`.

| Fallo | Caso en el set | Qué hacían las reglas de la PR #25 | Qué se espera |
|---|---|---|---|
| B8 | `cruza_de_mes`: cargo del 30 de junio sin gasto, cargo igual del 1 de julio con su gasto | Si junio se conciliaba primero, el cargo de junio se llevaba el gasto de julio | El gasto se une al cargo del 1 de julio, en cualquier orden de meses |
| B6 | `diario_desfasado`: el mismo almuerzo cuatro días seguidos, cobrado cada uno al día siguiente | Unía tres por la misma fecha, todos corridos, y dejaba un cargo suelto | Los cuatro pares, cada cargo con el gasto de la víspera |
| B5 | `gemelos_otro_comercio`: dos cargos iguales el mismo día, de una farmacia y una ferretería, y un gasto que dice "arreglo de la casa" | Unía el gasto al primero del archivo (la farmacia) | Abstención: ninguno de los dos se une |
| B7 | `palabras_vacias`: tres cargos y tres gastos en efectivo del mismo monto que solo comparten "quito", "para" o "compra online" | La regla 3 unía los tres | Ningún par |

El criterio, sobre el set completo y conciliando mes por mes, y lo que salió en la única corrida de las reglas nuevas (commit `e703909`):

| # | Criterio | Resultado |
|---|---|---|
| 1 | Ningún par equivocado | Cumple: 0 de 27 |
| 2 | Los 5 pares nuevos se encuentran (4 de `diario_desfasado` y 1 de `cruza_de_mes`) | Cumple: 5 de 5 |
| 3 | Abstención en `gemelos_otro_comercio`: ningún cargo se une y los dos salen como empate | Cumple |
| 4 | Ningún par en `palabras_vacias` | Cumple: 0 |
| 5 | Los 20 pares de la medición anterior siguen, con el mismo gasto | Cumple: 20 de 20 |
| 6 | El orden de los meses no cambia nada | Cumple: 0 pares distintos |

| | Reglas de la PR #25 | Reglas nuevas |
|---|---|---|
| Pares hechos | 28 | 27 |
| Correctos | 20 | 27 |
| Equivocados | 8 | 0 |
| Precisión | 71% | 100% |
| Recall | 20 de 29 (69%) | 27 de 29 (93%) |
| Pares distintos con los meses al revés | 2 | 0 |

La columna de la PR #25 es la corrida de las reglas viejas sobre el set nuevo, hecha antes de implementar: [`evals/corridas/cruce-99a42c4-antes.txt`](../evals/corridas/cruce-99a42c4-antes.txt). La medición original, sobre el set de 53 filas, sigue en [`evals/corridas/cruce-c7694f0.txt`](../evals/corridas/cruce-c7694f0.txt): 20 pares, 20 correctos, recall 20 de 24.

## Integridad de las conciliaciones

Un par vigente solo une un gasto vigente con un movimiento vigente de un estado vigente, uno a uno. Lo sostienen tres cosas:

- **Anular arrastra el par, en la misma transacción.** Borrar un gasto conciliado anula su par y el cargo vuelve a "sin conciliar". Editarlo (anular + crear) también: el gasto nuevo queda libre y el siguiente `--conciliar` lo cruza de nuevo. El par no se hereda porque la edición pudo cambiar el monto o la fecha.
- **Anular un estado anula sus movimientos y sus pares** (`RepositorioEstados.anular`). Después, el mismo archivo se puede importar otra vez y cruza igual que antes. Todavía no hay comando para anular un estado: es un método del repositorio.
- **`--conciliar` audita antes de cruzar.** Si encuentra un par vigente sobre un gasto anulado, sobre un movimiento o un estado anulado, o un movimiento o un gasto en más de un par, termina con error, lista los pares y no guarda nada.

Una base anterior a este arreglo, o tocada a mano, puede traer pares rotos. Se reparan así:

```bash
family-expense-bot --reparar-integridad
```

```bash
family-expense-bot --reparar-integridad --confirmar
```

El primero es una simulación: lista los pares que anularía y no cambia nada. Con `--confirmar` los anula en una transacción, con el motivo `reparado: <problema>`. Nada se borra. Si un movimiento o un gasto quedó en varios pares se anulan todos, y el siguiente `--conciliar` vuelve a cruzar lo que quedó libre.

## Decisiones

- **Línea totalmente vacía se salta; fila a medio llenar se rechaza.** La numeración de líneas se conserva, así que "línea 8" es la línea 8 del archivo en un editor. Una fila con solo separadores (`,,,`) cuenta como vacía: así exporta Excel una fila en blanco.
- **Las descripciones se guardan literales, aunque empiecen con `=`, `+`, `-` o `@`.** La base guarda lo que dice el banco: cambiar el texto al importar rompería la comparación de descripciones del cruce y lo que se guarda dejaría de ser lo que trae el archivo. El riesgo (que Excel o Power BI lo ejecuten como fórmula) existe solo cuando el texto sale a un CSV, así que se neutraliza **al exportar**, tanto en el `--exportar` de gastos que ya existe como en `--exportar-bi`. Va en una PR aparte (decidido el 2026-11-14, anotado en el roadmap); hasta que entre, `--exportar` no neutraliza.
- **Dos cargos idénticos el mismo día son dos movimientos.** Una sola fila repetida nunca se toma como duplicado; un bloque de dos o más, sí se señala.
- **La continuidad avisa, no rechaza.** Un estado anterior mal importado no debe bloquear al siguiente.
- **Los saldos declarados son obligatorios siempre** (decidido el 2026-11-14). Los escribe quien importa, mirando el estado; nunca se deducen del archivo. Si el saldo anterior saliera de la primera fila, esa fila no se verificaría contra nada y un archivo cortado al principio o al final cuadraría. Con los dos declarados, la primera fila tiene que encadenar con el saldo anterior y la última tiene que dar el saldo final (caso `primera_fila_corrida`).
- **Los avisos se muestran y no se guardan** (decidido el 2026-11-14). Guardarlos pediría una columna nueva en el esquema.
- **Los saldos declarados van como los muestra el banco.** En una tarjeta (`signo: positivo_sale`) lo que se debe va en positivo; adentro todo queda con signo negativo si sale plata.

## Límites conocidos

- **El cruce es uno a uno.** Un cargo que son varios gastos, o un gasto pagado en dos cargos, queda sin conciliar.
- **El pago de la tarjeta no cruza**, aunque esté registrado como gasto con el mismo monto y fecha.
- **La descripción se compara por palabras enteras.** "Librería" y "libros" no coinciden, ni un comercio que el banco abrevia.
- **Un gasto a más de tres días de su cargo no se une**, porque la regla por descripción está apagada.
- **El cruce no sabe qué estados faltan por importar.** Mira todos los movimientos de la base, pero si abril todavía no se importó, el cargo del 30 de marzo se lleva el gasto del 1 de abril igual. Conviene conciliar un mes cuando ya está importado el siguiente; un par mal hecho se anula y vuelve a cruzarse.
- **Un empate deja sin unir a todo su grupo.** Si el mismo monto se repite muchos días seguidos y en un punto hay dos asignaciones igual de buenas, no se une ningún cargo de esa racha, tampoco los que a ojo están claros.
- **Las palabras vacías todavía pueden desempatar.** "Para" o "quito" no están en `palabras_genericas`, así que cuentan como distintivas si deciden entre dos candidatos del mismo monto a tres días o menos. No alcanzan para crear un par, solo para elegir entre dos.
- **La regla por descripción no tiene precisión medida** y por eso está apagada.
- **No hay un set sin ver.** Todo el set sintético es de desarrollo.
- **Sin columna de saldo, solo se verifica el total.** Una fila perdida se nota porque el total no cuadra, pero no se sabe cuál fue. Dos errores que se compensan (+1 y −1 centavo) pasan sin que nada los delate: es el caso `errores_compensados_sin_saldo` de la tabla. Con columna de saldo, los dos se detectan en la línea exacta.
- **Los avisos no quedan en la base.** Salen una vez, al importar. Después solo se pueden reconstruir comparando los saldos de dos estados seguidos.
- **Una fila repetida suelta no se detecta por sí sola.** Si no es real, la delata el saldo.
- **El archivo va del movimiento más viejo al más nuevo.** Un banco que exporte al revés rompe el saldo corrido; haría falta una opción en el yaml. Se queda así hasta ver el banco real (G0).
- **No hay filas de totales al final.** Una fila "Total" se rechaza como fila a medio llenar. Se queda así hasta ver el banco real (G0).
- **Solo CSV.** Pichincha exporta PDF (G0): su lectura es la subfase G1b, todavía sin código. Excel depende de lo que responda Diners.
- **Una fecha en mes/día que también sirve como día/mes no se nota en esa fila** (05/03 es 5 de marzo o 3 de mayo). El archivo se rechaza igual en cuanto una fila tiene día mayor a 12 o cae fuera del periodo.

## Qué rechaza y con qué mensaje

Cada fila es un archivo de `tests/fixtures/estados/` importado de verdad contra una base en memoria. La tabla sale de un comando, y un test falla si esta página no coincide con él:

```bash
python scripts/estado_sintetico.py --tabla
```

<!-- tabla-casos:inicio -->
| Caso | Qué trae | Resultado | Mensaje exacto |
|---|---|---|---|
| `marzo` | Estado que cuadra, con columna de saldo | Entra | 17 movimientos |
| `abril` | El mes siguiente; empata con marzo | Entra | 5 movimientos |
| `marzo_coma` | Coma decimal, punto y coma, débito y crédito aparte, sin columna de saldo | Entra | 17 movimientos |
| `saldo_un_centavo` | Saldo final declarado un centavo arriba | Rechazado | El estado no cuadra: saldo anterior 1000.00 + créditos 1200.12 - débitos 485.39 = 1714.73, pero el saldo final es 1714.74 (diferencia 0.01). |
| `fecha_fuera_de_periodo` | Un movimiento fechado un día después del periodo | Rechazado | Línea 18: la fecha 2026-04-01 está fuera del periodo 2026-03-01 a 2026-03-31. |
| `coma_en_banco_de_punto` | Un monto con coma decimal ("-85,40") en un banco de punto | Rechazado | Línea 2: el monto '-85,40' no es válido: van solo dígitos y hasta dos decimales con punto, sin separador de miles. |
| `lineas_vacias` | Líneas totalmente vacías en medio y al final | Entra | 17 movimientos |
| `fila_a_medias` | Una fila sin monto ni saldo | Rechazado | Línea 8: fila a medio llenar, falta el monto y el saldo. |
| `archivo_repetido` | El mismo archivo por segunda vez | Rechazado | Este archivo ya se importó: es el estado #1 (banco_sintetico/ahorros, 2026-03-01 a 2026-03-31). |
| `reexportado` | El mismo mes exportado de nuevo (otros bytes: fin de línea de Windows) | Rechazado | El periodo 2026-03-01 a 2026-03-31 se cruza con el del estado #1 (2026-03-01 a 2026-03-31) de la misma cuenta. |
| `separador_de_miles` | Un monto con separador de miles ("1,200.00") | Rechazado | Línea 3: el monto '1,200.00' no es válido: van solo dígitos y hasta dos decimales con punto, sin separador de miles. |
| `tres_decimales` | Un monto con tres decimales ("-85.400") | Rechazado | Línea 2: el monto '-85.400' no es válido: van solo dígitos y hasta dos decimales con punto, sin separador de miles. |
| `encabezado_de_otro_banco` | Encabezado con las columnas de otro banco | Rechazado | Línea 1: al encabezado le faltan las columnas 'Fecha', 'Descripción', 'Monto' y 'Saldo'; trae 'Date', 'Description', 'Amount' y 'Balance'. ¿Es un estado de Banco Sintético? |
| `fecha_imposible` | Una fecha que no existe (31/02/2026) | Rechazado | Línea 6: la fecha '31/02/2026' no tiene el formato %d/%m/%Y. |
| `fecha_mes_dia` | Fechas en mes/día/año en un banco de día/mes/año | Rechazado | Línea 11: la fecha '03/15/2026' no tiene el formato %d/%m/%Y.<br>Línea 12: la fecha '03/15/2026' no tiene el formato %d/%m/%Y.<br>Línea 13: la fecha '03/18/2026' no tiene el formato %d/%m/%Y.<br>(y 5 más) |
| `errores_compensados` | Dos montos corridos +1 y -1 centavo; el total cuadra | Rechazado | Línea 2: el saldo no encadena: 1000.00 - 85.39 = 914.61, pero la fila dice 914.60.<br>Línea 5: el saldo no encadena: 2092.60 - 10.00 = 2082.60, pero la fila dice 2082.61. |
| `errores_compensados_sin_saldo` | Lo mismo en un banco sin columna de saldo (límite conocido) | Entra | 17 movimientos |
| `primera_fila_corrida` | El monto de la primera fila corrido un centavo | Rechazado | Línea 2: el saldo no encadena: 1000.00 - 85.39 = 914.61, pero la fila dice 914.60.<br>El estado no cuadra: saldo anterior 1000.00 + créditos 1200.12 - débitos 485.38 = 1714.74, pero el saldo final es 1714.73 (diferencia -0.01). |
| `fila_perdida` | Falta una fila, con columna de saldo | Rechazado | Línea 9: el saldo no encadena: 2063.41 - 35.00 = 2028.41, pero la fila dice 1983.41.<br>El estado no cuadra: saldo anterior 1000.00 + créditos 1200.12 - débitos 440.39 = 1759.73, pero el saldo final es 1714.73 (diferencia -45.00). |
| `fila_perdida_sin_saldo` | Falta una fila, sin columna de saldo | Rechazado | El estado no cuadra: saldo anterior 1000.00 + créditos 1200.12 - débitos 440.39 = 1759.73, pero el saldo final es 1714.73 (diferencia -45.00). |
| `bloque_duplicado` | Tres filas pegadas dos veces, con columna de saldo | Rechazado | Línea 7: el saldo no encadena: 2064.11 - 22.00 = 2042.11, pero la fila dice 2092.60.<br>El estado no cuadra: saldo anterior 1000.00 + créditos 1200.12 - débitos 535.88 = 1664.24, pero el saldo final es 1714.73 (diferencia 50.49).<br>Las líneas 7 a 9 repiten las líneas 4 a 6 y suman -50.49, justo la diferencia. |
| `bloque_duplicado_sin_saldo` | Tres filas pegadas dos veces, sin columna de saldo | Rechazado | El estado no cuadra: saldo anterior 1000.00 + créditos 1200.12 - débitos 535.88 = 1664.24, pero el saldo final es 1714.73 (diferencia 50.49).<br>Las líneas 9 a 11 repiten las líneas 6 a 8 y suman -50.49, justo la diferencia. |
| `descripciones_con_formula` | Descripciones que empiezan con = + - @ | Entra | 17 movimientos |
| `con_bom` | UTF-8 con BOM | Entra | 17 movimientos |
| `archivo_enorme` | Un archivo un byte más grande que el tope | Rechazado | El archivo pesa 1048577 bytes y el tope es 1048576: no se leyó. |
| `signo_invertido` | Débitos y créditos cambiados de columna | Rechazado | El estado no cuadra: saldo anterior 1000.00 + créditos 485.39 - débitos 1200.12 = 285.27, pero el saldo final es 1714.73 (diferencia 1429.46). |
| `monto_cero` | Un movimiento en cero | Rechazado | Línea 4: el monto es cero. |
| `sin_movimientos` | Sin movimientos, pero con saldo final distinto del anterior | Rechazado | El estado no cuadra: saldo anterior 1000.00 + créditos 0.00 - débitos 0.00 = 1000.00, pero el saldo final es 1005.00 (diferencia 5.00). |
| `continuidad` | Abril empieza con un saldo distinto del que cerró marzo | Entra con aviso | El estado #1 (2026-03-01 a 2026-03-31) cerró con 1714.73 y este empieza con 1713.73: hay -1.00 de diferencia entre los dos. |
<!-- tabla-casos:fin -->

Los mensajes de los casos que no están en la tabla (codificación, CSV mal formado, columna repetida, descripción larga, cuenta que parece un número) están en `tests/test_ingesta.py`, también completos.
