# ScoreRecord match evaluation pipeline

This document lists the code paths and scripts that contribute to each match
forecast shown in the GUI.

ScoreRecord's public forecast GUI must remain forecast-only: no EV, Kelly,
staking, or betting recommendation language belongs in the main product view.

## End-to-end flow

```text
Provider fixtures/history
  -> canonical teams and aliases
  -> match_history
  -> Elo + rating history
  -> league-scoped Dixon-Coles artifact
  -> fixture predictions
  -> forecast quality and explanations
  -> GUI /today and /match
  -> optional forecast snapshots
  -> settlement and reports after results
```

## Ingestion and fixture sources

| Command/script | Module | Purpose | Writes |
| --- | --- | --- | --- |
| `scorerecord ingest-football-data` | `src/scorerecord/ingest/football_data.py` | Loads Football-Data historical/current CSV rows and odds columns. | `historical_match`, `historical_odds`, `ingest_run`, `data_quality` |
| `scorerecord bridge-club-history` | `src/scorerecord/ingest/bridge.py` | Maps provider club names into canonical teams and inserts scored matches. | `team`, `team_alias`, `match_history` |
| `scorerecord ingest-fixtures-odds-api-io` | `src/scorerecord/ingest/fixtures_odds_api_io.py` | Fetches future fixtures from odds-api.io league slugs. | `fixture` |
| `scorerecord run-club-pilot-refresh` | `src/scorerecord/ingest/epl_pilot_refresh.py` | Controlled per-league refresh: health check, fixture ingest, Elo rebuild, prediction publish. | `fixture`, `team_rating`, `team_rating_history`, `prediction`, `model_artifact` |
| `scripts/run_daily_club_forecast.sh` | shell script | Daily multi-league wrapper around `run-club-pilot-refresh`. | Same as per-league refresh |
| `provider-probe-sportmonks-fixture` | `src/scorerecord/ingest/sportmonks_probe.py` | Read-only SportMonks fixture/enrichment probe. | None by default |
| `provider-probe-api-football-fixtures` | `src/scorerecord/ingest/api_football_probe.py` | Read-only API-Football fixture/team probe. | None by default |

## Ratings and feature construction

| Command/module | Code | Purpose |
| --- | --- | --- |
| `scorerecord build-elo` | `src/scorerecord/features/elo.py` | Rebuilds Elo snapshots and pre/post rating history from `match_history`. |
| `load_match_features` | `src/scorerecord/features/build.py` | Builds leakage-free historical features using pre-match Elo. |
| `_fixture_as_match_feature` | `src/scorerecord/predict/fixtures.py` | Converts a future fixture into model input using latest team ratings and league home advantage. |

## Model and prediction

| Command/module | Code | Purpose | Guardrail |
| --- | --- | --- | --- |
| `scorerecord predict-fixtures --league <CODE>` | `src/scorerecord/predict/fixtures.py` | Trains/fetches a league-scoped Dixon-Coles artifact and writes markets. | Fixture league must match artifact league. |
| `DixonColes` | `src/scorerecord/models/dixon_coles.py` | Generates score matrix and goal-market probabilities. | Uses league-scoped training rows. |
| `PlattCalibrator` | `src/scorerecord/models/calibration.py` | Calibrates binary goal markets where enough historical rows exist. | Calibrator scope stored in artifact JSON. |
| `select_primary_forecasts` | `src/scorerecord/forecast_policy.py` | Chooses OU2.5 GOALS principal and BTTS principal. | Excludes 1X2, exact score, double chance, OU1.5, OU3.5 from principal selection. |
| `signal_strength` | `src/scorerecord/forecast_policy.py` | Labels `NO_SIGNAL`, `LEAN`, `MODERATE`, `STRONG`/display bands. | Magnitude label only; not betting confidence. |

## Quality, evidence, and GUI

| Module | Purpose |
| --- | --- |
| `src/scorerecord/forecast_quality.py` | Reads `data_quality` rows for calibration sanity, OU monotonicity, and Elo coverage. |
| `src/scorerecord/forecast_snapshots.py` | Freezes pre-kickoff GUI principal forecasts and settles them later. |
| `src/scorerecord/odds_coverage.py` | Summarizes current odds coverage per fixture for match detail pages. |
| `src/scorerecord/odds_horizons.py` | Summarizes pre-kickoff odds capture horizons. |
| `src/scorerecord/health.py` | Database health checks used by deployment and `/health`. |
| `src/scorerecord/gui.py` | Renders landing page `/`, forecast list `/today`, match modal `/match/<id>`, and health `/health`. |

## Evaluation and reports

| Command | Module | Purpose |
| --- | --- | --- |
| `scorerecord forecast-report` | `src/scorerecord/forecast_snapshots.py` | Accuracy, Brier, log-loss by market/signal from frozen snapshots. |
| `scorerecord settle-forecast-snapshots` | `src/scorerecord/forecast_snapshots.py` | Settles frozen OU2.5/BTTS forecast snapshots. |
| `scorerecord historical-odds-report --market OU2.5` | `src/scorerecord/evaluate/historical_odds.py` | Audits Football-Data historical odds coverage and overround. |
| `scorerecord market-benchmark-report` | `src/scorerecord/evaluate/backtest.py` | Compares model probabilities against fair historical market probabilities. |
| `scorerecord calibration-diagnostics` | `src/scorerecord/evaluate/calibration_diagnostics.py` | Segmented calibration diagnostics before changing models/policies. |
| `scorerecord multi-league-readiness-report` | `src/scorerecord/ingest/readiness.py` | Checks if each league has history, fixtures, ratings, and predictions. |
| `scripts/run_phase13_audit.py` | audit script | Full production integration audit for artifact separation, provenance, GUI, health, and readiness. |

## Operational cadence

Recommended minimal cadence on the homeserver:

| Cadence | Command/timer | Reason |
| --- | --- | --- |
| Daily early morning | `scorerecord-club-refresh.timer` | Pick up new fixtures at least several days ahead and regenerate forecast-only predictions. |
| Hourly when fixtures are near | `scorerecord-odds.timer` | Keep odds coverage snapshots fresh for informational coverage. |
| Pre-kickoff window | `scorerecord-preplay-odds.timer` | Capture last pre-kickoff market snapshots for later analysis. |
| Postmatch | `scorerecord-postmatch.timer` | Ingest/settle results and frozen forecast snapshots. |
| Weekly | `scorerecord-weekly.timer` | Run diagnostics, drift checks, coverage summaries, and readiness reports. |

Daily refresh is enough for fixture discovery because odds-api.io returns many
pending events beyond the next three days. Hourly polling should be reserved for
odds freshness, not full model rebuilds.
