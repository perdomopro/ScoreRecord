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

As of 2026-11-13 only the first half of this flow runs automatically. The daily
job goes from provider fixtures to GUI predictions; nothing brings results back
in, so `match_history` stops in May-July 2026, snapshots are not frozen, and
settlement has nothing to settle. Details in
[critical-review.md](critical-review.md).

```text
Automated daily:   provider fixtures -> Elo -> Dixon-Coles artifact -> predictions -> GUI
Manual only:       Football-Data results -> historical_match -> bridge -> match_history
Not wired:         fixture scores, forecast snapshot freeze, club odds capture
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
| `scorerecord predict-fixtures --league <CODE>` | `src/scorerecord/predict/fixtures.py` | Retrains a league-scoped Dixon-Coles artifact on every call and writes markets for every future fixture. | Fixture league must match artifact league. Training rows are cut at `TRAINING_POLICY.trained_until`, a fixed `2026-06-11` for all scopes. |
| `DixonColes` | `src/scorerecord/models/dixon_coles.py` | Generates score matrix and goal-market probabilities. | Uses league-scoped training rows. |
| `PlattCalibrator` | `src/scorerecord/models/calibration.py` | Calibrates binary goal markets where enough historical rows exist. | Calibrator scope stored in artifact JSON. |
| `select_primary_forecasts` | `src/scorerecord/forecast_policy.py` | Chooses OU2.5 GOALS principal and BTTS principal. | Excludes 1X2, exact score, double chance, OU1.5, OU3.5 from principal selection. |
| `signal_strength` | `src/scorerecord/forecast_policy.py` | Labels `NO_SIGNAL`, `LEAN`, `MODERATE`, `STRONG`/display bands. | Magnitude label only; not betting confidence. |
| `Resultado estimado` | `src/scorerecord/gui.py` | Shows secondary 1X2 and double-chance diagnostics from the same score matrix. | Not a primary forecast; league 1X2 calibration remains diagnostic. |

## Quality, evidence, and GUI

| Module | Purpose |
| --- | --- |
| `src/scorerecord/forecast_quality.py` | Reads `data_quality` rows for calibration sanity, OU monotonicity, and Elo coverage. |
| `src/scorerecord/forecast_snapshots.py` | Freezes pre-kickoff GUI principal forecasts and settles them later. |
| `src/scorerecord/forecast_performance.py` | Aggregates settled `prediction_result` rows into top-pick accuracy, Brier, and log-loss by market and league. |
| `src/scorerecord/odds_coverage.py` | Summarizes current odds coverage per fixture for match detail pages. |
| `src/scorerecord/odds_horizons.py` | Summarizes pre-kickoff odds capture horizons. |
| `src/scorerecord/health.py` | Database health checks used by deployment and `/health`. |
| `src/scorerecord/gui.py` | Renders landing `/`, forecast list `/today`, top signals `/signals`, match modal `/match/<id>`, loaded fixtures `/history`, performance `/performance`, market context `/market`, and health `/health`. |

## Evaluation and reports

| Command | Module | Purpose |
| --- | --- | --- |
| `scorerecord forecast-report` | `src/scorerecord/forecast_snapshots.py` | Accuracy, Brier, log-loss by market/signal from frozen snapshots. |
| `scorerecord settle-forecast-snapshots` | `src/scorerecord/forecast_snapshots.py` | Settles frozen OU2.5/BTTS forecast snapshots. |
| `scorerecord forecast-performance-report --record-quality` | `src/scorerecord/forecast_performance.py` | Summarizes settled prediction performance and optionally persists the weekly evidence in `data_quality`. |
| `scorerecord historical-odds-report --market OU2.5` | `src/scorerecord/evaluate/historical_odds.py` | Audits Football-Data historical odds coverage and overround. |
| `scorerecord market-benchmark-report` | `src/scorerecord/evaluate/backtest.py` | Compares model probabilities against fair historical market probabilities. |
| `scorerecord calibration-diagnostics` | `src/scorerecord/evaluate/calibration_diagnostics.py` | Segmented calibration diagnostics before changing models/policies. |
| `scorerecord multi-league-readiness-report` | `src/scorerecord/ingest/readiness.py` | Checks if each league has history, fixtures, ratings, and predictions. |
| `scorerecord one-x-two-calibration-report` | `src/scorerecord/evaluate/one_x_two_calibration.py` | League-scoped walk-forward 1X2 diagnostics for Dixon-Coles vs baselines. |
| `scorerecord provider-stats-probe` | `src/scorerecord/ingest/provider_stats_probe.py` | Read-only SportMonks/TheStatsAPI coverage probe for stats and corners. |
| `scripts/run_phase13_audit.py` | audit script | Full production integration audit for artifact separation, provenance, GUI, health, and readiness. |

## Operational cadence

Cadence on the homeserver and what each timer actually achieves today:

| Cadence | Command/timer | Intended reason | Observed 2026-11-13 |
| --- | --- | --- | --- |
| Daily 05:35 | `scorerecord-club-refresh.timer` | Pick up new fixtures and regenerate forecast-only predictions. | Works for leagues with provider events. Retrains on unchanged data. |
| Hourly | `scorerecord-odds.timer` | Keep odds coverage snapshots fresh. | `matched_events=0`; queries the World Cup slug. |
| Pre-kickoff window | `scorerecord-preplay-odds.timer` | Capture last pre-kickoff market snapshots. | Not enabled. |
| Daily 23:30 | `scorerecord-postmatch.timer` | Settle predictions and frozen snapshots from local scores. | `settled=0`; club fixtures never get scores. |
| Sunday 09:00 | `scorerecord-weekly.timer` | Persist performance evidence, gates, drift checks. | Runs. Drift compares runs from different leagues and raises false alerts. |

Daily refresh is enough for fixture discovery because odds-api.io returns many
pending events beyond the next three days. Hourly polling should be reserved for
odds freshness, not full model rebuilds.

## Active forecast leagues

As of the 2026-07-29 homeserver validation, the forecast-only GUI is backed by
league-scoped refreshes for:

| League code | GUI label | Status 2026-07-29 | Future fixtures 2026-11-13 | History ends |
| --- | --- | --- | --- | --- |
| `LIGA_MX` | Liga MX | Active forecast pilot | 10 | 2026-07-27 |
| `MLS` | MLS | Active forecast pilot | 0 | 2026-07-26 |
| `EPL` | Premier League | Active forecast pilot | 151 | 2026-05-24 |
| `ARGENTINA` | Argentina | Active forecast pilot | 0 (provider returns no events) | 2026-07-26 |
| `BRASILEIRAO` | Brasileirao | Active forecast pilot | 41 | 2026-07-26 |

Continental competitions such as Champions League, Europa League, Libertadores,
and Sudamericana are provider-watchlist competitions only until ScoreRecord has
explicit historical scope, artifact policy, alias coverage, and readiness gates
for each one.

## Corners gate

Corners must not be modeled from the goal matrix. The accepted path is:

1. Run `provider-stats-probe` for SportMonks and TheStatsAPI across active GUI
   leagues.
2. Accept a league for corners only when recent finished-match coverage is
   `CORNERS_COVERAGE_OK` from at least one provider and provider IDs can be
   mapped safely.
3. Populate the existing `fixture_stats` table only after coverage is accepted.
4. Build a simple league/team/home-away corners baseline and benchmark it before
   adding markets such as `corners_over_8.5`, `corners_over_9.5`, or team
   corners to the GUI.
