# ScoreRecord Project

## Purpose

ScoreRecord is an offline-first quantitative football research system focused on the FIFA World Cup 2026. Its goal is to estimate match probabilities, evaluate model calibration, compare probabilities against market odds, and eventually recommend only value bets that pass explicit confidence and bankroll gates.

This is research software, not a profit guarantee. Every output must be auditable: source data, model version, run id, timestamp, market odds, recommendation logic, and later evaluation.

## Current Architecture

The project is a lightweight Python package backed by SQLite.

Core layers:

- `ingest`: loads public football data into normalized tables.
- `features`: builds leak-free pre-match features from historical data.
- `models`: holds baselines and future statistical models.
- `evaluate`: runs walk-forward backtests and writes metrics.
- `quality`: reports data quality signals before modeling.
- `runs`: records provenance in `ingest_run` and `data_quality`.
- SQLite schema: stores source data, ratings, predictions, recommendations, metrics, and quality logs.

The homeserver target is low memory, so the design avoids heavy services. Jobs should run in batch. The future GUI should read precomputed JSON/SQLite outputs and avoid live model computation.

## Data Sources

Implemented:

- `openfootball/worldcup.json`: World Cup 2026 fixture seed.
- `martj42/international_results`: historical international results and goalscorers from 2018 onward.
- `odds-api.io`: raw odds ingestion adapter and paper-analysis pipeline. Real API fetch is gated by `ODDS_API_IO_KEY`; dry-run is safe and does not spend requests.

Planned:

- API-Football: match stats such as shots, corners, cards, and possession.
- BALLDONTLIE or equivalent: player props, subject to coverage verification.

## Database Responsibilities

Important tables:

- `team`, `team_alias`: canonical teams and aliases with folded keys for case/accent-insensitive matching.
- `match_history`: historical international matches.
- `goal_history`: historical goals, including own-goal and penalty flags.
- `scorer_goal_history`: view over `goal_history` excluding own goals.
- `fixture`: World Cup 2026 fixtures.
- `team_rating`: current Elo snapshot per team.
- `team_rating_history`: pre/post-match Elo history by team and historical match.
- `odds`: raw bookmaker prices, stored before de-vig or recommendation logic.
- `odds_snapshots`: append-only live price snapshots; closing prices are derived from complete snapshots rather than written as capture-time truth.
- `edge_analysis`: model probability versus de-vigged market probability, raw implied probability, overround, odds quality diagnostics, EV edge, fixture Elo difference, close-match flag, high-edge reason, and conservative recommendation score.
- `model_artifact`: reproducible model artifact manifest with training cutoff, training rows, parameters, calibrators, validation run provenance, market-validation status, and git SHA.
- `prediction`: future model outputs for tournament fixtures.
- `prediction_result`: settled prediction labels and scoring metrics for finished fixtures.
- `recommendation`: future value-bet recommendations.
- `recommendation_result`: paper-recommendation CLV against closing odds.
- `eval_metric`: backtest metrics such as Brier and log-loss, linked to `ingest_run`.
- `ingest_run`: job provenance, status, source, and git SHA when available.
- `data_quality`: quality metrics tied to runs.
- `schema_migration`: idempotent schema-version ledger for operational migrations.

## Modeling Roadmap

Fase 0-1 is the data spine:

- Build schema.
- Ingest fixtures and historical results.
- Normalize team aliases.
- Build Elo snapshot and Elo history.
- Report data quality.

Fase 1.5 is traceability and backtesting harness:

- Record run provenance.
- Build pre-match features from `team_rating_history.rating_pre`.
- Run expanding-window walk-forward evaluation.
- Establish required baselines.

Fase 2 is match modeling:

- Add Dixon-Coles MLE/optimizer.
- Compare it against baselines in the same walk-forward harness.
- Write validated tournament predictions.

Fase 3 is richer modeling:

- Bayesian shrinkage where hardware allows.
- XGBoost or other calibrated models if they beat baselines.
- Ensemble weighting based on evaluation, not vibes.

Fase 4 is odds and value:

- Ingest odds snapshots.
- De-vig market probabilities.
- Compute model edge without recommending.
- Generate paper-only recommendations after edge checks.
- Track CLV against closing odds before any real staking.
- Gate recommendations by model calibration, edge threshold, freshness, confidence, and sustained CLV.
- Size paper recommendations with capped fractional Kelly.

Fase 5 is props and GUI:

- Add corners, cards, player shots, and scorer markets only when coverage is verified.
- Build FastAPI/Jinja2/HTMX GUI that reads precomputed outputs.

Fase 6 is deployment:

- Use SSH keys and rotated secrets.
- Run systemd timers with `MemoryMax`, `Nice`, and single-job scheduling.
- Deploy to `/home/adrian/scorerecord` on the homeserver with a dedicated SSH key, not password automation.

## Current Commands

```bash
scorerecord init-db
scorerecord migrate-db
scorerecord db-version
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
scorerecord compute-edge --dry-run
scorerecord recommend --dry-run
scorerecord compute-clv --dry-run
scorerecord db-summary
```

## Quality Gates

Current checks:

- Fixture resolution: distinguishes real unresolved aliases from expected knockout placeholders.
- Historical alias coverage: reports historical teams backed by seed aliases versus auto-created teams.
- Duplicate canonical keys: detects case/accent folding collisions that would require a real merge.

Current expected state:

- 104 World Cup fixtures.
- Unresolved fixtures are expected knockout placeholders until the bracket is known.
- Historical auto-created teams are expected because the historical dataset contains many non-World-Cup nations.
- Duplicate canonical keys should be zero.

Latest local quality report:

- Fixture resolution: 72/104 fully resolved after excluding unresolved group-position and knockout placeholders.
- Unresolved fixtures are knockout placeholders (`W73`, `L101`, `1B`, `2C`, `3A/B/C/D/F`, etc.).
- Historical aliases: 43/284 historical teams seed-backed, 241 auto-created.
- Duplicate canonical keys: 0.

## Baselines

The first required baselines are:

- `baseline_uniform`: permanent sanity floor at 1/3 for home/draw/away.
- `baseline_marginal`: predicts training-set class frequencies for home/draw/away.
- `baseline_logistic`: stdlib multinomial logistic regression over `elo_diff` and `home_field`.

Any future model must beat these in walk-forward log-loss before it is trusted.

Fase 2 candidate:

- `dixon_coles`: numpy-only bivariate Poisson goals model with ridge shrinkage, tuned 3-year time-decay half-life, home advantage, and low-score Dixon-Coles correction.
- `tune-decay`: sweeps Dixon-Coles half-life values and selects the winner by close-match log-loss.
- `goal-markets`: evaluates Dixon-Coles-derived OU and BTTS probabilities against binary marginal baselines.
- `predict-fixtures`: writes Dixon-Coles 1X2, OU, BTTS, double-chance, and top exact-score predictions only for resolved fixtures that have not started.
- `model-manifest`: prints the frozen production-candidate manifest and latest `model_artifact_id`.
- `settle-fixtures`: scores finished fixtures, guarded against post-kickoff backfill by default.

Frozen production-candidate manifest:

- `model_version`: `dixon_coles:hl3y:platt-goals-v1`.
- Base model: Dixon-Coles with 3-year half-life (`xi=0.000633011`).
- Goal markets calibrated with Platt scaling: BTTS, OU1.5, OU2.5, OU3.5.
- Calibration mode: prequential/out-of-fold for validation; current fixture-publish calibrators are fit on historical out-of-fold predictions.
- Predictive validation field: `predictive_validation_run_id`; the previous `validated_run_id` wording is intentionally retired because market validation is a separate question.
- Market validation status for the World Cup artifact: `NOT_TESTED` until real forward odds/CLV evidence exists.
- Training cutoff: `match_history.date < 2026-06-11`, so World Cup 2026 results cannot leak into the V1 artifact.
- Latest local predictive goal-market run: `run_id=19`.
- Latest local forward-only prediction publish with model artifact: `run_id=25`.
- Run IDs are local to each SQLite database. A local `run_id=19` and a homeserver `run_id=5` can both be the latest passing goal-market run in their own DBs.

Latest local walk-forward result, `--start-year 2021`:

- `baseline_uniform`: n=5730, Brier=0.6667, log-loss=1.0986, ECE=0.0977, close log-loss=1.0986.
- `baseline_marginal`: n=5730, Brier=0.6327, log-loss=1.0494, ECE=0.0016, close log-loss=1.0678.
- `baseline_logistic`: n=5730, Brier=0.5329, log-loss=0.9040, ECE=0.0237, close log-loss=1.0393.
- `dixon_coles` tuned to 3-year half-life: n=5730, Brier=0.5085, log-loss=0.8669, ECE=0.0195, close log-loss=1.0224.
- Competitive/friendly split for tuned `dixon_coles`: competitive log-loss=0.8543, friendly log-loss=0.9028.
- Metrics persist in `eval_metric`; the latest verified full run is `run_id=14` with `ingest_run.status=succeeded`.

Latest local calibrated goal-market walk-forward result, `run_id=19`:

- BTTS: marginal-window log-loss=0.6853; `dixon_coles_platt` log-loss=0.6757, ECE=0.0112, close log-loss=0.6918.
- OU1.5: marginal-window log-loss=0.5853; `dixon_coles_platt` log-loss=0.5705, ECE=0.0083, close log-loss=0.6105.
- OU2.5: marginal-window log-loss=0.6934; `dixon_coles_platt` log-loss=0.6694, ECE=0.0172, close log-loss=0.6745.
- OU3.5: marginal-window log-loss=0.6078; `dixon_coles_platt` log-loss=0.5807, ECE=0.0159, close log-loss=0.5486.
- Gate result: all calibrated goal markets passed. Each market beats its same-window marginal baseline and ECE is below 0.03.

Latest local fixture prediction publish, `run_id=21`:

- Predicted fixtures: 88 resolved fixtures.
- Skipped fixtures: unresolved knockout placeholders.
- Inserted predictions: 1672 rows across 1X2, OU1.5, OU2.5, OU3.5, BTTS, double chance, and top exact scores.
- Model version: `dixon_coles:hl3y:platt-goals-v1`.
- `data_quality` records the model manifest plus Platt `a`, `b`, `n`, raw mean, calibrated mean, observed rate, and delta mean per calibrated market.
- Fixture prediction features load the latest Elo from `team_rating`; missing ratings fall back to 1500 and are counted in `data_quality` under `fixture_elo`.
- World Cup fixture prediction intentionally uses `neutral=1` because matches are neutral-site tournament games.

Latest fixture-publish Platt artifacts, `run_id=21`:

- BTTS: a=0.6129, b=-0.0731, n=5730, raw_mean=0.4163, calibrated_mean=0.4292, observed_rate=0.4284, delta_mean=0.0128.
- OU1.5: a=0.6082, b=0.3788, n=5730, raw_mean=0.7129, calibrated_mean=0.7227, observed_rate=0.7265, delta_mean=0.0098.
- OU2.5: a=0.6001, b=0.0334, n=5730, raw_mean=0.4735, calibrated_mean=0.4932, observed_rate=0.4935, delta_mean=0.0197.
- OU3.5: a=0.6402, b=-0.2191, n=5730, raw_mean=0.2773, calibrated_mean=0.2978, observed_rate=0.2956, delta_mean=0.0205.
- Mean-calibration sanity check: all four markets improved versus raw mean error.

Settlement status:

- `prediction_result` is present and currently empty locally.
- Default dry-run blocks post-kickoff predictions: 50 finished fixtures, 0 settled, 2850 blocked backfill rows, 0 missing prediction fixtures.
- Explicit backfill dry-run would settle 2100 supported rows and skip 750 unsupported exact-score rows. This is only for audit/debug, not forward performance reporting.
- Current settlement labels support 1X2, BTTS, OU lines, and double chance; exact score is intentionally skipped until a proper multiclass exact-score scorer is added.

## Fase 4 Paper Odds Pipeline

Fase 4 is implemented as a paper pipeline, not a staking pipeline:

- `fetch-odds`: maps local resolved fixtures to Odds-API.io events and stores raw odds in `odds` plus append-only `odds_snapshots`.
- `compute-edge`: applies multiplicative de-vig from the latest complete market snapshot and writes EV/probability edge to `edge_analysis`.
- `recommend`: creates paper recommendations from the latest edge run, using a 5% default EV threshold, 25% max EV sanity cap, 15% minimum model probability, odds freshness/depth filters, max 1 pick per fixture, conservative recommendation score, quarter Kelly, and 1-unit cap.
- `compute-clv`: derives the closing price from the last complete `odds_snapshots` market before kickoff and after the recommendation, then writes ratio CLV, log CLV, and fair-EV CLV to `recommendation_result`.
- `calibrator-drift`: compares Platt coefficients between the two latest fixture-prediction runs and records drift alerts in `data_quality`.

Odds-API.io integration notes:

- Official base URL: `https://api.odds-api.io/v3`.
- Event mapping uses `/events?sport=football`; odds fetch uses `/odds?eventId=...`.
- Default league filter: `international-fifa-world-cup`.
- Event lookup is narrowed to the pending fixture date window and retried to avoid wasting time on broad event queries.
- Local fixtures are matched by UTC kickoff date to handle venues whose local date differs from the API UTC date.
- API key is read from `ODDS_API_IO_KEY` in environment or local `.env`.
- Default selected bookmakers are read from `ODDS_API_IO_BOOKMAKERS` and currently default to `Bet365,Unibet`.
- Free-tier discipline: `fetch-odds` defaults to 90 fixture odds requests and 36 seconds between odds calls.
- Market mapping is explicit: `ML`/`h2h` to `1X2`, `Over/Under`/`totals` to `OU{line}`, and `Both Teams to Score`/`btts` to `BTTS`.

Latest local Fase 4 dry-runs:

- `fetch-odds --dry-run --min-interval-seconds 0`: pending resolved future fixtures only; the exact count moves as matches kick off and results arrive.
- `compute-edge --dry-run`: 0 rows analyzed because no real odds are stored yet.
- `recommend --dry-run`: 0 recommendations because no edge run exists yet.
- `compute-clv --dry-run`: 0 CLV rows because no paper recommendations and closing odds exist yet.

Important gate:

- No real-money staking until forward CLV is positive over a meaningful sample. The current rule of thumb remains 50+ settled forward picks minimum, ideally 100+.

## Secrets

API keys, SSH passwords, bookmaker credentials, and bankroll amounts must not be committed. Use `.env` locally or a systemd `EnvironmentFile` on the homeserver.

## Change Log

### 2026-06-24 - Fase 1.5 baseline harness

- Added `features/build.py` for leak-free 1X2 feature extraction from `team_rating_history.rating_pre`.
- Added `models/baseline.py` with marginal and logistic 1X2 baselines.
- Added `evaluate/walk_forward.py` for expanding-window historical evaluation.
- Added `walk-forward` CLI command.
- Added `run_id` to `eval_metric` so backtest metrics retain provenance.
- Extended `quality-report` with duplicate canonical-key and historical alias coverage checks.
- Included `eval_metric` in `db-summary`.
- Created this complete project document and changelog.
- Verified the real-data walk-forward run: logistic baseline beats marginal baseline on Brier and log-loss.

### 2026-06-24 - Paso 0 calibration and segmentation

- Added `baseline_uniform` as a permanent 1X2 sanity floor.
- Added average one-vs-rest classwise Expected Calibration Error (ECE) with 10 reliability bins.
- Persisted ECE, global reliability bins, and per-outcome reliability bins to `data_quality`.
- Added walk-forward segmentation for close matches (`abs(elo_diff) < 75`), competitive matches, and friendlies.
- Extended CLI output to show ECE and segmented log-loss.
- Added regression tests for metric helpers and ECE behavior on imbalanced outcomes.
- Verified the real-data segmented walk-forward run: Elo remains useful globally, but close-match log-loss is much worse than aggregate log-loss.

### 2026-06-24 - Fase 2 Dixon-Coles candidate

- Added `models/dixon_coles.py`, a numpy-only Dixon-Coles bivariate Poisson model.
- Registered `dixon_coles` in the walk-forward model factories.
- Added `numpy` as a formal runtime dependency.
- Added a Dixon-Coles smoke test for valid 1X2 probability output.
- Verified real-data walk-forward run `run_id=11`: Dixon-Coles beats logistic globally and in close matches.
- Interpretation: Dixon-Coles is the current Fase 2 base candidate for 1X2, but the close-match gain is still thin and should be validated across decay settings.

### 2026-06-24 - Dixon-Coles decay tuning

- Added `evaluate/tune_decay.py` to sweep Dixon-Coles half-life values using the same walk-forward harness.
- Added `tune-decay` CLI command with configurable `--half-lives` and `--l2`.
- Added `tune-decay` to README and deployment command lists.
- Sweep trials and the selected winner persist to `data_quality` under `check_name='tune_decay'`.
- Ran default sweep over 0.5, 1.0, 1.5, 2.0, and 3.0 years in `run_id=12`; 3.0 years won by close-match log-loss.
- Ran extended sweep over 2.0, 3.0, 4.0, 5.0, and 6.0 years in `run_id=13`; 3.0 years remained best, so the optimum is not merely a grid boundary artifact.
- Extended sweep close log-loss: 2.0y=1.0234, 3.0y=1.0224, 4.0y=1.0226, 5.0y=1.0229, 6.0y=1.0232.
- Set Dixon-Coles default half-life to 3.0 years.
- Added regression tests for decay conversion and close-log-loss trial selection.
- Re-ran full walk-forward in `run_id=14`; tuned Dixon-Coles improved from log-loss=0.8791/close=1.0348 to log-loss=0.8669/close=1.0224.

### 2026-06-24 - Fase 2 goal markets

- Added `models/markets.py` with score-grid aggregations for 1X2, over/under, BTTS, double chance, and top exact scores.
- Exposed normalized Dixon-Coles score matrices through `DixonColes.score_matrix`.
- Added `evaluate/goal_markets.py` to backtest OU and BTTS markets against a binary marginal baseline.
- Added `goal-markets` CLI command with configurable OU lines.
- Goal-market metrics persist to `eval_metric`; ECE and reliability bins persist to `data_quality`.
- Added quality-gate records for each goal market, requiring Dixon-Coles to beat the marginal baseline and ECE to stay below 0.03.
- Added `predict/fixtures.py` and `predict-fixtures` to publish multi-market predictions to `prediction` for fixtures with resolved teams.
- `predict-fixtures` skips unresolved knockout placeholders and writes model provenance through `run_id`.
- Added tests for score-grid market aggregations.
- Ran real-data goal-market walk-forward in `run_id=16`.
- Published resolved fixture predictions in `run_id=17`.
- Current gate status: failed due goal-market ECE above 0.03, so the next technical step is calibration by market before Fase 4 odds/value.

### 2026-06-24 - Goal-market Platt calibration

- Chose Platt scaling over isotonic calibration because it is monotonic, low-parameter, and better suited to small yearly folds.
- Added `models/calibration.py` with a numpy Platt calibrator over `logit(probability)`.
- Integrated prequential market calibration in `goal-markets`: for year Y, each market calibrator is fit only on out-of-sample predictions from year Y-1, then applied to Y.
- Added `dixon_coles_platt` as a separate evaluated model while keeping raw `dixon_coles`.
- Updated goal-market gates to prefer `dixon_coles_platt` when available.
- Added `baseline_marginal_platt_window` so calibrated models are gated against a marginal baseline on the same prequential years.
- Added calibration tests for range safety and monotonic ordering.
- Ran calibrated goal-market walk-forward in `run_id=19`; all goal markets passed the log-loss and ECE gate.
- Updated `predict-fixtures` so OU and BTTS are calibrated with Platt scalers fit on historical out-of-fold predictions.
- Published calibrated fixture predictions in `run_id=20` with model version `dixon_coles:hl3y:platt-goals-v1`.

### 2026-06-24 - Model freeze and settlement loop

- Added `models/manifest.py` and `model-manifest` to freeze the current production candidate as `dixon_coles:hl3y:platt-goals-v1`.
- Persisted the frozen manifest to `data_quality` during `predict-fixtures`.
- Persisted Platt calibrator artifacts per market: coefficients `a`/`b`, calibration sample size, raw mean, calibrated mean, observed rate, delta mean, and mean-error sanity checks.
- Re-published fixture predictions in `run_id=21`; model manifest now points to validated `run_id=19` and prediction `run_id=21`.
- Added `prediction_result` for settled prediction labels, Brier, log-loss, settlement run provenance, and original prediction timestamp.
- Added `settle-fixtures` with `--dry-run`, `--include-settled`, `--allow-backfill`, and `--as-of`.
- Added a default forward-looking guard that blocks predictions generated after fixture kickoff unless `--allow-backfill` is explicit.
- Verified local settlement dry-run: default mode blocks 2850 backfilled prediction rows; explicit backfill mode would settle 2100 supported rows and skip 750 exact-score rows.
- Added settlement tests for post-kickoff blocking and 1X2 multiclass metric aggregation.

### 2026-06-24 - Fase 4 paper odds, edge, recommendations, and CLV

- Added `.env` loading for local secrets while keeping real keys ignored by git.
- Added `ODDS_API_IO_BASE_URL` and `ODDS_API_IO_BOOKMAKERS` placeholders to `.env.example`.
- Added `ingest/odds.py` and `fetch-odds` for raw Odds-API.io odds ingestion.
- Implemented documented Odds-API.io `/events` and `/odds` flow with explicit market mapping and a dry-run that does not call the network.
- Added `edge_analysis` table and `compute-edge` for multiplicative de-vig and EV/probability edge.
- Added `recommendation_result` table and extra paper-tracing columns on `recommendation`.
- Added `recommend` for paper-only recommendations with conservative defaults: 5% min EV, 15% min probability, quarter Kelly, and 1-unit cap.
- Added `compute-clv` to compare paper recommendations against later closing odds and aggregate CLV into `eval_metric`.
- Added Fase 4 tests for odds parsing, de-vig, edge persistence, Kelly sizing, recommendation generation, and CLV.
- Verified dry-run state: 38 pending resolved fixtures ready for odds fetch, no edge/recommendation/CLV rows until real odds are stored.
- Refined `fetch-odds` to target only pending resolved fixtures, avoiding wasted requests on fixtures already carrying final scores.
- Narrowed Odds-API.io event lookup to the pending fixture date window and added network retries for event/odds fetches.
- Added `ODDS_API_IO_LEAGUE` and defaulted real odds lookup to `international-fifa-world-cup`, avoiding broad all-football event scans.
- Refined `fetch-odds` again to skip fixtures whose UTC kickoff is already in the past and to match events by UTC date.

### 2026-06-24 - Homeserver deployment packaging

- Added `deploy/bootstrap_homeserver.sh` to create the server virtualenv, install ScoreRecord, initialize SQLite, create `.env` when missing, and install user systemd units.
- Added `deploy/Deploy-ScoreRecord.ps1` to archive the current committed tree, copy it to `/home/adrian/scorerecord`, bootstrap, and run smoke checks over SSH.
- Added `deploy/Set-RemoteOddsApiKey.ps1` to set `ODDS_API_IO_KEY` on the homeserver via stdin without committing or logging the secret.
- Added user systemd units/timers for hourly odds, daily predictions/recommendations, nightly settlement/CLV, and weekly monitoring.
- Set conservative homeserver resource limits: 2 BLAS/OpenMP threads, `MemoryMax` 700M-900M, `Nice=10`, and idle IO scheduling.
- Updated `docs/deployment.md` with the SSH-key flow and operational gates.
- Homeserver deployment was verified with an SSH key; public data pipeline ran successfully on the server and published 1672 fixture predictions.

### 2026-06-26 - Homeserver operational activation

- Deployed ScoreRecord to `/home/adrian/scorerecord` on the homeserver using the dedicated SSH key `~/.ssh/scorerecord_homeserver`.
- Ran the public-data pipeline remotely: 104 fixtures, 8155 historical matches, 10213 goals, 284 Elo-rated teams, 16310 Elo-history rows.
- Remote validation matched local expectations: Dixon-Coles + Platt model manifest `dixon_coles:hl3y:platt-goals-v1`, validated `run_id=5`, prediction `run_id=6`.
- Configured `ODDS_API_IO_KEY` in the remote `.env` through stdin without committing or printing the secret.
- Verified real Odds-API.io ingestion on one pending World Cup fixture: 10 raw odds rows, 2 bookmakers, 2 markets.
- Ran `compute-edge` on the remote odds sample: 10 analyzed rows, 2 edges above 5%, max EV edge 6.56%.
- Generated the first paper recommendation: Croatia home 1X2 at Unibet, model probability 0.6020, odds 1.77, EV edge 6.56%, stake 0.0213 units, confidence medium.
- Started user systemd timers for hourly odds, daily predictions/recommendations, nightly settlement/CLV, and weekly monitoring.

### 2026-06-27 - Production-quality fixes from operational review

- Fixed `predict-fixtures` so future fixture `MatchFeature` objects use latest Elo from `team_rating` instead of hardcoded `0.0`.
- Fixed World Cup ingestion so bracket placeholders such as `1B`, `2L`, and `3A/B/C/D/F` are not created as teams.
- Added Bosnia/Herzegovina aliases so fixture names resolve to the historical team with Elo.
- Seeded aliases now prefer the best referenced team and seeded alias mappings take precedence over stale exact-name duplicates, preventing fixture-only duplicates such as `Bosnia & Herzegovina`.
- Added fixture Elo quality logging with resolved fixture count, team-rating fallback count, and min/max fixture Elo difference.
- Updated the model manifest payload with `run_id_scope='sqlite_database_local'` to make local-vs-remote run IDs explicit.
- Verified homeserver `validated_run_id=5` is honest: BTTS, OU1.5, OU2.5, OU3.5, and `all_markets_passed` are all `1.0`.
- Documented that `neutral=1` is an intentional World Cup simplification, not a general soccer assumption.
- Re-published homeserver fixture predictions in `run_id=18`: 72 resolved fixtures, 32 unresolved placeholders, 1368 prediction rows.
- Verified homeserver `fixture_elo` quality for `run_id=18`: 144 team slots, 0 missing team-rating slots, min Elo diff -271.133, max Elo diff 317.310.
- Recomputed homeserver edge in `run_id=19`: 10 odds rows analyzed, all tied to `prediction_run_id=18`, 2 picks above 5% EV, 1 recommendation in dry-run, no CLV rows yet because closing odds are not available.

### 2026-06-27 - Production validation and monitoring hardening

- Ran production `walk-forward --start-year 2021` on the homeserver DB. Dixon-Coles 1X2 remained valid: n=5730, Brier=0.5085, log-loss=0.8669, ECE=0.0195, close log-loss=1.0224.
- Ran production `goal-markets --start-year 2021`; calibrated BTTS, OU1.5, OU2.5, and OU3.5 all beat their same-window marginal baselines with ECE below 0.03.
- Production manifest now resolves `validated_run_id=22` in the homeserver SQLite database; run IDs remain DB-local.
- Added `elo_diff` and `close_match` to `edge_analysis`, with lightweight migration support and edge-distribution quality metrics for close matches.
- Added `fetch-odds --closing-window-minutes`; when used with `--closing`, it captures only fixtures inside the upcoming window unless a specific fixture is requested.
- Added `scorerecord-preplay-odds.timer` to collect closing odds hourly at minute 25 for fixtures within the next 60 minutes.
- Added `calibrator-drift` and wired it into weekly monitoring so Platt coefficient changes above the configured threshold are persisted to `data_quality`.
- Deployed the monitoring update to the homeserver and started `scorerecord-preplay-odds.timer`; no fixtures were inside the 60-minute closing window at verification time.
- Recomputed homeserver edge in `run_id=23`: 48 odds rows analyzed, 10 close-match rows, 12 positive edges above 3%, 9 above 5%, max EV edge 42.32%.
- Verified `calibrator-drift` on the homeserver: previous prediction run `16`, current prediction run `18`, 4 markets compared, max relative change 0.0000, 0 alerts.
- Kept recommendations in dry-run after the larger odds sample: default 5% threshold would emit 5 recommendations from 9 candidates; 3% discovery threshold would emit 7 from 12 candidates. No new paper recommendation rows were inserted pending manual review of the high-edge 1X2 prices.

### 2026-06-27 - High-edge guardrails

- Diagnosed homeserver rows with EV edge above 20%. The affected fixtures were `Panama-England`, `Colombia-Portugal`, and `Algeria-Austria`; fixture IDs resolved to the expected teams, so no obvious fixture cross-match was found.
- The extreme values came from 1X2 model-market disagreement on long prices, not missing Elo fallbacks or unresolved teams.
- Added `edge_sanity` records in `data_quality`; every real `compute-edge` run now logs `high_edge_alert` and `high_edge_rows` with severity and top-edge details when any EV edge exceeds 25%.
- Added `recommend --max-edge`, defaulting to `0.25`, so paper recommendations exclude implausibly high edges unless explicitly overridden for manual investigation.
- Added regression tests for both the high-edge alert and the recommendation cap.
- Deployed the guardrail to the homeserver and recomputed edge in `run_id=52`: 59 rows analyzed, max EV edge 49.96%, 6 high-edge rows, `edge_sanity.high_edge_alert=1`.
- Verified `recommend --dry-run` after the cap: default settings would emit 6 recommendations, while `--max-edge 1.0` would emit 8; the two extra picks are above the sanity cap.
- The daily timer had already inserted `recommend` run `49` before this guardrail existed. Two rows in that pre-guardrail run exceed 25% EV (`Panama-England` draw and `Algeria-Austria` home); keep them for audit only and do not treat them as actionable without manual review.

### 2026-06-27 - Odds quality and correlation controls

- Extended `edge_analysis` with audit fields for `raw_implied_prob`, `market_overround`, `odds_books`, `median_odds`, `odds_ratio_to_median`, `odds_age_minutes`, `odds_quality_flags`, `high_edge_reason`, and `recommendation_score`.
- `compute-edge` now classifies high-edge cases into reasons such as `long_odds_tail`, `model_market_disagreement`, `calibration_tail_risk`, `draw_tail_risk`, `stale_odds`, `low_market_depth`, and `bookmaker_outlier`.
- `recommend` now filters stale, low-depth, and outlier odds by default through `--max-odds-age-minutes`, `--min-books`, and `--allow-odds-quality-flags`.
- `recommend` now ranks by `recommendation_score` instead of raw EV and defaults to `--max-picks-per-fixture 1` to avoid treating correlated picks from the same match as independent.
- `recommendation` rows now persist the source `edge_run_id`, `recommendation_score`, `odds_quality_flags`, and `high_edge_reason` for GUI audit surfaces.
- Odds market-depth checks normalize bookmaker variants such as `Bet365` and `Bet365 (no latency)` into one bookmaker family, so duplicated feeds do not inflate `odds_books`.
- Added regression tests for odds-quality exclusion, bookmaker-family depth, and anti-correlation by fixture.
- Deployed the controls to the homeserver and recomputed edge in `run_id=55`: 59 rows analyzed, all with `odds_books=2` after bookmaker-family normalization, 8 rows flagged `stale_odds`, and 51 rows unflagged.
- Verified `recommend --dry-run` with default policy: 14 candidates passed freshness/depth/max-edge filters, 3 recommendations remained after max 1 pick per fixture. No new recommendation rows were inserted.

### 2026-06-27 - Read-only paper/audit GUI

- Added a dependency-light `scorerecord gui` command using the Python standard library HTTP server so the homeserver can serve the dashboard without Streamlit/FastAPI overhead.
- The GUI selects the latest recommendation run that has both `edge_run_id` and `recommendation_score`, preventing pre-guardrail or legacy rows from being treated as actionable just because they are newer.
- Added an actionable paper-picks table with kickoff, teams, market, selection, odds, model probability, market probability, EV edge, recommendation score, stake fraction, market depth, odds age, quality flags, and edge reasons.
- Added an audit-only table for rows with EV above 25%, odds-quality flags, or high-edge reasons.
- Added KPI cards for edge run, recommendation run, rows analyzed, candidates, final recommendations, clean/flagged rows, high-edge rows, max EV, max recommendation score, and max odds age.
- Added `scorerecord-gui.service` for the homeserver with conservative memory/thread limits.
- Verified the homeserver GUI service manually: `active (running)`, 21.5 MB RSS, and `/healthz` returned HTTP 200.
- Added lightweight dashboard refinements: `OK/WARNING/STALE/NO DATA` status badge, last edge/recommendation update timestamps, explicit audit-only warning text, risk-colored rows, percentage formatting for stake fraction, and querystring filters for `date=YYYY-MM-DD` and `market=1X2|OU|BTTS`.
- Diagnosed audit-only duplicate-looking rows: `edge_analysis` intentionally stores bookmaker-level rows, so repeated logical picks can appear when several bookmakers quote the same selection.
- Deduplicated audit-only display by logical pick (`fixture_id`, `market`, `selection`) while preserving bookmaker-level data for recommendation selection and future CLV.
- Split GUI status wording into `Service status: OK` and `Data quality: ...` so odds/model warnings are not confused with service failure.
- Made paper recommendation filtering explicitly reject critical audit markers such as `stale_odds`, `low_market_depth`, `bookmaker_outlier`, `long_odds_tail`, `draw_tail_risk`, and `calibration_tail_risk`; plain `model_market_disagreement` remains allowed.
- Added `backtest` to generate walk-forward historical recommendation CSVs by both raw `edge_ev` and `recommendation_score` rankings.
- Added `calibration-report` to generate market/selection/probability-bucket calibration CSVs and flag supported bucket errors above 8 percentage points.
- Backtest outputs go to `outputs/backtests/` and are intentionally ignored by git.
- Because historical bookmaker odds are not available yet, the backtest uses a training-window marginal probability as a market proxy; ROI fields are diagnostic, not real betting ROI.
- Added conservative `capped_stake_units` alongside paper/model `stake_units`, defaulting to a 1% per-pick cap with extra reductions for long odds and 1X2 draws.

### 2026-06-27 - Historical odds backtest with Football-Data

- Added separate historical tables `historical_match` and `historical_odds` so real historical odds do not mix with live odds snapshots.
- Added `evaluate/historical_odds.py` dataclasses for provider-neutral historical events and quotes.
- Added `ingest/football_data.py` for Football-Data CSV ingestion with stable `source_event_id`, season normalization, date parsing, 1X2 Avg/Bet365/Max open odds, optional close odds, and data-quality counts.
- Added `ingest-football-data --league ... --season ...` CLI with `--refresh` and `--dry-run`.
- Added `THESTATSAPI_KEY` as an environment placeholder only; the current implementation uses public Football-Data CSV URLs and does not commit or require the provided secret.
- Extended `backtest` with `--odds-mode proxy|historical`, `--historical-odds-source`, `--entry-odds`, `--closing-odds`, `--market`, `--league`, and `--season`.
- Added `backtest --odds-mode historical` for Football-Data 1X2 using an offline club-league universe: it trains Dixon-Coles only on matches before each tested match date, then joins real historical entry/closing odds.
- Real-odds backtest writes separate CSVs: `backtest_recommendations_real_odds_*`, `backtest_summary_real_odds_*`, `calibration_by_bucket_real_odds_*`, and `clv_summary_real_odds_*`.
- Real profitability columns are named `profit_flat_real`, `profit_model_real`, `profit_capped_real`, `roi_*_real`, and `clv_real`.
- Proxy backtest outputs now carry `odds_source=training_marginal_proxy`, `is_real_historical_odds=0`, `is_timestamp_exact=0`, and `profitability_gate=blocked_proxy_odds` so they are not confused with real ROI.
- Added tests/smoke coverage for Football-Data parsing, historical schema initialization, and real-odds backtest output metadata.
- Scope note: Football-Data unlocks honest historical 1X2 ROI for covered club leagues; it does not directly validate World Cup 2026 profitability or intraday CLV timing.

### 2026-06-27 - Historical backtest reading discipline

- Kept `backtest --odds-mode historical --season` optional; omitting it evaluates every loaded season inside `--from/--to`, which is the preferred mode for multi-season walk-forward analysis.
- Added `ingest-football-data --from-season ... --to-season ...` for controlled multi-season Football-Data loads without hand-running six commands.
- Added `historical-odds-report --league ...` to report match counts, total odds rows, Avg open coverage, and Avg close coverage by season.
- Added `score_bucket` to proxy and real backtest recommendation CSVs and summaries so `recommendation_score` can be evaluated separately from raw EV.
- Added `positive_clv_rate`, `sample_size_tier`, and `decision_gate` to real-odds summaries and CLV summaries.
- Added a `sample_size_policy` block to real-odds backtest metadata: fewer than 100 picks is smoke/exploratory only; 100-299 is initial signal; 300-999 supports cautious adjustments; 1000+ is more stable, still with bucket checks.
- Added tests/smokes for season ranges, historical odds coverage reports, optional-season historical backtests, and the new score/CLV/sample fields.
- Critical interpretation: a small smoke run validates plumbing only. Threshold/score changes should wait for sufficient overall sample and per-bucket counts.
- Deployed the update to the homeserver, ingested EPL 2019-2025, and verified 6 seasons with 380 matches each and 100% Avg open/close coverage.
- Ran EPL historical real-odds backtest `run_id=75` for 2021-08-01 through 2025-05-31: 4560 predictions, 1444 selected policy rows, global tier `more_stable_signal`.
- Initial EPL read from `run_id=75`: capped ROI -8.46%, mean CLV -0.56%, positive CLV 43.56%; this is enough to review score/edge buckets, but not enough to blindly retune without per-bucket checks.

### 2026-06-27 - Historical policy selectivity gates

- Added `--min-score` to `recommend` and `backtest`; production recommendations now default to `min_score=0.05`, while backtests default to `0.0` for backward-compatible baselines unless specified.
- Added `--max-picks-per-day` to `recommend` and `backtest` so the policy can explicitly say "no more value today" instead of selecting almost one pick per fixture.
- Added `backtest-policy-grid` for Football-Data 1X2 historical odds. It fits/predicts once, then sweeps `min_score`, `min_edge`, and `max_picks_per_day` combinations without refitting per config.
- Policy-grid CSVs report compact rows by ranking strategy with sample tier, decision gate, capped ROI, mean CLV, positive CLV rate, drawdown, average edge, and average recommendation score.
- Policy-grid metadata and `data_quality` records warn against choosing the single best ROI row without bucket checks and cross-league validation.
- Added tests/smokes for min-score filtering, daily volume caps, and policy-grid output.
- Critical interpretation: the EPL result justifies adding selectivity controls; it does not yet justify hardcoding draw-specific or long-odds-specific penalties without grid and bucket evidence.
- Deployed the policy-gate update and ran EPL policy grid `run_id=77` over 2021-08-01 through 2025-05-31: 4560 prediction rows and 120 policy rows.
- Best useful CLV row in `run_id=77` was `min_edge=0.075`, `max_picks_per_day=3`, `ranking=edge_ev`: 555 bets, capped ROI -6.33%, mean CLV -0.45%, positive CLV 44.32%.
- Best useful capped-ROI row in `run_id=77` was `min_edge=0.075`, `max_picks_per_day=3`, `ranking=recommendation_score`: 555 bets, capped ROI -4.56%, mean CLV -0.58%, positive CLV 43.06%.
- Interpretation: selectivity improves the EPL historical result relative to the original -8.46% capped ROI, but the grid still does not show positive CLV; do not promote these gates as a profitable production policy without more evidence.

### 2026-06-27 - Market benchmark, shrink grid, and live odds snapshots

- Added `backtest-policy-grid --market-shrink-alphas` to test blends of model probability and fair market probability: `p_final = alpha * model_prob + (1 - alpha) * market_prob_fair`.
- The shrink grid recomputes EV, probability edge, high-edge reasons, and `recommendation_score` for each alpha before applying selection gates.
- Added `market-benchmark-report` for Football-Data 1X2 historical odds. It writes row-level and grouped CSVs comparing model log-loss/Brier/calibration against fair market probabilities.
- Benchmark groups include overall, selection, model probability bucket, market probability bucket, selection-plus-probability bucket, and odds bucket.
- Added `analyze-policy-grids` to aggregate `policy_grid_real_odds_*.csv` files across leagues by config, including `market_shrink_alpha`, and classify each policy as `candidate`, `review`, or `reject`.
- The policy-grid analyzer is intentionally conservative: `candidate` requires at least three leagues, at least 1000 bets, nonnegative weighted capped ROI, positive weighted CLV, positive CLV rate above 50%, and positive CLV in at least 60% of leagues.
- Added `odds_snapshots` as an append-only live price table populated during `fetch-odds`. The current edge layer still uses `odds`, while snapshots preserve event id, fixture id, bookmaker, market, selection, line, odds, timestamp, and commence time for future live CLV/drift analysis.
- Added `odds-snapshot-report` and `odds_snapshot_coverage` data-quality metrics so live collection can be audited by fixture, total snapshots, bookmaker count, market count, first snapshot, and latest snapshot.
- Added tests/smokes for market shrink, model-vs-market benchmark rows, cross-league grid aggregation, and live odds snapshots.
- Updated README command examples with the benchmark, shrink grid, and grid aggregation workflow.
- Critical interpretation: these tools decide whether the model needs market shrink or policy rejection; they do not prove future World Cup profitability and should not loosen paper-only staking gates without multi-league and live CLV evidence.

### 2026-06-30 - Reproducible multi-league validation runner

- Added `scripts/run_multileague_validation.sh` to run the current evidence block across `EPL`, `LA_LIGA`, `SERIE_A`, `BUNDESLIGA`, `LIGUE_1`, and `CHAMPIONSHIP`.
- The script ingests Football-Data seasons 2019-2020 through 2024-2025, writes coverage reports, runs `market-benchmark-report`, runs `backtest-policy-grid` with market-shrink alphas `1.0,0.75,0.5,0.25`, and aggregates policy-grid CSVs into `outputs/backtests/policy_grid_summary_multileague.csv`.
- The runner writes per-league outputs under `outputs/backtests/<league>/benchmark` and `outputs/backtests/<league>/policy-grid`, making it possible to compare benchmark and policy evidence without mixing all CSVs in one flat directory.
- Added a simple lock directory so accidental double launches fail fast instead of running two expensive validation jobs over the same database.
- This is an execution aid, not a model change: recommendation score and production paper policy remain unchanged until multi-league results are read.

### 2026-06-30 - Multi-league validation readout

- Verified the previously launched multi-league validation completed on the homeserver. It ingested and evaluated `LA_LIGA`, `SERIE_A`, `BUNDESLIGA`, `LIGUE_1`, and `CHAMPIONSHIP`; together with the existing EPL run, `analyze-policy-grids` read 6 policy-grid files and wrote `outputs/backtests/policy_grid_summary_multileague.csv`.
- Historical coverage was strong enough for screening: La Liga had 100% Avg open/close coverage; Serie A had 99.7%-100% depending on season; Bundesliga had 100%; Ligue 1 had 100% with shorter 2019-2020 and 2023-2025 seasons; Championship had 100%.
- Model-vs-market benchmark was negative for the model in every league: EPL `+0.0167`, La Liga `+0.0103`, Serie A `+0.0110`, Bundesliga `+0.0131`, Ligue 1 `+0.0104`, Championship `+0.0072` model-minus-market log-loss. Positive values mean fair market probabilities beat the model.
- Best useful policy rows by league still had negative CLV in all six leagues. Championship had one best useful row with positive capped ROI (`+3.63%`) but negative CLV (`-0.82%`), so it is not evidence of repeatable edge.
- Multi-league policy summary classified all 480 configurations as `reject`. For rows with at least 1000 bets, the best weighted CLV row was still negative: 5488 bets, weighted capped ROI `-7.76%`, weighted CLV `-0.79%`, positive CLV rate `44.77%`, only 1 profitable league, and 0 positive-CLV leagues.
- Conclusion: the current Dixon-Coles club-league policy does not beat the historical fair market. Do not promote a new default, do not loosen paper recommendations, and do not tune `recommendation_score` from intuition. The next analytical work should be model/calibration diagnostics by league, selection, odds bucket, and probability bucket.

### 2026-06-30 - Segmented calibration diagnostics

- Added `calibration-diagnostics` to produce the next required evidence layer before any scoring or model-policy changes.
- The command reuses the same walk-forward historical odds pipeline and writes: `calibration_diagnostics_rows_*`, `calibration_by_league_*`, `calibration_by_selection_*`, `calibration_by_prob_bucket_*`, `calibration_by_odds_bucket_*`, `model_vs_market_by_bucket_*`, `overconfidence_report_*`, and metadata JSON.
- Diagnostics include hit rate, average model probability, average fair-market probability, model/market log-loss, Brier, ECE, calibration error, overconfidence, and model-vs-market flags by league, selection, probability bucket, odds bucket, and combined league/selection/bucket segments.
- Added `calibration_diagnostics` data-quality metrics so the diagnostic run is traceable through `ingest_run` and `data_quality`.
- Critical interpretation: this still does not create edge. It identifies the segments where the model is overconfident, underconfident, or losing most clearly against the market so future calibration can be evidence-based and out-of-sample validated.

### 2026-06-30 - Calibration diagnostics readout

- Ran homeserver `calibration-diagnostics` for `EPL,LA_LIGA,SERIE_A,BUNDESLIGA,LIGUE_1,CHAMPIONSHIP` over 2021-08-01 through 2025-05-31. Run `139` wrote 28,089 prediction rows, 341 segment rows, and 239 supported over/under-confidence rows.
- Overall model-minus-market log-loss was `+0.011107`, confirming the fair market beats the model across the combined club-league sample. Overall ECE was not the core failure (`model_ece=0.0069`, `market_ece=0.0080`); the issue is weaker discrimination/likelihood versus market, not a simple global calibration offset.
- By selection, the model lost to market on all three outcomes: home `+0.0157`, away `+0.0153`, draw `+0.0023`. Draw is the least bad but still not a positive edge segment.
- By model-probability bucket, the model beat market only in low-probability tails `0-5%` and `5-10%`; supported mainstream buckets from `10-15%` through `75%+` all lost to market.
- By odds bucket, every bucket lost to market. The worst broad bucket was short prices `1.01-1.50` with model-minus-market log-loss `+0.0230`, driven by underconfidence on strong favorites: hit rate `75.3%`, model `68.6%`, market `72.2%`.
- Worst supported model-vs-market segments included EPL away `2.00-3.00`, EPL away `30-40%` model bucket, away `60-75%` model bucket, and EPL home/away mid-odds segments. These are not candidates for promotion; they are model-repair targets.
- Supported model-better rows exist only in narrow/low-probability slices, such as model `0-5%`, model `5-10%`, and EPL draw `3.00-5.00`, but the gains are small and not enough to justify a betting policy.
- Conclusion: ScoreRecord is now a reliable statistical audit tool for this question: it can say where the current model loses. The next improvement should be model/calibration research validated out of sample, not score tweaks, front-end work, or real staking.

### 2026-07-01 - Operational hardening, artifacts, and derived closing

- Added `schema_migration`, `migrate-db`, `db-version`, and `db-health` so schema state, SQLite integrity, foreign keys, kickoff UTC coverage, artifact coverage, forward-only prediction status, snapshot coverage, and failed runs can be checked explicitly.
- Added a shared UTC parser for ISO timestamps and openfootball-style `UTC-6` kickoff strings, then wired it into fixture ingest, odds matching, settlement, and migration backfill.
- Added `model_artifact` plus `prediction.artifact_id`. Fixture predictions now persist a reproducible artifact id built from model parameters, training cutoff, training-row count, Platt calibrator coefficients, predictive validation run, market-validation status, and git SHA.
- Renamed the semantic gate in new runs from `goal_market_gate` to `predictive_baseline_gate`; older rows are still readable for backward compatibility.
- Changed the model manifest vocabulary: `predictive_validation_run_id` is separate from `market_validation_status`, which remains `NOT_TESTED` until forward odds/CLV evidence exists.
- Froze World Cup V1 training at `match_history.date < 2026-06-11` to prevent tournament-result leakage.
- Made `predict-fixtures` forward-only for production publishing: unresolved fixtures, finished fixtures, and already-started fixtures are skipped.
- Changed `fetch-odds --closing` semantics to raw capture only. The flag may still limit the preplay window, but rows are stored as raw odds; closing truth is now derived later from `odds_snapshots`.
- Updated CLV computation to use the last complete append-only odds snapshot before kickoff and after the recommendation, matching fixture, market, selection, line-by-market, and bookmaker. `recommendation_result` now stores legacy ratio CLV plus `clv_log`, `clv_fair_ev`, and `closing_snapshot_at`.
- Changed `compute-edge` to use the latest complete market snapshot per fixture/bookmaker/market instead of mixing the latest individual selection rows; it now also reads only the latest successful `predict-fixtures` run and skips fixtures that have already started.
- Changed `recommend` to recompute odds age at recommendation time, preventing stale historical edge runs from emitting new paper picks.
- Local validation: `compileall` passed; a direct CLV harness produced derived ratio CLV, log CLV, and fair-EV CLV from a complete OU2.5 snapshot; `db-health` is OK for schema, integrity, FKs, kickoff UTC, latest artifact coverage, and latest forward-only predictions.
- Local warnings remaining are expected and informative: no local live snapshots and one recent failed run; legacy predictions remain recorded and are reported separately from the latest operational run.
- Homeserver pre-deploy backup: `/home/adrian/scorerecord/backups/scorerecord-db-predeploy-20260701.tgz`, SHA-256 `c3e64c4e913de368b7a345763714153fb55589c4a721a353727af61d8f316852`.
- Homeserver deployment verified: migrations at schema version 3; `db-health` OK for integrity, FKs, kickoff UTC, latest forward-only prediction run, legacy closing flags, and odds snapshots. One recent failed run remains as a warning to inspect, not a blocker.
- Homeserver latest `predict-fixtures` run inserted 0 rows because no resolved fixtures remain unstarted as of July 1, 2026; this is the intended forward-only behavior.
- Homeserver `compute-edge --dry-run` and `recommend --dry-run` both returned 0 rows, confirming old predictions/edges are not reused for new paper picks.
- Homeserver timers left active: `scorerecord-odds.timer`, `scorerecord-postmatch.timer`, and `scorerecord-weekly.timer`. `scorerecord-daily.timer` and `scorerecord-preplay-odds.timer` are disabled/inactive pending manual review.
- Homeserver GUI remained active after deploy on port `8765`, around 31 MB RSS under `MemoryMax=350M`.

### 2026-07-01 - Phase 1 closeout and Phase 2 scoring contract

- Updated stale-sensitive tests to use future synthetic timestamps and explicit successful `predict-fixtures` run ids, matching the current guardrail that `compute-edge` only reads the latest successful prediction run.
- Fixed two `edge_analysis` fixture inserts in recommendation tests that were missing the `edge_prob` value after the audit schema expansion.
- Added `ModelTrainingPolicy` to the frozen model manifest. The World Cup V1 cutoff is now exposed as `training_policy` with `policy_id=worldcup-2026-pre-tournament-v1` and `cutoff_rule=match_history.date < trained_until`; `predict-fixtures` consumes this policy instead of a loose cutoff constant.
- Added `db-failures` for recent failed-run inspection and `fixture-summary` for operational fixture coverage, including future resolved/unresolved counts and next unscored fixtures.
- Started Phase 2 by adding `evaluate/scoring.py` as the shared scoring contract: multiclass 1X2 Brier/log-loss are scored once per match, binary OU/BTTS Brier/log-loss are scored once per market row, and block bootstrap means can be computed by independent fixture/date/week blocks.
- Wired walk-forward, goal-market evaluation, settlement, and backtest binary log-loss through the shared scoring helpers, keeping existing output schemas stable while reducing metric drift risk.
- Added block bootstrap confidence columns to `market-benchmark-report` for `model_minus_market_logloss`, grouped by club league plus ISO week. This makes the model-vs-market readout less dependent on row-level independence assumptions.
- Added scoring tests for the 1X2 unit, binary unit, invalid units, World Cup date blocks, club league ISO-week blocks, and deterministic block bootstrap metadata.
- Tightened the Phase 2 scoring contract after review: multiclass probabilities are normalized before 1X2 log-loss/Brier, preserving exact `0/1` probabilities for Brier while clipping only log-loss extremes.
- Changed `market-benchmark-report` summaries so `1X2` is scored as one multiclass unit per match instead of three binary selection rows. Detail CSVs still expose selection rows for audit, but decision metrics now report `num_matches`, `num_selection_rows`, and `scoring_unit`.
- Guarded the future two-way benchmark contract too: if complete `over/under` or `yes/no` rows are present, the benchmark summary scores one binary unit per match using the positive side (`over`/`yes`) and records the original selection-row count separately.
- Increased benchmark bootstrap to 2000 block resamples and added `benchmark_state`: `PROMISING` only when the paired block CI is entirely below zero, `REJECTED_FOR_INDEPENDENT_VALUE` when it is entirely above zero or clearly positive at large sample, otherwise `INCONCLUSIVE`.
- At this point in the log, Football-Data real-odds benchmarking was still limited to `1X2`; the future Phase 3 OU2.5 benchmark remained blocked until complete totals ingestion and coverage checks.

### 2026-07-01 - Roadmap and Football-Data OU2.5 ingestion prep

- Added `docs/roadmap.md` as the decomposable execution plan for phases 0-9, including open Phase 0/1 loops, the Phase 2 scoring contract, the target Phase 3 OU2.5 benchmark, daily forecast web, odds coverage, daily-goals-v1 paper policy, forward validation, market-anchored residual modeling, and corners.
- Extended Football-Data parsing with `extract_ou25_quotes`, storing complete `OU2.5` two-way rows as `over`/`under`, `line=2.5`, `entry_odds_type=avg_preclose`, and `closing_odds_type=avg_close`.
- The OU2.5 extractor supports the target `Avg>2.5`/`Avg<2.5` and `AvgC>2.5`/`AvgC<2.5` columns plus common `BbAv`/`BbAvC` aliases, without counting missing OU markets when a CSV has no totals columns.
- Added `historical-odds-report --market OU2.5` so coverage can be audited before enabling the Phase 3 benchmark.
- Added tests for Football-Data OU2.5 parsing and OU2.5 coverage. The benchmark remains intentionally blocked until remote `pytest`/`ruff` are green and real multi-league totals coverage is confirmed.

### 2026-07-01 - Football-Data OU2.5 coverage hardening

- Added `historical_odds.metadata_json` under schema version 4 so OU2.5 rows retain the source column family (`Avg` vs `BbAv`) while keeping the normalized `avg_preclose` entry type for later benchmarking.
- Made OU2.5 ingestion explicitly require complete preclose two-way odds before emitting quotes; partial `over` or `under` rows are discarded and counted as invalid.
- Extended `historical-odds-report --market OU2.5` with complete two-way rate, close-complete rate, entry/close overround ranges, overround warning counts, and column-family distribution by league and season.
- Documented `avg_close` as a Football-Data average closing consensus/fair-price proxy, not a single-bookmaker closing quote.
- Added tests for incomplete two-way rejection, `BbAv` alias metadata, coverage completeness, and overround-health output. Phase 3 remains blocked until the formal gate and live coverage readout are green.

### 2026-07-04 - Controlled Football-Data league expansion

- Added controlled Football-Data aliases for Liga MX (`MEX`), MLS (`USA`), Brazil Serie A (`BRA`), and Argentina Liga Profesional (`ARG`) using the provider's `/new/*.csv` current-feed URLs.
- Added robust league-alias normalization so labels such as `Mexico (Liga MX)`, `USA (MLS)`, `Brasil (Serie A)`, and `Argentina (Liga Prof)` resolve directly.
- Guarded `/new/*.csv` leagues against season-range ingestion: they require a single `--season` label because the provider URL is a current feed, not a season-versioned archive.
- Documented the acceptance rule: first run local/remote dry-run and `historical-odds-report` by league, then accept or reject each league for market validation based on complete odds coverage. Leagues without complete odds may still be useful for forecasting/product, but not for edge validation.
- No homeserver deploy was performed for this change; the user requested local-only work until resources are available.

### 2026-07-04 - Forecast-only World Cup GUI

- Reworked `scorerecord gui` so `/` and `/today` are now a forecast-only World Cup view backed by `fixture`, `prediction`, `team`, and `model_artifact`; the old paper/edge dashboard moved to `/audit`.
- Added `/match/<fixture_id>` for fixture detail across calibrated goal markets (`OU1.5`, `OU2.5`, `OU3.5`, and `BTTS`) with model probability, fair odds, artifact id, and market-validation status.
- Kept the implementation dependency-light on the existing stdlib HTTP server for homeserver reliability. The UI uses server-rendered HTML/CSS with daisyUI/Pico-style components and can be upgraded to FastAPI/Jinja2/HTMX later without changing the data contract.
- Added explicit product language: `FORECAST_ONLY`, `No es recomendacion de apuesta`, no EV/Kelly/edge wording in the forecast surface, and the display timezone from `SCORERECORD_DISPLAY_TZ` defaulting to `America/Mexico_City`.
- Added forecast states for unresolved fixtures, missing predictions, already-started fixtures, quiet forecasts below the 60% display threshold, and inconsistent prediction data.
- Added an OU monotonicity sanity check during fixture prediction publishing and in the GUI. If `P(Over 1.5) >= P(Over 2.5) >= P(Over 3.5)` is violated, the GUI hides the goal markets for that fixture and records/prints the inconsistency instead of showing a misleading forecast.
- Added `resolve-fixture-teams --fixture-id ... --home ... --away ...` to manually resolve bracket placeholders to existing team aliases without auto-creating teams.
- Added tests for the forecast GUI contract, match detail rendering, OU monotonicity hiding, manual fixture resolution, and persisted prediction-sanity metrics.
- Hardened lightweight DB column migrations against benign concurrent startup races by ignoring only the specific SQLite `duplicate column name` condition after a second process already added the same column.
- Deployed the forecast-only GUI to the homeserver and restarted `scorerecord-gui.service`. Remote `/healthz` returned `ok`; `/today` contains `FORECAST_ONLY`; `/audit` still contains the operational paper/audit dashboard.
- Remote validation after deploy: full `pytest` passed with 84 tests; targeted `ruff` over the touched files passed. Full-repo `ruff` remains a separate cleanup gate with 81 legacy style findings outside this GUI change.
- Remote fixture summary after deploy: 104 fixtures, 54 finished, 0 missing kickoff UTC, 0 future resolved, 14 future unresolved, and 36 past unscored. The GUI is therefore live and honest, but it cannot show new forecast cards until bracket placeholders are resolved or the fixture feed is refreshed with future resolved teams.

### 2026-07-04 - Fixture resolution and GUI safety hardening

- Renamed the `/audit` recommendation section to `Legacy Paper Recommendations` and added a `LEGACY AUDIT MODE - NO BETTING RECOMMENDATIONS` banner so the old paper pipeline is not confused with the forecast product.
- Renamed match-detail fair prices to `Precio teorico del modelo` and added explicit copy that the value is `1 / model probability`, not a real bookmaker quote or betting recommendation.
- Added explicit `90 min / tiempo reglamentario` labeling for goal-market forecasts. Knockout forecasts should be read as regulation-time probabilities, not extra-time or penalty outcomes.
- Hardened World Cup fixture ingestion with stable IDs for new ingests (`wc2026-match-###`) and compatibility reconciliation for existing legacy IDs. Existing rows are matched by stable id, legacy slug, or kickoff/stage/group/ground fingerprint; unreferenced legacy rows can be re-keyed safely, while referenced rows are reused to avoid breaking historical predictions/odds.
- Changed World Cup fixture upserts so placeholder-only refreshes do not erase a manual team resolution. Manual `home_id`/`away_id` values are preserved when the provider still sends `W73`/`L101`, and placeholders are cleared once real teams exist.
- Changed `resolve-fixture-teams` so it clears placeholders, marks the fixture scheduled, and records a manual `ingest_run` plus `data_quality` row with the resolved fixture and submitted team aliases.
- Added tests for stable fixture IDs, placeholder-to-real reingestion, manual-resolution preservation across refreshes, cleared placeholders, and manual-run audit logging.
- Homeserver verification after refresh: `ingest-worldcup --refresh` kept 104 fixtures, improved fixture resolution to 96/104, and produced `future_resolved=6`, `future_unresolved=8`, `past_unfinished=0`.
- Duplicate natural-slot check after the stable fixture-id change: `fixtures_total=104`, `duplicate_natural_slots=0`.
- Homeserver prediction publish after `build-elo`: `predicted_fixtures=6`, `skipped_fixtures=98`, `inserted_predictions=114`, latest prediction run `285`.
- Forecast quality gate after publish: all four Platt calibrator sanity metrics (`OU1.5`, `OU2.5`, `OU3.5`, `BTTS`) have `metric_value=1.0`; `prediction_sanity.ou_monotonicity_violations=0`; `fixture_elo.missing_team_rating_slots=0`.
- Homeserver validation after this hardening: focused GUI/World Cup/fixture tests passed `17 passed`; full suite passed `88 passed`; targeted `ruff` over touched files passed.
- GUI smoke after restart: `/healthz` returned `ok`; `/today?date=2026-07-05` contains `FORECAST_ONLY`; `/audit` contains `LEGACY AUDIT MODE - NO BETTING RECOMMENDATIONS`; `/match/wc2026-match-091` contains `Precio teorico del modelo` and `90 min / tiempo reglamentario`.

### 2026-06-24 - Fase 1.5 traceability and Elo history

- Added `team_rating_history` with pre/post Elo by historical match.
- Added `ingest_run` and `data_quality`.
- Added fixture resolution quality reporting.
- Added accent/case-folded alias keys.
- Added `scorer_goal_history` to exclude own goals from future scorer modeling.
