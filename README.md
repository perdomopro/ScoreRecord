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
scorerecord fetch-odds --closing --closing-window-minutes 60 --dry-run
scorerecord compute-edge --dry-run
scorerecord recommend --dry-run
scorerecord compute-clv --dry-run
scorerecord calibrator-drift
scorerecord db-summary
```

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
7. CLV tracking before any real-money staking.
8. Props, corners, cards, and player shots only after confidence badges and coverage checks exist.

## Safety

This is educational/research software. It estimates probabilities with uncertainty; it does not guarantee profit. Never commit API keys, SSH passwords, bankroll amounts, or bookmaker credentials.

## Full Project Notes

See [docs/project.md](docs/project.md) for the full architecture, roadmap, quality gates, and change log.
