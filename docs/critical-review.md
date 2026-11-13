# Revisión crítica — 2026-11-13

Auditoría completa del repositorio (`e1f24b2`), del despliegue en el homeserver
y de la GUI en vivo. Sustituye a la revisión anterior, que estaba escrita para
el alcance del Mundial 2026. El plan para corregir lo que aquí se describe está
en [improvement-plan.md](improvement-plan.md).

Toda la evidencia se obtuvo en modo solo lectura: `journalctl`, consultas
`mode=ro` sobre `data/scorerecord.db`, navegación de la GUI y la suite de tests
en local. No se modificó nada en el servidor.

## Resumen

El proyecto tiene una base seria (procedencia de cada predicción, walk-forward
sin fuga de datos, benchmarks contra mercado, GUI limpia), pero **el circuito de
producción está roto en cuatro puntos** y por eso lleva meses sin mejorar ni
medirse:

1. El modelo se entrena cada día con datos que terminan en junio/julio.
2. Los partidos jugados nunca reciben marcador, así que nada se liquida.
3. El job de cuotas consulta la liga del Mundial y no captura nada desde julio.
4. No se congelan pronósticos, así que no hay historial de aciertos.

Mientras tanto `/health` aparece en verde. El trabajo más valioso no es un
modelo nuevo sino cerrar ese circuito; después, con datos frescos y medición
real, tiene sentido mejorar modelos y rediseñar la GUI.

## Qué corre hoy en producción

| Timer | Cuándo | Qué hace | Estado real |
| --- | --- | --- | --- |
| `scorerecord-club-refresh` | Diario 05:35 | Por liga: fixtures de odds-api.io → rebuild Elo → reentrena Dixon-Coles + Platt → publica predicciones | Corre; 3 de 5 ligas con partidos futuros |
| `scorerecord-odds` | Cada hora | `fetch-odds` | Corre; `matched_events=0` en todas las ejecuciones |
| `scorerecord-postmatch` | Diario 23:30 | Liquida predicciones, snapshots y CLV | Corre; `settled=0` todas las noches |
| `scorerecord-weekly` | Domingo 09:00 | Reportes, gates de mercados de goles, drift | Corre; drift da 4 falsas alertas |
| `scorerecord-gui` | Servicio | GUI en `0.0.0.0:8765` | Activo; 268 MB de 350 MB permitidos |

La GUI está publicada en internet con Tailscale Funnel (proxy a
`127.0.0.1:8765`). Los timers `daily`, `epl-pilot` y `preplay-odds` existen en
el repo pero no están activos.

## Hallazgos críticos (pipeline)

### P0-1. El entrenamiento está congelado en junio de 2026

`TRAINING_POLICY.trained_until = "2026-06-11"` es el corte del Mundial y está
fijo en `models/manifest.py`. `predict/fixtures.py` lo aplica también a las
ligas de clubes. Además el refresh diario no ingiere resultados nuevos:

- `historical_match` y `match_history` terminan en mayo (Europa) o el
  26-27 de julio (América).
- Todos los artefactos registran `trained_until = 2026-06-11` y el mismo número
  de filas cada día (EPL 1900, Liga MX 4655, Brasileirao 6034).
- La temporada 2026-27 de la Premier no existe para el modelo. Coventry y Hull
  tienen 0 partidos de entrenamiento, así que se predicen como un equipo medio
  de Premier.

Consecuencia: cada día se gastan ~2.5 minutos de CPU reentrenando exactamente
el mismo modelo sobre los mismos datos.

### P0-2. Los partidos jugados nunca reciben resultado

`ingest_fixtures_odds_api_io` solo escribe equipos y hora; nunca `home_score`,
`away_score` ni `status`. Hay 855 fixtures de clubes ya jugados sin marcador y
todos siguen en `status='scheduled'`. El proveedor sí devuelve `status` y
`scores` en cada evento, pero no se leen.

Consecuencia: `settle-fixtures` reporta `settled_predictions=0` cada noche y
`/performance` sigue mostrando 40 partidos del Mundial liquidados el 8 de julio.

### P0-3. El job de cuotas apunta al Mundial

`fetch-odds` usa `ODDS_API_IO_LEAGUE`, que no está definido en el `.env` del
servidor y cae al valor por defecto `international-fifa-world-cup`. Resultado
de cada ejecución horaria: `fixtures_seen=202 matched_events=0
snapshot_rows_inserted=0 api_requests_used=1`. Son ~24 peticiones diarias
desperdiciadas y cero cuotas de clubes capturadas. `odds_snapshots` tiene 4394
filas de 13 partidos, todas del Mundial.

Consecuencia: la comparación modelo-mercado de cada partido dice "Datos de
mercado incompletos" y la página `/market` no tiene nada que mostrar.

### P0-4. No se congelan pronósticos

`freeze-forecast-snapshot` no forma parte del refresh diario. La tabla
`forecast_snapshot` tiene 4 filas, de julio. Además `_latest_prediction_run_id`
toma el último run global, que pertenece a una sola liga, así que aunque se
llamara solo congelaría la última liga procesada.

### P0-5. `/health` no detecta nada de lo anterior

Los checks miran integridad de SQLite, llaves foráneas y que el último run haya
terminado. No hay checks de frescura de resultados, antigüedad del corte de
entrenamiento, racha de `matched_events=0`, ni snapshots pendientes. El refresh
de una liga sin eventos termina como `OK_NO_EVENTS` con código de salida 0.

## Hallazgos importantes (operación)

- **Crecimiento sin control.** Cada día se reinsertan las predicciones de todos
  los fixtures futuros (EPL: 151 partidos hasta febrero de 2027, 2869 filas
  diarias). La tabla `prediction` tiene 351 mil filas y con su índice ocupa
  95 MB de los 348 MB de la base. El WAL pesa 60 MB sin checkpoint.
- **Sin respaldos.** El último respaldo de la base es del 14 de julio.
- **Elo se recalcula cinco veces al día** (borra e inserta 84 mil filas por
  liga) y Dixon-Coles no usa Elo como entrada; solo se muestra en la GUI.
- **Configuración de ligas frágil.** La temporada está fija en `2026-2027`
  incluso para ligas de año calendario. El slug de Liga MX es
  `mexico-liga-mx-apertura` y dejará de servir en el Clausura de enero.
  Argentina devuelve cero eventos con su slug actual y MLS ya no tiene partidos
  futuros; la GUI no explica ninguno de los dos casos.
- **Drift con falsas alertas.** `calibrator-drift` compara los dos últimos runs
  aunque sean de ligas distintas: `max_relative_change=2.86 alerts=4`.
- **Pipeline de apuestas en papel sigue vivo.** `compute-clv` corre cada noche
  sobre 15 recomendaciones de julio, aunque el producto es solo pronóstico.
- **Sin alertas.** Ningún unit tiene `OnFailure=`. El servidor estuvo apagado
  del 28 de octubre al 10 de noviembre y no hubo aviso.
- **Deriva entre repo y servidor.** El servidor corre Python 3.13 (los docs
  dicen 3.11), `code_git_sha` es `None` en todos los artefactos porque se
  despliega con `git archive`, y la raíz del servidor tiene archivos basura
  (`adrian@homeserver`, `cd`, `ssh`, `run.sh`, `write.sh`).

## Hallazgos de exposición pública

- La GUI escucha en `0.0.0.0:8765`, alcanzable desde la LAN además del Funnel.
- `/audit` es accesible públicamente y muestra picks en papel con EV y stake,
  lo que contradice el carácter de solo pronóstico del producto.
- `render_error` devuelve el texto de la excepción al visitante.
- El servidor es `http.server` de la librería estándar: sin compresión, sin
  límite de peticiones, `Cache-Control: no-store`, y `BrokenPipeError` llena
  el journal. `/history` carga hasta 2000 fixtures por petición.

## Modelos

Lo que existe: Dixon-Coles por liga (decaimiento de 3 años, ridge `l2=1.0`,
600 épocas fijas de Adam, `rho` ajustado después en rejilla), calibración Platt
por liga para OU1.5/2.5/3.5 y BTTS, y 1X2 sin calibrar.

Debilidades medidas:

- **Casi no discrimina en goles.** En el último run, la probabilidad de Over 2.5
  tiene desviación estándar de 0.04-0.05 entre partidos y BTTS de 0.02-0.03.
  El pronóstico "principal" de la GUI es en la práctica la tasa base de la
  liga. El 1X2, que la GUI trata como secundario, es donde el modelo sí separa
  partidos (desviación 0.07-0.15).
- **Pierde contra el mercado.** Benchmark OU2.5 en 6 ligas, 2021-2025: log-loss
  del modelo 0.6824 contra 0.6733 del mercado pre-cierre (IC de la diferencia
  enteramente sobre cero). Goals V2 con tiros mejora al modelo base (−0.0094)
  pero sigue por detrás del mercado (+0.0075) y no está en producción.
- **Hiperparámetros heredados.** La vida media de 3 años se afinó con partidos
  de selecciones y se aplica a clubes sin reajustar; `l2` nunca se afinó.
- **Ascendidos sin prior.** Hay 2760 partidos de Championship en la base, pero
  el modelo de EPL solo entrena con filas `tournament == 'EPL'`.
- **Fuerza de señal mal definida.** Se mide como distancia al 50%. Over 1.5 al
  73% sale como "Señal muy fuerte" cuando 73% es la tasa base del mercado.
- **Incertidumbre heurística.** El "rango orientativo" (±4.5 pp, ±8.5 pp) son
  puntos asignados por reglas, no un intervalo estadístico.

## Frontend

Lo que funciona: identidad visual consistente, sin desbordes horizontales en
móvil, filtro por liga, navegación por fechas con conteo de partidos.

Problemas de uso:

1. **La página principal sale vacía.** `/today` sin partidos hoy muestra "SIN
   PARTIDOS… Revisa fixture-summary, refresh de liga y predict-fixtures", un
   mensaje para el operador, en vez de saltar a la próxima jornada.
2. **El contenido empieza muy abajo.** Antes del primer partido hay aviso
   legal, franja `FORECAST_ONLY` con artifact y zona horaria, pestañas de liga
   y barra de fechas. En un teléfono el primer partido aparece tras ~1500 px y
   el encabezado fijo ocupa media pantalla.
3. **Lenguaje de operador.** `FORECAST_ONLY`, `Artifact 477afadd3431`,
   `Prediction run`, `NOT_TESTED`, `LEAN`, "Health", "Performance". Español e
   inglés mezclados y faltan acentos ("recomendacion", "Proxima").
4. **Tarjetas muy densas.** Cada partido muestra unas 15 cifras y repite dos
   veces "NO VALIDADO CONTRA MERCADO". Tres partidos ocupan 6000 px en móvil.
   Todo es texto; no hay barras ni gráficas.
5. **Cifra ambigua.** El "80%" junto a cada señal es calidad de datos, no
   probabilidad, no tiene etiqueta y queda tapado por el botón "Detalle".
6. **Falta lo que un aficionado busca.** Goles esperados, marcadores más
   probables (se calculan y guardan, pero no se muestran), forma reciente,
   enfrentamientos previos, tabla de la liga, escudos.
7. **Historial sin veredicto.** "Partidos pasados" lista marcadores hasta julio
   con la etiqueta "Histórico cargado"; no dice qué pronosticó el modelo ni si
   acertó.
8. **Performance y Mercado no informan.** Performance es una tabla del Mundial;
   Mercado lista filas crudas `odds_ingest markets_seen 0.000` y declara
   `NOT_TESTED` aunque el benchmark documentado rechazó al modelo.

Problemas técnicos: `gui.py` tiene 5600 líneas con HTML armado en f-strings;
25 KB de CSS van incrustados en cada página (57% del peso); no hay archivos
estáticos, plantillas ni API JSON; `/today` no tiene `<h1>`; la señal se
codifica solo con color; no hay modo oscuro.

## Calidad de código

- En Windows fallan 36 de 273 tests por falta de `tzdata` (no está declarado
  como dependencia). Con `tzdata` instalado fallan 3 en
  `test_league_aware_artifacts.py` porque usan fechas fijas que ya pasaron; el
  "pytest en verde" de los docs ya no es cierto en ninguna plataforma.
- `ruff check` reporta 256 hallazgos (208 por longitud de línea).
- No hay CI.
- Tres monolitos: `gui.py` (5600 líneas), `evaluate/backtest.py` (3345) y
  `cli.py` (1640).
- Archivos sueltos en la raíz: `fix.py`, `query_db.py`, `debug_test.py`,
  `ingest.sh`, `scratch/`.
- `fixture.kickoff_local` es nulo en fixtures de clubes, pero
  `publish_fixture_predictions` ordena y fecha con esa columna.

## Qué está bien y conviene conservar

- Procedencia completa: `ingest_run`, `model_artifact` con hash, `data_quality`.
- Evaluación walk-forward sin fuga y calibración fuera de muestra.
- Honestidad del producto: benchmarks contra mercado documentados aunque el
  resultado fuera negativo.
- Huella mínima en el servidor: una dependencia (`numpy`), picos de 64-144 MB.
- Límites de recursos en systemd y secretos fuera de git (`.env` en modo 600).
