# ScoreRecord Runbook

This is the operational guide for running ScoreRecord locally, validating changes,
shipping them to the homeserver, and pushing them to git. Keep secrets in `.env`;
never paste API keys into commands, commits, reports, or screenshots.

## Current Operating Truth

As of 2026-11-13:

- Product mode remains `FORECAST_ONLY` / research.
- Phase 3 rejected independent Dixon-Coles value against club OU2.5 market
  probabilities, so Phase 6 value/paper candidates stay blocked.
- The active operating path is multi-league club forecasting. Future fixtures
  exist for `EPL`, `LIGA_MX`, and `BRASILEIRAO`; `MLS` has none left and
  `ARGENTINA` returns no provider events.
- Production publishes forecasts daily but does not learn or measure: results
  are not ingested, the training cutoff is fixed at 2026-06-11, odds capture
  matches zero fixtures, and no forecast snapshots are frozen. `db-health` and
  `/health` stay green through all of that. See
  [critical-review.md](critical-review.md) and
  [improvement-plan.md](improvement-plan.md).
- The working copy has no `.venv` and no local database. Create both with the
  setup below; a local DB is only needed to run the CLI or GUI locally.
- Full `pytest` is green again (292 passed); the date-dependent tests now use a
  fixed clock and `tzdata` is a declared Windows dependency.
- The loop fixes are in the working tree, not on the server. See "After the
  next deploy" in [deployment.md](deployment.md).

## Environment Variables

Use `.env` locally and on the homeserver:

```dotenv
SCORERECORD_DB_PATH=data/scorerecord.db
SCORERECORD_RAW_DIR=data/raw
SCORERECORD_DISPLAY_TZ=America/Mexico_City
ODDS_API_IO_KEY=<secret>
THESTATSAPI_KEY=<secret>
ODDS_API_IO_BOOKMAKERS=Bet365,Unibet
ODDS_API_IO_LEAGUE=international-fifa-world-cup
```

`ODDS_API_IO_LEAGUE` accepts a single slug and only affects `fetch-odds`. The
World Cup value shown here is the code default and matches no club fixture.

Optional provider variables:

```dotenv
THE_ODDS_API_KEY=<secret>
API_FOOTBALL_KEY=<secret>
RESPONSIBLE_GAMBLING_URL=<url>
```

## Local Setup

Any Python 3.11 or newer works (verified with 3.14 on Windows and 3.13 on the
homeserver):

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
```

`tzdata` is installed automatically on Windows, where `zoneinfo` has no system
timezone database.

Full-repo `ruff` has legacy findings. For commits, run targeted ruff over
touched files and keep the full-repo debt visible instead of pretending it is
closed.

## Local Validation Gate

Run this before committing:

```powershell
python -m compileall -q src tests scripts
python -m pytest -q
python -m ruff check <touched-python-files>
git diff --check
git status --short
```

Verified local result on 2026-11-13 (Windows, Python 3.14, numpy, `tzdata`):

```text
pytest -q: 292 passed
ruff check src tests: 256 findings (208 E501)
```

## Core Local Commands

Database and World Cup spine:

```powershell
scorerecord migrate-db
scorerecord db-version
scorerecord db-health
scorerecord fixture-summary --limit 12
scorerecord ingest-worldcup --refresh
scorerecord ingest-history
scorerecord build-elo
scorerecord quality-report
```

Forecast publishing:

```powershell
scorerecord predict-fixtures --league WORLD_CUP_2026
scorerecord predict-fixtures --league EPL
scorerecord model-manifest
scorerecord freeze-forecast-snapshot --today
scorerecord forecast-report
```

Forecast GUI:

```powershell
scorerecord gui --host 127.0.0.1 --port 8765
```

Open:

```text
http://127.0.0.1:8765/
http://127.0.0.1:8765/partidos?fecha=2026-11-21&liga=EPL
http://127.0.0.1:8765/partido/<fixture_id>
http://127.0.0.1:8765/resultados?dias=30
http://127.0.0.1:8765/desempeno
http://127.0.0.1:8765/estado
```

Routes and the model+market blend are described in the README "Dashboard"
section. The dashboard needs an existing database file; it opens it read-only
and does not create or migrate it.

## Football-Data Historical Market Research

Use this for validation and diagnostics, not for daily future fixtures:

```powershell
scorerecord ingest-football-data --league EPL --from-season 2021-2022 --to-season 2024-2025 --refresh
scorerecord historical-odds-report --league EPL --market OU2.5
scorerecord market-benchmark-report --market OU2.5 --from 2021-08-01 --to 2025-05-31 --leagues EPL,LA_LIGA,SERIE_A,BUNDESLIGA,LIGUE_1,CHAMPIONSHIP --entry-odds avg_preclose --closing-odds avg_close --calibration prequential --bootstrap-block week --bootstrap-samples 2000 --min-train 500
```

Interpretation:

- `PROMISING`: proceed carefully to paper-candidate design.
- `INCONCLUSIVE`: segment diagnostics before any policy.
- `REJECTED_FOR_INDEPENDENT_VALUE`: do not build EV paper candidates from the
  independent model. Current project state is in this camp.

## EPL Daily Pilot

Dry-run first:

```powershell
scorerecord ingest-fixtures-odds-api-io --league EPL --league-slug england-premier-league --competition "English Premier League" --season 2026-2027 --dry-run --json
scorerecord run-epl-pilot-refresh --dry-run --json
scorerecord multi-league-readiness-report --leagues EPL,WORLD_CUP_2026 --json
```

Live run only after dry-run output and `db-health` are sane:

```powershell
scorerecord run-epl-pilot-refresh --json
scorerecord db-health
scorerecord fixture-summary --limit 12
```

## Club Forecast Leagues

Supported GUI club presets (`src/scorerecord/league_config.py`):

| Code | odds-api.io slugs | Football-Data file |
| --- | --- | --- |
| `LIGA_MX` | `mexico-liga-mx-apertura`, `mexico-liga-mx-clausura` | `new/MEX.csv` |
| `MLS` | `usa-mls`, `usa-mls-playoffs` | `new/USA.csv` |
| `EPL` | `england-premier-league` | `E0` |
| `LA_LIGA` | `spain-laliga` | `SP1` |
| `SERIE_A` | `italy-serie-a` | `I1` |
| `BUNDESLIGA` | `germany-bundesliga` | `D1` |
| `LIGUE_1` | `france-ligue-1` | `F1` |
| `CHAMPIONSHIP` | `england-championship` | `E1` |
| `ARGENTINA` | `argentina-primera-lpf-clausura`, `argentina-primera-lpf-clausura-playoffs` | `new/ARG.csv` |
| `BRASILEIRAO` | `brazil-brasileiro-serie-a` | `new/BRA.csv` |

Slugs are checked against odds-api.io `/leagues`. The account allows 100
requests per hour: the daily loop uses one per slug (13), each `fetch-odds`
run at most 60.

The daily production loop is `scripts/run_daily_club_forecast.sh`. Its steps can
be run by hand:

```powershell
scorerecord backup-db --keep 7
scorerecord ingest-results --league EPL        # Football-Data -> match_history -> fixture scores
scorerecord build-elo
scorerecord run-club-pilot-refresh --league EPL --skip-elo --horizon-days 21 --json
scorerecord freeze-forecast-snapshot --window-hours 36 --include-result
scorerecord settle-fixtures
scorerecord settle-forecast-snapshots
scorerecord prune-predictions --dry-run
scorerecord db-health --strict
```

`ingest-results` never creates teams. If Football-Data uses a club name with no
alias, the bridge reports it under `missing_teams` and that league stays stale
until the alias is added.

### Adding a league

1. Add a `LeagueConfig` in `src/scorerecord/league_config.py` with the
   odds-api.io slug and the Football-Data code, and add the code to
   `GUI_LEAGUE_ORDER` and to `LEAGUES` in `scripts/run_daily_club_forecast.sh`.
2. Map every odds-api.io team name to the Football-Data name in
   `SEED_ALIASES` (`src/scorerecord/aliases.py`). `run-club-pilot-refresh`
   lists the names it cannot resolve under `unmapped_names`. Promoted clubs
   that are new to the database are created by the seed with their
   Football-Data name.
3. Deploy. `migrate-db` (run by every deploy) seeds the aliases.
4. On the server: `ingest-results --league <CODE>` and `build-elo`. Past
   seasons, if missing, need `ingest-football-data` and `bridge-club-history`.
5. `model-compare --leagues <CODE> --write-params` decides the league's
   settings; other leagues in `league_params.json` are kept. Commit the file
   and deploy again. Until then the league uses the defaults.

First-time setup for Liga MX or MLS requires historical rows, bridge, ratings,
fixtures, and predictions:

```powershell
scorerecord ingest-football-data --league LIGA_MX --season 2026-2027 --refresh
scorerecord bridge-club-history --league LIGA_MX --source football-data --from-season 2026-2027 --to-season 2026-2027 --allow-create-teams
scorerecord run-club-pilot-refresh --league LIGA_MX --json

scorerecord ingest-football-data --league MLS --season 2026-2027 --refresh
scorerecord bridge-club-history --league MLS --source football-data --from-season 2026-2027 --to-season 2026-2027 --allow-create-teams
scorerecord run-club-pilot-refresh --league MLS --json
```

The dashboard opens on the next day with fixtures; check one league with:

```powershell
curl "http://100.104.72.97:8765/api/partidos?liga=LIGA_MX&fecha=2026-11-22"
```

SportMonks is available as a read-only probe/enrichment source:

```powershell
scorerecord provider-probe-sportmonks-fixture --fixture-id 19439627 --json
```

Store its token only in `.env` as `SPORTMONKS_API_TOKEN`; do not commit it.

Phase 13 audit, read-only by default:

```powershell
python scripts/run_phase13_audit.py --skip-gui
python scripts/run_phase13_audit.py --host 127.0.0.1 --port 8765
```

Use `--execute-refresh` only when you intentionally want the audit to run the
live EPL refresh.

The audit writes these evidence artifacts under `outputs/benchmarks/`:

- `artifact_audit.json`
- `prediction_provenance.json`
- `calibrator_audit.json`
- `training_scope_audit.json`
- `team_audit.json`
- `rating_coverage.json`
- `bridge_audit.json`
- `prediction_integrity.json`
- `db_health_final.json`
- `multi_league_readiness.json`
- `gui_validation.json`
- `daily_refresh_validation.json`
- `performance_and_regression.json`
- `phase_13_4_2_final_report.md`

The audit is allowed to return warnings while the EPL artifact has not been
published yet. It should not return `CRITICAL` or `ERROR` before Phase 13 is
considered closed.

## Model Changes

Every model change goes through the comparison gate:

```powershell
scorerecord model-compare                 # all dashboard leagues, about 10 minutes
scorerecord model-compare --leagues EPL --half-lives 1,2,3 --l2s 1,3
scorerecord model-compare --write-params  # also rewrite models/league_params.json
scorerecord model-compare --no-pool       # skip the pooled variant (faster)
```

It writes `outputs/benchmarks/model_compare_<stamp>.json|.md` and records one
`data_quality` row per league, which `/desempeno` shows. Run it on a copy of
the production database, review the report, then commit
`src/scorerecord/models/league_params.json` and deploy. Publishing reads that
file on every run; a league without an entry uses the defaults (three-year
half-life, ridge 1, uncalibrated 1X2, market weight 0.7).

Leagues listed in `POOLS` (`evaluate/model_compare.py`; today the EPL with the
Championship) also try a pooled variant: one rating per team across both
divisions and a goal-level offset per division, so promoted teams start from
their second-division rating. It is chosen only if it wins on validation and
the whole tuned model then passes the test window; `pool_with` in
`league_params.json` makes the daily publish train the same way.

`scorerecord-model-compare.timer` runs the gate on the 1st of every month
(about an hour of CPU at `Nice=15`). It refreshes the comparison on
`/desempeno` and never writes `league_params.json`.

## Homeserver Deploy

Commit first; only `HEAD` is deployed. Then:

```powershell
.\deploy\Deploy-ScoreRecord.ps1
```

The script, the cleanup of files dropped between releases, alerts, backups and
restore are described in [deployment.md](deployment.md).

Remote checks:

```powershell
ssh adrian@100.104.72.97 "cd ~/scorerecord && .venv/bin/scorerecord db-health"
ssh adrian@100.104.72.97 "systemctl --user list-timers 'scorerecord*' --no-pager"
curl http://100.104.72.97:8765/healthz
```

The dashboard answers only on the tailnet address, so `curl` must run from a
machine on the tailnet.

## Git Workflow

`origin` is the GitHub repository and the default branch is `main`:

```powershell
git remote -v
```

Normal commit flow:

```powershell
git status --short
git diff --stat
git add <files>
git commit -m "Short imperative summary"
git push -u origin <branch>
```

If you prefer feature branches:

```powershell
git switch -c codex/phase-13-audit-hardening
git add <files>
git commit -m "Harden Phase 13 artifact scope"
git push -u origin codex/phase-13-audit-hardening
```

Do not commit:

- `.env`
- local DB backups containing secrets or private operational history
- raw API responses with keys in URLs or headers
- generated `outputs/` artifacts unless intentionally needed for a report

## Troubleshooting

Read-only production checks that expose the silent failures:

```bash
journalctl --user -u scorerecord-odds.service -n 5 --no-pager        # matched_events=0 means no odds
journalctl --user -u scorerecord-postmatch.service -n 5 --no-pager   # settled=0 means no results
cd /home/adrian/scorerecord && .venv/bin/python - <<'EOF'
import sqlite3
c = sqlite3.connect("file:data/scorerecord.db?mode=ro", uri=True)
for title, sql in [
    ("played fixtures without score",
     "SELECT league_code, COUNT(*) FROM fixture WHERE kickoff_utc < datetime('now','-2 days') AND home_score IS NULL GROUP BY 1"),
    ("latest training row per competition",
     "SELECT tournament, MAX(date) FROM match_history GROUP BY 1 ORDER BY 2 DESC LIMIT 12"),
    ("forecast snapshots",
     "SELECT COUNT(*), MAX(as_of_utc) FROM forecast_snapshot"),
]:
    print("##", title)
    for row in c.execute(sql):
        print(*row)
EOF
```

The server has no `sqlite3` command-line tool, hence the venv Python.

No matches on dashboard:

```powershell
scorerecord fixture-summary --limit 12
scorerecord predict-fixtures --league EPL
scorerecord db-health
```

If `future_resolved=0`, the GUI is not broken; there are no future resolved
fixtures for that scope in the DB.

The dashboard reads, per fixture, the latest prediction run published before
kickoff. A fixture without such a run shows "Sin pronóstico publicado". Expected
goals and the score heatmap come from `prediction_score_grid`, which only exists
for runs published after schema version 8; older runs show the rest of the page.

If the dashboard answers "No pudimos generar esta página", the cause is in the
service log: `journalctl --user -u scorerecord-gui.service -n 50 --no-pager`.

Prediction run inserts zero rows:

- Check fixture `kickoff_utc`.
- Check fixture scores are still null.
- Check `league_code` matches the artifact scope.
- Check `team_rating` exists or expect Elo fallback warnings.

Odds coverage says `NO_ODDS`:

- Confirm `odds_snapshots` has rows.
- Confirm `OU2.5` rows have `line = 2.5`.
- Confirm both sides are present from the same bookmaker and snapshot.

Phase 6 remains blocked:

- Do not show EV, Kelly, stake, value, or betting recommendation language unless
  a future phase explicitly reopens paper candidates after market validation.
