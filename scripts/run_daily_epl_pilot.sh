#!/usr/bin/env bash
set -e

# Controlled Daily EPL Pilot Refresh

# Determine the absolute path to the virtualenv and command
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." >/dev/null 2>&1 && pwd)"
SCORERECORD_CMD="$DIR/.venv/bin/scorerecord"

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Starting EPL Pilot Refresh"

# Pass all arguments through to the native CLI command (e.g. --dry-run)
"$SCORERECORD_CMD" run-epl-pilot-refresh --json "$@"

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Finished EPL Pilot Refresh"
