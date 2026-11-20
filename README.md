# ScoreRecord

ScoreRecord is a forecast-only sports prediction system focused on audited
football/soccer forecasts. It combines league-scoped historical data, Elo,
Dixon-Coles goal models, calibration, fixture provenance, health checks, and a
lightweight GUI for match forecasts.

Repository: https://github.com/perdomopro/ScoreRecord

The public GUI is deliberately not a betting app: it does not show EV, Kelly,
stake sizing, or "best bet" language in the forecast view.

## Status (2026-11-13)

A full audit found the production feedback loop open: no club results, a
training cutoff frozen at 2026-06-11, odds capture pointed at the World Cup, and
no frozen forecasts. The code in this branch closes that loop (improvement plan
phases 1 and 2) and was rehearsed end to end on a copy of the production
database. It is **not deployed yet**; the homeserver still runs the old code.

- What is wrong and the evidence: [docs/critical-review.md](docs/critical-review.md)
- What to do about it, in order: [docs/improvement-plan.md](docs/improvement-plan.md)

## How Production Runs

```text
05:35 daily   club-refresh   backup -> results (Football-Data) -> Elo -> fixtures (odds-api.io)
                             -> Dixon-Coles + Platt -> predictions (next 21 days)
                             -> freeze next 36 h -> settle -> prune -> db-health
every 3 h     odds           fetch-odds for fixtures in the next 48 h -> odds_snapshots
23:30 daily   postmatch      results -> settle-fixtures -> settle-forecast-snapshots
Sun 09:00     weekly         performance report, goal-market gates, calibrator drift
always on     gui            read-only dashboard on port 8765
```

All jobs are systemd user timers on the homeserver and share one SQLite file,
`data/scorerecord.db`. The GUI only reads. See
[docs/deployment.md](docs/deployment.md) for units, limits, and exposure.

## Current Scope

ScoreRecord is implemented as a lightweight Python package designed for a small
homeserver:

- SQLite schema for teams, aliases, historical matches, fixtures, predictions,
  forecast snapshots, odds snapshots, recommendations, metrics, and audit runs.
- Cached ingestion for `openfootball/worldcup.json`.
- Cached ingestion for `martj42/international_results`.
- Football-Data CSV ingestion for covered club leagues.
- odds-api.io fixture and odds snapshot ingestion.
- Read-only SportMonks/API-Football probes for provider validation.
- Read-only SportMonks/TheStatsAPI stats probe for corners coverage validation.
- Seed team aliases for common naming mismatches.
- Elo calculation over international and club match history.
- Pre/post-match Elo history for leakage-free walk-forward features.
- Basic run tracking and data quality logging.
- League-scoped model artifacts for club forecast pilots.
- Read-only dashboard (`src/scorerecord/web`): matchday, match detail, results,
  performance against league averages and the market, status, and a JSON API.
- Secondary result diagnostics for 1X2 and double chance, kept out of the
  principal OU2.5/BTTS forecast selector.
- CLI entry points that can run locally or on the homeserver.

## Quick Start

Python 3.11 or newer.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
scorerecord init-db
scorerecord migrate-db
scorerecord db-health
scorerecord ingest-worldcup
scorerecord ingest-history
scorerecord build-elo
scorerecord quality-report
scorerecord walk-forward --start-year 2021
scorerecord tune-decay --start-year 2021
scorerecord goal-markets --start-year 2021
scorerecord predict-fixtures
scorerecord model-manifest
scorerecord settle-fixtures --dry-run
scorerecord fetch-odds --dry-run
scorerecord fetch-odds --preplay-window-minutes 60 --dry-run
scorerecord odds-snapshot-report
scorerecord odds-coverage-summary --date 2026-07-05
scorerecord odds-coverage-summary --fixture-id wc2026-match-091
scorerecord compute-edge --dry-run
scorerecord recommend --max-edge 0.25 --max-odds-age-minutes 180 --min-books 2 --dry-run
scorerecord compute-clv --dry-run
scorerecord calibrator-drift
scorerecord backtest --from 2024-01-01 --to 2024-12-31
scorerecord ingest-football-data --league EPL --season 2023-2024 --dry-run
scorerecord ingest-football-data --league EPL --from-season 2019-2020 --to-season 2024-2025
scorerecord ingest-football-data --league "Mexico (Liga MX)" --season 2025-2026 --dry-run
scorerecord historical-odds-report --league EPL
scorerecord historical-odds-report --league "Mexico (Liga MX)" --market OU2.5
scorerecord historical-odds-report --league EPL --market OU2.5
scorerecord backtest --from 2023-08-01 --to 2024-05-31 --odds-mode historical --league EPL --season 2023-2024 --market 1X2
scorerecord backtest --from 2021-08-01 --to 2025-05-31 --odds-mode historical --league EPL --market 1X2 --min-train 500
scorerecord backtest-policy-grid --from 2021-08-01 --to 2025-05-31 --league EPL --min-train 500
scorerecord backtest-policy-grid --from 2021-08-01 --to 2025-05-31 --league EPL --min-train 500 --market-shrink-alphas 1.0,0.75,0.5,0.25
scorerecord market-benchmark-report --market OU2.5 --from 2021-08-01 --to 2025-05-31 --leagues EPL,LA_LIGA,SERIE_A,BUNDESLIGA,LIGUE_1,CHAMPIONSHIP --entry-odds avg_preclose --closing-odds avg_close --calibration prequential --bootstrap-block week --bootstrap-samples 2000 --min-train 500
scorerecord analyze-policy-grids "outputs/backtests/policy_grid_real_odds_*.csv"
scorerecord calibration-diagnostics --from 2021-08-01 --to 2025-05-31 --leagues EPL,LA_LIGA,SERIE_A,BUNDESLIGA,LIGUE_1,CHAMPIONSHIP --min-train 500
scripts/run_multileague_validation.sh
scorerecord calibration-report --from 2024-01-01 --to 2024-12-31
scorerecord one-x-two-calibration-report --leagues LIGA_MX,MLS,EPL,ARGENTINA,BRASILEIRAO
scorerecord provider-stats-probe --providers sportmonks,thestatsapi --leagues LIGA_MX,MLS,EPL,ARGENTINA,BRASILEIRAO
scorerecord gui --host 127.0.0.1 --port 8765
scorerecord db-summary
```

## Dashboard

`scorerecord gui` serves a read-only dashboard (package `src/scorerecord/web`,
Jinja2 templates, no build step). It opens the database in read-only mode and
never fits a model at request time.

| Route | Shows |
| --- | --- |
| `/` | Landing page: project title, description, repository link, and buttons to the sections |
| `/partidos` | Next matchday with fixtures, grouped by league: 1X2 bar, the most likely outcome, expected goals, and goals / both-teams-score labels highlighted when they differ from the league average. `?fecha=YYYY-MM-DD&liga=EPL` |
| `/partido/<fixture_id>` | Model, market, and blended probabilities; league averages; expected goals; score heatmap; recent form; Elo; head-to-head; provenance |
| `/resultados` | Pre-kickoff forecast against the final score, with hits per market. `?dias=7`, `14`, `30` or `90` |
| `/desempeno` | Log-loss against the league base rate and against the market, calibration, and monthly trend |
| `/estado` | Health checks and latest job runs |
| `/api/partidos`, `/api/partido/<id>`, `/api/desempeno` | The same data as JSON |

When pre-kickoff odds exist, the headline 1X2 and both-teams-score numbers blend
model and market by logarithmic pooling with a provisional market weight of 0.7
(`MARKET_WEIGHT` in `web/data.py`); otherwise they are the model alone. League
averages are the frequencies in that league over the 730 days before each match.
Old URLs (`/today` and `/signals` to `/partidos`, `/match/<id>`, `/health`, `/performance`, `/history`) redirect;
the paper-betting `/audit` view is retired.

`odds-coverage-summary` still grades odds capture per fixture and market
(`READY_FOR_ANALYSIS`, `PARTIAL_COVERAGE`, `WEAK_COVERAGE`, `NO_ODDS`) from the
CLI. Corners are not shown until provider stats coverage and a separate
benchmark pass.
The older audit route is kept hidden for legacy review only.
Use `SCORERECORD_DISPLAY_TZ` to change the display timezone; it defaults to
`America/Mexico_City`.

Backtest CSVs are written to `outputs/backtests/`. Until historical bookmaker odds are
available, those reports use a training-window marginal probability as a market proxy, so
they are for calibration and ranking diagnostics rather than real ROI claims.

For covered club leagues, `ingest-football-data` can load Football-Data CSV results,
1X2 odds, and OU2.5 totals where the CSV exposes complete two-way prices. The
OU2.5 coverage report includes two-way completeness, close completeness, column-family
counts, and overround ranges. Then `backtest --odds-mode historical` writes
separate real-odds CSVs with `profit_*_real` and `clv_real`. This is a club-league
historical ROI harness, not a direct World Cup profitability proof.
Mexico/Liga MX, MLS, Brazil Serie A, and Argentina Liga Profesional are wired as
Football-Data `/new/*.csv` current feeds. Use one explicit `--season` label and audit
coverage before treating them as anything more than forecasting/product candidates.
The dashboard and the daily loop cover ten leagues: Liga MX, MLS, Premier League,
LaLiga, Serie A, Bundesliga, Ligue 1, Championship, Argentina and Brasileirao.
Champions League, Europa League, Libertadores, and Sudamericana are provider
watchlist competitions until ScoreRecord has league-scoped historical artifacts
for them. See "Adding a league" in `docs/runbook.md`.
See `docs/data_sources.md` and `docs/match_evaluation_pipeline.md`.
Use `--season` on historical backtests only when you intentionally want to evaluate one
season; omit it to evaluate all loaded seasons inside `--from/--to`.
Use `--min-score` and `--max-picks-per-day` to make the policy selective enough to
say "no pick today"; use `backtest-policy-grid` to compare those gates before changing
production defaults. Use `--market-shrink-alphas` to test whether blending model
probabilities toward fair market probabilities improves historical robustness.
`market-benchmark-report` compares the model directly against fair market probabilities
by selection and probability buckets, while `analyze-policy-grids` consolidates policy
CSV results across leagues. `calibration-diagnostics` writes the deeper segmented
diagnostics needed before changing the model or recommendation score.

If you do not install the package, the same commands work with:

```powershell
$env:PYTHONPATH="src"
python -m scorerecord.cli init-db
```

## Critical Product Notes

This project should not start with "best bets". It should start with auditability: every recommendation must be traceable to source data, model version, market odds, timestamp, and later result. Otherwise the GUI may look convincing while the edge is unmeasurable.

The strongest early path is:

1. Data spine and aliases.
2. Baseline Elo plus Dixon-Coles goals model.
3. Backtesting and calibration using pre-match features only.
4. Frozen model manifests and settlement metrics for finished fixtures.
5. Raw odds ingestion, de-vig, and edge analysis.
6. Paper-only recommendations with small capped Kelly.
7. Forecast-only dashboard; the paper/edge audit view has been retired.
8. CLV tracking before any real-money staking.
9. Props, corners, cards, and player shots only after confidence badges and coverage checks exist.

## Safety

This is educational/research software. It estimates probabilities with uncertainty; it does not guarantee profit. Never commit API keys, SSH passwords, bankroll amounts, or bookmaker credentials.

## Full Project Notes

| Document | Use it for |
| --- | --- |
| [docs/critical-review.md](docs/critical-review.md) | Current audit: what is broken or weak, with evidence |
| [docs/improvement-plan.md](docs/improvement-plan.md) | Phased implementation plan |
| [docs/deployment.md](docs/deployment.md) | Homeserver layout, timers, exposure |
| [docs/runbook.md](docs/runbook.md) | Local setup, validation, deploy, troubleshooting |
| [docs/match_evaluation_pipeline.md](docs/match_evaluation_pipeline.md) | Which code produces each forecast |
| [docs/data_sources.md](docs/data_sources.md) | Providers, slugs, admission checklist |
| [docs/roadmap.md](docs/roadmap.md) | Phase history up to Phase 14 |
| [docs/project.md](docs/project.md) | Architecture notes and change log |
