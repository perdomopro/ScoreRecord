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
- Full `pytest` is not green: 3 tests in `test_league_aware_artifacts.py` seed a
  fixture on 2026-08-15, which is now in the past, so the fixture is skipped as
  already started.

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
pip install tzdata
```

`tzdata` is required on Windows because `zoneinfo` has no system timezone
database there. Without it 36 tests fail with `ZoneInfoNotFoundError`. It is
not yet declared in `pyproject.toml`.

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
pytest -q: 270 passed, 3 failed (test_league_aware_artifacts.py, hardcoded past date)
pytest -q without tzdata: 237 passed, 36 failed
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
http://127.0.0.1:8765/today
http://127.0.0.1:8765/today?league=EPL
http://127.0.0.1:8765/today?league=WORLD_CUP_2026
http://127.0.0.1:8765/audit
```

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

- `LIGA_MX`: odds-api.io slug `mexico-liga-mx-apertura` (Apertura only; the
  Clausura needs a different slug)
- `MLS`: odds-api.io slug `usa-mls`
- `EPL`: odds-api.io slug `england-premier-league`
- `ARGENTINA`: odds-api.io slug `argentina-primera-lpf-clausura` (returns no
  events as of 2026-11-13)
- `BRASILEIRAO`: odds-api.io slug `brazil-brasileiro-serie-a`

The daily production loop is `scripts/run_daily_club_forecast.sh`, which calls
`run-club-pilot-refresh` once per league. That command does not ingest results.
To bring match history up to date by hand, per league:

```powershell
scorerecord ingest-football-data --league EPL --season 2026-2027 --refresh
scorerecord bridge-club-history --league EPL --source football-data --from-season 2026-2027 --to-season 2026-2027 --allow-create-teams
scorerecord build-elo
```

New rows only reach the model once the fixed `trained_until = 2026-06-11`
cutoff in `models/manifest.py` is replaced (improvement plan, phase 1.2).

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

The dashboard is date-scoped, but it shows `Proximos partidos cargados` with
date links:

```powershell
curl "http://100.104.72.97:8765/today?league=ALL&view=forecast"
curl "http://100.104.72.97:8765/today?league=LIGA_MX&view=forecast"
curl "http://100.104.72.97:8765/today?league=MLS&view=forecast"
```

The main forecast navbar should show forecast navigation and `/audit`; it should
not surface `view=paper` or `view=research` as primary product tabs.

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

## Homeserver Deploy

Deploy current working tree to the homeserver:

```powershell
& .\deploy\Deploy-ScoreRecord.ps1 -HostName 100.104.72.97 -KeyPath "$env:USERPROFILE\.ssh\scorerecord_homeserver" -SkipBootstrap
```

Remote validation:

```powershell
ssh -i "$env:USERPROFILE\.ssh\scorerecord_homeserver" adrian@100.104.72.97 "cd /home/adrian/scorerecord && .venv/bin/scorerecord db-health"
ssh -i "$env:USERPROFILE\.ssh\scorerecord_homeserver" adrian@100.104.72.97 "cd /home/adrian/scorerecord && .venv/bin/python -m pytest -q"
ssh -i "$env:USERPROFILE\.ssh\scorerecord_homeserver" adrian@100.104.72.97 "cd /home/adrian/scorerecord && .venv/bin/python -m ruff check <touched-python-files>"
```

GUI service:

```powershell
ssh -i "$env:USERPROFILE\.ssh\scorerecord_homeserver" adrian@100.104.72.97 "systemctl --user restart scorerecord-gui.service"
ssh -i "$env:USERPROFILE\.ssh\scorerecord_homeserver" adrian@100.104.72.97 "systemctl --user status scorerecord-gui.service --no-pager"
curl http://100.104.72.97:8765/healthz
```

Timers:

```powershell
ssh -i "$env:USERPROFILE\.ssh\scorerecord_homeserver" adrian@100.104.72.97 "systemctl --user list-timers 'scorerecord-*' --all --no-pager"
ssh -i "$env:USERPROFILE\.ssh\scorerecord_homeserver" adrian@100.104.72.97 "systemctl --user status scorerecord-epl-pilot.timer --no-pager"
```

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

For the multi-league forecast view, remember that the page is date-scoped. If
today has no fixtures, open the next fixture date explicitly:

```powershell
curl "http://100.104.72.97:8765/today?date=2026-08-21&league=ALL&view=forecast"
curl "http://100.104.72.97:8765/today?date=2026-08-21&league=EPL&view=forecast"
```

The GUI intentionally reads the latest prediction run per fixture. A new EPL
publish should not hide older World Cup forecasts on `league=ALL`.

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
