"""Historical walk-forward backtest and calibration CSV reports.

The current project has no historical bookmaker odds. These reports therefore use a
training-window marginal probability as a market proxy. That makes the reports useful
for calibration, bucket diagnostics, and comparing recommendation_score vs raw EV,
but not for claiming real betting ROI.
"""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from scorerecord.evaluate.edge import _high_edge_reason, _recommendation_score
from scorerecord.evaluate.goal_markets import _walk_forward_predictions, build_goal_markets
from scorerecord.evaluate.recommendations import (
    _has_disqualifying_recommendation_marker,
    capped_real_stake,
    kelly_stake,
)
from scorerecord.features.build import OUTCOME_LABELS, MatchFeature, load_match_features
from scorerecord.models.calibration import PlattCalibrator
from scorerecord.models.dixon_coles import DixonColes
from scorerecord.models.markets import btts_probability, one_x_two_probabilities, over_probability
from scorerecord.runs import ingest_run, record_quality


DEFAULT_OU_LINES = (1.5, 2.5, 3.5)
_EPS = 1e-12


@dataclass(frozen=True)
class HistoricalPrediction:
    match: MatchFeature
    market: str
    selection: str
    model_prob: float
    market_prob: float
    edge_ev: float
    edge_prob: float
    recommendation_score: float
    high_edge_reason: str
    odds_quality_flags: str
    won: int

    @property
    def odds(self) -> float:
        return 1.0 / max(self.market_prob, _EPS)

    @property
    def edge_bucket(self) -> str:
        return edge_bucket(self.edge_ev)

    @property
    def odds_bucket(self) -> str:
        return odds_bucket(self.odds)

    @property
    def prob_bucket(self) -> str:
        return prob_bucket(self.model_prob)

    @property
    def market_type(self) -> str:
        if self.market.startswith("OU"):
            return "OU"
        return self.market


@dataclass(frozen=True)
class BacktestBet:
    prediction: HistoricalPrediction
    ranking_strategy: str
    model_stake_fraction: float
    capped_stake_fraction: float


@dataclass(frozen=True)
class ReportSummary:
    run_id: int
    output_dir: Path
    rows_written: int
    summary_rows: int
    calibration_rows: int

    def as_row(self) -> str:
        return (
            f"run_id={self.run_id} output_dir={self.output_dir} "
            f"rows_written={self.rows_written} summary_rows={self.summary_rows} "
            f"calibration_rows={self.calibration_rows}"
        )


def run_historical_backtest(
    conn: sqlite3.Connection,
    *,
    from_date: date,
    to_date: date,
    out_dir: Path,
    start_year: int = 2021,
    min_train: int = 200,
    min_edge: float = 0.05,
    max_edge: float = 0.25,
    min_prob: float = 0.15,
    max_picks_per_fixture: int = 1,
    cwd: Path | None = None,
) -> ReportSummary:
    matches = _filtered_matches(conn, from_date=from_date, to_date=to_date)
    all_matches = load_match_features(conn)
    team_names = _team_names(conn)
    generated_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with ingest_run(conn, "backtest", source="match_history", cwd=cwd) as run_id:
        predictions = _walk_forward_market_predictions(
            all_matches,
            from_date=from_date,
            to_date=to_date,
            start_year=start_year,
            min_train=min_train,
        )
        selected = _select_backtest_bets(
            predictions,
            min_edge=min_edge,
            max_edge=max_edge,
            min_prob=min_prob,
            max_picks_per_fixture=max_picks_per_fixture,
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        recommendations_path = out_dir / f"backtest_recommendations_{run_id}.csv"
        summary_path = out_dir / f"backtest_summary_{run_id}.csv"
        calibration_path = out_dir / f"calibration_by_bucket_{run_id}.csv"
        metadata_path = out_dir / f"backtest_metadata_{run_id}.json"

        _write_backtest_recommendations(recommendations_path, selected, team_names)
        summary_rows = _summary_rows(selected)
        _write_dict_rows(summary_path, summary_rows)
        calibration_rows = _calibration_rows(predictions)
        _write_dict_rows(calibration_path, calibration_rows)
        metadata = {
            "run_id": run_id,
            "generated_utc": generated_utc,
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "start_year": start_year,
            "min_train": min_train,
            "min_edge": min_edge,
            "max_edge": max_edge,
            "min_prob": min_prob,
            "max_picks_per_fixture": max_picks_per_fixture,
            "odds_source": "training_marginal_proxy",
            "warning": "No historical bookmaker odds are available; ROI is proxy-only.",
            "matches_in_requested_window": len(matches),
            "prediction_rows": len(predictions),
            "recommendation_rows": len(selected),
        }
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
        _record_backtest_quality(conn, run_id, metadata, summary_rows, calibration_rows)
        conn.commit()
    return ReportSummary(
        run_id=run_id,
        output_dir=out_dir,
        rows_written=len(selected),
        summary_rows=len(summary_rows),
        calibration_rows=len(calibration_rows),
    )


def run_calibration_report(
    conn: sqlite3.Connection,
    *,
    from_date: date,
    to_date: date,
    out_dir: Path,
    start_year: int = 2021,
    min_train: int = 200,
    cwd: Path | None = None,
) -> ReportSummary:
    all_matches = load_match_features(conn)
    generated_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with ingest_run(conn, "calibration-report", source="match_history", cwd=cwd) as run_id:
        predictions = _walk_forward_market_predictions(
            all_matches,
            from_date=from_date,
            to_date=to_date,
            start_year=start_year,
            min_train=min_train,
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        calibration_path = out_dir / f"calibration_by_bucket_{run_id}.csv"
        market_path = out_dir / f"calibration_by_market_{run_id}.csv"
        metadata_path = out_dir / f"calibration_metadata_{run_id}.json"
        calibration_rows = _calibration_rows(predictions)
        market_rows = _calibration_market_rows(predictions)
        _write_dict_rows(calibration_path, calibration_rows)
        _write_dict_rows(market_path, market_rows)
        metadata = {
            "run_id": run_id,
            "generated_utc": generated_utc,
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "start_year": start_year,
            "min_train": min_train,
            "prediction_rows": len(predictions),
            "problem_rule": "count >= 50 and abs(avg_model_prob - actual_hit_rate) > 0.08",
        }
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
        _record_calibration_quality(conn, run_id, metadata, calibration_rows, market_rows)
        conn.commit()
    return ReportSummary(
        run_id=run_id,
        output_dir=out_dir,
        rows_written=0,
        summary_rows=len(market_rows),
        calibration_rows=len(calibration_rows),
    )


def _filtered_matches(
    conn: sqlite3.Connection,
    *,
    from_date: date,
    to_date: date,
) -> list[MatchFeature]:
    return [
        match
        for match in load_match_features(conn)
        if from_date.isoformat() <= match.match_date <= to_date.isoformat()
    ]


def _walk_forward_market_predictions(
    matches: list[MatchFeature],
    *,
    from_date: date,
    to_date: date,
    start_year: int,
    min_train: int,
) -> list[HistoricalPrediction]:
    by_year: dict[int, list[MatchFeature]] = {}
    for match in matches:
        by_year.setdefault(match.year, []).append(match)
    if not by_year:
        raise ValueError("No historical matches available for backtest")

    output: list[HistoricalPrediction] = []
    for year in range(start_year, max(by_year) + 1):
        test = [
            match
            for match in by_year.get(year, [])
            if from_date.isoformat() <= match.match_date <= to_date.isoformat()
        ]
        if not test:
            continue
        train = [match for match in matches if match.year < year]
        if len(train) < min_train:
            continue
        model = DixonColes().fit(train)
        calibrators = _fit_goal_calibrators(train, min_train=min_train)
        market_baselines = _market_baselines(train)
        for match in test:
            score_grid = model.score_matrix(match)
            output.extend(_match_predictions(match, score_grid, market_baselines, calibrators))
    if not output:
        raise ValueError("Backtest produced no predictions; check dates/start_year/min_train")
    return output


def _match_predictions(
    match: MatchFeature,
    score_grid: object,
    market_baselines: dict[tuple[str, str], float],
    calibrators: dict[str, PlattCalibrator],
) -> list[HistoricalPrediction]:
    rows: list[tuple[str, str, float, int]] = []
    for selection, prob in zip(OUTCOME_LABELS, one_x_two_probabilities(score_grid)):
        won = 1 if OUTCOME_LABELS[match.outcome] == selection else 0
        rows.append(("1X2", selection, prob, won))
    for line in DEFAULT_OU_LINES:
        market = f"OU{line}"
        over = _calibrate(market, over_probability(score_grid, line), calibrators)
        label_over = 1 if (match.home_score + match.away_score) > line else 0
        rows.append((market, "over", over, label_over))
        rows.append((market, "under", 1.0 - over, 1 - label_over))
    btts = _calibrate("BTTS", btts_probability(score_grid), calibrators)
    label_btts = 1 if match.home_score >= 1 and match.away_score >= 1 else 0
    rows.append(("BTTS", "yes", btts, label_btts))
    rows.append(("BTTS", "no", 1.0 - btts, 1 - label_btts))

    predictions: list[HistoricalPrediction] = []
    close_match = int(abs(match.elo_diff) < 75.0)
    for market, selection, prob, won in rows:
        market_prob = market_baselines.get((market, selection), 0.5)
        odds = 1.0 / max(market_prob, _EPS)
        edge_ev = prob * odds - 1.0
        edge_prob = prob - market_prob
        high_edge_reason = _high_edge_reason(
            market=market,
            selection=selection,
            odds_decimal=odds,
            prob_model=prob,
            edge_ev=edge_ev,
            edge_prob=edge_prob,
            odds_quality_flags="",
        )
        score = _recommendation_score(
            edge_ev=edge_ev,
            odds_decimal=odds,
            market=market,
            selection=selection,
            odds_quality_flags="",
            close_match=close_match,
        )
        predictions.append(
            HistoricalPrediction(
                match=match,
                market=market,
                selection=selection,
                model_prob=prob,
                market_prob=market_prob,
                edge_ev=edge_ev,
                edge_prob=edge_prob,
                recommendation_score=score,
                high_edge_reason=high_edge_reason,
                odds_quality_flags="",
                won=won,
            )
        )
    return predictions


def _fit_goal_calibrators(
    train: list[MatchFeature],
    *,
    min_train: int,
) -> dict[str, PlattCalibrator]:
    if len({match.year for match in train}) < 2:
        return {}
    try:
        predictions = _walk_forward_predictions(
            train,
            markets=build_goal_markets(ou_lines=DEFAULT_OU_LINES),
            start_year=min(match.year for match in train) + 1,
            min_train=min_train,
        )
    except ValueError:
        return {}
    calibrators: dict[str, PlattCalibrator] = {}
    for market in [f"OU{line}" for line in DEFAULT_OU_LINES] + ["BTTS"]:
        rows = predictions.get(("dixon_coles", market), [])
        if not rows:
            continue
        calibrators[market] = PlattCalibrator().fit(
            [row.prob for row in rows],
            [row.label for row in rows],
        )
    return calibrators


def _calibrate(market: str, prob: float, calibrators: dict[str, PlattCalibrator]) -> float:
    calibrator = calibrators.get(market)
    return prob if calibrator is None else calibrator.transform(prob)


def _market_baselines(matches: list[MatchFeature]) -> dict[tuple[str, str], float]:
    events: dict[tuple[str, str], list[int]] = {}
    for match in matches:
        for selection in OUTCOME_LABELS:
            events.setdefault(("1X2", selection), []).append(
                1 if OUTCOME_LABELS[match.outcome] == selection else 0
            )
        for line in DEFAULT_OU_LINES:
            market = f"OU{line}"
            over = 1 if (match.home_score + match.away_score) > line else 0
            events.setdefault((market, "over"), []).append(over)
            events.setdefault((market, "under"), []).append(1 - over)
        btts = 1 if match.home_score >= 1 and match.away_score >= 1 else 0
        events.setdefault(("BTTS", "yes"), []).append(btts)
        events.setdefault(("BTTS", "no"), []).append(1 - btts)
    return {
        key: _smoothed_rate(values)
        for key, values in events.items()
        if values
    }


def _smoothed_rate(values: list[int]) -> float:
    return (sum(values) + 0.5) / (len(values) + 1.0)


def _select_backtest_bets(
    predictions: list[HistoricalPrediction],
    *,
    min_edge: float,
    max_edge: float,
    min_prob: float,
    max_picks_per_fixture: int,
) -> list[BacktestBet]:
    eligible = [
        row
        for row in predictions
        if row.edge_ev >= min_edge
        and row.edge_ev <= max_edge
        and row.model_prob >= min_prob
        and not _has_disqualifying_recommendation_marker(
            row.odds_quality_flags,
            row.high_edge_reason,
        )
    ]
    selected: list[BacktestBet] = []
    for strategy, ranker in [
        ("edge_ev", lambda row: (row.edge_ev, row.recommendation_score)),
        ("recommendation_score", lambda row: (row.recommendation_score, row.edge_ev)),
    ]:
        selected.extend(
            _top_by_fixture(
                eligible,
                ranking_strategy=strategy,
                ranker=ranker,
                max_picks_per_fixture=max_picks_per_fixture,
            )
        )
    return sorted(
        selected,
        key=lambda bet: (
            bet.prediction.match.match_date,
            bet.prediction.match.source_match_id,
            bet.ranking_strategy,
        ),
    )


def _top_by_fixture(
    rows: list[HistoricalPrediction],
    *,
    ranking_strategy: str,
    ranker: Callable[[HistoricalPrediction], tuple[float, float]],
    max_picks_per_fixture: int,
) -> list[BacktestBet]:
    output: list[BacktestBet] = []
    by_fixture: dict[str, list[HistoricalPrediction]] = {}
    for row in rows:
        by_fixture.setdefault(row.match.source_match_id, []).append(row)
    for fixture_rows in by_fixture.values():
        ordered = sorted(fixture_rows, key=ranker, reverse=True)
        for row in ordered[:max_picks_per_fixture]:
            model_stake = kelly_stake(row.model_prob, row.odds)
            output.append(
                BacktestBet(
                    prediction=row,
                    ranking_strategy=ranking_strategy,
                    model_stake_fraction=model_stake,
                    capped_stake_fraction=capped_real_stake(
                        model_stake=model_stake,
                        odds_decimal=row.odds,
                        market=row.market,
                        selection=row.selection,
                    ),
                )
            )
    return output


def _write_backtest_recommendations(
    path: Path,
    bets: list[BacktestBet],
    team_names: dict[int, str],
) -> None:
    rows = []
    for bet in bets:
        row = bet.prediction
        match = row.match
        profit_flat = row.odds - 1.0 if row.won else -1.0
        profit_model = bet.model_stake_fraction * profit_flat
        profit_capped = bet.capped_stake_fraction * profit_flat
        rows.append(
            {
                "ranking_strategy": bet.ranking_strategy,
                "as_of_date": match.match_date,
                "fixture_id": match.source_match_id,
                "kickoff_time": match.match_date,
                "home_team": team_names.get(match.home_id, str(match.home_id)),
                "away_team": team_names.get(match.away_id, str(match.away_id)),
                "market": row.market,
                "selection": row.selection,
                "odds": row.odds,
                "model_prob": row.model_prob,
                "fair_market_prob": row.market_prob,
                "edge_ev": row.edge_ev,
                "recommendation_score": row.recommendation_score,
                "model_stake_fraction": bet.model_stake_fraction,
                "capped_stake_fraction": bet.capped_stake_fraction,
                "result": f"{match.home_score}-{match.away_score}",
                "won": row.won,
                "profit_flat": profit_flat,
                "profit_staked": profit_model,
                "profit_capped_staked": profit_capped,
                "closing_odds": "",
                "edge_bucket": row.edge_bucket,
                "odds_bucket": row.odds_bucket,
                "prob_bucket": row.prob_bucket,
                "market_type": row.market_type,
                "competition_type": _competition_type(match),
                "high_edge_reason": row.high_edge_reason,
                "odds_quality_flags": row.odds_quality_flags,
            }
        )
    _write_dict_rows(path, rows)


def _summary_rows(bets: list[BacktestBet]) -> list[dict[str, object]]:
    groupers: list[tuple[str, Callable[[BacktestBet], str]]] = [
        ("overall", lambda bet: "all"),
        ("ranking_strategy", lambda bet: bet.ranking_strategy),
        ("market", lambda bet: bet.prediction.market),
        ("market_selection", lambda bet: f"{bet.prediction.market}:{bet.prediction.selection}"),
        ("edge_bucket", lambda bet: bet.prediction.edge_bucket),
        ("odds_bucket", lambda bet: bet.prediction.odds_bucket),
        ("prob_bucket", lambda bet: bet.prediction.prob_bucket),
        ("competition_type", lambda bet: _competition_type(bet.prediction.match)),
    ]
    output: list[dict[str, object]] = []
    for group_type, key_fn in groupers:
        grouped: dict[str, list[BacktestBet]] = {}
        for bet in bets:
            grouped.setdefault(key_fn(bet), []).append(bet)
        for group, rows in sorted(grouped.items()):
            output.append(_metric_row(group_type, group, rows))
    return output


def _metric_row(group_type: str, group: str, bets: list[BacktestBet]) -> dict[str, object]:
    flat_profits = [_profit_flat(bet.prediction) for bet in bets]
    model_profits = [bet.model_stake_fraction * profit for bet, profit in zip(bets, flat_profits)]
    capped_profits = [
        bet.capped_stake_fraction * profit for bet, profit in zip(bets, flat_profits)
    ]
    model_staked = sum(bet.model_stake_fraction for bet in bets)
    capped_staked = sum(bet.capped_stake_fraction for bet in bets)
    return {
        "group_type": group_type,
        "group": group,
        "num_bets": len(bets),
        "hit_rate": _mean([bet.prediction.won for bet in bets]),
        "avg_odds": _mean([bet.prediction.odds for bet in bets]),
        "avg_edge_ev": _mean([bet.prediction.edge_ev for bet in bets]),
        "avg_recommendation_score": _mean(
            [bet.prediction.recommendation_score for bet in bets]
        ),
        "profit_flat": sum(flat_profits),
        "profit_staked": sum(model_profits),
        "profit_capped_staked": sum(capped_profits),
        "roi_flat": sum(flat_profits) / len(flat_profits) if flat_profits else 0.0,
        "roi_staked": sum(model_profits) / model_staked if model_staked > 0 else 0.0,
        "roi_capped_staked": sum(capped_profits) / capped_staked if capped_staked > 0 else 0.0,
        "max_drawdown": max_drawdown(flat_profits),
        "brier_score": _mean(
            [(bet.prediction.model_prob - bet.prediction.won) ** 2 for bet in bets]
        ),
        "log_loss": _mean(
            [_binary_logloss(bet.prediction.model_prob, bet.prediction.won) for bet in bets]
        ),
    }


def _calibration_rows(predictions: list[HistoricalPrediction]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str, str], list[HistoricalPrediction]] = {}
    for row in predictions:
        grouped.setdefault((row.market, row.selection, row.prob_bucket), []).append(row)
    output = []
    for (market, selection, bucket), rows in sorted(grouped.items()):
        avg_prob = _mean([row.model_prob for row in rows])
        hit_rate = _mean([row.won for row in rows])
        error = hit_rate - avg_prob
        output.append(
            {
                "market": market,
                "selection": selection,
                "prob_bucket": bucket,
                "count": len(rows),
                "avg_model_prob": avg_prob,
                "actual_hit_rate": hit_rate,
                "avg_market_prob": _mean([row.market_prob for row in rows]),
                "brier_score": _mean([(row.model_prob - row.won) ** 2 for row in rows]),
                "log_loss": _mean([_binary_logloss(row.model_prob, row.won) for row in rows]),
                "calibration_error": error,
                "abs_calibration_error": abs(error),
                "problem": int(len(rows) >= 50 and abs(error) > 0.08),
            }
        )
    return output


def _calibration_market_rows(predictions: list[HistoricalPrediction]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], list[HistoricalPrediction]] = {}
    for row in predictions:
        grouped.setdefault((row.market, row.selection), []).append(row)
    output = []
    for (market, selection), rows in sorted(grouped.items()):
        ece = expected_calibration_error(rows)
        output.append(
            {
                "market": market,
                "selection": selection,
                "count": len(rows),
                "avg_model_prob": _mean([row.model_prob for row in rows]),
                "actual_hit_rate": _mean([row.won for row in rows]),
                "avg_market_prob": _mean([row.market_prob for row in rows]),
                "brier_score": _mean([(row.model_prob - row.won) ** 2 for row in rows]),
                "log_loss": _mean([_binary_logloss(row.model_prob, row.won) for row in rows]),
                "ece": ece,
            }
        )
    return output


def expected_calibration_error(rows: list[HistoricalPrediction], *, n_bins: int = 10) -> float:
    if not rows:
        return 0.0
    total = len(rows)
    ece = 0.0
    for index in range(n_bins):
        lower = index / n_bins
        upper = (index + 1) / n_bins
        selected = [
            row
            for row in rows
            if row.model_prob >= lower and (row.model_prob < upper or index == n_bins - 1)
        ]
        if not selected:
            continue
        ece += (len(selected) / total) * abs(
            _mean([row.model_prob for row in selected]) - _mean([row.won for row in selected])
        )
    return ece


def edge_bucket(value: float) -> str:
    return _bucket(
        value,
        [
            (0.025, "0.0-2.5%"),
            (0.05, "2.5-5.0%"),
            (0.10, "5.0-10.0%"),
            (0.15, "10.0-15.0%"),
            (0.20, "15.0-20.0%"),
            (0.25, "20.0-25.0%"),
        ],
        high="25.0%+",
        low="<0.0%",
    )


def odds_bucket(value: float) -> str:
    return _bucket(
        value,
        [
            (1.50, "1.01-1.50"),
            (2.00, "1.50-2.00"),
            (3.00, "2.00-3.00"),
            (5.00, "3.00-5.00"),
            (8.00, "5.00-8.00"),
        ],
        high="8.00+",
        low="<1.01",
    )


def prob_bucket(value: float) -> str:
    return _bucket(
        value,
        [
            (0.05, "0-5%"),
            (0.10, "5-10%"),
            (0.15, "10-15%"),
            (0.20, "15-20%"),
            (0.30, "20-30%"),
            (0.40, "30-40%"),
            (0.50, "40-50%"),
            (0.60, "50-60%"),
            (0.75, "60-75%"),
        ],
        high="75%+",
        low="<0%",
    )


def _bucket(
    value: float,
    thresholds: list[tuple[float, str]],
    *,
    high: str,
    low: str,
) -> str:
    if value < 0:
        return low
    for threshold, label in thresholds:
        if value < threshold:
            return label
    return high


def _profit_flat(row: HistoricalPrediction) -> float:
    return row.odds - 1.0 if row.won else -1.0


def _binary_logloss(prob: float, label: int) -> float:
    p = min(max(prob, _EPS), 1.0 - _EPS)
    return -(label * math.log(p) + (1 - label) * math.log(1.0 - p))


def _mean(values: Iterable[float | int]) -> float:
    rows = [float(value) for value in values]
    return sum(rows) / len(rows) if rows else 0.0


def max_drawdown(profits: list[float]) -> float:
    peak = 0.0
    equity = 0.0
    worst = 0.0
    for profit in profits:
        equity += profit
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return worst


def _competition_type(match: MatchFeature) -> str:
    tournament = (match.tournament or "").casefold()
    return "friendly" if "friendly" in tournament else "competitive"


def _team_names(conn: sqlite3.Connection) -> dict[int, str]:
    return {
        int(row["team_id"]): str(row["canonical"])
        for row in conn.execute("SELECT team_id, canonical FROM team")
    }


def _write_dict_rows(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _record_backtest_quality(
    conn: sqlite3.Connection,
    run_id: int,
    metadata: dict[str, object],
    summary_rows: list[dict[str, object]],
    calibration_rows: list[dict[str, object]],
) -> None:
    overall = [row for row in summary_rows if row["group_type"] == "ranking_strategy"]
    details = {**metadata, "ranking_strategy_rows": overall}
    for metric_name, value in [
        ("prediction_rows", metadata["prediction_rows"]),
        ("recommendation_rows", metadata["recommendation_rows"]),
        ("summary_rows", len(summary_rows)),
        ("calibration_rows", len(calibration_rows)),
    ]:
        record_quality(
            conn,
            run_id,
            "historical_backtest",
            metric_name,
            float(value),
            json.dumps(details, sort_keys=True),
        )


def _record_calibration_quality(
    conn: sqlite3.Connection,
    run_id: int,
    metadata: dict[str, object],
    calibration_rows: list[dict[str, object]],
    market_rows: list[dict[str, object]],
) -> None:
    problem_rows = [row for row in calibration_rows if int(row["problem"]) == 1]
    details = {
        **metadata,
        "problem_rows": problem_rows[:25],
        "market_rows": market_rows,
    }
    for metric_name, value in [
        ("prediction_rows", metadata["prediction_rows"]),
        ("calibration_rows", len(calibration_rows)),
        ("problem_rows", len(problem_rows)),
        ("max_market_ece", max((float(row["ece"]) for row in market_rows), default=0.0)),
    ]:
        record_quality(
            conn,
            run_id,
            "calibration_report",
            metric_name,
            float(value),
            json.dumps(details, sort_keys=True),
        )
