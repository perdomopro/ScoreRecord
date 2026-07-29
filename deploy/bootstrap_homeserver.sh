#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${SCORERECORD_APP_DIR:-/home/adrian/scorerecord}"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"

cd "$APP_DIR"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="python3"
fi

"$PYTHON_BIN" -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip wheel
python -m pip install -e .

mkdir -p data data/raw logs

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
fi

SCORERECORD_DB_PATH=data/scorerecord.db .venv/bin/scorerecord migrate-db

mkdir -p "$HOME/.config/systemd/user"
cp deploy/systemd/*.service deploy/systemd/*.timer "$HOME/.config/systemd/user/"

systemctl --user daemon-reload
systemctl --user enable scorerecord-gui.service
systemctl --user enable scorerecord-odds.timer
systemctl --user enable scorerecord-club-refresh.timer
systemctl --user enable scorerecord-postmatch.timer
systemctl --user enable scorerecord-weekly.timer
loginctl enable-linger "$USER" >/dev/null 2>&1 || true

cat <<'MSG'
ScoreRecord installed.

Next server-side steps:
1. Edit /home/adrian/scorerecord/.env and set ODDS_API_IO_KEY.
2. Run: .venv/bin/scorerecord db-health
3. Inspect dry-runs: .venv/bin/scorerecord compute-edge --dry-run && .venv/bin/scorerecord recommend --dry-run
4. Start the dashboard when ready:
   systemctl --user start scorerecord-gui.service
5. Start conservative timers when ready:
   systemctl --user start scorerecord-odds.timer scorerecord-postmatch.timer scorerecord-weekly.timer
   systemctl --user start scorerecord-club-refresh.timer
6. Start preplay raw snapshots or daily recommendations only after reviewing db-health and dry-runs:
   systemctl --user start scorerecord-preplay-odds.timer
   systemctl --user start scorerecord-daily.timer
MSG
