"""Historical walk-forward backtest and calibration CSV reports.

Proxy mode uses a training-window marginal probability as a market proxy. That makes
the reports useful for calibration, bucket diagnostics, and comparing
recommendation_score vs raw EV, but not for claiming real betting ROI.

Historical mode is deliberately separate: it uses real historical odds stored in
`historical_odds`, currently for Football-Data 1X2 only.
"""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from itertools import product
from pathlib import Path
from typing import Callable, Iterable

from scorerecord.evaluate.edge import _high_edge_reason, _recommendation_score, devig_1x2
from scorerecord.evaluate.goal_markets import _walk_forward_predictions, build_goal_markets
from scorerecord.evaluate.recommendations import (
    _has_disqualifying_recommendation_marker,
    capped_real_stake,
    kelly_stake,
)
from scorerecord.features.build import OUTCOME_LABELS, MatchFeature, load_match_features
from scorerecord.ingest.football_data import normalize_season, resolve_league_code
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
    def score_bucket(self) -> str:
        return score_bucket(self.recommendation_score)

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
class HistoricalOddsMatch:
    source: str
    source_event_id: str
    league_code: str
    season: str
    match_date: str
    home_team: str
    away_team: str
    home_goals: int
    away_goals: int
    result_1x2: str
    home_id: int
    away_id: int

    @property
    def year(self) -> int:
        return int(self.match_date[:4])

    def as_feature(self) -> MatchFeature:
        return MatchFeature(
            source_match_id=self.source_event_id,
            match_date=self.match_date,
            home_id=self.home_id,
            away_id=self.away_id,
            home_score=self.home_goals,
            away_score=self.away_goals,
            neutral=0,
            tournament=f"football-data:{self.league_code}",
            elo_home_pre=1500.0,
            elo_away_pre=1500.0,
        )


@dataclass(frozen=True)
class HistoricalOddsMarket:
    source: str
    source_event_id: str
    bookmaker: str
    entry_odds_type: str
    closing_odds_type: str | None
    entry_odds: dict[str, float]
    closing_odds: dict[str, float | None]
    market_probs: dict[str, float]
    market_overround: float
    is_real_historical_odds: bool
    is_timestamp_exact: bool


@dataclass(frozen=True)
class RealOddsPrediction:
    match: HistoricalOddsMatch
    market: str
    selection: str
    bookmaker: str
    entry_odds: float
    entry_odds_type: str
    closing_odds: float | None
    closing_odds_type: str | None
    model_prob: float
    market_prob: float
    market_overround: float
    edge_ev: float
    edge_prob: float
    recommendation_score: float
    high_edge_reason: str
    odds_quality_flags: str
    won: int
    is_real_historical_odds: bool
    is_timestamp_exact: bool

    @property
    def edge_bucket(self) -> str:
        return edge_bucket(self.edge_ev)

    @property
    def odds_bucket(self) -> str:
        return odds_bucket(self.entry_odds)

    @property
    def prob_bucket(self) -> str:
        return prob_bucket(self.model_prob)

    @property
    def score_bucket(self) -> str:
        return score_bucket(self.recommendation_score)


@dataclass(frozen=True)
class RealOddsBet:
    prediction: RealOddsPrediction
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


@dataclass(frozen=True)
class PolicyGridSummary:
    run_id: int
    output_dir: Path
    rows_written: int
    prediction_rows: int

    def as_row(self) -> str:
        return (
            f"run_id={self.run_id} output_dir={self.output_dir} "
            f"rows_written={self.rows_written} prediction_rows={self.prediction_rows}"
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
    min_score: float = 0.0,
    max_picks_per_fixture: int = 1,
    max_picks_per_day: int | None = None,
    odds_mode: str = "proxy",
    historical_odds_source: str = "football-data",
    entry_odds: str = "avg_open",
    closing_odds: str = "avg_close",
    market: str = "1X2",
    league: str | None = None,
    season: str | None = None,
    cwd: Path | None = None,
) -> ReportSummary:
    if odds_mode == "historical":
        return run_real_odds_backtest(
            conn,
            from_date=from_date,
            to_date=to_date,
            out_dir=out_dir,
            start_year=start_year,
            min_train=min_train,
            min_edge=min_edge,
            max_edge=max_edge,
            min_prob=min_prob,
            min_score=min_score,
            max_picks_per_fixture=max_picks_per_fixture,
            max_picks_per_day=max_picks_per_day,
            historical_odds_source=historical_odds_source,
            entry_odds=entry_odds,
            closing_odds=closing_odds,
            market=market,
            league=league,
            season=season,
            cwd=cwd,
        )
    if odds_mode != "proxy":
        raise ValueError("odds_mode must be either 'proxy' or 'historical'")

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
            min_score=min_score,
            max_picks_per_fixture=max_picks_per_fixture,
            max_picks_per_day=max_picks_per_day,
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
            "min_score": min_score,
            "max_picks_per_fixture": max_picks_per_fixture,
            "max_picks_per_day": max_picks_per_day,
            "odds_source": "training_marginal_proxy",
            "odds_mode": "proxy",
            "is_real_historical_odds": False,
            "is_timestamp_exact": False,
            "profitability_gate": "blocked_proxy_odds",
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


def run_real_odds_backtest(
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
    min_score: float = 0.0,
    max_picks_per_fixture: int = 1,
    max_picks_per_day: int | None = None,
    historical_odds_source: str = "football-data",
    entry_odds: str = "avg_open",
    closing_odds: str = "avg_close",
    market: str = "1X2",
    league: str | None = None,
    season: str | None = None,
    cwd: Path | None = None,
) -> ReportSummary:
    if market != "1X2":
        raise ValueError("Historical real-odds backtest currently supports only market=1X2")
    if historical_odds_source != "football-data":
        raise ValueError("Only historical_odds_source=football-data is supported")

    league_code = resolve_league_code(league) if league else None
    normalized_season = normalize_season(season) if season else None
    generated_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with ingest_run(conn, "backtest-real-odds", source=historical_odds_source, cwd=cwd) as run_id:
        matches = _load_historical_odds_matches(
            conn,
            source=historical_odds_source,
            league_code=league_code,
            test_season=normalized_season,
            to_date=to_date,
        )
        odds = _load_historical_odds_markets(
            conn,
            source=historical_odds_source,
            entry_odds_type=entry_odds,
            closing_odds_type=closing_odds,
        )
        predictions = _walk_forward_real_odds_predictions(
            matches,
            odds=odds,
            from_date=from_date,
            to_date=to_date,
            start_year=start_year,
            min_train=min_train,
            test_season=normalized_season,
        )
        selected = _select_real_odds_bets(
            predictions,
            min_edge=min_edge,
            max_edge=max_edge,
            min_prob=min_prob,
            min_score=min_score,
            max_picks_per_fixture=max_picks_per_fixture,
            max_picks_per_day=max_picks_per_day,
        )

        out_dir.mkdir(parents=True, exist_ok=True)
        recommendations_path = out_dir / f"backtest_recommendations_real_odds_{run_id}.csv"
        summary_path = out_dir / f"backtest_summary_real_odds_{run_id}.csv"
        calibration_path = out_dir / f"calibration_by_bucket_real_odds_{run_id}.csv"
        clv_path = out_dir / f"clv_summary_real_odds_{run_id}.csv"
        metadata_path = out_dir / f"backtest_metadata_real_odds_{run_id}.json"

        _write_real_odds_recommendations(recommendations_path, selected)
        summary_rows = _real_summary_rows(selected)
        _write_dict_rows(summary_path, summary_rows)
        calibration_rows = _real_calibration_rows(predictions)
        _write_dict_rows(calibration_path, calibration_rows)
        clv_rows = _real_clv_rows(selected)
        _write_dict_rows(clv_path, clv_rows)

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
            "min_score": min_score,
            "max_picks_per_fixture": max_picks_per_fixture,
            "max_picks_per_day": max_picks_per_day,
            "odds_mode": "historical",
            "odds_source": historical_odds_source,
            "entry_odds_type": entry_odds,
            "closing_odds_type": closing_odds,
            "market": market,
            "league_code": league_code,
            "test_season": normalized_season,
            "model_universe": "football_data_club_results",
            "decision_time_rule": "pre_match_open_proxy",
            "is_real_historical_odds": True,
            "is_timestamp_exact": False,
            "profitability_gate": (
                "ok_real_historical_odds"
                if selected
                else "blocked_insufficient_sample_or_no_edges"
            ),
            "historical_matches_loaded": len(matches),
            "odds_markets_loaded": len(odds),
            "prediction_rows": len(predictions),
            "recommendation_rows": len(selected),
            "sample_size_policy": {
                "lt_100": "smoke_or_exploratory_no_threshold_changes",
                "100_299": "initial_signal_review_only",
                "300_999": "useful_signal_cautious_adjustments_allowed",
                "gte_1000": "more_stable_signal_with_bucket_checks",
                "bucket_minimum_for_review": 50,
                "bucket_minimum_for_adjustment": 100,
            },
            "warning": (
                "Football-Data odds unlock real historical 1X2 ROI for covered club "
                "leagues, not direct World Cup/team-selection profitability."
            ),
        }
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
        _record_real_backtest_quality(
            conn,
            run_id,
            metadata,
            summary_rows,
            calibration_rows,
            clv_rows,
        )
        conn.commit()

    return ReportSummary(
        run_id=run_id,
        output_dir=out_dir,
        rows_written=len(selected),
        summary_rows=len(summary_rows),
        calibration_rows=len(calibration_rows),
    )


def run_real_odds_policy_grid(
    conn: sqlite3.Connection,
    *,
    from_date: date,
    to_date: date,
    out_dir: Path,
    start_year: int = 2021,
    min_train: int = 200,
    min_scores: tuple[float, ...] = (0.0, 0.05, 0.075, 0.10, 0.125),
    min_edges: tuple[float, ...] = (0.03, 0.05, 0.075),
    max_picks_per_days: tuple[int | None, ...] = (None, 10, 5, 3),
    max_edge: float = 0.25,
    min_prob: float = 0.15,
    max_picks_per_fixture: int = 1,
    historical_odds_source: str = "football-data",
    entry_odds: str = "avg_open",
    closing_odds: str = "avg_close",
    market: str = "1X2",
    league: str | None = None,
    season: str | None = None,
    cwd: Path | None = None,
) -> PolicyGridSummary:
    if market != "1X2":
        raise ValueError("Policy grid currently supports only market=1X2")
    if historical_odds_source != "football-data":
        raise ValueError("Only historical_odds_source=football-data is supported")

    league_code = resolve_league_code(league) if league else None
    normalized_season = normalize_season(season) if season else None
    generated_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with ingest_run(conn, "backtest-policy-grid", source=historical_odds_source, cwd=cwd) as run_id:
        matches = _load_historical_odds_matches(
            conn,
            source=historical_odds_source,
            league_code=league_code,
            test_season=normalized_season,
            to_date=to_date,
        )
        odds = _load_historical_odds_markets(
            conn,
            source=historical_odds_source,
            entry_odds_type=entry_odds,
            closing_odds_type=closing_odds,
        )
        predictions = _walk_forward_real_odds_predictions(
            matches,
            odds=odds,
            from_date=from_date,
            to_date=to_date,
            start_year=start_year,
            min_train=min_train,
            test_season=normalized_season,
        )
        rows: list[dict[str, object]] = []
        for min_score, min_edge, max_per_day in product(
            min_scores,
            min_edges,
            max_picks_per_days,
        ):
            selected = _select_real_odds_bets(
                predictions,
                min_edge=min_edge,
                max_edge=max_edge,
                min_prob=min_prob,
                min_score=min_score,
                max_picks_per_fixture=max_picks_per_fixture,
                max_picks_per_day=max_per_day,
            )
            rows.extend(
                _policy_grid_rows(
                    selected,
                    min_score=min_score,
                    min_edge=min_edge,
                    max_picks_per_day=max_per_day,
                )
            )

        out_dir.mkdir(parents=True, exist_ok=True)
        grid_path = out_dir / f"policy_grid_real_odds_{run_id}.csv"
        metadata_path = out_dir / f"policy_grid_metadata_{run_id}.json"
        _write_dict_rows(grid_path, rows)
        metadata = {
            "run_id": run_id,
            "generated_utc": generated_utc,
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "start_year": start_year,
            "min_train": min_train,
            "min_scores": list(min_scores),
            "min_edges": list(min_edges),
            "max_picks_per_days": [
                "none" if value is None else value for value in max_picks_per_days
            ],
            "max_edge": max_edge,
            "min_prob": min_prob,
            "max_picks_per_fixture": max_picks_per_fixture,
            "odds_mode": "historical",
            "odds_source": historical_odds_source,
            "entry_odds_type": entry_odds,
            "closing_odds_type": closing_odds,
            "market": market,
            "league_code": league_code,
            "test_season": normalized_season,
            "prediction_rows": len(predictions),
            "grid_rows": len(rows),
            "selection_note": (
                "Use this grid to compare CLV/ROI/sample size; do not choose the "
                "single best ROI row without bucket and robustness checks."
            ),
        }
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
        _record_policy_grid_quality(conn, run_id, metadata, rows)
        conn.commit()
    return PolicyGridSummary(
        run_id=run_id,
        output_dir=out_dir,
        rows_written=len(rows),
        prediction_rows=len(predictions),
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


def _load_historical_odds_matches(
    conn: sqlite3.Connection,
    *,
    source: str,
    league_code: str | None,
    test_season: str | None,
    to_date: date,
) -> list[HistoricalOddsMatch]:
    params: list[object] = [source, to_date.isoformat()]
    filters = [
        "source = ?",
        "match_date <= ?",
        "home_goals IS NOT NULL",
        "away_goals IS NOT NULL",
        "result_1x2 IN ('home', 'draw', 'away')",
    ]
    if league_code is not None:
        filters.append("league_code = ?")
        params.append(league_code)
    rows = conn.execute(
        f"""
        SELECT *
        FROM historical_match
        WHERE {" AND ".join(filters)}
        ORDER BY match_date, source_event_id
        """,
        params,
    ).fetchall()
    if not rows:
        raise ValueError("No completed historical matches available for real-odds backtest")

    team_keys = sorted(
        {
            _historical_team_key(str(row["home_team"]))
            for row in rows
        }
        | {
            _historical_team_key(str(row["away_team"]))
            for row in rows
        }
    )
    team_ids = {key: index + 1 for index, key in enumerate(team_keys)}
    output = [
        HistoricalOddsMatch(
            source=str(row["source"]),
            source_event_id=str(row["source_event_id"]),
            league_code=str(row["league_code"]),
            season=str(row["season"]),
            match_date=str(row["match_date"]),
            home_team=str(row["home_team"]),
            away_team=str(row["away_team"]),
            home_goals=int(row["home_goals"]),
            away_goals=int(row["away_goals"]),
            result_1x2=str(row["result_1x2"]),
            home_id=team_ids[_historical_team_key(str(row["home_team"]))],
            away_id=team_ids[_historical_team_key(str(row["away_team"]))],
        )
        for row in rows
    ]
    if test_season is not None and not any(match.season == test_season for match in output):
        raise ValueError(f"No completed historical matches found for test season {test_season}")
    return output


def _load_historical_odds_markets(
    conn: sqlite3.Connection,
    *,
    source: str,
    entry_odds_type: str,
    closing_odds_type: str | None,
) -> dict[str, HistoricalOddsMarket]:
    rows = conn.execute(
        """
        SELECT *
        FROM historical_odds
        WHERE source = ?
          AND market = '1X2'
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

    markets: dict[str, HistoricalOddsMarket] = {}
    for (source_event_id, bookmaker), group in grouped.items():
        by_selection = {str(row["selection"]): row for row in group}
        if not {"home", "draw", "away"}.issubset(by_selection):
            continue
        entry = {
            selection: float(by_selection[selection]["entry_odds"])
            for selection in OUTCOME_LABELS
        }
        probs = dict(zip(OUTCOME_LABELS, devig_1x2(entry["home"], entry["draw"], entry["away"])))
        close_type = None
        closing: dict[str, float | None] = {}
        if closing_odds_type:
            closing_complete = all(
                by_selection[selection]["closing_odds"] is not None
                and str(by_selection[selection]["closing_odds_type"] or "") == closing_odds_type
                for selection in OUTCOME_LABELS
            )
            if closing_complete:
                close_type = closing_odds_type
                closing = {
                    selection: float(by_selection[selection]["closing_odds"])
                    for selection in OUTCOME_LABELS
                }
            else:
                closing = {selection: None for selection in OUTCOME_LABELS}
        else:
            closing = {selection: None for selection in OUTCOME_LABELS}
        markets[source_event_id] = HistoricalOddsMarket(
            source=source,
            source_event_id=source_event_id,
            bookmaker=bookmaker,
            entry_odds_type=entry_odds_type,
            closing_odds_type=close_type,
            entry_odds=entry,
            closing_odds=closing,
            market_probs=probs,
            market_overround=sum(1.0 / odd for odd in entry.values()),
            is_real_historical_odds=True,
            is_timestamp_exact=False,
        )
    if not markets:
        raise ValueError(
            f"No complete 1X2 historical odds found for entry_odds_type={entry_odds_type}"
        )
    return markets


def _walk_forward_real_odds_predictions(
    matches: list[HistoricalOddsMatch],
    *,
    odds: dict[str, HistoricalOddsMarket],
    from_date: date,
    to_date: date,
    start_year: int,
    min_train: int,
    test_season: str | None,
) -> list[RealOddsPrediction]:
    from_iso = from_date.isoformat()
    to_iso = to_date.isoformat()
    test_matches = [
        match
        for match in matches
        if from_iso <= match.match_date <= to_iso
        and match.year >= start_year
        and (test_season is None or match.season == test_season)
        and match.source_event_id in odds
    ]
    if not test_matches:
        raise ValueError("No historical matches with complete real odds in requested window")

    features_by_id = {match.source_event_id: match.as_feature() for match in matches}
    by_date: dict[str, list[HistoricalOddsMatch]] = {}
    for match in test_matches:
        by_date.setdefault(match.match_date, []).append(match)

    output: list[RealOddsPrediction] = []
    for match_date in sorted(by_date):
        train = [
            match.as_feature()
            for match in matches
            if match.match_date < match_date
        ]
        if len(train) < min_train:
            continue
        model = DixonColes().fit(train)
        for match in by_date[match_date]:
            feature = features_by_id[match.source_event_id]
            probs = dict(zip(OUTCOME_LABELS, one_x_two_probabilities(model.score_matrix(feature))))
            market = odds[match.source_event_id]
            for selection in OUTCOME_LABELS:
                model_prob = probs[selection]
                market_prob = market.market_probs[selection]
                entry_odd = market.entry_odds[selection]
                edge_ev = model_prob * entry_odd - 1.0
                edge_prob = model_prob - market_prob
                high_edge_reason = _high_edge_reason(
                    market="1X2",
                    selection=selection,
                    odds_decimal=entry_odd,
                    prob_model=model_prob,
                    edge_ev=edge_ev,
                    edge_prob=edge_prob,
                    odds_quality_flags="",
                )
                recommendation_score = _recommendation_score(
                    edge_ev=edge_ev,
                    odds_decimal=entry_odd,
                    market="1X2",
                    selection=selection,
                    odds_quality_flags="",
                    close_match=None,
                )
                output.append(
                    RealOddsPrediction(
                        match=match,
                        market="1X2",
                        selection=selection,
                        bookmaker=market.bookmaker,
                        entry_odds=entry_odd,
                        entry_odds_type=market.entry_odds_type,
                        closing_odds=market.closing_odds[selection],
                        closing_odds_type=market.closing_odds_type,
                        model_prob=model_prob,
                        market_prob=market_prob,
                        market_overround=market.market_overround,
                        edge_ev=edge_ev,
                        edge_prob=edge_prob,
                        recommendation_score=recommendation_score,
                        high_edge_reason=high_edge_reason,
                        odds_quality_flags="",
                        won=int(match.result_1x2 == selection),
                        is_real_historical_odds=market.is_real_historical_odds,
                        is_timestamp_exact=market.is_timestamp_exact,
                    )
                )
    if not output:
        raise ValueError("Real-odds backtest produced no predictions; check min_train/date range")
    return output


def _select_real_odds_bets(
    predictions: list[RealOddsPrediction],
    *,
    min_edge: float,
    max_edge: float,
    min_prob: float,
    min_score: float,
    max_picks_per_fixture: int,
    max_picks_per_day: int | None,
) -> list[RealOddsBet]:
    eligible = [
        row
        for row in predictions
        if row.edge_ev >= min_edge
        and row.edge_ev <= max_edge
        and row.model_prob >= min_prob
        and row.recommendation_score >= min_score
        and row.is_real_historical_odds
        and not _has_disqualifying_recommendation_marker(
            row.odds_quality_flags,
            row.high_edge_reason,
        )
    ]
    selected: list[RealOddsBet] = []
    for strategy, ranker in [
        ("edge_ev", lambda row: (row.edge_ev, row.recommendation_score)),
        ("recommendation_score", lambda row: (row.recommendation_score, row.edge_ev)),
    ]:
        by_fixture: dict[str, list[RealOddsPrediction]] = {}
        for row in eligible:
            by_fixture.setdefault(row.match.source_event_id, []).append(row)
        strategy_rows: list[RealOddsPrediction] = []
        for rows in by_fixture.values():
            ordered = sorted(rows, key=ranker, reverse=True)
            strategy_rows.extend(ordered[:max_picks_per_fixture])
        if max_picks_per_day is not None and max_picks_per_day >= 0:
            strategy_rows = _limit_real_rows_per_day(
                strategy_rows,
                ranker=ranker,
                max_picks_per_day=max_picks_per_day,
            )
        for row in strategy_rows:
            model_stake = kelly_stake(row.model_prob, row.entry_odds)
            selected.append(
                RealOddsBet(
                    prediction=row,
                    ranking_strategy=strategy,
                    model_stake_fraction=model_stake,
                    capped_stake_fraction=capped_real_stake(
                        model_stake=model_stake,
                        odds_decimal=row.entry_odds,
                        market=row.market,
                        selection=row.selection,
                    ),
                )
            )
    return sorted(
        selected,
        key=lambda bet: (
            bet.prediction.match.match_date,
            bet.prediction.match.source_event_id,
            bet.ranking_strategy,
        ),
    )


def _limit_real_rows_per_day(
    rows: list[RealOddsPrediction],
    *,
    ranker: Callable[[RealOddsPrediction], tuple[float, float]],
    max_picks_per_day: int,
) -> list[RealOddsPrediction]:
    by_day: dict[str, list[RealOddsPrediction]] = {}
    for row in rows:
        by_day.setdefault(row.match.match_date, []).append(row)
    output: list[RealOddsPrediction] = []
    for day_rows in by_day.values():
        output.extend(sorted(day_rows, key=ranker, reverse=True)[:max_picks_per_day])
    return output


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
    min_score: float = 0.0,
    max_picks_per_fixture: int,
    max_picks_per_day: int | None = None,
) -> list[BacktestBet]:
    eligible = [
        row
        for row in predictions
        if row.edge_ev >= min_edge
        and row.edge_ev <= max_edge
        and row.model_prob >= min_prob
        and row.recommendation_score >= min_score
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
                max_picks_per_day=max_picks_per_day,
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
    max_picks_per_day: int | None,
) -> list[BacktestBet]:
    output: list[BacktestBet] = []
    by_fixture: dict[str, list[HistoricalPrediction]] = {}
    for row in rows:
        by_fixture.setdefault(row.match.source_match_id, []).append(row)
    strategy_rows: list[HistoricalPrediction] = []
    for fixture_rows in by_fixture.values():
        ordered = sorted(fixture_rows, key=ranker, reverse=True)
        strategy_rows.extend(ordered[:max_picks_per_fixture])
    if max_picks_per_day is not None and max_picks_per_day >= 0:
        strategy_rows = _limit_proxy_rows_per_day(
            strategy_rows,
            ranker=ranker,
            max_picks_per_day=max_picks_per_day,
        )
    for row in strategy_rows:
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


def _limit_proxy_rows_per_day(
    rows: list[HistoricalPrediction],
    *,
    ranker: Callable[[HistoricalPrediction], tuple[float, float]],
    max_picks_per_day: int,
) -> list[HistoricalPrediction]:
    by_day: dict[str, list[HistoricalPrediction]] = {}
    for row in rows:
        by_day.setdefault(row.match.match_date, []).append(row)
    output: list[HistoricalPrediction] = []
    for day_rows in by_day.values():
        output.extend(sorted(day_rows, key=ranker, reverse=True)[:max_picks_per_day])
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
                "proxy_profit_flat": profit_flat,
                "proxy_profit_staked": profit_model,
                "proxy_profit_capped_staked": profit_capped,
                "odds_source": "training_marginal_proxy",
                "is_real_historical_odds": 0,
                "is_timestamp_exact": 0,
                "profitability_gate": "blocked_proxy_odds",
                "closing_odds": "",
                "edge_bucket": row.edge_bucket,
                "odds_bucket": row.odds_bucket,
                "prob_bucket": row.prob_bucket,
                "score_bucket": row.score_bucket,
                "market_type": row.market_type,
                "competition_type": _competition_type(match),
                "high_edge_reason": row.high_edge_reason,
                "odds_quality_flags": row.odds_quality_flags,
            }
        )
    _write_dict_rows(path, rows)


def _write_real_odds_recommendations(path: Path, bets: list[RealOddsBet]) -> None:
    rows = []
    for bet in bets:
        row = bet.prediction
        match = row.match
        profit_flat = _real_profit(row, stake=1.0)
        profit_model = _real_profit(row, stake=bet.model_stake_fraction)
        profit_capped = _real_profit(row, stake=bet.capped_stake_fraction)
        clv = _real_clv(row)
        rows.append(
            {
                "ranking_strategy": bet.ranking_strategy,
                "event_id": match.source_event_id,
                "source": match.source,
                "league": match.league_code,
                "season": match.season,
                "match_date": match.match_date,
                "home_team": match.home_team,
                "away_team": match.away_team,
                "market": row.market,
                "selection": row.selection,
                "decision_time_rule": "pre_match_open_proxy",
                "bookmaker": row.bookmaker,
                "entry_odds": row.entry_odds,
                "entry_odds_type": row.entry_odds_type,
                "closing_odds": row.closing_odds if row.closing_odds is not None else "",
                "closing_odds_type": row.closing_odds_type or "",
                "model_prob": row.model_prob,
                "fair_market_prob": row.market_prob,
                "market_overround": row.market_overround,
                "edge_ev": row.edge_ev,
                "edge_prob": row.edge_prob,
                "recommendation_score": row.recommendation_score,
                "model_stake_fraction": bet.model_stake_fraction,
                "capped_stake_fraction": bet.capped_stake_fraction,
                "result": row.selection if row.won else match.result_1x2,
                "score": f"{match.home_goals}-{match.away_goals}",
                "won": row.won,
                "profit_flat_real": profit_flat,
                "profit_model_real": profit_model,
                "profit_capped_real": profit_capped,
                "clv_real": clv if clv is not None else "",
                "odds_source": row.match.source,
                "is_real_historical_odds": int(row.is_real_historical_odds),
                "is_timestamp_exact": int(row.is_timestamp_exact),
                "profitability_gate": "ok_real_historical_odds",
                "edge_bucket": row.edge_bucket,
                "odds_bucket": row.odds_bucket,
                "prob_bucket": row.prob_bucket,
                "score_bucket": row.score_bucket,
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
        ("score_bucket", lambda bet: bet.prediction.score_bucket),
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


def _real_summary_rows(bets: list[RealOddsBet]) -> list[dict[str, object]]:
    groupers: list[tuple[str, Callable[[RealOddsBet], str]]] = [
        ("overall", lambda bet: "all"),
        ("ranking_strategy", lambda bet: bet.ranking_strategy),
        ("league", lambda bet: bet.prediction.match.league_code),
        ("season", lambda bet: bet.prediction.match.season),
        ("selection", lambda bet: bet.prediction.selection),
        ("edge_bucket", lambda bet: bet.prediction.edge_bucket),
        ("odds_bucket", lambda bet: bet.prediction.odds_bucket),
        ("prob_bucket", lambda bet: bet.prediction.prob_bucket),
        ("score_bucket", lambda bet: bet.prediction.score_bucket),
    ]
    output: list[dict[str, object]] = []
    for group_type, key_fn in groupers:
        grouped: dict[str, list[RealOddsBet]] = {}
        for bet in bets:
            grouped.setdefault(key_fn(bet), []).append(bet)
        for group, rows in sorted(grouped.items()):
            output.append(_real_metric_row(group_type, group, rows))
    return output


def _policy_grid_rows(
    bets: list[RealOddsBet],
    *,
    min_score: float,
    min_edge: float,
    max_picks_per_day: int | None,
) -> list[dict[str, object]]:
    grouped: dict[str, list[RealOddsBet]] = {}
    for bet in bets:
        grouped.setdefault(bet.ranking_strategy, []).append(bet)
    output: list[dict[str, object]] = []
    for ranking_strategy, rows in sorted(grouped.items()):
        metrics = _real_metric_row("ranking_strategy", ranking_strategy, rows)
        output.append(
            {
                "min_score": min_score,
                "min_edge": min_edge,
                "max_picks_per_day": (
                    "none" if max_picks_per_day is None else max_picks_per_day
                ),
                "ranking_strategy": ranking_strategy,
                "num_bets": metrics["num_bets"],
                "sample_size_tier": metrics["sample_size_tier"],
                "decision_gate": metrics["decision_gate"],
                "hit_rate": metrics["hit_rate"],
                "avg_entry_odds": metrics["avg_entry_odds"],
                "avg_edge_ev": metrics["avg_edge_ev"],
                "avg_recommendation_score": metrics["avg_recommendation_score"],
                "roi_flat_real": metrics["roi_flat_real"],
                "roi_capped_real": metrics["roi_capped_real"],
                "max_drawdown_flat": metrics["max_drawdown_flat"],
                "mean_clv_real": metrics["mean_clv_real"],
                "positive_clv_rate": metrics["positive_clv_rate"],
                "clv_rows": metrics["clv_rows"],
                "brier_score": metrics["brier_score"],
                "log_loss": metrics["log_loss"],
            }
        )
    if not output:
        for ranking_strategy in ("edge_ev", "recommendation_score"):
            output.append(
                {
                    "min_score": min_score,
                    "min_edge": min_edge,
                    "max_picks_per_day": (
                        "none" if max_picks_per_day is None else max_picks_per_day
                    ),
                    "ranking_strategy": ranking_strategy,
                    "num_bets": 0,
                    "sample_size_tier": sample_size_tier(0),
                    "decision_gate": decision_gate(0),
                    "hit_rate": 0.0,
                    "avg_entry_odds": 0.0,
                    "avg_edge_ev": 0.0,
                    "avg_recommendation_score": 0.0,
                    "roi_flat_real": 0.0,
                    "roi_capped_real": 0.0,
                    "max_drawdown_flat": 0.0,
                    "mean_clv_real": 0.0,
                    "positive_clv_rate": 0.0,
                    "clv_rows": 0,
                    "brier_score": 0.0,
                    "log_loss": 0.0,
                }
            )
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
        "profitability_gate": "blocked_proxy_odds",
        "num_bets": len(bets),
        "sample_size_tier": sample_size_tier(len(bets)),
        "decision_gate": decision_gate(len(bets)),
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
        "proxy_profit_flat": sum(flat_profits),
        "proxy_profit_staked": sum(model_profits),
        "proxy_profit_capped_staked": sum(capped_profits),
        "proxy_roi_flat": sum(flat_profits) / len(flat_profits) if flat_profits else 0.0,
        "proxy_roi_staked": sum(model_profits) / model_staked if model_staked > 0 else 0.0,
        "proxy_roi_capped_staked": (
            sum(capped_profits) / capped_staked if capped_staked > 0 else 0.0
        ),
        "max_drawdown": max_drawdown(flat_profits),
        "brier_score": _mean(
            [(bet.prediction.model_prob - bet.prediction.won) ** 2 for bet in bets]
        ),
        "log_loss": _mean(
            [_binary_logloss(bet.prediction.model_prob, bet.prediction.won) for bet in bets]
        ),
    }


def _real_metric_row(group_type: str, group: str, bets: list[RealOddsBet]) -> dict[str, object]:
    flat_profits = [_real_profit(bet.prediction, stake=1.0) for bet in bets]
    model_profits = [
        _real_profit(bet.prediction, stake=bet.model_stake_fraction) for bet in bets
    ]
    capped_profits = [
        _real_profit(bet.prediction, stake=bet.capped_stake_fraction) for bet in bets
    ]
    model_staked = sum(bet.model_stake_fraction for bet in bets)
    capped_staked = sum(bet.capped_stake_fraction for bet in bets)
    clv_values = [
        clv
        for bet in bets
        for clv in [_real_clv(bet.prediction)]
        if clv is not None
    ]
    positive_clv_rate = (
        sum(1 for value in clv_values if value > 0) / len(clv_values)
        if clv_values
        else 0.0
    )
    return {
        "group_type": group_type,
        "group": group,
        "profitability_gate": "ok_real_historical_odds" if bets else "blocked_insufficient_sample",
        "num_bets": len(bets),
        "sample_size_tier": sample_size_tier(len(bets)),
        "decision_gate": decision_gate(len(bets)),
        "hit_rate": _mean([bet.prediction.won for bet in bets]),
        "avg_entry_odds": _mean([bet.prediction.entry_odds for bet in bets]),
        "avg_closing_odds": _mean(
            [
                bet.prediction.closing_odds
                for bet in bets
                if bet.prediction.closing_odds is not None
            ]
        ),
        "avg_edge_ev": _mean([bet.prediction.edge_ev for bet in bets]),
        "avg_recommendation_score": _mean(
            [bet.prediction.recommendation_score for bet in bets]
        ),
        "profit_flat_real": sum(flat_profits),
        "profit_model_real": sum(model_profits),
        "profit_capped_real": sum(capped_profits),
        "roi_flat_real": sum(flat_profits) / len(flat_profits) if flat_profits else 0.0,
        "roi_model_real": sum(model_profits) / model_staked if model_staked > 0 else 0.0,
        "roi_capped_real": sum(capped_profits) / capped_staked if capped_staked > 0 else 0.0,
        "max_drawdown_flat": max_drawdown(flat_profits),
        "mean_clv_real": _mean(clv_values),
        "positive_clv_rate": positive_clv_rate,
        "clv_rows": len(clv_values),
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


def _real_calibration_rows(predictions: list[RealOddsPrediction]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str, str], list[RealOddsPrediction]] = {}
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


def _real_clv_rows(bets: list[RealOddsBet]) -> list[dict[str, object]]:
    groupers: list[tuple[str, Callable[[RealOddsBet], str]]] = [
        ("overall", lambda bet: "all"),
        ("ranking_strategy", lambda bet: bet.ranking_strategy),
        ("selection", lambda bet: bet.prediction.selection),
        ("edge_bucket", lambda bet: bet.prediction.edge_bucket),
        ("odds_bucket", lambda bet: bet.prediction.odds_bucket),
        ("score_bucket", lambda bet: bet.prediction.score_bucket),
    ]
    output: list[dict[str, object]] = []
    for group_type, key_fn in groupers:
        grouped: dict[str, list[float]] = {}
        for bet in bets:
            clv = _real_clv(bet.prediction)
            if clv is None:
                continue
            grouped.setdefault(key_fn(bet), []).append(clv)
        for group, values in sorted(grouped.items()):
            positive_clv_rate = (
                sum(1 for value in values if value > 0) / len(values)
                if values
                else 0.0
            )
            output.append(
                {
                    "group_type": group_type,
                    "group": group,
                    "clv_rows": len(values),
                    "sample_size_tier": sample_size_tier(len(values)),
                    "decision_gate": decision_gate(len(values)),
                    "mean_clv_real": _mean(values),
                    "positive_clv_rate": positive_clv_rate,
                    "median_proxy": "",
                    "clv_reference": "football_data_close",
                    "is_timestamp_exact": 0,
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


def score_bucket(value: float) -> str:
    return _bucket(
        value,
        [
            (0.025, "0.0-2.5%"),
            (0.05, "2.5-5.0%"),
            (0.075, "5.0-7.5%"),
            (0.10, "7.5-10.0%"),
            (0.15, "10.0-15.0%"),
            (0.20, "15.0-20.0%"),
        ],
        high="20.0%+",
        low="<0.0%",
    )


def sample_size_tier(count: int) -> str:
    if count < 100:
        return "smoke_or_exploratory"
    if count < 300:
        return "initial_signal"
    if count < 1000:
        return "useful_signal"
    return "more_stable_signal"


def decision_gate(count: int) -> str:
    if count < 100:
        return "no_threshold_changes"
    if count < 300:
        return "review_only"
    if count < 1000:
        return "cautious_adjustments_allowed"
    return "adjustments_supported_with_bucket_checks"


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


def _real_profit(row: RealOddsPrediction, *, stake: float) -> float:
    if stake <= 0:
        return 0.0
    return stake * (row.entry_odds - 1.0) if row.won else -stake


def _real_clv(row: RealOddsPrediction) -> float | None:
    if row.closing_odds is None or row.closing_odds <= 1.0:
        return None
    return row.entry_odds / row.closing_odds - 1.0


def _historical_team_key(value: str) -> str:
    return " ".join(value.casefold().split())


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


def _record_real_backtest_quality(
    conn: sqlite3.Connection,
    run_id: int,
    metadata: dict[str, object],
    summary_rows: list[dict[str, object]],
    calibration_rows: list[dict[str, object]],
    clv_rows: list[dict[str, object]],
) -> None:
    overall = [
        row
        for row in summary_rows
        if row["group_type"] == "ranking_strategy"
    ]
    details = {
        **metadata,
        "ranking_strategy_rows": overall,
        "clv_summary_rows": clv_rows,
    }
    clv_count = sum(int(row["clv_rows"]) for row in clv_rows if row["group_type"] == "overall")
    for metric_name, value in [
        ("prediction_rows", metadata["prediction_rows"]),
        ("recommendation_rows", metadata["recommendation_rows"]),
        ("summary_rows", len(summary_rows)),
        ("calibration_rows", len(calibration_rows)),
        ("clv_rows", clv_count),
        ("real_historical_odds", 1.0),
        ("timestamp_exact", 0.0),
    ]:
        record_quality(
            conn,
            run_id,
            "historical_real_odds_backtest",
            metric_name,
            float(value),
            json.dumps(details, sort_keys=True),
        )


def _record_policy_grid_quality(
    conn: sqlite3.Connection,
    run_id: int,
    metadata: dict[str, object],
    rows: list[dict[str, object]],
) -> None:
    useful_rows = [
        row
        for row in rows
        if str(row["decision_gate"]) in {
            "cautious_adjustments_allowed",
            "adjustments_supported_with_bucket_checks",
        }
    ]
    best_clv = max(
        useful_rows or rows,
        key=lambda row: (
            float(row["mean_clv_real"]),
            float(row["positive_clv_rate"]),
            float(row["roi_capped_real"]),
        ),
        default=None,
    )
    details = {
        **metadata,
        "best_clv_row": best_clv,
        "warning": (
            "Policy grid rows are for robustness screening. Avoid selecting a policy "
            "from one league without bucket checks and cross-league validation."
        ),
    }
    for metric_name, value in [
        ("prediction_rows", metadata["prediction_rows"]),
        ("grid_rows", len(rows)),
        ("useful_grid_rows", len(useful_rows)),
        ("best_mean_clv", float(best_clv["mean_clv_real"]) if best_clv else 0.0),
        (
            "best_positive_clv_rate",
            float(best_clv["positive_clv_rate"]) if best_clv else 0.0,
        ),
    ]:
        record_quality(
            conn,
            run_id,
            "historical_policy_grid",
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
