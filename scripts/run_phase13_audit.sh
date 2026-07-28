#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." >/dev/null 2>&1 && pwd)"
cd "$DIR"

# Ensure outputs/benchmarks exists
mkdir -p outputs/benchmarks

# Run the python audit script
if [ -f ".venv/bin/python" ]; then
    .venv/bin/python scripts/run_phase13_audit.py "$@"
else
    .venv/Scripts/python scripts/run_phase13_audit.py "$@"
fi
