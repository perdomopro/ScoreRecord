#!/usr/bin/env bash
# Run by OnFailure= of every ScoreRecord unit (scorerecord-notify@<unit>.service).
#
# Always appends the failure to data/alerts.log, which db-health and /estado
# read. When SCORERECORD_NOTIFY_URL is set in .env (for example an ntfy.sh topic
# URL), it also POSTs the last journal lines there.
set -u

unit="${1:-unknown}"
app_dir="${SCORERECORD_APP_DIR:-/home/adrian/scorerecord}"
stamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

mkdir -p "$app_dir/data"
printf '%s %s failed\n' "$stamp" "$unit" >> "$app_dir/data/alerts.log"
echo "ScoreRecord: $unit failed at $stamp"

if [ -n "${SCORERECORD_NOTIFY_URL:-}" ]; then
  details="$(journalctl --user -u "$unit" -n 12 --no-pager -o cat 2>/dev/null | tail -c 3000)"
  curl -fsS -m 15 \
    -H "Title: ScoreRecord: $unit fallo" \
    -H "Tags: warning" \
    --data-binary "${details:-sin detalle en el journal}" \
    "$SCORERECORD_NOTIFY_URL" >/dev/null \
    || echo "could not deliver the notification"
fi
