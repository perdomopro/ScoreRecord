#!/usr/bin/env bash
# Daily forecast loop for the club leagues.
#
#   backup -> results -> Elo -> fixtures + predictions -> freeze -> settle -> prune -> health
#
# Every step runs even if an earlier one fails, so one league or one provider
# error does not stop the rest. The exit status is non-zero if any step failed.
set -u

APP_DIR="${SCORERECORD_APP_DIR:-/home/adrian/scorerecord}"
LEAGUES="${SCORERECORD_DAILY_CLUB_LEAGUES:-LIGA_MX MLS EPL LA_LIGA SERIE_A BUNDESLIGA LIGUE_1 CHAMPIONSHIP ARGENTINA BRASILEIRAO}"
MAX_REQUESTS="${SCORERECORD_DAILY_CLUB_MAX_REQUESTS:-1}"
HORIZON_DAYS="${SCORERECORD_PREDICTION_HORIZON_DAYS:-21}"
FREEZE_WINDOW_HOURS="${SCORERECORD_FREEZE_WINDOW_HOURS:-36}"
BACKUP_KEEP="${SCORERECORD_BACKUP_KEEP:-7}"
CLI=".venv/bin/scorerecord"

cd "$APP_DIR" || exit 1

status=0
log() { echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] $*"; }
step() {
  log "$*"
  if ! "$@"; then
    status=1
    log "FAILED: $*"
  fi
}

log "Starting club forecast refresh: $LEAGUES"

step "$CLI" backup-db --keep "$BACKUP_KEEP"

# 1. Results from Football-Data into match_history and onto played fixtures.
for league in $LEAGUES; do
  step "$CLI" ingest-results --league "$league"
done

# 2. Ratings once for every league.
step "$CLI" build-elo

# 3. Upcoming fixtures and predictions per league.
for league in $LEAGUES; do
  step "$CLI" run-club-pilot-refresh \
    --league "$league" \
    --max-requests "$MAX_REQUESTS" \
    --horizon-days "$HORIZON_DAYS" \
    --skip-elo \
    --json
done

# 4. Freeze what is about to be played; settle what has been played.
step "$CLI" freeze-forecast-snapshot --window-hours "$FREEZE_WINDOW_HOURS" --include-result
step "$CLI" settle-fixtures
step "$CLI" settle-forecast-snapshots

# 5. Keep the database from growing with superseded prediction runs.
step "$CLI" prune-predictions

step "$CLI" db-health

log "Finished club forecast refresh status=$status"
exit "$status"
