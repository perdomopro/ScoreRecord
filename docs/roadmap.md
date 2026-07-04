# ScoreRecord Technical Roadmap

Purpose: keep the project decomposable into small, safe increments. Each phase has an
objective, technical work, exit criteria, dependencies, and run-sized increments. Do not
advance a phase as trusted until the previous phase exit criteria are actually met.

## Current State - 2026-07-04

| Phase | State |
| --- | --- |
| 0 - Forward capture | Code ready; verify future fixtures with `kickoff_utc` before relying on timers. |
| 1 - Repo and DB stabilization | Code ready; formal gate still pending remote `pytest` plus `ruff`. |
| 2 - Evaluation and scoring | Code ready; quarantined until the formal gate is green. |
| 3 - Club OU2.5 benchmark | Coverage prep implemented; blocked until coverage readout and green gate. |
| 4 - Today forecast web | Slim forecast-only GUI deployed and verified; blocked for fresh cards by 0 future resolved fixtures. |
| 5-9 | Pending. |

Open loops that close Phase 0/1 when remote execution is available:

- Full `pytest` is green on the homeserver as of 2026-07-04: 84 passed.
- Full-repo `ruff` is still open with legacy style findings; touched GUI files pass targeted `ruff`.
- Run `fixture-summary` and confirm remaining World Cup fixtures have `kickoff_utc`.
- Run `db-failures --days 7` and explain the recent failed run.
- Confirm the homeserver `.venv` has dev tools or install `.[dev]`.

## Non-Negotiables

- Paper only until forward validation.
- Forecast is not validated betting edge.
- `odds_snapshots` is append-only.
- Capture real odds now; missed snapshots cannot be recovered later.
- No value label without forward CLV.
- No retuning inside the evaluation window.

Product maturity:

```text
FORECAST_ONLY -> PAPER_CANDIDATE -> VALIDATED_RECOMMENDATION
```

`VALIDATED_RECOMMENDATION` is blocked until forward evidence exists.

Market validation status:

```text
NOT_TESTED -> REJECTED / INCONCLUSIVE / PROMISING / FORWARD_VALIDATED
```

## Phase 0 - Forward Capture

Objective: accumulate real odds without contaminating closing-price semantics.

Technical work:

- Inspect systemd timers and unit `ExecStart` before disabling anything.
- Keep hourly odds capture; keep automated recommendations off unless intentionally reviewed.
- Back up the remote DB before capture changes and record SHA-256.
- Capture raw snapshots, never write capture-time rows as closing truth.

Exit criteria:

- New `odds_snapshots` rows exist with matched fixture ids, bookmakers, and markets.
- Old edge/recommendation rows are not reused for new paper picks.

Run-sized increments:

1. Verify timers.
2. Backup and hash.
3. `fetch-odds --dry-run`.
4. Real capture.
5. `odds-snapshot-report`.

## Phase 1 - Repo and DB Stabilization

Objective: honest, reproducible foundations.

Technical work:

- Fix stale-sensitive tests and misaligned inserts.
- Use versioned migrations plus `db-version`, `migrate-db`, and `db-health`.
- Use shared UTC parsing and backfill `fixture.kickoff_utc`.
- Persist `model_artifact` and `prediction.artifact_id`.
- Separate `predictive_validation_run_id` from `market_validation_status`.
- Keep training cutoff in `ModelTrainingPolicy`.
- Make `predict-fixtures` forward-only.

Exit criteria:

- Full `pytest` green.
- `ruff` clean.
- `PRAGMA integrity_check = ok`.
- `PRAGMA foreign_key_check = 0`.
- `kickoff_utc` populated for actionable fixtures.
- Migrations idempotent.

Run-sized increments:

1. Migrations and DB commands.
2. UTC parser and kickoff backfill.
3. Model artifact.
4. Validation vocabulary.
5. Gate green.

## Phase 2 - Evaluation Scoring

Objective: correct statistical units before benchmarks.

Contract:

- 1X2 is one multiclass row per match.
- 1X2 log-loss is `-ln(p_actual)` with log-loss clipping.
- 1X2 Brier is the sum over home/draw/away.
- Multiclass probabilities are normalized before metrics.
- OU and BTTS are one binary row per match, using the positive side: `over` or `yes`.
- De-vig uses normalized inverse odds for both two-way and three-way markets.
- Model-vs-market comparisons are paired on the same matches.
- Bootstrap resamples blocks, not individual rows.
- Club blocks are `league:ISO-week`; World Cup blocks are match dates.
- Report both `num_matches` and `num_selection_rows`.

Exit criteria:

- Scoring tests cover multiclass, binary, normalization, blocked wrong units, and block bootstrap.
- Settlement, walk-forward, goal markets, and benchmark reports use the shared scoring helpers.

Run-sized increments:

1. Multiclass formulas and normalization.
2. Binary formulas.
3. Block bootstrap.
4. Wire callers.
5. Tests.

## Phase 3 - Club OU2.5 Benchmark

Objective: sanity-check whether Dixon-Coles contributes against real totals markets in
covered club leagues. This does not validate World Cup profitability.

Technical work:

- Ingest Football-Data OU2.5 quotes as complete two-way markets:
  - Entry/preclose: `Avg>2.5` and `Avg<2.5`, with aliases such as `BbAv>2.5`.
  - Close: `AvgC>2.5` and `AvgC<2.5`, with aliases such as `BbAvC>2.5`.
  - Internal names: `avg_preclose` and `avg_close`.
  - Persist `metadata_json.entry_column_family` to segment `Avg` versus `BbAv`.
- Use `historical-odds-report --market OU2.5` before benchmarking:
  - Check complete two-way rate.
  - Check close-complete rate separately.
  - Check entry and close overround ranges.
  - Treat suspicious overround warnings as ingest/parsing blockers.
- Expand coverage carefully:
  - Liga MX, MLS, Brazil Serie A, and Argentina Liga Profesional use Football-Data
    `/new/*.csv` current feeds, not season-versioned archive URLs.
  - Require one explicit `--season` label and reject season-range ingestion.
  - Accept each league for market validation only if coverage is complete enough.
  - Use leagues with poor odds coverage for forecasting/product only.
- Generalize `market-benchmark-report` after coverage is verified.
- Use one binary observation per match.
- De-vig two-way odds from the same bookmaker/snapshot family.
- Add prequential calibration only from out-of-sample prior predictions.

Target command:

```bash
scorerecord market-benchmark-report \
  --market OU2.5 \
  --from 2021-08-01 \
  --to 2025-05-31 \
  --leagues EPL,LA_LIGA,SERIE_A,BUNDESLIGA,LIGUE_1,CHAMPIONSHIP \
  --entry-odds avg_preclose \
  --closing-odds avg_close \
  --calibration prequential \
  --bootstrap-block week \
  --bootstrap-samples 2000
```

Expected outputs:

- `model_calibrated_logloss`
- `market_preclose_logloss`
- `market_close_logloss`
- `delta_calibrated_vs_preclose`
- `delta_calibrated_vs_close`
- Brier, ECE, and confidence intervals for paired deltas
- `benchmark_state`

Interpretation:

- `PROMISING`: calibrated model beats preclose with stable block CI below zero.
- `INCONCLUSIVE`: CI includes zero or varies too much by league.
- `REJECTED_FOR_INDEPENDENT_VALUE`: clearly worse; move to market-anchored residual modeling.

Exit criteria:

- OU2.5 coverage report proves complete two-way markets.
- Benchmark report has block CI and a justified `benchmark_state`.
- No policy grid or threshold sweep before the frozen benchmark spec runs.

Run-sized increments:

1. `extract_ou25_quotes`.
2. OU2.5 coverage report.
3. Generalized benchmark loading.
4. Prequential calibrated OU2.5 probabilities.
5. Multi-league benchmark readout.

## Phase 4 - Today Forecast Web

Objective: usable visual product that does not depend on financial validation.

Technical work:

- Current slim path:
  - Query from `fixture`, `prediction`, `team`, and `model_artifact`, not `recommendation`.
  - Show forecast states: `SIN DATOS`, `POR DEFINIR`, `SIN PRONOSTICO`,
    `DATOS INCONSISTENTES`, and `FORECAST_ONLY`.
  - Use `SCORERECORD_DISPLAY_TZ=America/Mexico_City` by default.
  - Add `/today` and `/match/<fixture_id>`.
  - Move the older paper/edge audit surface to `/audit`.
  - Show only calibrated goal markets (`OU1.5`, `OU2.5`, `OU3.5`, `BTTS`).
  - Hide goal markets when OU monotonicity fails.
  - Avoid EV, Kelly, stake, edge, or recommendation language in the forecast view.
- Later paper path:
  - Add `daily_decision` and `daily_candidate`.
  - Add `build-daily-decisions --date <d> --mode forecast`.
  - Promote paper candidates only after the odds/CLV phases are ready.

Exit criteria:

- Page works without odds.
- Local kickoff time is correct.
- It does not imply a validated bet.

## Phase 5 - Serious Odds Layer

Objective: reliable, de-viggable snapshots.

Technical work:

- Keep `odds_snapshots` immutable.
- Add `external_event_map`.
- Derive horizons after capture: T-24H, T-6H, T-1H, T-15M, close.
- Require complete same-bookmaker two-way snapshots before de-vigging totals.
- Add `odds-coverage-report`.

Exit criteria:

- V1 market scope is based on observed real coverage, not assumptions.

## Phase 6 - `daily-goals-v1` Paper Engine

Objective: turn forecasts and prices into deterministic paper candidates.

Frozen initial policy:

```json
{
  "policy_version": "daily-goals-v1",
  "paper_only": true,
  "markets": ["OU1.5", "OU2.5", "OU3.5", "BTTS"],
  "min_forecast_probability": 0.60,
  "min_paper_ev": 0.03,
  "max_paper_ev": 0.15,
  "min_decimal_odds": 1.20,
  "max_decimal_odds": 3.50,
  "max_snapshot_age_minutes": 90,
  "min_complete_bookmakers": 2,
  "max_picks_per_fixture": 1,
  "max_picks_per_day": 3,
  "max_same_direction_per_day": 2,
  "stake_mode": "flat",
  "stake_units": 1.0,
  "allow_validated_label": false
}
```

Exit criteria:

- Same input gives same decision.
- Every exclusion has a reason code.
- Still paper only.

## Phase 7 - Settlement and Forward Validation

Objective: measure forward CLV and ROI honestly.

Technical work:

- Add `daily_pick_result`.
- Store `clv_log = ln(entry / closing)`.
- Store `clv_fair_ev = entry * closing_fair_prob - 1`.
- Track predictive metrics and economic metrics.
- Bootstrap by day/slate.
- Pre-register evaluation windows before measuring.

Maturity:

- `EARLY`: fewer than 50 picks or fewer than 10 days.
- `EVALUATING`: 50+ picks, inconclusive CI.
- `PROMISING`: 100+ picks, 20+ days, 90% CLV CI above zero.
- `FORWARD_VALIDATED`: 200+ picks, 95% CI above zero, stable by market, no retuning.

## Phase 8 - Market-Anchored Residual Model

Objective: if independent modeling loses, stop adding features blindly.

Technical work:

- Use `logit(p_final) = logit(p_market_at_decision) + residual_model(features)`.
- Treat Dixon-Coles outputs as features, not truth.
- Alternative target: predict fair closing probability from earlier market prices plus features.
- Benchmark market-at-decision vs market-plus-residual vs closing market.

## Phase 9 - Corners

Objective: separate secondary model after goals workflow is measured end to end.

Technical work:

- Start with `corners-coverage-report`.
- Use Negative Binomial, not Dixon-Coles.
- Add corner-specific history, features, artifacts, predictions, odds, and validation tables.
- Validate forward because historical corner odds coverage is limited.

## Global Order

```text
1. Forward capture
2. Stabilization and migrations
3. Scoring contract
4. Club OU2.5 benchmark
5. Forecast web
6. Odds coverage and consensus
7. daily-goals-v1 paper
8. Forward validation
9. Residual market model / corners
```

Golden rule: keep infrastructure reusable, ship the useful forecast view early, and do
not confuse a reasonable forecast with a profitable strategy.
