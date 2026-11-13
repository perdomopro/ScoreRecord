# Homeserver Deployment Notes

Target: low-memory Linux homeserver reachable as `adrian@homeserver` or `adrian@100.104.72.97`.

Verified against the live server on 2026-11-13. Known production defects are
listed in [critical-review.md](critical-review.md); this document describes how
the deployment is wired, including the parts that currently misbehave.

## What Runs

| Unit | Schedule | Command | State on 2026-11-13 |
| --- | --- | --- | --- |
| `scorerecord-gui.service` | always on | `scorerecord gui --host 0.0.0.0 --port 8765` | active |
| `scorerecord-club-refresh.timer` | daily 05:35 (+20 min jitter) | `scripts/run_daily_club_forecast.sh` | active |
| `scorerecord-odds.timer` | hourly | `scorerecord fetch-odds` | active, captures nothing |
| `scorerecord-postmatch.timer` | daily 23:30 | `settle-fixtures`, `settle-forecast-snapshots`, `compute-clv` | active, settles nothing |
| `scorerecord-weekly.timer` | Sunday 09:00 | performance report, `goal-markets`, `calibrator-drift`, `db-summary` | active |
| `scorerecord-daily.timer` | — | World Cup daily job | not enabled (legacy) |
| `scorerecord-epl-pilot.timer` | — | single-league pilot | not enabled (superseded) |
| `scorerecord-preplay-odds.timer` | — | pre-kickoff odds | not enabled |

The daily refresh loops over `LIGA_MX MLS EPL ARGENTINA BRASILEIRAO`. For each
league it spends one odds-api.io request on fixtures, rebuilds Elo, retrains
Dixon-Coles and the Platt calibrators, and inserts predictions for every future
fixture. It does not ingest results and does not freeze forecast snapshots.

`fetch-odds` reads `ODDS_API_IO_LEAGUE`. The server `.env` does not set it, so
the job queries `international-fifa-world-cup` and matches zero club fixtures.

## Public Exposure

The GUI is published to the internet with Tailscale Funnel:

```text
https://<host>.<tailnet>.ts.net  ->  http://127.0.0.1:8765
```

Check or change it on the server:

```bash
tailscale funnel status
tailscale funnel reset        # stop publishing; the tailnet address keeps working
```

The service also binds `0.0.0.0`, so port 8765 is reachable from the LAN and
from the tailnet address directly. Every route is public, including the legacy
`/audit` view. Do not commit the real Funnel hostname to this repository.

## Layout

Production path:

```bash
/home/adrian/scorerecord
```

Runtime files on the server:

- `/home/adrian/scorerecord/.env`: secrets and environment.
- `/home/adrian/scorerecord/data/scorerecord.db`: SQLite database.
- `/home/adrian/scorerecord/data/raw`: network cache.
- `/home/adrian/scorerecord/.venv`: Python virtualenv (Python 3.13.5 today;
  the package requires 3.11 or newer).

The database is about 350 MB plus a WAL that reaches 60 MB. There is no
scheduled backup; the newest copy under `data/backups/` is from 2026-07-14.
Take one before any migration or cleanup:

```bash
cd /home/adrian/scorerecord
.venv/bin/python - <<'EOF'
import sqlite3, time
src = sqlite3.connect("data/scorerecord.db")
dst = sqlite3.connect(time.strftime("data/backups/scorerecord_%Y%m%dT%H%M%SZ.db", time.gmtime()))
src.backup(dst)
dst.close(); src.close()
EOF
```

The `sqlite3` command-line tool is not installed on the server; use the venv
Python for ad-hoc queries, opening the file read-only
(`sqlite3.connect("file:data/scorerecord.db?mode=ro", uri=True)`).

The deploy copies a `git archive`, so the server has no `.git` directory and
`model_artifact.code_git_sha` is recorded as `NULL`.

Secrets stay out of git. Set `ODDS_API_IO_KEY` only in the server `.env`.

## Deploy From Windows

Create and authorize a dedicated SSH key first:

```powershell
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\scorerecord_homeserver" -N "" -C "scorerecord-homeserver"
Get-Content "$env:USERPROFILE\.ssh\scorerecord_homeserver.pub" | ssh adrian@homeserver "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

Then deploy the current committed tree:

```powershell
.\deploy\Deploy-ScoreRecord.ps1 -HostName homeserver
```

Set the odds API key without writing it to git or shell history:

```powershell
.\deploy\Set-RemoteOddsApiKey.ps1 -HostName homeserver
```

The default Odds-API.io league for `fetch-odds` is still
`international-fifa-world-cup`. That default is wrong for the club leagues the
product now serves; until `fetch-odds` iterates the active leagues
(improvement plan, phase 1.4) the hourly job produces no rows.

The deploy script:

- archives `HEAD` with `git archive`;
- copies it to `/home/adrian/scorerecord`;
- runs `deploy/bootstrap_homeserver.sh`;
- verifies `model-manifest`, `fetch-odds --dry-run`, and `db-summary`.

## Bootstrap

`deploy/bootstrap_homeserver.sh` installs with:

```bash
"${PYTHON_BIN:-python3.11}" -m venv .venv   # set PYTHON_BIN if 3.11 is absent
. .venv/bin/activate
python -m pip install --upgrade pip wheel
python -m pip install -e .
SCORERECORD_DB_PATH=data/scorerecord.db .venv/bin/scorerecord init-db
```

If `.env` does not exist, it copies `.env.example` and sets mode `600`.

## First Server Run

After editing `/home/adrian/scorerecord/.env` with `ODDS_API_IO_KEY`:

```bash
cd /home/adrian/scorerecord
.venv/bin/scorerecord ingest-worldcup
.venv/bin/scorerecord ingest-history
.venv/bin/scorerecord build-elo
.venv/bin/scorerecord quality-report
.venv/bin/scorerecord walk-forward --start-year 2021
.venv/bin/scorerecord goal-markets --start-year 2021
.venv/bin/scorerecord predict-fixtures
.venv/bin/scorerecord model-manifest
.venv/bin/scorerecord fetch-odds --dry-run --min-interval-seconds 0
.venv/bin/scorerecord db-summary
```

For a real odds fetch:

```bash
.venv/bin/scorerecord fetch-odds
```

Keep the default odds rate limit unless the account quota changes.

## systemd User Timers

The bootstrap copies user units to `~/.config/systemd/user` and enables timers without starting them.

Start timers when `.env` is ready:

```bash
systemctl --user start scorerecord-gui.service
systemctl --user start scorerecord-club-refresh.timer
systemctl --user start scorerecord-odds.timer
# Keep these stopped until the derived-closing and paper policy checks are reviewed:
# systemctl --user start scorerecord-preplay-odds.timer
# systemctl --user start scorerecord-daily.timer
systemctl --user start scorerecord-postmatch.timer
systemctl --user start scorerecord-weekly.timer
```

Recommended schedule:

- Forecast and health dashboard: `scorerecord-gui.service` on port `8765`
- Daily club fixture/prediction refresh: `scorerecord-club-refresh.timer`
- Hourly odds: `scorerecord-odds.timer`
- Pre-play raw odds snapshots: use only after confirming the service no longer writes capture-time `is_closing=1`; closing is derived from complete `odds_snapshots` before kickoff.
- Daily predictions + paper recommendations: keep stopped until `db-health`, `model-manifest`, `compute-edge --dry-run`, and `recommend --dry-run` are reviewed after deployment.
- Nightly 23:30 settlement + CLV: `scorerecord-postmatch.timer`; it also
  settles frozen forecast snapshots from local fixture scores.
- Weekly monitoring: `scorerecord-weekly.timer`; it records
  `forecast-performance-report --record-quality`, prints `forecast-report`, then
  runs goal-market gates, Platt coefficient drift, and DB summary.

Resource limits are deliberately conservative:

- `MemoryMax=700M` for odds/postmatch jobs.
- `MemoryMax=350M` for the read-only GUI.
- `MemoryMax=450M` for the daily club forecast refresh.
- `MemoryMax=900M` for modeling/monitoring jobs.
- `OMP_NUM_THREADS=2`, `OPENBLAS_NUM_THREADS=2`.
- `Nice=10`, `IOSchedulingClass=idle`.

Inspect logs:

```bash
journalctl --user -u scorerecord-daily.service -n 100 --no-pager
journalctl --user -u scorerecord-club-refresh.service -n 100 --no-pager
journalctl --user -u scorerecord-odds.service -n 100 --no-pager
journalctl --user -u scorerecord-gui.service -n 100 --no-pager
```

Open the dashboard from the Tailscale network:

```text
http://100.104.72.97:8765/
```

No unit has `OnFailure=` and nothing sends notifications. A job that exits 0
with `OK_NO_EVENTS`, `matched_events=0`, or `settled=0` looks healthy to
systemd, so read the journal output, not only the exit status.

## Important Operational Gate

Do not use real staking until paper CLV is positive on a forward sample. Keep quarter Kelly and the 1-unit cap until there are at least 100 tracked paper picks with positive CLV.

## Run ID Scope

`ingest_run.run_id` values are local to each SQLite database. The homeserver manifest is authoritative for production even when its `predictive_validation_run_id` differs from the local development DB. `market_validation_status=NOT_TESTED` means the artifact has not beaten real forward market/CLV evidence yet. Verify production gates with:

```bash
cd /home/adrian/scorerecord
.venv/bin/scorerecord migrate-db
.venv/bin/scorerecord db-health
.venv/bin/scorerecord model-manifest
.venv/bin/python -c "import sqlite3; c=sqlite3.connect('file:data/scorerecord.db?mode=ro', uri=True); [print(*r) for r in c.execute(\"SELECT run_id, metric_name, metric_value FROM data_quality WHERE check_name IN ('predictive_baseline_gate','goal_market_gate') ORDER BY run_id, metric_name\")]"
```
