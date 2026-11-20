# Homeserver Deployment

Target: low-memory Linux homeserver, `adrian@100.104.72.97` on the tailnet.
Verified against the live server on 2026-11-19.

## Access

The dashboard is reachable only through the tailnet:

```text
http://100.104.72.97:8765/
```

`scorerecord-gui.service` binds the address in `SCORERECORD_GUI_HOST` from the
server `.env` (the tailnet IP); without it, `127.0.0.1`. It is not reachable
from the home LAN (`192.168.100.0/24`) or the internet. Tailscale Funnel is off;
check with `tailscale funnel status` (`No serve config`). If the tailnet
address is not up yet at boot the bind fails and systemd retries every 10 s
indefinitely.

## What Runs

| Unit | Schedule | Does |
| --- | --- | --- |
| `scorerecord-club-refresh.timer` | daily 05:35 (+20 min jitter) | `scripts/run_daily_club_forecast.sh`: `backup-db`, `ingest-results` per league, `build-elo` once, `run-club-pilot-refresh --skip-elo --horizon-days 21` per league, `freeze-forecast-snapshot --window-hours 36 --include-result`, `settle-fixtures`, `settle-forecast-snapshots`, `prune-predictions`, `db-health` |
| `scorerecord-odds.timer` | every 3 h | `fetch-odds --horizon-hours 48 --max-requests 60` by provider event id |
| `scorerecord-postmatch.timer` | daily 23:30 | `ingest-results`, `settle-fixtures`, `settle-forecast-snapshots` |
| `scorerecord-weekly.timer` | Sunday 09:00 | performance report, `goal-markets`, `calibrator-drift`, `db-summary` |
| `scorerecord-model-compare.timer` | 1st of the month 01:30 | `model-compare` (evidence for `/desempeno`; does not change production settings) |
| `scorerecord-gui.service` | always on | read-only dashboard (`src/scorerecord/web`) |
| `scorerecord-notify@.service` | on failure | see "Failure alerts" |

All units run as systemd user services with `Nice=10`, idle IO, one or two BLAS
threads, and `MemoryMax` between 300 and 900 MB. The daily loop peaks around
420 MB; the dashboard uses about 50 MB.

Logs:

```bash
journalctl --user -u scorerecord-club-refresh.service -n 100 --no-pager
journalctl --user -u scorerecord-odds.service -n 50 --no-pager
journalctl --user -u scorerecord-gui.service -n 50 --no-pager
systemctl --user list-timers 'scorerecord*' --no-pager
```

## Failure Alerts

Every unit has `OnFailure=scorerecord-notify@%n.service`, which runs
`scripts/notify_failure.sh`. It always appends a line to `data/alerts.log`;
`db-health` and `/estado` show `service_failures` as `WARNING` for 7 days after
an entry. When `SCORERECORD_NOTIFY_URL` is set in `.env` it also POSTs the last
journal lines to that URL (headers follow ntfy: `Title`, `Tags`). Without it no
message leaves the server.

The dashboard uses `Restart=always`, so a crash restarts it instead of
triggering an alert.

Test the path without touching real units:

```bash
systemd-run --user --unit=scorerecord-selftest \
  -p "OnFailure=scorerecord-notify@scorerecord-selftest.service.service" /bin/false
cat ~/scorerecord/data/alerts.log      # then delete the selftest line
```

## Layout

```text
/home/adrian/scorerecord
├── .env                 secrets and settings, mode 600, never in git
├── .deploy-manifest     files shipped by the current release (see Deploy)
├── VERSION              commit SHA of the current release
├── .venv/               Python virtualenv (Python 3.13 today; 3.11+ required)
├── data/scorerecord.db  SQLite database, about 260 MB after VACUUM
├── data/backups/        rotating backups
├── data/raw/            provider download cache
├── data/alerts.log      unit failures (only exists after one)
└── outputs/             refresh and benchmark JSON artifacts
```

The `sqlite3` command-line tool is not installed; use the venv Python and open
the file read-only for ad-hoc queries:
`sqlite3.connect("file:data/scorerecord.db?mode=ro", uri=True)`.

## Backups

`backup-db` runs first in the daily loop. It writes a consistent copy with the
SQLite backup API, gzips it (`scorerecord_<UTC stamp>.db.gz`), and keeps the 7
newest copies plus the newest copy of each of the last 4 ISO weeks. Files with
other names (for example `scorerecord_predeploy_*.db`) are never rotated.

Restore:

```bash
cd /home/adrian/scorerecord
systemctl --user stop scorerecord-gui.service
gunzip -k data/backups/scorerecord_<stamp>.db.gz
mv data/scorerecord.db data/scorerecord.db.broken
mv data/backups/scorerecord_<stamp>.db data/scorerecord.db
systemctl --user start scorerecord-gui.service
```

To reclaim space after large deletions, once and with the dashboard stopped:
`.venv/bin/scorerecord prune-predictions --vacuum` (8 s on the current file).

## Deploy From Windows

```powershell
.\deploy\Deploy-ScoreRecord.ps1
```

Deploys the committed `HEAD` only (it warns about uncommitted changes):

1. `git archive` of `HEAD`, copied to the server.
2. `deploy/apply_release.sh` deletes every file the previous release shipped
   (`.deploy-manifest`) that this release no longer contains, unpacks the
   archive, and writes the new manifest. Only paths from a manifest can be
   deleted; `data/`, `.venv/`, `.env`, and `outputs/` never are.
3. `VERSION` gets the commit SHA, so `model_artifact.code_git_sha` is recorded.
4. `deploy/bootstrap_homeserver.sh` reinstalls the package into `.venv`, runs
   migrations, installs the systemd units, and removes retired ones.
5. Restarts the dashboard and runs `db-health` and `model-manifest`.

Parameters: `-HostName` (default `100.104.72.97`), `-KeyPath` (used only if the
file exists, otherwise your default SSH identity), `-SkipBootstrap`.

Set the odds API key without writing it to git or shell history:

```powershell
.\deploy\Set-RemoteOddsApiKey.ps1 -HostName 100.104.72.97
```

## Provider Facts

- **Results come from Football-Data.** odds-api.io drops events once they
  finish, so `ingest-results` downloads the current Football-Data file per
  league and copies scores onto fixtures by league, teams, and date (±2 days).
  Some feeds (Brasileirao, Liga MX) publish several days late.
- **The odds plan allows 100 requests per hour.** The daily loop uses one per
  league slug (13 for the ten leagues); each odds run uses at most 60.
- **The odds plan allows one bookmaker (Unibet)** and returns 1X2 and
  both-teams-score, not totals. `fetch-odds` intersects `ODDS_API_IO_BOOKMAKERS`
  with `/bookmakers/selected`, because a single paid-only name makes the
  provider reject the whole request.

## First Install On A New Server

```bash
cd /home/adrian/scorerecord
bash deploy/bootstrap_homeserver.sh        # venv, .env from .env.example, units
nano .env                                  # ODDS_API_IO_KEY, SCORERECORD_GUI_HOST
.venv/bin/scorerecord ingest-history
.venv/bin/scorerecord ingest-results            # every dashboard league
.venv/bin/scorerecord build-elo
systemctl --user start scorerecord-gui.service scorerecord-club-refresh.timer \
  scorerecord-odds.timer scorerecord-postmatch.timer scorerecord-weekly.timer \
  scorerecord-model-compare.timer
systemctl --user start scorerecord-club-refresh.service   # first full run by hand
```

Club history for a new league also needs `ingest-football-data` and
`bridge-club-history` over past seasons; see the runbook.

## Run ID Scope

`ingest_run.run_id` values are local to each SQLite database. The homeserver
database is authoritative for production. `market_validation_status=NOT_TESTED`
means the artifact has not been shown to beat market prices on forward data.
