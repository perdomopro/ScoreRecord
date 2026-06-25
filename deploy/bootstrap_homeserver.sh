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

SCORERECORD_DB_PATH=data/scorerecord.db .venv/bin/scorerecord init-db

mkdir -p "$HOME/.config/systemd/user"
cp deploy/systemd/*.service deploy/systemd/*.timer "$HOME/.config/systemd/user/"

systemctl --user daemon-reload
systemctl --user enable scorerecord-odds.timer
systemctl --user enable scorerecord-daily.timer
systemctl --user enable scorerecord-postmatch.timer
systemctl --user enable scorerecord-weekly.timer
loginctl enable-linger "$USER" >/dev/null 2>&1 || true

cat <<'MSG'
ScoreRecord installed.

Next server-side steps:
1. Edit /home/adrian/scorerecord/.env and set ODDS_API_IO_KEY.
2. Run: systemctl --user start scorerecord-daily.service
3. Inspect: journalctl --user -u scorerecord-daily.service -n 100 --no-pager
4. Start timers when ready:
   systemctl --user start scorerecord-odds.timer scorerecord-daily.timer scorerecord-postmatch.timer scorerecord-weekly.timer
MSG
