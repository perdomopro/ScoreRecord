"""Walk-forward evaluation for goal markets derived from Dixon-Coles score grids."""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from scorerecord.evaluate.walk_forward import _is_friendly
from scorerecord.features.build import MatchFeature, load_match_features
from scorerecord.models.calibration import PlattCalibrator
from scorerecord.models.dixon_coles import DixonColes
from scorerecord.models.markets import btts_probability, over_probability
from scorerecord.runs import ingest_run, record_quality


_EPS = 1e-12
DEFAULT_OU_LINES = (1.5, 2.5, 3.5)


@dataclass(frozen=True)
class BinaryMarket:
    market: str
    event: Callable[[MatchFeature], int]
    probability: Callable[[object], float]


@dataclass(frozen=True)
class GoalMarketResult:
    model: str
    market: str
    n: int
    brier: float
    logloss: float
    ece: float
    close_n: int
    close_logloss: float | None
    competitive_n: int
    competitive_logloss: float | None
    friendly_n: int
    friendly_logloss: float | None

    def as_row(self) -> str:
        close = "na" if self.close_logloss is None else f"{self.close_logloss:.4f}"
        comp = "na" if self.competitive_logloss is None else f"{self.competitive_logloss:.4f}"
        friendly = "na" if self.friendly_logloss is None else f"{self.friendly_logloss:.4f}"
        return (
            f"{self.model:<24} {self.market:<7} n={self.n:<6} "
            f"brier={self.brier:.4f} logloss={self.logloss:.4f} ece={self.ece:.4f} "
            f"close_ll={close} comp_ll={comp} friendly_ll={friendly}"
        )


@dataclass(frozen=True)
class _BinaryPrediction:
    match: MatchFeature
    prob: float
    label: int


def build_goal_markets(*, ou_lines: tuple[float, ...] = DEFAULT_OU_LINES) -> list[BinaryMarket]:
    markets = [
        BinaryMarket(
            market=f"OU{line}",
            event=lambda match, line=line: 1
            if (match.home_score + match.away_score) > line
            else 0,
            probability=lambda score_grid, line=line: over_probability(score_grid, line),
        )
        for line in ou_lines
    ]
    markets.append(
        BinaryMarket(
            market="BTTS",
            event=lambda match: 1 if match.home_score >= 1 and match.away_score >= 1 else 0,
            probability=btts_probability,
        )
    )
    return markets


def run_goal_market_walk_forward(
    conn: sqlite3.Connection,
    *,
    start_year: int = 2021,
    ou_lines: tuple[float, ...] = DEFAULT_OU_LINES,
    min_train: int = 200,
    cwd: Path | None = None,
) -> list[GoalMarketResult]:
    matches = load_match_features(conn)
    markets = build_goal_markets(ou_lines=ou_lines)
    results: list[GoalMarketResult] = []
    as_of = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with ingest_run(conn, "goal-markets", source="match_history", cwd=cwd) as run_id:
        record_quality(
            conn,
            run_id,
            "goal_markets",
            "feature_rows",
            float(len(matches)),
            json.dumps({"start_year": start_year, "markets": [m.market for m in markets]}),
        )
        predictions = _walk_forward_predictions(
            matches,
            markets=markets,
            start_year=start_year,
            min_train=min_train,
        )
        for key, rows in predictions.items():
            model, market = key
            result = _summarize_predictions(model=model, market=market, rows=rows)
            results.append(result)
            conn.execute(
                """
                INSERT OR REPLACE INTO eval_metric
                  (run_id, as_of, model, market, n, brier, logloss, roi, clv)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    run_id,
                    as_of,
                    result.model,
                    result.market,
                    result.n,
                    result.brier,
                    result.logloss,
                ),
            )
            _record_goal_market_quality(conn, run_id, result, rows)
        _record_goal_market_gates(conn, run_id, results)
        conn.commit()
    return sorted(results, key=lambda result: (result.market, result.model))


def _walk_forward_predictions(
    matches: list[MatchFeature],
    *,
    markets: list[BinaryMarket],
    start_year: int,
    min_train: int,
) -> dict[tuple[str, str], list[_BinaryPrediction]]:
    by_year: dict[int, list[MatchFeature]] = {}
    for match in matches:
        by_year.setdefault(match.year, []).append(match)
    if not by_year:
        raise ValueError("No matches with features to evaluate")

    output: dict[tuple[str, str], list[_BinaryPrediction]] = {}
    previous_raw_by_market: dict[str, list[_BinaryPrediction]] = {}
    for year in range(start_year, max(by_year) + 1):
        test = by_year.get(year, [])
        if not test:
            continue
        train = [match for match in matches if match.year < year]
        if len(train) < min_train:
            continue

        dc = DixonColes().fit(train)
        marginal_probs = {
            market.market: _marginal_event_probability(train, market.event) for market in markets
        }
        current_raw_by_market: dict[str, list[_BinaryPrediction]] = {
            market.market: [] for market in markets
        }
        current_marginal_by_market: dict[str, list[_BinaryPrediction]] = {
            market.market: [] for market in markets
        }
        for match in test:
            score_grid = dc.score_matrix(match)
            for market in markets:
                label = market.event(match)
                raw = _BinaryPrediction(
                    match=match,
                    prob=market.probability(score_grid),
                    label=label,
                )
                output.setdefault(("dixon_coles", market.market), []).append(raw)
                current_raw_by_market[market.market].append(raw)
                output.setdefault(("baseline_marginal", market.market), []).append(
                    _BinaryPrediction(match=match, prob=marginal_probs[market.market], label=label)
                )
                current_marginal_by_market[market.market].append(
                    _BinaryPrediction(match=match, prob=marginal_probs[market.market], label=label)
                )
        for market in markets:
            calibration_rows = previous_raw_by_market.get(market.market, [])
            if not calibration_rows:
                continue
            calibrator = PlattCalibrator().fit(
                [row.prob for row in calibration_rows],
                [row.label for row in calibration_rows],
            )
            for row in current_raw_by_market[market.market]:
                output.setdefault(("dixon_coles_platt", market.market), []).append(
                    _BinaryPrediction(
                        match=row.match,
                        prob=calibrator.transform(row.prob),
                        label=row.label,
                    )
                )
            output.setdefault(("baseline_marginal_platt_window", market.market), []).extend(
                current_marginal_by_market[market.market]
            )
        previous_raw_by_market = current_raw_by_market
    return output


def _marginal_event_probability(
    matches: list[MatchFeature],
    event: Callable[[MatchFeature], int],
) -> float:
    if not matches:
        return 0.5
    return sum(event(match) for match in matches) / len(matches)


def _summarize_predictions(
    *,
    model: str,
    market: str,
    rows: list[_BinaryPrediction],
) -> GoalMarketResult:
    if not rows:
        raise ValueError(f"No predictions for {model} {market}")
    brier = sum((row.prob - row.label) ** 2 for row in rows) / len(rows)
    logloss = sum(_binary_logloss(row.prob, row.label) for row in rows) / len(rows)
    ece, _ = binary_ece(rows)
    close_logloss, close_n = _segment_logloss(
        rows,
        lambda row: abs(row.match.elo_diff) < 75.0,
    )
    competitive_logloss, competitive_n = _segment_logloss(
        rows,
        lambda row: not _is_friendly(row.match.tournament),
    )
    friendly_logloss, friendly_n = _segment_logloss(
        rows,
        lambda row: _is_friendly(row.match.tournament),
    )
    return GoalMarketResult(
        model=model,
        market=market,
        n=len(rows),
        brier=brier,
        logloss=logloss,
        ece=ece,
        close_n=close_n,
        close_logloss=close_logloss,
        competitive_n=competitive_n,
        competitive_logloss=competitive_logloss,
        friendly_n=friendly_n,
        friendly_logloss=friendly_logloss,
    )


def _binary_logloss(prob: float, label: int) -> float:
    p = min(max(prob, _EPS), 1.0 - _EPS)
    return -(label * math.log(p) + (1 - label) * math.log(1.0 - p))


def binary_ece(
    rows: list[_BinaryPrediction],
    *,
    n_bins: int = 10,
) -> tuple[float, list[dict[str, float]]]:
    bins = [{"n": 0.0, "prob_sum": 0.0, "hit_sum": 0.0} for _ in range(n_bins)]
    for row in rows:
        bin_index = min(n_bins - 1, int(row.prob * n_bins))
        bins[bin_index]["n"] += 1.0
        bins[bin_index]["prob_sum"] += row.prob
        bins[bin_index]["hit_sum"] += row.label

    ece = 0.0
    details: list[dict[str, float]] = []
    total = float(len(rows))
    for index, row in enumerate(bins):
        n = row["n"]
        lower = index / n_bins
        upper = (index + 1) / n_bins
        if n == 0:
            details.append({"bin": float(index), "lower": lower, "upper": upper, "n": 0.0})
            continue
        avg_probability = row["prob_sum"] / n
        frequency = row["hit_sum"] / n
        gap = abs(avg_probability - frequency)
        ece += (n / total) * gap
        details.append(
            {
                "bin": float(index),
                "lower": lower,
                "upper": upper,
                "n": n,
                "avg_probability": avg_probability,
                "frequency": frequency,
                "gap": gap,
            }
        )
    return ece, details


def _segment_logloss(
    rows: list[_BinaryPrediction],
    predicate: Callable[[_BinaryPrediction], bool],
) -> tuple[float | None, int]:
    selected = [row for row in rows if predicate(row)]
    if not selected:
        return None, 0
    return sum(_binary_logloss(row.prob, row.label) for row in selected) / len(selected), len(
        selected
    )


def _record_goal_market_quality(
    conn: sqlite3.Connection,
    run_id: int,
    result: GoalMarketResult,
    rows: list[_BinaryPrediction],
) -> None:
    _, bins = binary_ece(rows)
    payload = {
        "ece": result.ece,
        "calibration_bins": bins,
        "segments": {
            "close": {"n": result.close_n, "logloss": result.close_logloss},
            "competitive": {"n": result.competitive_n, "logloss": result.competitive_logloss},
            "friendly": {"n": result.friendly_n, "logloss": result.friendly_logloss},
        },
    }
    record_quality(
        conn,
        run_id,
        "goal_markets",
        f"{result.model}_{result.market}_ece",
        result.ece,
        json.dumps(payload, sort_keys=True),
    )


def _record_goal_market_gates(
    conn: sqlite3.Connection,
    run_id: int,
    results: list[GoalMarketResult],
    *,
    max_ece: float = 0.03,
) -> None:
    gate_check_name = "predictive_baseline_gate"
    by_market_model = {(result.market, result.model): result for result in results}
    markets = sorted({result.market for result in results})
    all_pass = True
    details = []
    for market in markets:
        dc = by_market_model.get((market, "dixon_coles_platt")) or by_market_model.get(
            (market, "dixon_coles")
        )
        baseline = (
            by_market_model.get((market, "baseline_marginal_platt_window"))
            if dc and dc.model == "dixon_coles_platt"
            else by_market_model.get((market, "baseline_marginal"))
        )
        if dc is None or baseline is None:
            all_pass = False
            details.append({"market": market, "passed": False, "reason": "missing_result"})
            continue
        passed = dc.logloss < baseline.logloss and dc.ece < max_ece
        all_pass = all_pass and passed
        details.append(
            {
                "market": market,
                "passed": passed,
                "dc_logloss": dc.logloss,
                "baseline_logloss": baseline.logloss,
                "dc_ece": dc.ece,
                "selected_model": dc.model,
                "max_ece": max_ece,
                "dc_close_logloss": dc.close_logloss,
                "baseline_close_logloss": baseline.close_logloss,
            }
        )
        record_quality(
            conn,
            run_id,
            gate_check_name,
            f"{market}_passed",
            1.0 if passed else 0.0,
            json.dumps(details[-1], sort_keys=True),
        )
    record_quality(
        conn,
        run_id,
        gate_check_name,
        "all_markets_passed",
        1.0 if all_pass else 0.0,
        json.dumps({"markets": details}, sort_keys=True),
    )
