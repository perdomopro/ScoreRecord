# ScoreRecord Runbook

This is the operational guide for running ScoreRecord locally, validating changes,
shipping them to the homeserver, and pushing them to git. Keep secrets in `.env`;
never paste API keys into commands, commits, reports, or screenshots.

## Current Operating Truth

- Product mode remains `FORECAST_ONLY` / research.
- Phase 3 rejected independent Dixon-Coles value against club OU2.5 market
  probabilities, so Phase 6 value/paper candidates stay blocked.
- The active operating path is multi-league forecasting and market-anchored
  research, not betting recommendations.
- The local `.venv` in this workspace is stale: its Python points at a removed
  WindowsApps Python 3.11 path. Full tests can still run with the Codex bundled
  Python by pre-importing bundled NumPy and then loading pytest from the stale
  venv site-packages.
- Local `db-health` is green except `odds_snapshots` is empty. That means the
  local DB is structurally healthy but has no live odds capture evidence.
- Local `fixture-summary` currently has no future World Cup fixtures. Club/EPL
  fixtures are the practical path for a daily GUI after the World Cup schedule
  window has passed.

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

Optional provider variables:

```dotenv
THE_ODDS_API_KEY=<secret>
API_FOOTBALL_KEY=<secret>
RESPONSIBLE_GAMBLING_URL=<url>
```

## Local Setup

Preferred setup after repairing Python on Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
```

If the local Python launcher is unavailable, use the Codex bundled Python for
CLI commands:

```powershell
$py = "C:\Users\ajvpl\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$env:PYTHONPATH = "src"
& $py -m scorerecord.cli db-health
```

To run pytest with the current stale venv without mixing its incompatible NumPy:

```powershell
$py = "C:\Users\ajvpl\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$env:PYTHONPATH = "src"
& $py -c "import sys, numpy; sys.path.append(r'.venv\Lib\site-packages'); import pytest; raise SystemExit(pytest.main(['-q']))"
```

`ruff.exe` still works directly from the stale venv:

```powershell
.\.venv\Scripts\ruff.exe check src tests scripts
```

Full-repo `ruff` currently has legacy findings. For commits, run targeted ruff
over touched files and keep the full-repo debt visible instead of pretending it
is closed.

## Local Validation Gate

Run this before committing:

```powershell
$py = "C:\Users\ajvpl\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$env:PYTHONPATH = "src"
& $py -m compileall src tests scripts
& $py -c "import sys, numpy; sys.path.append(r'.venv\Lib\site-packages'); import pytest; raise SystemExit(pytest.main(['-q']))"
.\.venv\Scripts\ruff.exe check <touched-python-files>
git diff --check
git status --short
```

Current verified local result on 2026-07-28:

```text
compileall src tests scripts: passed
pytest -q: 241 passed
targeted ruff for Phase 13 artifact-scope files: passed
git diff --check: passed
full-repo ruff: fails with legacy style findings
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

This local clone currently has no `origin` remote configured. Add one once the
GitHub/Git server repo exists:

```powershell
git remote add origin <repo-url>
git remote -v
```

Normal commit flow:

```powershell
git status --short
git diff --stat
git add <files>
git commit -m "Short imperative summary"
git push -u origin master
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

No matches on dashboard:

```powershell
scorerecord fixture-summary --limit 12
scorerecord predict-fixtures --league EPL
scorerecord db-health
```

If `future_resolved=0`, the GUI is not broken; there are no future resolved
fixtures for that scope in the DB.

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
