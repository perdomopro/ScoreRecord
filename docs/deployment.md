# Homeserver Deployment Notes

Target: low-memory Linux homeserver reachable as `adrian@homeserver` or `adrian@100.104.72.97`.

## Layout

Production path:

```bash
/home/adrian/scorerecord
```

Runtime files on the server:

- `/home/adrian/scorerecord/.env`: secrets and environment.
- `/home/adrian/scorerecord/data/scorerecord.db`: SQLite database.
- `/home/adrian/scorerecord/data/raw`: network cache.
- `/home/adrian/scorerecord/.venv`: Python virtualenv.

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

The default Odds-API.io league is `international-fifa-world-cup`; override with `ODDS_API_IO_LEAGUE` only if the provider changes the slug.

The deploy script:

- archives `HEAD` with `git archive`;
- copies it to `/home/adrian/scorerecord`;
- runs `deploy/bootstrap_homeserver.sh`;
- verifies `model-manifest`, `fetch-odds --dry-run`, and `db-summary`.

## Bootstrap

`deploy/bootstrap_homeserver.sh` installs with:

```bash
python3.11 -m venv .venv
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
systemctl --user start scorerecord-odds.timer
systemctl --user start scorerecord-daily.timer
systemctl --user start scorerecord-postmatch.timer
systemctl --user start scorerecord-weekly.timer
```

Recommended schedule:

- Hourly odds: `scorerecord-odds.timer`
- Daily 08:00 predictions + paper recommendations: `scorerecord-daily.timer`
- Nightly 23:30 settlement + CLV: `scorerecord-postmatch.timer`
- Weekly monitoring: `scorerecord-weekly.timer`

Resource limits are deliberately conservative:

- `MemoryMax=700M` for odds/postmatch jobs.
- `MemoryMax=900M` for modeling/monitoring jobs.
- `OMP_NUM_THREADS=2`, `OPENBLAS_NUM_THREADS=2`.
- `Nice=10`, `IOSchedulingClass=idle`.

Inspect logs:

```bash
journalctl --user -u scorerecord-daily.service -n 100 --no-pager
journalctl --user -u scorerecord-odds.service -n 100 --no-pager
```

## Important Operational Gate

Do not use real staking until paper CLV is positive on a forward sample. Keep quarter Kelly and the 1-unit cap until there are at least 100 tracked paper picks with positive CLV.

## Run ID Scope

`ingest_run.run_id` values are local to each SQLite database. The homeserver manifest is authoritative for production even when its `validated_run_id` differs from the local development DB. Verify production gates with:

```bash
cd /home/adrian/scorerecord
.venv/bin/scorerecord model-manifest
sqlite3 data/scorerecord.db "SELECT run_id, metric_name, metric_value FROM data_quality WHERE check_name='goal_market_gate' ORDER BY run_id, metric_name;"
```
