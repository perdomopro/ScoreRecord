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
- `edge_analysis`: model probability versus de-vigged market probability and EV edge.
- `prediction`: future model outputs for tournament fixtures.
- `prediction_result`: settled prediction labels and scoring metrics for finished fixtures.
- `recommendation`: future value-bet recommendations.
- `recommendation_result`: paper-recommendation CLV against closing odds.
- `eval_metric`: backtest metrics such as Brier and log-loss, linked to `ingest_run`.
- `ingest_run`: job provenance, status, source, and git SHA when available.
- `data_quality`: quality metrics tied to runs.

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
- 16 unresolved fixtures are knockout placeholders.
- Historical auto-created teams are expected because the historical dataset contains many non-World-Cup nations.
- Duplicate canonical keys should be zero.

Latest local quality report:

- Fixture resolution: 88/104 fully resolved.
- Unresolved fixtures: 16 knockout placeholders (`W73`, `L101`, etc.).
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
- `predict-fixtures`: writes Dixon-Coles 1X2, OU, BTTS, double-chance, and top exact-score predictions for resolved tournament fixtures.
- `model-manifest`: prints the frozen production-candidate manifest.
- `settle-fixtures`: scores finished fixtures, guarded against post-kickoff backfill by default.

Frozen production-candidate manifest:

- `model_version`: `dixon_coles:hl3y:platt-goals-v1`.
- Base model: Dixon-Coles with 3-year half-life (`xi=0.000633011`).
- Goal markets calibrated with Platt scaling: BTTS, OU1.5, OU2.5, OU3.5.
- Calibration mode: prequential/out-of-fold for validation; current fixture-publish calibrators are fit on historical out-of-fold predictions.
- Latest validated goal-market run: `run_id=19`.
- Latest prediction publish with manifest and calibrator artifacts: `run_id=21`.

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
- Skipped fixtures: 16 knockout placeholders.
- Inserted predictions: 1672 rows across 1X2, OU1.5, OU2.5, OU3.5, BTTS, double chance, and top exact scores.
- Model version: `dixon_coles:hl3y:platt-goals-v1`.
- `data_quality` records the model manifest plus Platt `a`, `b`, `n`, raw mean, calibrated mean, observed rate, and delta mean per calibrated market.

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

- `fetch-odds`: maps local resolved fixtures to Odds-API.io events and stores raw odds in `odds`.
- `compute-edge`: applies multiplicative de-vig and writes EV/probability edge to `edge_analysis`.
- `recommend`: creates paper recommendations from the latest edge run, using a 5% default EV threshold, 15% minimum model probability, quarter Kelly, and 1-unit cap.
- `compute-clv`: compares paper recommendations to later `is_closing=1` odds and writes granular CLV to `recommendation_result`.

Odds-API.io integration notes:

- Official base URL: `https://api.odds-api.io/v3`.
- Event mapping uses `/events?sport=football`; odds fetch uses `/odds?eventId=...`.
- Event lookup is narrowed to the pending fixture date window and retried to avoid wasting time on broad event queries.
- API key is read from `ODDS_API_IO_KEY` in environment or local `.env`.
- Default selected bookmakers are read from `ODDS_API_IO_BOOKMAKERS` and currently default to `Bet365,Unibet`.
- Free-tier discipline: `fetch-odds` defaults to 90 fixture odds requests and 36 seconds between odds calls.
- Market mapping is explicit: `ML`/`h2h` to `1X2`, `Over/Under`/`totals` to `OU{line}`, and `Both Teams to Score`/`btts` to `BTTS`.

Latest local Fase 4 dry-runs:

- `fetch-odds --dry-run --min-interval-seconds 0`: 38 pending resolved fixtures planned, 0 rows inserted, 39 planned API requests including the event-index request.
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

### 2026-06-24 - Homeserver deployment packaging

- Added `deploy/bootstrap_homeserver.sh` to create the server virtualenv, install ScoreRecord, initialize SQLite, create `.env` when missing, and install user systemd units.
- Added `deploy/Deploy-ScoreRecord.ps1` to archive the current committed tree, copy it to `/home/adrian/scorerecord`, bootstrap, and run smoke checks over SSH.
- Added `deploy/Set-RemoteOddsApiKey.ps1` to set `ODDS_API_IO_KEY` on the homeserver via stdin without committing or logging the secret.
- Added user systemd units/timers for hourly odds, daily predictions/recommendations, nightly settlement/CLV, and weekly monitoring.
- Set conservative homeserver resource limits: 2 BLAS/OpenMP threads, `MemoryMax` 700M-900M, `Nice=10`, and idle IO scheduling.
- Updated `docs/deployment.md` with the SSH-key flow and operational gates.
- Homeserver deployment was verified with an SSH key; public data pipeline ran successfully on the server and published 1672 fixture predictions.

### 2026-06-24 - Fase 1.5 traceability and Elo history

- Added `team_rating_history` with pre/post Elo by historical match.
- Added `ingest_run` and `data_quality`.
- Added fixture resolution quality reporting.
- Added accent/case-folded alias keys.
- Added `scorer_goal_history` to exclude own goals from future scorer modeling.
