# ScoreRecord Technical Roadmap

Purpose: keep the project decomposable into small, safe increments. Each phase has an
objective, technical work, exit criteria, dependencies, and run-sized increments. Do not
advance a phase as trusted until the previous phase exit criteria are actually met.

## Current State - 2026-07-29

| Phase | State |
| --- | --- |
| 0 - Forward capture | Code ready; verify future fixtures with `kickoff_utc` before relying on timers. |
| 1 - Repo and DB stabilization | Remote full `pytest` is green; full-repo `ruff` legacy debt remains separate. |
| 2 - Evaluation and scoring | Code ready; no longer blocked by test execution, still not market-validated. |
| 3 - Club OU2.5 benchmark | Benchmark closed; model rejected for independent value. Phase 6 remains explicitly blocked. |
| 4 - Today forecast web | Forecast GUI deployed and verified with 6 future resolved fixtures and clean forecast quality gates. |
| 4.5 - Forecast recommendation UX | Implemented and deployed: principal-by-family forecasts, still no odds or value language. |
| 4.6 - Forecast snapshots | Implemented, deployed, migrated to schema 6, and remote-gated green. |
| 5.1 - Odds coverage display | Implemented, deployed, and remote-gated green. Informational coverage only. |
| 5.2 - Odds horizon coverage | Implemented, deployed, and remote-gated green. No EV or closing derivation. |
| 5.3 - Operational odds coverage summary | Implemented, deployed, and remote-gated green. Observability only. |
| 6 | Explicitly Blocked |
| 7 - Independent evaluation | Bypassed. Model rejected for independent value. |
| 8 - Market-anchored benchmark | Active path. Residual modeling. |
| 9 | Pending. |
| 13 - Daily multi-league forecast | Active for Liga MX, MLS, EPL, Argentina, and Brasileirao; daily timer enabled. |
| 14 - Result diagnostics and corners discovery | Active: 1X2/double-chance diagnostics plus SportMonks/TheStatsAPI corners probe. |

Open loops that remain after the remote gate:

- Full `pytest` is green on the homeserver as of 2026-07-04: 84 passed.
- Full `pytest` after fixture-resolution hardening is green on the homeserver: 88 passed.
- Full `pytest` after forecast snapshot hardening is green on the homeserver as of 2026-07-07: 98 passed.
- Full `pytest` after odds coverage display is green on the homeserver as of 2026-07-07: 106 passed.
- Full `pytest` after odds horizon coverage is green on the homeserver as of 2026-07-07: 118 passed.
- Phase 5.3 local gate: `compileall` and 11 manual coverage-summary tests passed.
- Phase 5.3 remote gate is green: focused tests passed `41 passed`, full suite
  passed `129 passed`, targeted `ruff` passed, deploy-time `db-health` passed,
  and CLI smoke passed for fixture/date `odds-coverage-summary`.
- Phase 3 benchmark was successfully run locally after reingesting Football-Data
  for the 6 club leagues (2021-2022 to 2024-2025). The model was rejected for
  independent value against fair market probabilities. Phase 6 (daily-goals-v1)
  is blocked. Proceed to Phase 8 (market-anchored residual model).
- Full-repo `ruff` is still open with legacy style findings; touched GUI files pass targeted `ruff`.
- `fixture-summary` after the 2026-07-08 refresh confirms 104 fixtures, 0 missing
  kickoff UTC, 3 future resolved, and 5 future unresolved. The 2026-07-09 match
  is resolved as France vs Morocco and is visible in the forecast GUI.
- Deploy-time `db-health` reports `recent_failed_runs: 0`.
- The homeserver `.venv` has the required dev tools for remote gates; pytest and
  targeted ruff both ran successfully.
- Local Phase 13.4.2 hardening on 2026-07-28: `compileall` passed, full pytest
  passed `241 passed`, targeted ruff over touched files passed, and
  `git diff --check` passed. Full-repo ruff still fails with legacy findings.
- Phase 13.4.2 production audit on 2026-07-29 returned `PASSED`,
  `CRITICAL=None`, and `ERROR=None`; only warning is the expected legacy
  World Cup artifact policy metadata.
- Daily multileague club refresh on 2026-07-29 returned `OK` for `LIGA_MX`,
  `MLS`, `EPL`, `ARGENTINA`, and `BRASILEIRAO`, with `unresolved_fixtures=0`
  for all five leagues.
- `scorerecord-club-refresh.timer` is enabled for daily fixture/model refresh;
  the old `scorerecord-epl-pilot.timer` is disabled to avoid duplicate work.
- Result probabilities are visible only as secondary diagnostics; OU2.5/BTTS
  remain the principal forecast families.
- Forecast GUI navigation was polished on 2026-08-02: `/` is a clean landing
  page, `/today` no longer repeats the global upcoming-fixture inventory, and
  `/history` owns upcoming loaded fixtures plus past loaded fixtures.
- Corners remain gated behind `provider-stats-probe` coverage and a future
  benchmark; no corners market is shown in the GUI yet.

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
- Current implementation supports:
  - `--market OU2.5`
  - `--leagues EPL,LA_LIGA,SERIE_A,BUNDESLIGA,LIGUE_1,CHAMPIONSHIP`
  - `--entry-odds avg_preclose`
  - `--closing-odds avg_close`
  - `--calibration prequential`
  - `--bootstrap-block week`
  - `--bootstrap-samples 2000`
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
3. Generalized benchmark loading. Done in code.
4. Prequential calibrated OU2.5 probabilities. Done in code.
5. Multi-league benchmark readout. Run completed. Status: REJECTED_FOR_INDEPENDENT_VALUE.

Next sequencing note:

Phase 3 is closed and the independent Dixon-Coles model was demonstrably rejected for independent value against fair market probabilities. Phase 6 (Paper Candidates) remains explicitly blocked and World Cup betting is NOT validated. 
The active research path WAS Phase 8.0 (Market-Anchored Residual Benchmark). However, the Phase 8 benchmark readout resulted in RESIDUAL_INCONCLUSIVE. The residual model did not improve upon `market_preclose` and was worse than the `offset-only` baseline. 
The practical decision is a no-go for Phase 6 paper candidates. We must now explore Phase 8.1 segmented diagnostics or Phase 9 Corners.

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
  - Label goal markets as 90-minute regulation-time forecasts.
  - Keep `/audit` clearly marked as deprecated legacy paper/audit mode.
  - Preserve manual fixture resolutions through placeholder-only reingests.
- Later paper path:
  - Add `daily_decision` and `daily_candidate`.
  - Add `build-daily-decisions --date <d> --mode forecast`.
  - Promote paper candidates only after the odds/CLV phases are ready.

Exit criteria:

- Page works without odds.
- Local kickoff time is correct.
- It does not imply a validated bet.

## Phase 4.5 - Forecast Recommendation UX

Objective: make the forecast web useful without crossing into betting recommendation
language.

Technical work:

- Show `Pronosticos principales del modelo`, never `Apuesta recomendada`.
- Select one principal per forecast family:
  - `GOLES`: best calibrated side on `OU2.5`, the line tied to the Phase 3
    benchmark, not the easiest high-probability OU line.
  - `BTTS`: best calibrated yes/no side.
- Keep the selector restricted to calibrated goal markets only: `OU1.5`, `OU2.5`,
  `OU3.5`, and `BTTS`. Do not let `1X2` or exact-score rows become principal
  forecasts.
- Display signal strength as distance from 50%, not as betting confidence or
  market edge.
- Classify principal binary markets by distance from 50%:
  `NO_SIGNAL` under 3 points, `LEAN` 3-8 points, `MODERATE` 8-15 points,
  and `STRONG` at 15+ points. Always keep the side and probability visible as
  diagnostics.
- Show a compact model-quality panel from latest `data_quality`: calibration sanity,
  OU monotonicity, fixture Elo coverage, and market-validation status.
- Keep `market_validation_status` visible as `NOT_TESTED` until market validation
  genuinely changes.

Exit criteria:

- A fixture can show independent principal forecasts for goals and BTTS.
- Probabilities close to 50% render as `Sin señal clara`, not as strong forecasts.
- Calibration, OU monotonicity, Elo coverage, and market-validation status are visible.
- No page uses EV, Kelly, stake, value, or betting recommendation language.
- Tests prove that an uncalibrated `1X2` row cannot be selected by the forecast UI.

## Phase 4.6 - Forecast Snapshot And Settlement

Objective: freeze what the forecast-only GUI showed before kickoff and settle it
later against final scores.

Technical work:

- Add `forecast_snapshot` with the frozen fixture, prediction run, artifact, market,
  selection, probability, signal code, model version, validation status, and
  settlement fields.
- Keep selection and signal policy in shared `forecast_policy.py`, not in the GUI.
- Scope quality-panel reads to the displayed `prediction_run_id`.
- Freeze quality context in `quality_status_json`.
- Add `freeze-forecast-snapshot --date YYYY-MM-DD` and `--today`.
- Use the same GUI selection rules: `OU2.5` for `GOLES` and `BTTS` yes/no. Do not
  freeze `1X2`, double chance, exact score, `OU1.5`, or `OU3.5`.
- Only freeze resolved, unstarted fixtures and keep snapshots idempotent by
  `fixture_id + prediction_run_id + market`.
- Add `settle-forecast-snapshots` for OU2.5 and BTTS final-score settlement.
- Add `forecast-report` with totals, pending rows, accuracy, Brier, and log-loss by
  market and signal code.
- Show frozen forecast rows in `/match`, including settlement outcome when present.

Exit criteria:

- Freeze creates exactly OU2.5 and BTTS rows for forecastable fixtures.
- Freeze skips started fixtures and is idempotent.
- Settlement handles OU2.5 over/under and BTTS yes/no.
- Report groups metrics by market and signal code.
- No odds, EV, Kelly, stake, value, or betting recommendation language is introduced.
- Remote pytest, ruff, and GUI smoke must pass before treating this phase as fully
  closed.

## Phase 5 - Serious Odds Layer

Objective: reliable, de-viggable snapshots.

Technical work:

- Keep `odds_snapshots` immutable.
- Add `/match` odds coverage for `OU2.5` and `BTTS` only.
- Report `NO_ODDS`, `INCOMPLETE_MARKET`, `STALE_ODDS`, and `COVERAGE_OK`.
- Require `line = 2.5` for `OU2.5`; legacy NULL-line rows do not count.
- Show latest capture time, age, bookmaker count, complete-bookmaker count, and best
  observed side odds. Label these as observed by side, not as a de-vig market pair.
- Add `external_event_map`.
- Derive horizon coverage after capture: T24H, T6H, T1H, T15M, and
  LAST_PRE_KICKOFF.
- Label LAST_PRE_KICKOFF as an observed pre-kickoff snapshot, not official closing
  odds.
- Require complete same-bookmaker two-way snapshots before de-vigging totals.
- Add `odds-coverage-report` and `odds-horizon-report`.
- Add `odds-coverage-summary` for fixture/date readiness labels over the horizon
  observations:
  - `READY_FOR_ANALYSIS`: last pre-kickoff coverage, near-kickoff coverage, and at
    least two complete bookmakers.
  - `PARTIAL_COVERAGE`: at least one complete horizon, but not enough for the
    ready label.
  - `WEAK_COVERAGE`: incomplete-only observations, or complete observations with
    too little bookmaker depth.
  - `NO_ODDS`: no horizon observations.

Exit criteria:

- V1 market scope is based on observed real coverage, not assumptions.
- Coverage display remains informational only: no EV, no paper candidate, no Kelly,
  no stake, and no value/recommendation language.
- Closing odds are not derived in this phase; horizon coverage is read-only
  observability over append-only `odds_snapshots`.
- `READY_FOR_ANALYSIS` is a data-quality label for later analysis only; it does
  not promote the fixture to the next product maturity state.

## Phase 6 - `daily-goals-v1` Paper Engine

Objective: turn forecasts and prices into deterministic paper candidates.

Dependency:

- Phase 3 OU2.5 benchmark must be read before Phase 6 shows paper EV. Phase 5 can
  collect quotes in parallel, but `daily-goals-v1` should not present edge on top of
  an unbenchmarked independent model.

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

- **Phase 10.0:** Shots/SOT Feasibility Audit — **CLOSED** (`V2_ELIGIBLE` for top leagues)
- **Phase 10.1:** Shots/SOT Process Rating Prototype — **CLOSED** (`PROCESS_PROMISING`. Goals V2 path proceeds.)
- **Phase 10.2:** Goals V2 Lambda Modeling — **CLOSED** (Strict weekly prequential, 0 external API calls)
- **Phase 10.3:** Market benchmark for Goals V2 — **CLOSED** (`MARKET_REJECTED`. Beaten by market preclose odds. No paper candidates unlocked.)
- **Phase 11.2.0:** Multi-book Sharp/Soft Coverage Audit — **CLOSED** (`NOT_FEASIBLE`. Local DB lacks sharp reference books. Betting paths remain explicitly blocked.)
- **Phase 12:** Recreational Forecast-only Framing — **CLOSED** (Web GUI reframed to emphasize model vs market without betting recommendations. Clean, neutral language used.)

## Phase 8 - Market-Anchored Residual Model (Status: CLOSED, RESIDUAL_INCONCLUSIVE)

Objective: if independent modeling loses, stop adding features blindly.

Technical work:

- Use `logit(p_final) = logit(p_market_at_decision) + residual_model(features)`.
- Treat Dixon-Coles outputs as features, not truth.
- Benchmark market-at-decision vs market-plus-residual vs closing market.

Readout (2026-07-11):
- `delta_residual_vs_preclose` = `0.000334` (CI: `[-0.000003, 0.000668]`).
- State: `RESIDUAL_INCONCLUSIVE`.
- Practical decision: no-go for Phase 6 paper candidates. The residual model did not improve `market_preclose` and was worse than offset-only.
- Phase 6 remains BLOCKED.
- Next options: Phase 8.1 segmented diagnostics or Phase 9 corners exploration.


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
6. Forecast recommendation UX
7. Odds coverage and consensus
8. daily-goals-v1 paper, only after Phase 3 readout
9. Forward validation
10. Residual market model / corners
```

Golden rule: keep infrastructure reusable, ship the useful forecast view early, and do
not confuse a reasonable forecast with a profitable strategy.


## Phase 13 - Daily Multi-League Forecast Pipeline

Objective: Expand ScoreRecord from a single World Cup tournament to a fully automated daily multi-league forecast pipeline.

### Phase 13.0 — Multi-League Forecast Readiness Audit
### Phase 13 — Multi-League Automation
- **13.0 Multi-League Readiness Audit** (CLOSED)
  - Readiness CLI and baseline gap analysis.
- **13.1 Fixture Schema + Provider Pilot** (CLOSED)
  - Safely migrated `fixture` schema (`league_code`, `competition`, `season`).
  - Evaluated and integrated Odds-API.io as the initial fixture provider for EPL pilot.
  - Implemented `ingest-fixtures-odds-api` dry-run mode for evaluation.
- **13.2-13.4 Multi-League Prediction Flow** (IN PROGRESS)
  - Established odds-api.io as the primary live fixture provider for the EPL pilot.
  - Implemented `provider-probe-api-football-fixtures` as a read-only secondary/backup provider probe for stable league/team/fixture IDs and stats.
  - Hardened authentication and error handling for provider polling logic.
  - Added `run-epl-pilot-refresh` for daily EPL fixture ingest, Elo rebuild,
    scoped prediction publishing, and post-run health status.
  - Hardened `predict-fixtures` so default publishing is scoped to
    `WORLD_CUP_2026`; EPL publishing must use `--league EPL`.
  - Added league-aware model artifact policies and calibrator scope metadata.
  - Added Phase 13.4.2 read-only audit script for artifact separation,
    prediction provenance, calibrator scope, DB health, readiness, and GUI smoke.
  - Phase 13.4.2 production audit passed on the homeserver after EPL artifact
    republish; remaining findings are warnings/infos, not critical errors.
  - Fixed deploy smoke false failure from `db-summary` exit handling.
  - Forecast GUI uses the latest prediction run per fixture, so `league=ALL`
    can display multiple scopes whose predictions were published at different
    times.
  - GUI league filtering now focuses on `ALL`, `LIGA_MX`, `MLS`, `EPL`,
    `ARGENTINA`, and `BRASILEIRAO`; the World Cup filter tab is removed from
    the primary forecast navigation.
  - Added a landing-only `/` page without the sticky navbar, with centered
    ScoreRecord branding, repo link, warning, and league/Health entry buttons.
  - Added next-fixture date links and upcoming-date chips so users do not have
    to click through empty days to find a loaded match.
  - Removed legacy `paper`/`research` links from the main forecast navbar so the
    GUI stays forecast-only while still exposing `/audit` as a technical view.
  - Added read-only SportMonks fixture probe for enrichment evaluation.
  - Homeserver validation on 2026-07-28: `LIGA_MX`, `MLS`, and `EPL` all reached
    `READY_FOR_PILOT` with future fixtures, zero unresolved fixtures, ratings
    coverage, and fresh prediction rows.
  - Forecast GUI now includes per-fixture data-quality score, heuristic
    uncertainty band, rule-based explanation factors, model evidence, explicit
    no-prediction reasons, and a compact operational health panel.
  - Forecast GUI polish on 2026-07-29 moved operational/model health into
    `/health`, simplified primary navigation, added the ScoreRecord landing hero,
    adopted the green brand palette (`#34CF72` / `#1C331D`), and changed match
    details to modal overlays instead of full-page navigation.
  - Added `ARGENTINA` and `BRASILEIRAO` as domestic pilot candidates backed by
    odds-api.io catalog slugs and Football-Data aliases.
  - Registered Champions League, Europa League, Libertadores, and Sudamericana
    as provider watchlist competitions only; they must not enter primary GUI
    forecasts until they have scoped historical data and model artifacts.
  - Added `scorerecord-club-refresh.service/timer` and
    `scripts/run_daily_club_forecast.sh` for a conservative daily fixture and
    forecast refresh loop.
  - Added `docs/data_sources.md` and `docs/match_evaluation_pipeline.md`.

### Phase 13.5 - Forward evidence and monitoring (NEXT)
- Accumulate forecast snapshots and settled outcomes before claiming real
  performance.
- Build a performance page from `forecast_snapshot`/`prediction_result` with
  accuracy, Brier, logloss, calibration, skipped fixtures, and reasons by league
  and market.
- Add drift monitoring only after there is a stable baseline distribution per
  league/market. The detector should compare weekly prediction distributions,
  Elo distribution, calibrator status, and no-prediction rates.
- Keep benchmark/performance labels separate from betting value. No ROI/CLV
  claims without real captured odds and forward settlement.

### Future 13.X
- Promote `ARGENTINA` and `BRASILEIRAO` only after homeserver readiness confirms
  future fixtures, aliases, ratings, and prediction provenance.
- Add continental competition forecasts only after a separate cup/continental
  training policy exists.
- Add GUI competition filters to navigate the multi-league data cleanly.

Conditions for inclusion:
Only include a league if future fixtures are ingested, teams can be mapped, model ratings exist, predictions can be generated, and the GUI can display them cleanly.
