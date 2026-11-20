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
# Units retired from the repo: stop them and remove the installed copies.
for legacy in scorerecord-daily scorerecord-epl-pilot scorerecord-preplay-odds \
    scorerecord-postmatch scorerecord-weekly scorerecord-model-compare; do
  systemctl --user disable --now "$legacy.timer" >/dev/null 2>&1 || true
  rm -f "$HOME/.config/systemd/user/$legacy.service" "$HOME/.config/systemd/user/$legacy.timer"
done
cp deploy/systemd/*.service deploy/systemd/*.timer "$HOME/.config/systemd/user/"

systemctl --user daemon-reload
systemctl --user enable scorerecord-gui.service
# scorerecord-odds.timer is enabled by hand once the provider account allows odds (see docs/deployment.md).
systemctl --user enable scorerecord-club-refresh.timer
loginctl enable-linger "$USER" >/dev/null 2>&1 || true

cat <<'MSG'
ScoreRecord installed.

Next server-side steps:
1. Edit /home/adrian/scorerecord/.env and set ODDS_API_IO_KEY.
2. Run: .venv/bin/scorerecord db-health
3. Restart the dashboard to load the new code:
   systemctl --user restart scorerecord-gui.service
4. Start the timers:
   systemctl --user start scorerecord-club-refresh.timer scorerecord-odds.timer
5. Run the daily loop once by hand to check it end to end:
   systemctl --user start scorerecord-club-refresh.service
   journalctl --user -u scorerecord-club-refresh.service -n 80 --no-pager
MSG
