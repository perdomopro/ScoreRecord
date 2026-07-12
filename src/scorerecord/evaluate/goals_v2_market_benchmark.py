"""Goals V2 Market Benchmark for OU2.5.

Benchmarks Goals V2 lambda-derived OU2.5 probabilities against historical
fair market probabilities using walk-forward evaluation.

This is a market benchmark only — it does NOT unlock paper candidates,
EV, Kelly, stake, or recommendations.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import numpy as np

from scorerecord.evaluate.scoring import block_bootstrap_mean, club_week_block
from scorerecord.features.shots_ratings import ProcessRatingTrackerEWMA
from scorerecord.ingest.football_data import resolve_league_code
from scorerecord.models.goals_v2_lambdas import (
    PoissonRegressor,
    compute_independent_probabilities,
)
from scorerecord.time_utils import parse_kickoff_utc

# ---------------------------------------------------------------------------
# Public OU2.5 odds loading wrapper
# ---------------------------------------------------------------------------

def load_historical_ou25_markets(
    conn: sqlite3.Connection,
    *,
    source: str = "football-data",
    entry_odds_type: str = "avg_preclose",
    closing_odds_type: str | None = "avg_close",
) -> dict[str, dict[str, Any]]:
    """Load de-vigged OU2.5 historical odds keyed by source_event_id.

    Returns a dict of source_event_id -> {
        "entry_over_odds": float,
        "entry_under_odds": float,
        "preclose_fair_over": float,
        "preclose_fair_under": float,
        "close_over_odds": float | None,
        "close_under_odds": float | None,
        "close_fair_over": float | None,
        "close_fair_under": float | None,
        "preclose_overround": float,
        "close_overround": float | None,
    }
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT *
        FROM historical_odds
        WHERE source = ?
          AND market = 'OU2.5'
          AND line = 2.5
          AND entry_odds_type = ?
          AND entry_odds IS NOT NULL
        ORDER BY source_event_id, bookmaker, selection
        """,
        (source, entry_odds_type),
    ).fetchall()

    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        key = (str(row["source_event_id"]), str(row["bookmaker"] or ""))
        grouped.setdefault(key, []).append(row)

    markets: dict[str, dict[str, Any]] = {}
    for (source_event_id, _bookmaker), group in grouped.items():
        by_sel = {str(r["selection"]): r for r in group}
        if not {"over", "under"}.issubset(by_sel):
            continue

        entry_over = float(by_sel["over"]["entry_odds"])
        entry_under = float(by_sel["under"]["entry_odds"])
        if entry_over <= 1.0 or entry_under <= 1.0:
            continue

        over_inv = 1.0 / entry_over
        under_inv = 1.0 / entry_under
        total = over_inv + under_inv

        result: dict[str, Any] = {
            "entry_over_odds": entry_over,
            "entry_under_odds": entry_under,
            "preclose_fair_over": over_inv / total,
            "preclose_fair_under": under_inv / total,
            "preclose_overround": total,
            "close_over_odds": None,
            "close_under_odds": None,
            "close_fair_over": None,
            "close_fair_under": None,
            "close_overround": None,
        }

        if closing_odds_type:
            close_ok = all(
                by_sel[sel]["closing_odds"] is not None
                and str(by_sel[sel]["closing_odds_type"] or "") == closing_odds_type
                and float(by_sel[sel]["closing_odds"]) > 1.0
                for sel in ("over", "under")
            )
            if close_ok:
                c_over = float(by_sel["over"]["closing_odds"])
                c_under = float(by_sel["under"]["closing_odds"])
                c_over_inv = 1.0 / c_over
                c_under_inv = 1.0 / c_under
                c_total = c_over_inv + c_under_inv
                result["close_over_odds"] = c_over
                result["close_under_odds"] = c_under
                result["close_fair_over"] = c_over_inv / c_total
                result["close_fair_under"] = c_under_inv / c_total
                result["close_overround"] = c_total

        markets[source_event_id] = result

    return markets


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GoalsV2MarketBenchmarkReport:
    """Report for Goals V2 Market Benchmark OU2.5."""

    market: str
    train_date_range: str
    eval_date_range: str
    leagues: str

    # Coverage
    raw_lambda_predictions: int
    matched_preclose_count: int
    matched_close_count: int
    missing_market_count: int
    missing_close_count: int
    missing_lambda_count: int
    invalid_odds_count: int

    # Logloss
    market_preclose_logloss: float
    market_close_logloss: float | None
    goals_v2_goals_baseline_logloss: float
    goals_v2_process_only_logloss: float
    goals_v2_goals_plus_process_logloss: float

    # Brier
    market_preclose_brier: float
    market_close_brier: float | None
    goals_v2_goals_plus_process_brier: float

    # Deltas
    delta_gpp_vs_market_preclose: float
    delta_gpp_vs_market_close: float | None
    delta_gpp_vs_goals_baseline_lambda: float
    delta_gpp_vs_process_only_lambda: float

    # Bootstrap CIs
    ci_delta_gpp_vs_market_preclose: tuple[float, float]
    ci_delta_gpp_vs_market_close: tuple[float, float] | None
    ci_delta_gpp_vs_goals_baseline_lambda: tuple[float, float]
    ci_delta_gpp_vs_process_only_lambda: tuple[float, float]

    # Segmented
    by_league_metrics: dict[str, Any]
    by_season_metrics: dict[str, Any]
    calibration_buckets: dict[str, Any]

    benchmark_state: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _binary_logloss(y: np.ndarray, p: np.ndarray) -> float:
    """Mean binary log-loss."""
    eps = 1e-15
    p = np.clip(p, eps, 1.0 - eps)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _binary_brier(y: np.ndarray, p: np.ndarray) -> float:
    """Mean Brier score."""
    return float(np.mean((p - y) ** 2))


def _binary_ece(y: np.ndarray, p: np.ndarray, n_bins: int = 10) -> float:
    """Binary expected calibration error with equal-width bins."""
    n = len(y)
    if n == 0:
        return 0.0
    ece = 0.0
    for i in range(n_bins):
        lower = i / n_bins
        upper = (i + 1) / n_bins
        if i == n_bins - 1:
            mask = (p >= lower) & (p <= upper)
        else:
            mask = (p >= lower) & (p < upper)
        count = int(np.sum(mask))
        if count == 0:
            continue
        avg_prob = float(np.mean(p[mask]))
        hit_rate = float(np.mean(y[mask]))
        ece += (count / n) * abs(avg_prob - hit_rate)
    return ece


def _calibration_buckets(
    y: np.ndarray, p: np.ndarray, n_bins: int = 10,
) -> dict[str, dict[str, float]]:
    """Decile-based calibration buckets."""
    if len(y) == 0:
        return {}
    percentiles = np.linspace(0, 100, n_bins + 1)
    bins = np.percentile(p, percentiles)
    bins[0] = 0.0
    bins[-1] = 1.0001
    result: dict[str, dict[str, float]] = {}
    for i in range(n_bins):
        mask = (p > bins[i]) & (p <= bins[i + 1])
        if i == 0:
            mask = (p >= bins[i]) & (p <= bins[i + 1])
        count = int(np.sum(mask))
        if count > 0:
            result[f"Decile {i + 1}"] = {
                "count": float(count),
                "predicted": float(np.mean(p[mask])),
                "actual": float(np.mean(y[mask])),
            }
    return result


def _get_iso_week(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


# ---------------------------------------------------------------------------
# Main benchmark function
# ---------------------------------------------------------------------------

def run_goals_v2_market_benchmark(
    conn: sqlite3.Connection,
    *,
    train_from: date,
    eval_from: date,
    to_date: date,
    leagues: tuple[str, ...],
    market: str = "OU2.5",
    entry_odds: str = "avg_preclose",
    closing_odds: str = "avg_close",
    half_life_days: float = 365.0,
    min_train: int = 500,
    bootstrap_samples: int = 2000,
    predictions_out: str | None = None,
) -> GoalsV2MarketBenchmarkReport:
    """Run Goals V2 OU2.5 market benchmark.

    Trains lambda models from train_from, evaluates from eval_from to to_date,
    and compares model OU2.5 probabilities against historical market fair
    probabilities.
    """
    if market != "OU2.5":
        raise ValueError(f"Only OU2.5 market is supported, got {market}")

    # --- Step 1: Resolve leagues ---
    league_codes = []
    for lg in leagues:
        try:
            league_codes.append(resolve_league_code(lg))
        except ValueError:
            pass
    if not league_codes:
        raise ValueError(f"No valid league codes resolved from {leagues}")

    # --- Step 2: Load historical OU2.5 odds ---
    ou25_markets = load_historical_ou25_markets(
        conn,
        source="football-data",
        entry_odds_type=entry_odds,
        closing_odds_type=closing_odds,
    )

    # --- Step 3: Build walk-forward lambda predictions ---
    conn.row_factory = sqlite3.Row
    placeholders = ",".join(["?"] * len(league_codes))
    query = f"""
        SELECT
            id,
            source_event_id,
            league_code,
            season,
            match_date,
            home_team,
            away_team,
            home_goals,
            away_goals,
            raw_json
        FROM historical_match
        WHERE league_code IN ({placeholders})
          AND match_date >= ?
          AND match_date <= ?
        ORDER BY match_date ASC
    """
    params = list(league_codes) + [
        train_from.isoformat(),
        to_date.isoformat(),
    ]
    rows = conn.execute(query, params).fetchall()

    tracker = ProcessRatingTrackerEWMA(half_life_days=half_life_days)
    dataset: list[dict[str, Any]] = []

    for r in rows:
        match_dt = parse_kickoff_utc(kickoff_utc=r["match_date"])
        if not match_dt:
            match_dt = datetime.fromisoformat(
                r["match_date"]
            ).replace(tzinfo=UTC)

        home = r["home_team"]
        away = r["away_team"]
        home_g = r["home_goals"]
        away_g = r["away_goals"]

        raw = json.loads(r["raw_json"]) if r["raw_json"] else {}

        try:
            hs = float(raw.get("HS", ""))
            as_ = float(raw.get("AS", ""))
            hst = float(raw.get("HST", ""))
            ast = float(raw.get("AST", ""))
        except (ValueError, TypeError):
            continue

        home_ratings = tracker.get_ratings(home, match_dt)
        away_ratings = tracker.get_ratings(away, match_dt)

        dataset.append({
            "id": r["id"] if "id" in r.keys() else None,
            "source_event_id": r["source_event_id"],
            "league": r["league_code"],
            "season": r["season"],
            "match_dt": match_dt,
            "iso_week": _get_iso_week(match_dt),
            "home": home,
            "away": away,
            "home_goals": home_g,
            "away_goals": away_g,
            "total_goals": home_g + away_g,
            # Goal Baseline Features
            "gb_h_gf": home_ratings["goals_for"],
            "gb_h_ga": home_ratings["goals_against"],
            "gb_a_gf": away_ratings["goals_for"],
            "gb_a_ga": away_ratings["goals_against"],
            # Shots Features
            "sh_h_sf": home_ratings["shots_for"],
            "sh_h_sa": home_ratings["shots_against"],
            "sh_a_sf": away_ratings["shots_for"],
            "sh_a_sa": away_ratings["shots_against"],
            # SOT Features
            "sot_h_stf": home_ratings["sot_for"],
            "sot_h_sta": home_ratings["sot_against"],
            "sot_a_stf": away_ratings["sot_for"],
            "sot_a_sta": away_ratings["sot_against"],
        })

        tracker.update(home, match_dt, home_g, away_g, hs, as_, hst, ast)
        tracker.update(away, match_dt, away_g, home_g, as_, hs, ast, hst)

    dataset.sort(key=lambda x: x["match_dt"])

    # --- Step 4: Walk-forward weekly loop ---
    weeks_data: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in dataset:
        weeks_data[d["iso_week"]].append(d)
    sorted_weeks = sorted(weeks_data.keys())

    eval_from_dt = datetime(
        eval_from.year, eval_from.month, eval_from.day, tzinfo=UTC,
    )

    train_history: list[dict[str, Any]] = []

    # Models (warm_start=False for strict weight reset)
    mod_gb_h = PoissonRegressor(4, warm_start=False)
    mod_gb_a = PoissonRegressor(4, warm_start=False)
    mod_po_h = PoissonRegressor(8, warm_start=False)
    mod_po_a = PoissonRegressor(8, warm_start=False)
    mod_gpp_h = PoissonRegressor(12, warm_start=False)
    mod_gpp_a = PoissonRegressor(12, warm_start=False)

    def _extract_x(
        data: list[dict[str, Any]], variant: str,
    ) -> np.ndarray:
        if variant == "gb":
            return np.array([
                [d["gb_h_gf"], d["gb_h_ga"],
                 d["gb_a_gf"], d["gb_a_ga"]]
                for d in data
            ])
        elif variant == "po":
            return np.array([
                [d["sh_h_sf"], d["sh_h_sa"],
                 d["sh_a_sf"], d["sh_a_sa"],
                 d["sot_h_stf"], d["sot_h_sta"],
                 d["sot_a_stf"], d["sot_a_sta"]]
                for d in data
            ])
        elif variant == "gpp":
            return np.hstack([
                _extract_x(data, "gb"),
                _extract_x(data, "po"),
            ])
        return np.array([])

    # Collect lambda predictions for eval-window matches
    lambda_predictions: list[dict[str, Any]] = []

    for week in sorted_weeks:
        week_data = weeks_data[week]

        if len(train_history) >= min_train:
            y_home = np.array(
                [d["home_goals"] for d in train_history],
            )
            y_away = np.array(
                [d["away_goals"] for d in train_history],
            )

            mod_gb_h.fit(_extract_x(train_history, "gb"), y_home)
            mod_gb_a.fit(_extract_x(train_history, "gb"), y_away)
            mod_po_h.fit(_extract_x(train_history, "po"), y_home)
            mod_po_a.fit(_extract_x(train_history, "po"), y_away)
            mod_gpp_h.fit(_extract_x(train_history, "gpp"), y_home)
            mod_gpp_a.fit(_extract_x(train_history, "gpp"), y_away)

            # Only evaluate matches in eval window
            eval_matches = [
                d for d in week_data if d["match_dt"] >= eval_from_dt
            ]

            if eval_matches:
                lam_gb_h = mod_gb_h.predict(
                    _extract_x(eval_matches, "gb"),
                )
                lam_gb_a = mod_gb_a.predict(
                    _extract_x(eval_matches, "gb"),
                )
                lam_po_h = mod_po_h.predict(
                    _extract_x(eval_matches, "po"),
                )
                lam_po_a = mod_po_a.predict(
                    _extract_x(eval_matches, "po"),
                )
                lam_gpp_h = mod_gpp_h.predict(
                    _extract_x(eval_matches, "gpp"),
                )
                lam_gpp_a = mod_gpp_a.predict(
                    _extract_x(eval_matches, "gpp"),
                )

                for i, d in enumerate(eval_matches):
                    probs_gb = compute_independent_probabilities(
                        lam_gb_h[i], lam_gb_a[i],
                    )
                    probs_po = compute_independent_probabilities(
                        lam_po_h[i], lam_po_a[i],
                    )
                    probs_gpp = compute_independent_probabilities(
                        lam_gpp_h[i], lam_gpp_a[i],
                    )

                    lambda_predictions.append({
                        "id": d["id"],
                        "source_event_id": d["source_event_id"],
                        "date": d["match_dt"].isoformat(),
                        "league": d["league"],
                        "season": d["season"],
                        "iso_week": d["iso_week"],
                        "home_team": d["home"],
                        "away_team": d["away"],
                        "home_goals": int(d["home_goals"]),
                        "away_goals": int(d["away_goals"]),
                        "total_goals": int(d["total_goals"]),
                        "label_over_25": (
                            1 if d["total_goals"] > 2.5 else 0
                        ),
                        "gb_p_over_25": float(probs_gb["p_over_25"]),
                        "po_p_over_25": float(probs_po["p_over_25"]),
                        "gpp_p_over_25": float(probs_gpp["p_over_25"]),
                        "lambda_home_gpp": float(lam_gpp_h[i]),
                        "lambda_away_gpp": float(lam_gpp_a[i]),
                        "lambda_total_gpp": float(
                            lam_gpp_h[i] + lam_gpp_a[i],
                        ),
                    })

        train_history.extend(week_data)

    raw_lambda_count = len(lambda_predictions)

    # --- Step 5: Join lambda predictions to market odds ---
    matched_rows: list[dict[str, Any]] = []
    missing_market = 0
    invalid_odds = 0

    for pred in lambda_predictions:
        seid = pred["source_event_id"]
        if seid not in ou25_markets:
            missing_market += 1
            continue

        mkt = ou25_markets[seid]

        # Validate odds
        if (mkt["entry_over_odds"] <= 1.0
                or mkt["entry_under_odds"] <= 1.0):
            invalid_odds += 1
            continue

        row = {**pred}
        row["market_preclose_fair"] = mkt["preclose_fair_over"]
        row["market_close_fair"] = mkt.get("close_fair_over")
        row["entry_over_odds"] = mkt["entry_over_odds"]
        row["entry_under_odds"] = mkt["entry_under_odds"]
        row["close_over_odds"] = mkt.get("close_over_odds")
        row["close_under_odds"] = mkt.get("close_under_odds")
        matched_rows.append(row)

    # Deduplicate: one row per source_event_id
    seen_ids: set[str] = set()
    unique_rows: list[dict[str, Any]] = []
    for row in matched_rows:
        seid = row["source_event_id"]
        if seid not in seen_ids:
            seen_ids.add(seid)
            unique_rows.append(row)
    matched_rows = unique_rows

    matched_preclose_count = len(matched_rows)

    # Close subset
    close_rows = [
        r for r in matched_rows if r["market_close_fair"] is not None
    ]
    matched_close_count = len(close_rows)
    missing_close_count = matched_preclose_count - matched_close_count

    # Missing lambda = odds exist but no lambda prediction
    lambda_seids = {p["source_event_id"] for p in lambda_predictions}
    missing_lambda = sum(
        1 for seid in ou25_markets if seid not in lambda_seids
    )

    # --- Step 6: Compute metrics on preclose set ---
    if matched_preclose_count == 0:
        return _empty_report(
            market, train_from, eval_from, to_date, leagues,
            raw_lambda_count, missing_market, missing_close_count,
            missing_lambda, invalid_odds,
        )

    y = np.array([r["label_over_25"] for r in matched_rows])
    p_preclose = np.array([r["market_preclose_fair"] for r in matched_rows])
    p_gb = np.array([r["gb_p_over_25"] for r in matched_rows])
    p_po = np.array([r["po_p_over_25"] for r in matched_rows])
    p_gpp = np.array([r["gpp_p_over_25"] for r in matched_rows])

    ll_preclose = _binary_logloss(y, p_preclose)
    ll_gb = _binary_logloss(y, p_gb)
    ll_po = _binary_logloss(y, p_po)
    ll_gpp = _binary_logloss(y, p_gpp)

    brier_preclose = _binary_brier(y, p_preclose)
    brier_gpp = _binary_brier(y, p_gpp)

    delta_gpp_vs_preclose = ll_gpp - ll_preclose
    delta_gpp_vs_gb = ll_gpp - ll_gb
    delta_gpp_vs_po = ll_gpp - ll_po

    # Close metrics (separate subset)
    ll_close: float | None = None
    brier_close: float | None = None
    delta_gpp_vs_close: float | None = None
    ci_close: tuple[float, float] | None = None

    if close_rows:
        y_close = np.array([r["label_over_25"] for r in close_rows])
        p_close_fair = np.array(
            [r["market_close_fair"] for r in close_rows],
        )
        p_gpp_close = np.array(
            [r["gpp_p_over_25"] for r in close_rows],
        )
        ll_close = _binary_logloss(y_close, p_close_fair)
        brier_close = _binary_brier(y_close, p_close_fair)
        delta_gpp_vs_close = (
            _binary_logloss(y_close, p_gpp_close) - ll_close
        )

        # Bootstrap CI for close delta
        close_blocks: dict[str, list[float]] = defaultdict(list)
        for r in close_rows:
            bk = club_week_block(r["date"][:10], r["league"])
            ll_gpp_r = -(
                r["label_over_25"] * math.log(max(r["gpp_p_over_25"], 1e-15))
                + (1 - r["label_over_25"])
                * math.log(max(1 - r["gpp_p_over_25"], 1e-15))
            )
            ll_cl_r = -(
                r["label_over_25"]
                * math.log(max(r["market_close_fair"], 1e-15))
                + (1 - r["label_over_25"])
                * math.log(max(1 - r["market_close_fair"], 1e-15))
            )
            close_blocks[bk].append(ll_gpp_r - ll_cl_r)

        if close_blocks:
            ci_obj = block_bootstrap_mean(
                close_blocks,
                iterations=bootstrap_samples,
                seed=2026,
            )
            ci_close = (ci_obj.lower, ci_obj.upper)

    # --- Step 7: Bootstrap CIs for preclose set ---
    # Per-row delta: logloss_gpp - logloss_ref
    def _row_ll(label: int, prob: float) -> float:
        eps = 1e-15
        p = max(min(prob, 1 - eps), eps)
        return -(label * math.log(p) + (1 - label) * math.log(1 - p))

    blocks_preclose: dict[str, list[float]] = defaultdict(list)
    blocks_gb: dict[str, list[float]] = defaultdict(list)
    blocks_po: dict[str, list[float]] = defaultdict(list)

    for r in matched_rows:
        bk = club_week_block(r["date"][:10], r["league"])
        ll_gpp_r = _row_ll(r["label_over_25"], r["gpp_p_over_25"])
        ll_pre_r = _row_ll(r["label_over_25"], r["market_preclose_fair"])
        ll_gb_r = _row_ll(r["label_over_25"], r["gb_p_over_25"])
        ll_po_r = _row_ll(r["label_over_25"], r["po_p_over_25"])

        blocks_preclose[bk].append(ll_gpp_r - ll_pre_r)
        blocks_gb[bk].append(ll_gpp_r - ll_gb_r)
        blocks_po[bk].append(ll_gpp_r - ll_po_r)

    ci_preclose_obj = block_bootstrap_mean(
        blocks_preclose, iterations=bootstrap_samples, seed=2026,
    )
    ci_gb_obj = block_bootstrap_mean(
        blocks_gb, iterations=bootstrap_samples, seed=2026,
    )
    ci_po_obj = block_bootstrap_mean(
        blocks_po, iterations=bootstrap_samples, seed=2026,
    )

    ci_preclose = (ci_preclose_obj.lower, ci_preclose_obj.upper)
    ci_gb = (ci_gb_obj.lower, ci_gb_obj.upper)
    ci_po = (ci_po_obj.lower, ci_po_obj.upper)

    # --- Step 8: By-league and by-season metrics ---
    by_league: dict[str, dict[str, Any]] = {}
    by_season: dict[str, dict[str, Any]] = {}

    for r in matched_rows:
        lg = r["league"]
        sn = r["season"]
        label = r["label_over_25"]

        for seg, key in [(by_league, lg), (by_season, sn)]:
            if key not in seg:
                seg[key] = {
                    "count": 0,
                    "sum_gpp_ll": 0.0,
                    "sum_preclose_ll": 0.0,
                    "sum_gb_ll": 0.0,
                }
            seg[key]["count"] += 1
            seg[key]["sum_gpp_ll"] += _row_ll(label, r["gpp_p_over_25"])
            seg[key]["sum_preclose_ll"] += _row_ll(
                label, r["market_preclose_fair"],
            )
            seg[key]["sum_gb_ll"] += _row_ll(label, r["gb_p_over_25"])

    for seg in (by_league, by_season):
        for k, v in seg.items():
            n = v["count"]
            seg[k] = {
                "count": n,
                "gpp_logloss": v["sum_gpp_ll"] / n,
                "preclose_logloss": v["sum_preclose_ll"] / n,
                "gb_logloss": v["sum_gb_ll"] / n,
                "delta_gpp_vs_preclose": (
                    (v["sum_gpp_ll"] - v["sum_preclose_ll"]) / n
                ),
                "delta_gpp_vs_gb": (
                    (v["sum_gpp_ll"] - v["sum_gb_ll"]) / n
                ),
            }

    # --- Step 9: Calibration buckets ---
    calib = {
        "market_preclose": _calibration_buckets(y, p_preclose),
        "goals_v2_goals_baseline": _calibration_buckets(y, p_gb),
        "goals_v2_goals_plus_process": _calibration_buckets(y, p_gpp),
    }

    # --- Step 10: State determination (based on market_preclose) ---
    neg_leagues = sum(
        1 for v in by_league.values()
        if v["delta_gpp_vs_preclose"] < 0
    )
    neg_seasons = sum(
        1 for v in by_season.values()
        if v["delta_gpp_vs_preclose"] < 0
    )

    if (
        delta_gpp_vs_preclose < 0
        and ci_preclose[1] < 0
        and neg_leagues >= max(len(by_league) * 0.5, 1)
        and neg_seasons >= max(len(by_season) * 0.5, 1)
        and matched_preclose_count >= 1000
    ):
        state = "MARKET_PROMISING"
    elif (
        delta_gpp_vs_preclose > 0
        and ci_preclose[0] > 0
    ):
        state = "MARKET_REJECTED"
    else:
        state = "MARKET_INCONCLUSIVE"

    # --- Step 11: Write predictions JSONL ---
    if predictions_out and matched_rows:
        with open(predictions_out, "w", encoding="utf-8") as f:
            for r in matched_rows:
                out_row = {
                    "source_event_id": r["source_event_id"],
                    "match_id": r["id"],
                    "date": r["date"][:10],
                    "league": r["league"],
                    "season": r["season"],
                    "home_team": r["home_team"],
                    "away_team": r["away_team"],
                    "home_goals": r["home_goals"],
                    "away_goals": r["away_goals"],
                    "total_goals": r["total_goals"],
                    "label_over_25": r["label_over_25"],
                    "market_preclose_fair": r["market_preclose_fair"],
                    "market_close_fair": r["market_close_fair"],
                    "goals_v2_goals_baseline_p_over_25": (
                        r["gb_p_over_25"]
                    ),
                    "goals_v2_process_only_p_over_25": (
                        r["po_p_over_25"]
                    ),
                    "goals_v2_goals_plus_process_p_over_25": (
                        r["gpp_p_over_25"]
                    ),
                    "lambda_home_gpp": r["lambda_home_gpp"],
                    "lambda_away_gpp": r["lambda_away_gpp"],
                    "lambda_total_gpp": r["lambda_total_gpp"],
                }
                f.write(json.dumps(out_row) + "\n")

    return GoalsV2MarketBenchmarkReport(
        market=market,
        train_date_range=f"{train_from} to {to_date}",
        eval_date_range=f"{eval_from} to {to_date}",
        leagues=",".join(leagues),
        raw_lambda_predictions=raw_lambda_count,
        matched_preclose_count=matched_preclose_count,
        matched_close_count=matched_close_count,
        missing_market_count=missing_market,
        missing_close_count=missing_close_count,
        missing_lambda_count=missing_lambda,
        invalid_odds_count=invalid_odds,
        market_preclose_logloss=ll_preclose,
        market_close_logloss=ll_close,
        goals_v2_goals_baseline_logloss=ll_gb,
        goals_v2_process_only_logloss=ll_po,
        goals_v2_goals_plus_process_logloss=ll_gpp,
        market_preclose_brier=brier_preclose,
        market_close_brier=brier_close,
        goals_v2_goals_plus_process_brier=brier_gpp,
        delta_gpp_vs_market_preclose=delta_gpp_vs_preclose,
        delta_gpp_vs_market_close=delta_gpp_vs_close,
        delta_gpp_vs_goals_baseline_lambda=delta_gpp_vs_gb,
        delta_gpp_vs_process_only_lambda=delta_gpp_vs_po,
        ci_delta_gpp_vs_market_preclose=ci_preclose,
        ci_delta_gpp_vs_market_close=ci_close,
        ci_delta_gpp_vs_goals_baseline_lambda=ci_gb,
        ci_delta_gpp_vs_process_only_lambda=ci_po,
        by_league_metrics=by_league,
        by_season_metrics=by_season,
        calibration_buckets=calib,
        benchmark_state=state,
    )


def _empty_report(
    market: str,
    train_from: date,
    eval_from: date,
    to_date: date,
    leagues: tuple[str, ...],
    raw_lambda: int,
    missing_market: int,
    missing_close: int,
    missing_lambda: int,
    invalid_odds: int,
) -> GoalsV2MarketBenchmarkReport:
    return GoalsV2MarketBenchmarkReport(
        market=market,
        train_date_range=f"{train_from} to {to_date}",
        eval_date_range=f"{eval_from} to {to_date}",
        leagues=",".join(leagues),
        raw_lambda_predictions=raw_lambda,
        matched_preclose_count=0,
        matched_close_count=0,
        missing_market_count=missing_market,
        missing_close_count=missing_close,
        missing_lambda_count=missing_lambda,
        invalid_odds_count=invalid_odds,
        market_preclose_logloss=0.0,
        market_close_logloss=None,
        goals_v2_goals_baseline_logloss=0.0,
        goals_v2_process_only_logloss=0.0,
        goals_v2_goals_plus_process_logloss=0.0,
        market_preclose_brier=0.0,
        market_close_brier=None,
        goals_v2_goals_plus_process_brier=0.0,
        delta_gpp_vs_market_preclose=0.0,
        delta_gpp_vs_market_close=None,
        delta_gpp_vs_goals_baseline_lambda=0.0,
        delta_gpp_vs_process_only_lambda=0.0,
        ci_delta_gpp_vs_market_preclose=(0.0, 0.0),
        ci_delta_gpp_vs_market_close=None,
        ci_delta_gpp_vs_goals_baseline_lambda=(0.0, 0.0),
        ci_delta_gpp_vs_process_only_lambda=(0.0, 0.0),
        by_league_metrics={},
        by_season_metrics={},
        calibration_buckets={},
        benchmark_state="MARKET_INCONCLUSIVE",
    )
