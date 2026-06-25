# Homeserver Deployment Notes

Target: low-memory Linux homeserver.

## Suggested Layout

```bash
mkdir -p /home/adrian/scorerecord
cd /home/adrian/scorerecord
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e .
```

Keep secrets in `/home/adrian/scorerecord/.env` or systemd `EnvironmentFile=`, never in git.

## Fase 0 Commands

```bash
SCORERECORD_DB_PATH=data/scorerecord.db scorerecord init-db
SCORERECORD_DB_PATH=data/scorerecord.db scorerecord ingest-worldcup
SCORERECORD_DB_PATH=data/scorerecord.db scorerecord ingest-history
SCORERECORD_DB_PATH=data/scorerecord.db scorerecord build-elo
SCORERECORD_DB_PATH=data/scorerecord.db scorerecord quality-report
SCORERECORD_DB_PATH=data/scorerecord.db scorerecord walk-forward --start-year 2021
SCORERECORD_DB_PATH=data/scorerecord.db scorerecord tune-decay --start-year 2021
SCORERECORD_DB_PATH=data/scorerecord.db scorerecord goal-markets --start-year 2021
SCORERECORD_DB_PATH=data/scorerecord.db scorerecord predict-fixtures
SCORERECORD_DB_PATH=data/scorerecord.db scorerecord model-manifest
SCORERECORD_DB_PATH=data/scorerecord.db scorerecord settle-fixtures --dry-run
SCORERECORD_DB_PATH=data/scorerecord.db scorerecord fetch-odds --dry-run
SCORERECORD_DB_PATH=data/scorerecord.db scorerecord compute-edge --dry-run
SCORERECORD_DB_PATH=data/scorerecord.db scorerecord recommend --dry-run
SCORERECORD_DB_PATH=data/scorerecord.db scorerecord compute-clv --dry-run
SCORERECORD_DB_PATH=data/scorerecord.db scorerecord db-summary
```

For a real odds fetch, set `ODDS_API_IO_KEY` and keep the default rate limit unless you have upgraded quota:

```bash
SCORERECORD_DB_PATH=data/scorerecord.db scorerecord fetch-odds
```

## Future systemd Service Shape

```ini
[Service]
Type=oneshot
WorkingDirectory=/home/adrian/scorerecord
EnvironmentFile=/home/adrian/scorerecord/.env
Environment=OMP_NUM_THREADS=4
Environment=OPENBLAS_NUM_THREADS=4
MemoryMax=1200M
Nice=10
IOSchedulingClass=idle
ExecStart=/home/adrian/scorerecord/.venv/bin/scorerecord daily
```

Add timers only after the ingestion and model commands are idempotent in production.
