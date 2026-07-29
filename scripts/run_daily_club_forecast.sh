#!/usr/bin/env bash
set -u

APP_DIR="${SCORERECORD_APP_DIR:-/home/adrian/scorerecord}"
LEAGUES="${SCORERECORD_DAILY_CLUB_LEAGUES:-LIGA_MX MLS EPL ARGENTINA BRASILEIRAO}"
MAX_REQUESTS="${SCORERECORD_DAILY_CLUB_MAX_REQUESTS:-1}"

cd "$APP_DIR" || exit 1

status=0
echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Starting club forecast refresh: $LEAGUES"

for league in $LEAGUES; do
  echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Refreshing $league"
  if ! .venv/bin/scorerecord run-club-pilot-refresh \
      --league "$league" \
      --max-requests "$MAX_REQUESTS" \
      --json; then
    status=1
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] $league refresh failed"
  fi
done

.venv/bin/scorerecord db-health || status=1

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Finished club forecast refresh status=$status"
exit "$status"
