#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${SCORERECORD_ROOT:-/home/adrian/scorerecord}"
OUT_ROOT="${SCORERECORD_BACKTEST_OUT:-outputs/backtests}"
LEAGUES="${SCORERECORD_LEAGUES:-EPL LA_LIGA SERIE_A BUNDESLIGA LIGUE_1 CHAMPIONSHIP}"
FROM_DATE="${SCORERECORD_FROM_DATE:-2021-08-01}"
TO_DATE="${SCORERECORD_TO_DATE:-2025-05-31}"
FROM_SEASON="${SCORERECORD_FROM_SEASON:-2019-2020}"
TO_SEASON="${SCORERECORD_TO_SEASON:-2024-2025}"
MIN_TRAIN="${SCORERECORD_MIN_TRAIN:-500}"
MARKET_SHRINK_ALPHAS="${SCORERECORD_MARKET_SHRINK_ALPHAS:-1.0,0.75,0.5,0.25}"
MIN_SCORES="${SCORERECORD_MIN_SCORES:-0,0.05,0.075,0.10,0.125}"
MIN_EDGES="${SCORERECORD_MIN_EDGES:-0.03,0.05,0.075}"
MAX_PICKS_PER_DAYS="${SCORERECORD_MAX_PICKS_PER_DAYS:-none,10,5,3}"
LOCK_DIR="${SCORERECORD_MULTILEAGUE_LOCK:-/tmp/scorerecord-multileague-validation.lock}"

if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
  echo "Another multi-league validation appears to be running: ${LOCK_DIR}" >&2
  exit 2
fi
trap 'rmdir "${LOCK_DIR}"' EXIT

cd "${ROOT_DIR}"
mkdir -p "${OUT_ROOT}"

echo "===== $(date -Is) multi-league validation start ====="
echo "root=${ROOT_DIR}"
echo "out_root=${OUT_ROOT}"
echo "leagues=${LEAGUES}"
echo "window=${FROM_DATE}..${TO_DATE} seasons=${FROM_SEASON}..${TO_SEASON}"
echo "min_train=${MIN_TRAIN} market_shrink_alphas=${MARKET_SHRINK_ALPHAS}"

for league in ${LEAGUES}; do
  league_out="${OUT_ROOT}/${league}"
  mkdir -p "${league_out}/benchmark" "${league_out}/policy-grid"

  echo "===== $(date -Is) ingest ${league} ====="
  .venv/bin/scorerecord ingest-football-data \
    --league "${league}" \
    --from-season "${FROM_SEASON}" \
    --to-season "${TO_SEASON}"

  echo "===== $(date -Is) coverage ${league} ====="
  .venv/bin/scorerecord historical-odds-report \
    --league "${league}" | tee "${league_out}/historical_odds_report.txt"

  echo "===== $(date -Is) benchmark ${league} ====="
  .venv/bin/scorerecord market-benchmark-report \
    --from "${FROM_DATE}" \
    --to "${TO_DATE}" \
    --league "${league}" \
    --min-train "${MIN_TRAIN}" \
    --out-dir "${league_out}/benchmark"

  echo "===== $(date -Is) policy grid ${league} ====="
  .venv/bin/scorerecord backtest-policy-grid \
    --from "${FROM_DATE}" \
    --to "${TO_DATE}" \
    --league "${league}" \
    --min-train "${MIN_TRAIN}" \
    --market-shrink-alphas "${MARKET_SHRINK_ALPHAS}" \
    --min-scores "${MIN_SCORES}" \
    --min-edges "${MIN_EDGES}" \
    --max-picks-per-days "${MAX_PICKS_PER_DAYS}" \
    --out-dir "${league_out}/policy-grid"

  echo "===== $(date -Is) done ${league} ====="
done

echo "===== $(date -Is) aggregate policy grids ====="
.venv/bin/scorerecord analyze-policy-grids \
  "${OUT_ROOT}"/*/policy-grid/policy_grid_real_odds_*.csv \
  --out "${OUT_ROOT}/policy_grid_summary_multileague.csv"

echo "===== $(date -Is) multi-league validation complete ====="
