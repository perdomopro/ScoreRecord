# ScoreRecord

ScoreRecord is an offline-first research project for the FIFA World Cup 2026. The first milestone is intentionally small: build a trustworthy local data spine before adding odds, recommendations, Kelly sizing, GUI, or heavier models.

## Current Scope

Fase 0-1 is implemented as a lightweight Python package:

- SQLite schema for teams, aliases, historical matches, tournament fixtures, predictions, recommendations, and metrics.
- Cached ingestion for `openfootball/worldcup.json`.
- Cached ingestion for `martj42/international_results`.
- Seed team aliases for common naming mismatches.
- Elo calculation over international results from 2018 onward.
- Pre/post-match Elo history for leakage-free walk-forward features.
- Basic run tracking and data quality logging.
- CLI entry points that can run locally or on the homeserver.

## Quick Start

```powershell
py -3.11 -m venv .venv
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
scorerecord market-benchmark-report --from 2021-08-01 --to 2025-05-31 --league EPL --min-train 500
scorerecord analyze-policy-grids "outputs/backtests/policy_grid_real_odds_*.csv"
scorerecord calibration-diagnostics --from 2021-08-01 --to 2025-05-31 --leagues EPL,LA_LIGA,SERIE_A,BUNDESLIGA,LIGUE_1,CHAMPIONSHIP --min-train 500
scripts/run_multileague_validation.sh
scorerecord calibration-report --from 2024-01-01 --to 2024-12-31
scorerecord gui --host 127.0.0.1 --port 8765
scorerecord db-summary
```

The read-only GUI opens on a forecast-only World Cup view. It reads from
`fixture`, `prediction`, `team`, and `model_artifact`, not from paper
recommendations:

```text
http://127.0.0.1:8765/today?date=2026-07-04
http://127.0.0.1:8765/match/<fixture_id>
http://127.0.0.1:8765/audit
```

The forecast view is deliberately `FORECAST_ONLY`: it shows calibrated goal-market
probabilities, principal forecasts by market family, model-implied prices, and
informational odds coverage for OU2.5/BTTS on match pages. It does not compute
paper candidates, stake sizing, or betting recommendations. The confidence label is
model magnitude only and is not validated against World Cup results or market
prices. The older paper/edge dashboard remains available at `/audit` for operational review.
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
7. Forecast-only GUI for World Cup probabilities; paper/edge rows stay in `/audit`.
8. CLV tracking before any real-money staking.
9. Props, corners, cards, and player shots only after confidence badges and coverage checks exist.

## Safety

This is educational/research software. It estimates probabilities with uncertainty; it does not guarantee profit. Never commit API keys, SSH passwords, bankroll amounts, or bookmaker credentials.

## Full Project Notes

See [docs/project.md](docs/project.md) for the full architecture, quality gates, and
change log. See [docs/roadmap.md](docs/roadmap.md) for the phased execution plan.
