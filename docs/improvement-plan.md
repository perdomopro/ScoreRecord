# Plan de mejora — 2026-11-13

Plan de implementación derivado de [critical-review.md](critical-review.md).
Está ordenado por dependencia: cada fase deja el sistema en un estado útil y
habilita la siguiente. Las fases 1 y 2 son correcciones; las fases 3 a 5 son
lo que lleva el proyecto "al siguiente nivel".

## Correcciones tras la verificación (2026-11-19)

| Hueco | Corrección |
| --- | --- |
| GUI visible en la red de casa | Escucha solo en la IP del tailnet (`SCORERECORD_GUI_HOST`); desde `192.168.100.195` se rechaza la conexión |
| Deploy no borraba archivos | `deploy/apply_release.sh` con manifiesto; el primer deploy borró el código viejo y los units retirados |
| Basura en el servidor | Movida a `~/scorerecord_attic_20261119` (no se borró nada que no estuviera en git) |
| Sin alertas | `OnFailure=` en todos los units → `data/alerts.log`, check `service_failures` y envío opcional a `SCORERECORD_NOTIFY_URL`; probado con un unit que falla a propósito |
| Respaldos de 2.8 GB | Comprimidos con gzip, 7 diarios + 4 semanales |
| Base sin `VACUUM` | 357 MB → 257 MB |
| `/estado` lento | Ya no corre el `integrity_check` (lo hace la corrida diaria) |
| Fuera de temporada | `/partidos` marca las ligas sin partidos en 3 semanas y lo explica |
| CI y lint | Hallazgos de `ruff` (salvo longitud de línea) corregidos; CI corre `ruff` y `pytest` en cada push |

Sigue pendiente a propósito: dividir `cli.py`, renombrar `epl_pilot_refresh.py`,
la deuda de longitud de línea, saltar el reentrenamiento sin datos nuevos y
la cadencia de cuotas por cercanía al partido. Elegir dónde llegan las alertas
(`SCORERECORD_NOTIFY_URL`) es decisión del dueño.

## Verificación al 2026-11-19

Revisión de lo ejecutado contra el servidor, la base de producción (solo
lectura), el repositorio y GitHub. ✓ hecho y verificado · ◐ parcial · ✗ pendiente.

| Punto | Estado | Evidencia o hueco |
| --- | --- | --- |
| 0.1 Respaldo | ✓ | `backup-db` diario con 7 copias rotativas y respaldos previos a cada deploy (2.8 GB en total) |
| 0.2 Cuotas vacías | ✓ | Se corrigió en vez de detenerlo: cada corrida captura 15-17 partidos (Unibet, 1X2 y ambos anotan) |
| 0.3 Exposición | ✓ | Funnel apagado (`No serve config`) |
| 0.4 GUI y `/audit` | ◐ | `/audit` responde 404, pero la GUI escucha en `0.0.0.0:8765`: además del tailnet, la ve cualquier equipo de la red de casa |
| 0.5 Limpieza del servidor | ✗ | Siguen `debug_test.py`, `fix.py`, `query_db.py`, `ingest.sh`, `scratch/`, `run*.sh`, `write*.sh`, `diagnostics_output.json`, `gui.log` |
| 1.1 Resultados | ✓ | 839 de 855 partidos atrasados recibieron marcador. Football-Data publica algunas ligas con días de retraso (Brasileirao); 3 partidos siguen sin marcador (MLS 1, Argentina 2), probablemente reprogramados |
| 1.2 Corte móvil | ◐ | Los artefactos ya entrenan hasta el último partido. El modelo se reajusta cada día aunque no haya datos nuevos (~2 min de CPU) |
| 1.3 Snapshots | ✓ | Los 9 partidos jugados desde el deploy tienen pronóstico congelado; se liquidan cuando llega el marcador |
| 1.4 Cuotas por liga | ◐ | Funciona por id de evento. Cada 3 horas en lugar de T-24h/T-6h/T-1h; el plan gratuito no da Más/Menos |
| 1.5 Almacenamiento | ◐ | Poda y horizonte activos (22 mil filas de predicción). Nunca se ejecutó `VACUUM` (la base sigue en 341 MB) y los respaldos son copias completas sin nivel semanal |
| 1.6 Ligas | ◐ | Slugs de Clausura y playoffs verificados. Falta mostrar "fuera de temporada" en la GUI |
| 1.7 Health | ◐ | Siete checks nuevos en `db-health` y `/estado`. Falta `OnFailure=` con notificación |
| 2 Higiene | ◐ | 281 tests en verde y archivos muertos fuera del repo. El CI nunca corrió: los 4 commits de las fases 1-3 no están en GitHub. `ruff` marca 212 hallazgos; `cli.py` creció a 1,759 líneas |
| 3 GUI | ◐ | Todas las páginas en producción. Faltan el desglose por fuerza de señal y el estado de fuera de temporada; `/estado` tarda ~4 s por el `integrity_check` |
| Deploy | ✗ | El deploy descomprime sobre la carpeta existente y nunca borra archivos: los units retirados (`daily`, `epl-pilot`, `preplay-odds`) se reinstalaron (deshabilitados) y `gui.py` hubo que borrarlo a mano |

Primera lectura del modelo con 836 partidos reales (pronósticos del modelo
congelado en junio, antes de la fase 1):

| Mercado | Acierto | Log-loss modelo | Log-loss promedio de liga |
| --- | --- | --- | --- |
| Resultado | 45.7% | 1.050 | 1.084 |
| Más/Menos 2.5 | 57.1% | 0.680 | 0.686 |
| Ambos anotan | 56.3% | 0.681 | 0.681 |

- El modelo elige al local en 78% de los partidos; el local ganó 42%. Casi
  nunca elige empate (13 de 836) y hubo 28% de empates.
- Subestima los goles de esta temporada: Más de 2.5 pronosticado 49.8%, real
  53.8%; ambos anotan 53.2% contra 58.5%. Cuando dice ~35% de Más de 2.5, ocurre
  48% de las veces.
- Contra el mercado (26 partidos con cuotas): correlación 0.82, pero el modelo es
  menos decidido (desviación 0.115 contra 0.155) y difiere 7 puntos en promedio;
  las brechas mayores están en Brasileirao.

## Estado al 2026-11-13 (tarde)

Decisiones tomadas: GUI solo dentro del tailnet; stack ligero recomendado
(Starlette + Jinja2); mostrar mercado y consenso junto al modelo; archivar el
pipeline de apuestas en papel.

| Fase | Estado |
| --- | --- |
| 0 Contención | Pendiente: son acciones en el servidor (ver [deployment.md](deployment.md)) |
| 1.1 Resultados | Hecho. La fuente es Football-Data, no odds-api.io, que borra los eventos terminados |
| 1.2 Corte móvil | Hecho. Elo una vez por corrida; el artefacto solo cambia cuando llegan resultados |
| 1.3 Snapshots | Hecho, con 1X2 y una sola congelación por partido. La liquidación usa una predicción por partido |
| 1.4 Cuotas | Hecho. Usa el id de evento del fixture y los bookmakers que la cuenta permite |
| 1.5 Almacenamiento | Hecho: horizonte de 21 días, poda y respaldo rotativo. La base queda en ~320 MB, no en 150 |
| 1.6 Ligas | Parcial: slugs extra verificados (Clausura, playoffs). Falta el estado de fuera de temporada en la GUI |
| 1.7 Health | Hecho: siete checks nuevos en `db-health` y `/health`. Falta `OnFailure=` con notificación |
| 2 Higiene | Hecho salvo los 256 avisos de `ruff` y la división de `cli.py` |
| 3 GUI | Hecho en código (ver abajo); sin desplegar |
| 4-5 | Sin empezar |

Fase 3, implementada el 2026-11-19 en `src/scorerecord/web` (reemplaza a
`gui.py`): Jinja2 sobre el servidor de la librería estándar (una sola dependencia
nueva; Starlette y uvicorn no aportaban nada a un panel de solo lectura), rutas
`/`, `/partido/<id>`, `/resultados`, `/desempeno`, `/estado` y API JSON, caché por
ETag, gzip, modo oscuro, tooltips, matriz de marcadores (nueva tabla
`prediction_score_grid`), lectura combinada modelo + mercado, comparación contra
el promedio de cada liga y calibración. Ensayada en el homelab con una copia de la
base en el puerto 8766.

Las fases 1 y 2 están desplegadas desde el 2026-11-13. Se ensayaron completas en el homeserver sobre una
copia de la base: 839 de 855 partidos jugados recibieron marcador, las cinco
ligas quedaron con partidos futuros y el modelo entrenado hasta el último
partido disponible.

Primera medición real hacia adelante (913 partidos de agosto a noviembre,
pronosticados por el modelo congelado en junio): Más/Menos 2.5 acierta 55.9%
con log-loss 0.686; ambos anotan 55.3% y 0.685; 1X2 45.4% y 1.038.

## Principios

- **Primero el circuito, luego el modelo.** Sin resultados frescos y sin
  liquidación no se puede saber si un modelo nuevo es mejor.
- **Todo cambio de modelo pasa por un gate.** Mismo walk-forward, mismas ligas,
  diferencia con intervalo de confianza contra el modelo en producción.
- **El producto sigue siendo solo pronóstico.** Las cuotas se usan como
  referencia y como fuente de información, no para recomendar apuestas.
- **El servidor es pequeño.** Nada de servicios pesados ni pasos de build de
  frontend; picos por debajo de los `MemoryMax` actuales.

## Fase 0 — Contención (un rato, sin cambios de modelo)

Acciones operativas en el homeserver. Requieren tu visto bueno porque tocan
producción.

| # | Acción | Hecho cuando |
| --- | --- | --- |
| 0.1 | Respaldar la base antes de tocar nada (API `backup` de Python; el servidor no tiene el CLI `sqlite3`, ver [deployment.md](deployment.md)) | Existe un respaldo de hoy |
| 0.2 | Detener `scorerecord-odds.timer` hasta la fase 1.4 | No hay más ejecuciones con `matched_events=0` |
| 0.3 | Decidir exposición pública: mantener Funnel o dejar solo tailnet | Decisión tomada y aplicada |
| 0.4 | GUI en `127.0.0.1` (Funnel ya hace proxy a localhost) y `/audit` fuera del sitio público | `ss -ltn` muestra `127.0.0.1:8765`; `/audit` responde 404 desde fuera |
| 0.5 | Limpiar la raíz del servidor (`adrian@homeserver`, `cd`, `ssh`, `run.sh`, `write.sh`, `data/scorerecord.sqlite` vacío) | `ls` solo muestra el árbol desplegado |

## Fase 1 — Cerrar el circuito de datos

Objetivo: que cada día el sistema aprenda de los partidos de ayer y mida lo que
pronosticó. Es la fase con mayor retorno.

### 1.1 Ingesta de resultados

- Leer `status` y `scores` de los eventos de odds-api.io en
  `ingest/fixtures_odds_api_io.py` y pedir también los eventos terminados de
  los últimos días. Escribir `home_score`, `away_score` y `status='finished'`.
  Confirmar el parámetro de estado contra la documentación del proveedor antes
  de implementarlo.
- Nuevo paso `promote-finished-fixtures`: cada fixture terminado se inserta en
  `match_history` con `tournament = league_code`. Deduplicar contra las filas
  puenteadas de Football-Data por (liga, equipos, fecha ±1 día).
- Refresco semanal de Football-Data (`ingest-football-data --refresh` +
  `bridge-club-history`) como segunda fuente y para traer tiros de las ligas
  europeas.
- Rellenar `kickoff_local` al ingerir fixtures de clubes.

Hecho cuando: no quedan fixtures con más de 48 h de jugados sin marcador y
`match_history` llega hasta ayer en las ligas activas.

### 1.2 Corte de entrenamiento móvil

- Separar políticas en `models/manifest.py`: la del Mundial queda congelada;
  las ligas de clubes usan `trained_until = fecha de ejecución`.
- Saltar el reentrenamiento cuando el conjunto de entrenamiento no cambió
  (hash de filas) y reutilizar el artefacto anterior.
- Reconstruir Elo una sola vez por ejecución diaria, no una por liga.

Hecho cuando: `model_artifact.trained_until` avanza cada día con partidos
nuevos y el número de artefactos deja de crecer en días sin partidos.

### 1.3 Snapshots y liquidación

- Corregir `freeze_forecast_snapshot` para tomar el último run **por fixture**.
- Congelar en el refresh diario los pronósticos de los partidos de las
  próximas 24-36 h, incluyendo 1X2.
- Ampliar `forecast_snapshots` para liquidar 1X2 además de OU2.5 y BTTS.
- `settle-fixtures` y `settle-forecast-snapshots` ya corren cada noche;
  empezarán a liquidar en cuanto exista 1.1.

Hecho cuando: `/performance` muestra partidos de clubes liquidados esta semana.

### 1.4 Cuotas por liga

- `fetch-odds` itera las ligas activas de `league_config` en lugar de un único
  `ODDS_API_IO_LEAGUE`, limitado a fixtures de los próximos N días y con un
  presupuesto diario de peticiones explícito.
- Cadencia por cercanía al partido (por ejemplo T-24h, T-6h, T-1h) en vez de
  cada hora para todo.
- Retirar del servicio nocturno `compute-clv` y el resto del pipeline de
  apuestas en papel.

Hecho cuando: los partidos de la semana tienen filas en `odds_snapshots` y la
ficha de partido muestra la probabilidad implícita del mercado.

### 1.5 Almacenamiento

- Predecir solo fixtures dentro de un horizonte (14 días por defecto).
- Conservar por fixture la última predicción y la congelada; borrar runs
  intermedios. `VACUUM` y checkpoint del WAL tras la limpieza inicial.
- Respaldo diario con rotación (7 diarios, 4 semanales) como unit propio.

Hecho cuando: la base baja de ~350 MB a menos de 150 MB y deja de crecer de
forma lineal.

### 1.6 Configuración de ligas

- `LeagueConfig` con lista de slugs por liga (Apertura/Clausura, fase regular
  y liguilla/playoffs) y tipo de temporada (año calendario o cruzada).
- Estado explícito por liga: activa, fuera de temporada, sin eventos del
  proveedor. La GUI lo muestra en lugar de una lista vacía.
- Revisar con `/leagues` los slugs actuales de Argentina, Liga MX y MLS.

### 1.7 Health que diga la verdad

Nuevos checks en `health.py`, visibles en `/health` y con código de salida:

| Check | Falla cuando |
| --- | --- |
| Frescura de resultados | Hay fixtures con más de 48 h de jugados sin marcador |
| Corte de entrenamiento | `trained_until` tiene más de 7 días en una liga activa |
| Cuotas | Tres ejecuciones seguidas con `matched_events=0` |
| Snapshots | Hay partidos de hoy con predicción y sin snapshot |
| Horizonte | Una liga activa no tiene fixtures futuros |
| Respaldo | El último respaldo tiene más de 48 h |

Además: `OnFailure=` en todos los units con notificación (ntfy o Telegram),
`OK_NO_EVENTS` como advertencia visible, `calibrator-drift` comparando runs de
la misma liga, y un archivo `VERSION` escrito en el deploy para que
`code_git_sha` deje de ser `None`.

## Fase 2 — Higiene de código

Se puede hacer en paralelo con la fase 1.

- Añadir `tzdata` como dependencia en Windows.
- Quitar las fechas fijas de los tests (reloj inyectable); hoy fallan 3.
- GitHub Actions: `pytest` y `ruff` en Linux y Windows.
- `ruff format` y limpiar los 256 hallazgos, o relajar `E501` de forma
  explícita.
- Borrar `fix.py`, `query_db.py`, `debug_test.py`, `ingest.sh`, `scratch/` y
  los units `scorerecord-daily`, `scorerecord-epl-pilot`,
  `scorerecord-preplay-odds`.
- Renombrar `epl_pilot_refresh.py` a `club_refresh.py` y partir `cli.py` por
  grupos de comandos.
- Actualizar `pyproject.toml` (descripción todavía dice "World Cup 2026").

## Fase 3 — Rediseño de la GUI

Objetivo: que alguien que no conoce el proyecto entienda en cinco segundos qué
partidos hay, qué dice el modelo y qué tan bien le ha ido.

### 3.1 Base técnica

- Dividir `gui.py` en paquete: `gui/data.py` (consultas), `gui/viewmodels.py`,
  `gui/templates/` (Jinja2) y `gui/static/` (CSS y JS servidos con caché).
- Mantener render en servidor. Recomendación: Starlette + Jinja2 + uvicorn con
  un poco de JS propio; sin SPA ni paso de build. Añade ~3 dependencias ligeras.
- Endpoints JSON (`/api/fixtures`, `/api/match/<id>`, `/api/performance`).
- Caché por `run_id` más reciente con `ETag` en lugar de TTL de 10 s; gzip.
- Página de error genérica; el detalle va al log.

### 3.2 Arquitectura de información

| Hoy | Propuesta |
| --- | --- |
| Landing + Partidos + Mejores señales | **Inicio**: próxima jornada con partidos, agrupada por día y liga |
| Historial (próximos / pasados) | **Resultados**: pronóstico contra marcador, con acierto o fallo |
| Performance + Mercado | **Desempeño**: una sola página con modelo, tasa base y mercado |
| Health | **Estado**: se queda, enlazada desde el pie, fuera del menú principal |

- Inicio nunca sale vacío: si hoy no hay partidos, abre la siguiente fecha.
- Un aviso legal de una línea, descartable, más el texto completo en el pie.
- Modelo, artifact, run y zona horaria pasan a un "Acerca de este pronóstico"
  plegable dentro de la ficha.
- Español consistente y con acentos; códigos internos fuera de la interfaz.

### 3.3 Lista de partidos

Fila compacta por partido, expandible:

- Hora local y relativa ("mañana 17:00"), equipos, liga.
- Barra 1X2 de tres segmentos con porcentajes.
- Goles esperados por equipo y marcador más probable.
- Dos indicadores cortos: Más/Menos 2.5 y Ambos anotan, resaltados solo cuando
  se apartan de la tasa base de la liga.
- Probabilidad del mercado al lado cuando haya cuotas.

Meta: una jornada de 10 partidos visible en dos pantallas de teléfono.

### 3.4 Ficha de partido

- Encabezado con probabilidades 1X2 y goles esperados.
- Mapa de calor de marcadores (la matriz ya se calcula) y los 5 marcadores
  más probables (ya se guardan en `prediction`).
- Mercados de goles como barras con la tasa base de la liga marcada.
- Forma reciente (últimos 5), enfrentamientos previos y evolución de Elo.
- Modelo contra mercado cuando exista snapshot de cuotas.
- Después del partido: marcador final y veredicto de cada pronóstico.

### 3.5 Desempeño

- Curva de calibración (pronosticado contra observado) por mercado.
- Log-loss y Brier acumulados en el tiempo contra tasa base y contra mercado.
- Desglose por liga y por fuerza de señal.
- Tamaño de muestra siempre visible; por debajo de 100 partidos, aviso de
  muestra pequeña.

### 3.6 Detalles

- Corregir el indicador de calidad de datos tapado por el botón y etiquetarlo.
- Un `<h1>` por página; señal con texto o icono además de color.
- Modo oscuro con `prefers-color-scheme`.
- Encabezado fijo compacto en móvil (una línea con menú).

## Fase 4 — Modelos

Empieza cuando la fase 1 lleve al menos unas semanas acumulando datos.

### 4.1 Gate único de comparación

Comando `model-compare` que corra candidatos y modelo actual sobre el mismo
walk-forward y reporte por liga: log-loss, Brier, RPS para 1X2, ECE y delta
pareado con intervalo bootstrap por semana; contra el mercado donde existan
cuotas históricas. Un candidato se promueve solo si mejora al modelo actual
con intervalo que excluye cero y no empeora la calibración. Reutiliza
`evaluate/backtest.py`, `goals_v2_market_benchmark.py` y `scoring.py`.

### 4.2 Mejoras por orden de valor esperado

| # | Mejora | Por qué | Costo |
| --- | --- | --- | --- |
| a | Datos frescos (fase 1) | Faltan 3.5 meses y toda la temporada actual de EPL | Ya incluido |
| b | Reajustar vida media y `l2` por liga | Se afinaron con selecciones | Bajo; `tune-decay` ya existe |
| c | Entrenamiento conjunto por país con efecto de división | Coventry y Hull entran con 0 partidos; hay 2760 de Championship | Medio |
| d | Calibrar 1X2 y promoverlo a pronóstico principal | Es donde el modelo discrimina y lo que la gente quiere ver | Bajo-medio |
| e | Señal relativa a la tasa base de la liga | Hoy Over 1.5 al 73% sale como "muy fuerte" | Bajo |
| f | Goals V2 con tiros en producción para ligas con datos | Ya validado: −0.0094 de log-loss conjunto | Medio |
| g | Estadísticas y xG de API-Football para Liga MX, MLS, Argentina, Brasil | Football-Data no trae tiros para esas ligas; la llave ya está configurada | Medio; 100 peticiones/día |
| h | Ensamble por stacking (Dixon-Coles, Elo, Goals V2) | Suele dar una mejora pequeña y estable | Medio |
| i | Pronóstico de consenso con mercado | El mercado gana por 0.009 de log-loss; combinar es la forma honesta de dar el mejor número | Medio; requiere 1.4 |
| j | Optimización con criterio de convergencia y `rho` conjunto | Hoy son 600 épocas fijas y `rho` en rejilla posterior | Bajo |
| k | Intervalo de incertidumbre por bootstrap de parámetros | El rango actual es heurístico | Medio |

Sobre (i): la ficha mostraría tres números claramente etiquetados — modelo,
mercado y consenso — sin lenguaje de valor ni de apuesta.

### 4.3 Monitoreo en vivo

- Calibración y log-loss de los snapshots liquidados, por liga y por semana.
- Alerta cuando el log-loss móvil de 8 semanas supere a la tasa base.
- Drift de calibradores por liga con umbral sobre cambio absoluto.

## Fase 5 — Ampliaciones

- La Liga, Serie A, Bundesliga, Ligue 1 y Championship ya tienen histórico con
  tiros en la base; solo faltan slugs del proveedor y alias.
- Copas continentales con política de entrenamiento propia.
- Córners y tarjetas una vez que 4.2-g dé cobertura de estadísticas.
- Notificación diaria opcional con los partidos del día.

## Orden sugerido

```text
Fase 0  ──►  Fase 1 (1.1 → 1.2 → 1.3 → 1.7 → 1.4 → 1.5 → 1.6)
                │
Fase 2  ────────┤ (en paralelo)
                ▼
             Fase 3  ──►  Fase 4  ──►  Fase 5
```

La fase 3 puede empezar en cuanto 1.1-1.3 estén desplegadas, porque el
rediseño depende de tener resultados y pronósticos liquidados que mostrar.

## Decisiones pendientes

1. **Exposición pública.** ¿La GUI debe seguir abierta a internet por Funnel o
   solo dentro del tailnet?
2. **Stack de la GUI.** ¿Aceptas añadir Starlette + Jinja2 + uvicorn, o
   prefieres seguir solo con librería estándar y dividir el archivo?
3. **Cuotas en la interfaz.** ¿Quieres mostrar la probabilidad del mercado y un
   pronóstico de consenso junto al del modelo?
4. **Pipeline de apuestas en papel.** ¿Se archiva (edge, recommend, CLV,
   `/audit`) o se conserva oculto?
5. **Ligas.** ¿Se añaden ya las cinco europeas que tienen histórico?
