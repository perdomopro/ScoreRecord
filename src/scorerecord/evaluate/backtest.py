"""Historical walk-forward backtest and calibration CSV reports.

Proxy mode uses a training-window marginal probability as a market proxy. That makes
the reports useful for calibration, bucket diagnostics, and comparing
recommendation_score vs raw EV, but not for claiming real betting ROI.

Historical mode is deliberately separate: it uses real historical odds stored in
`historical_odds`.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from itertools import product
from pathlib import Path

from scorerecord.evaluate.edge import _high_edge_reason, _recommendation_score, devig_1x2
from scorerecord.evaluate.goal_markets import _walk_forward_predictions, build_goal_markets
from scorerecord.evaluate.recommendations import (
    _has_disqualifying_recommendation_marker,
    capped_real_stake,
    kelly_stake,
)
from scorerecord.evaluate.scoring import (
    EPS,
    binary_brier,
    binary_logloss,
    block_bootstrap_mean,
    club_week_block,
    multiclass_brier,
    multiclass_expected_calibration_error,
    multiclass_logloss,
    normalize_probabilities,
)
from scorerecord.features.build import OUTCOME_LABELS, MatchFeature, load_match_features
from scorerecord.ingest.football_data import normalize_season, resolve_league_code
from scorerecord.models.calibration import PlattCalibrator
from scorerecord.models.dixon_coles import DixonColes
from scorerecord.models.markets import btts_probability, one_x_two_probabilities, over_probability
from scorerecord.runs import ingest_run, record_quality


DEFAULT_OU_LINES = (1.5, 2.5, 3.5)
_EPS = EPS


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
class BenchmarkUnit:
    match: HistoricalOddsMatch
    market: str
    scoring_unit: str
    outcome: str
    label: int
    model_probs: tuple[float, ...]
    market_probs: tuple[float, ...]
    model_prob: float
    market_prob: float
    entry_odds: float
    market_overround: float
    model_logloss: float
    market_logloss: float
    model_brier: float
    market_brier: float
    model_top_selection: str
    market_top_selection: str
    num_selection_rows: int

    @property
    def prob_bucket(self) -> str:
        return prob_bucket(self.model_prob)

    @property
    def market_prob_bucket(self) -> str:
        return prob_bucket(self.market_prob)

    @property
    def odds_bucket(self) -> str:
        return odds_bucket(self.entry_odds)


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


@dataclass(frozen=True)
class MarketBenchmarkSummary:
    run_id: int
    output_dir: Path
    rows_written: int
    summary_rows: int
    model_minus_market_logloss: float
    benchmark_state: str | None = None
    market: str = "1X2"
    num_matches: int | None = None
    num_selection_rows: int | None = None
    model_raw_logloss: float | None = None
    model_calibrated_logloss: float | None = None
    market_preclose_logloss: float | None = None
    market_close_logloss: float | None = None
    delta_calibrated_vs_preclose: float | None = None
    delta_calibrated_vs_close: float | None = None
    ci_delta_calibrated_vs_preclose: tuple[float, float] | None = None
    ci_delta_calibrated_vs_close: tuple[float, float] | None = None

    def as_row(self) -> str:
        base = (
            f"run_id={self.run_id} output_dir={self.output_dir} "
            f"rows_written={self.rows_written} summary_rows={self.summary_rows} "
            f"market={self.market} "
            f"model_minus_market_logloss={self.model_minus_market_logloss:.6f}"
        )
        if self.benchmark_state is None:
            return base
        ci_pre = _format_ci(self.ci_delta_calibrated_vs_preclose)
        ci_close = _format_ci(self.ci_delta_calibrated_vs_close)
        return (
            f"{base} benchmark_state={self.benchmark_state} "
            f"num_matches={self.num_matches} "
            f"num_selection_rows={self.num_selection_rows} "
            f"model_raw_logloss={_format_optional_metric(self.model_raw_logloss)} "
            f"model_calibrated_logloss={_format_optional_metric(self.model_calibrated_logloss)} "
            f"market_preclose_logloss={_format_optional_metric(self.market_preclose_logloss)} "
            f"market_close_logloss={_format_optional_metric(self.market_close_logloss)} "
            f"delta_calibrated_vs_preclose="
            f"{_format_optional_metric(self.delta_calibrated_vs_preclose)} "
            f"delta_calibrated_vs_close={_format_optional_metric(self.delta_calibrated_vs_close)} "
            f"ci_delta_calibrated_vs_preclose={ci_pre} "
            f"ci_delta_calibrated_vs_close={ci_close}"
        )


@dataclass(frozen=True)
class Ou25BenchmarkPrediction:
    match: HistoricalOddsMatch
    label: int
    model_raw_prob: float
    model_calibrated_prob: float | None
    market_preclose_prob: float
    market_close_prob: float | None
    entry_over_odds: float
    entry_under_odds: float
    closing_over_odds: float | None
    closing_under_odds: float | None
    market_preclose_overround: float
    market_close_overround: float | None


@dataclass(frozen=True)
class Ou25MetricSet:
    model_name: str
    market_reference: str
    num_matches: int
    num_selection_rows: int
    model_logloss: float
    market_logloss: float
    model_brier: float
    market_brier: float
    model_ece: float
    market_ece: float
    delta_logloss: float
    delta_brier: float
    ci_low: float | None
    ci_high: float | None
    bootstrap_blocks: int
    bootstrap_iterations: int
    benchmark_state: str


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
    generated_utc = datetime.now(UTC).isoformat(timespec="seconds")

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
    generated_utc = datetime.now(UTC).isoformat(timespec="seconds")

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
    market_shrink_alphas: tuple[float, ...] = (1.0,),
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
    generated_utc = datetime.now(UTC).isoformat(timespec="seconds")

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
        for alpha in market_shrink_alphas:
            alpha_predictions = _shrink_real_odds_predictions(predictions, alpha=alpha)
            for min_score, min_edge, max_per_day in product(
                min_scores,
                min_edges,
                max_picks_per_days,
            ):
                selected = _select_real_odds_bets(
                    alpha_predictions,
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
                        market_shrink_alpha=alpha,
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
            "market_shrink_alphas": list(market_shrink_alphas),
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


def run_market_benchmark_report(
    conn: sqlite3.Connection,
    *,
    from_date: date,
    to_date: date,
    out_dir: Path,
    start_year: int = 2021,
    min_train: int = 200,
    historical_odds_source: str = "football-data",
    entry_odds: str | None = None,
    closing_odds: str = "avg_close",
    market: str = "1X2",
    league: str | None = None,
    leagues: tuple[str, ...] | None = None,
    season: str | None = None,
    calibration: str = "prequential",
    bootstrap_block: str = "week",
    bootstrap_samples: int = 2000,
    cwd: Path | None = None,
) -> MarketBenchmarkSummary:
    if market not in {"1X2", "OU2.5"}:
        raise ValueError("Market benchmark currently supports market=1X2 or OU2.5")
    if historical_odds_source != "football-data":
        raise ValueError("Only historical_odds_source=football-data is supported")
    if calibration != "prequential":
        raise ValueError("Only calibration=prequential is supported for market benchmarks")
    if bootstrap_block != "week":
        raise ValueError("Only bootstrap_block=week is supported for club benchmarks")
    entry_odds = entry_odds or ("avg_preclose" if market == "OU2.5" else "avg_open")

    league_values = tuple(leagues or (() if league is None else (league,)))
    league_codes = tuple(resolve_league_code(value) for value in league_values)
    league_code = league_codes[0] if len(league_codes) == 1 else None
    normalized_season = normalize_season(season) if season else None
    generated_utc = datetime.now(UTC).isoformat(timespec="seconds")

    with ingest_run(conn, "market-benchmark", source=historical_odds_source, cwd=cwd) as run_id:
        matches = _load_historical_odds_matches(
            conn,
            source=historical_odds_source,
            league_code=league_code,
            league_codes=league_codes or None,
            test_season=normalized_season,
            to_date=to_date,
        )
        odds = _load_historical_odds_markets(
            conn,
            source=historical_odds_source,
            entry_odds_type=entry_odds,
            closing_odds_type=closing_odds,
            market=market,
        )
        if market == "OU2.5":
            return _run_ou25_market_benchmark_report(
                conn,
                run_id=run_id,
                matches=matches,
                odds=odds,
                from_date=from_date,
                to_date=to_date,
                out_dir=out_dir,
                start_year=start_year,
                min_train=min_train,
                historical_odds_source=historical_odds_source,
                entry_odds=entry_odds,
                closing_odds=closing_odds,
                league_codes=league_codes,
                normalized_season=normalized_season,
                calibration=calibration,
                bootstrap_samples=bootstrap_samples,
                generated_utc=generated_utc,
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
        row_details = _market_benchmark_detail_rows(predictions)
        summary_rows = _market_benchmark_summary_rows(predictions)
        overall = next(
            row
            for row in summary_rows
            if row["group_type"] == "overall" and row["group"] == "all"
        )

        out_dir.mkdir(parents=True, exist_ok=True)
        detail_path = out_dir / f"market_benchmark_rows_{run_id}.csv"
        summary_path = out_dir / f"market_benchmark_summary_{run_id}.csv"
        metadata_path = out_dir / f"market_benchmark_metadata_{run_id}.json"
        _write_dict_rows(detail_path, row_details)
        _write_dict_rows(summary_path, summary_rows)
        metadata = {
            "run_id": run_id,
            "generated_utc": generated_utc,
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "start_year": start_year,
            "min_train": min_train,
            "odds_source": historical_odds_source,
            "entry_odds_type": entry_odds,
            "closing_odds_type": closing_odds,
            "market": market,
            "league_code": league_code,
            "league_codes": list(league_codes),
            "test_season": normalized_season,
            "prediction_rows": len(predictions),
            "summary_rows": len(summary_rows),
            "overall": overall,
            "interpretation": (
                "Negative model_minus_market_logloss means the model beat fair market "
                "probabilities for this historical sample. Positive means shrink or "
                "market-following deserves serious review."
            ),
        }
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
        _record_market_benchmark_quality(conn, run_id, metadata, summary_rows)
        conn.commit()

    return MarketBenchmarkSummary(
        run_id=run_id,
        output_dir=out_dir,
        rows_written=len(row_details),
        summary_rows=len(summary_rows),
        model_minus_market_logloss=float(overall["model_minus_market_logloss"]),
        market=market,
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
    generated_utc = datetime.now(UTC).isoformat(timespec="seconds")
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


def _run_ou25_market_benchmark_report(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    matches: list[HistoricalOddsMatch],
    odds: dict[str, HistoricalOddsMarket],
    from_date: date,
    to_date: date,
    out_dir: Path,
    start_year: int,
    min_train: int,
    historical_odds_source: str,
    entry_odds: str,
    closing_odds: str | None,
    league_codes: tuple[str, ...],
    normalized_season: str | None,
    calibration: str,
    bootstrap_samples: int,
    generated_utc: str,
) -> MarketBenchmarkSummary:
    predictions = _walk_forward_ou25_benchmark_predictions(
        matches,
        odds=odds,
        from_date=from_date,
        to_date=to_date,
        start_year=start_year,
        min_train=min_train,
        calibration=calibration,
    )
    detail_rows = _ou25_benchmark_detail_rows(predictions)
    metric_sets = _ou25_benchmark_metric_sets(
        predictions,
        bootstrap_samples=bootstrap_samples,
    )
    summary_rows = [_ou25_metric_row(metric) for metric in metric_sets]
    overall = _ou25_overall_summary(metric_sets)

    out_dir.mkdir(parents=True, exist_ok=True)
    detail_path = out_dir / f"market_benchmark_ou25_rows_{run_id}.csv"
    summary_path = out_dir / f"market_benchmark_ou25_summary_{run_id}.csv"
    metadata_path = out_dir / f"market_benchmark_ou25_metadata_{run_id}.json"
    _write_dict_rows(detail_path, detail_rows)
    _write_dict_rows(summary_path, summary_rows)
    metadata = {
        "run_id": run_id,
        "generated_utc": generated_utc,
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "start_year": start_year,
        "min_train": min_train,
        "odds_source": historical_odds_source,
        "entry_odds_type": entry_odds,
        "closing_odds_type": closing_odds,
        "market": "OU2.5",
        "league_codes": list(league_codes),
        "test_season": normalized_season,
        "calibration": calibration,
        "bootstrap_block": "league_iso_week",
        "bootstrap_samples": bootstrap_samples,
        "prediction_rows": len(detail_rows),
        "summary_rows": len(summary_rows),
        "odds_coverage_counts": {
            "matches_seen": len(matches),
            "complete_preclose_markets": len(odds),
            "benchmark_matches": overall["num_matches"],
            "benchmark_selection_rows": overall["num_selection_rows"],
            "close_complete_matches": sum(
                1 for row in predictions if row.market_close_prob is not None
            ),
        },
        "overall": overall,
        "interpretation": (
            "This club OU2.5 benchmark compares independent Dixon-Coles probabilities "
            "against Football-Data fair market probabilities. It does not validate the "
            "World Cup or create paper candidates."
        ),
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    _record_ou25_market_benchmark_quality(conn, run_id, metadata, metric_sets)
    conn.commit()

    return MarketBenchmarkSummary(
        run_id=run_id,
        output_dir=out_dir,
        rows_written=len(detail_rows),
        summary_rows=len(summary_rows),
        model_minus_market_logloss=float(overall["delta_calibrated_vs_preclose"]),
        benchmark_state=str(overall["benchmark_state"]),
        market="OU2.5",
        num_matches=int(overall["num_matches"]),
        num_selection_rows=int(overall["num_selection_rows"]),
        model_raw_logloss=float(overall["model_raw_logloss"]),
        model_calibrated_logloss=float(overall["model_calibrated_logloss"]),
        market_preclose_logloss=float(overall["market_preclose_logloss"]),
        market_close_logloss=_optional_float(overall["market_close_logloss"]),
        delta_calibrated_vs_preclose=float(overall["delta_calibrated_vs_preclose"]),
        delta_calibrated_vs_close=_optional_float(overall["delta_calibrated_vs_close"]),
        ci_delta_calibrated_vs_preclose=overall["ci_delta_calibrated_vs_preclose"],
        ci_delta_calibrated_vs_close=overall["ci_delta_calibrated_vs_close"],
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
    league_codes: tuple[str, ...] | None = None,
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
    if league_codes:
        placeholders = ",".join("?" for _ in league_codes)
        filters.append(f"league_code IN ({placeholders})")
        params.extend(league_codes)
    elif league_code is not None:
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
    market: str = "1X2",
) -> dict[str, HistoricalOddsMarket]:
    if market == "OU2.5":
        return _load_historical_ou25_markets(
            conn,
            source=source,
            entry_odds_type=entry_odds_type,
            closing_odds_type=closing_odds_type,
        )
    if market != "1X2":
        raise ValueError("Historical odds loader supports market=1X2 or OU2.5")
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
        probs = dict(
            zip(
                OUTCOME_LABELS,
                devig_1x2(entry["home"], entry["draw"], entry["away"]),
                strict=True,
            )
        )
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


def _load_historical_ou25_markets(
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

    markets: dict[str, HistoricalOddsMarket] = {}
    for (source_event_id, bookmaker), group in grouped.items():
        by_selection = {str(row["selection"]): row for row in group}
        if not {"over", "under"}.issubset(by_selection):
            continue
        source_families = {
            _historical_odds_family(by_selection[selection])
            for selection in ("over", "under")
        }
        if len(source_families) > 1:
            continue
        entry = {
            selection: float(by_selection[selection]["entry_odds"])
            for selection in ("over", "under")
        }
        if entry["over"] <= 1.0 or entry["under"] <= 1.0:
            continue
        market_probs = _devig_two_way(entry["over"], entry["under"])
        close_type = None
        closing: dict[str, float | None] = {"over": None, "under": None}
        if closing_odds_type:
            closing_complete = all(
                by_selection[selection]["closing_odds"] is not None
                and str(by_selection[selection]["closing_odds_type"] or "") == closing_odds_type
                and float(by_selection[selection]["closing_odds"]) > 1.0
                for selection in ("over", "under")
            )
            if closing_complete:
                close_type = closing_odds_type
                closing = {
                    selection: float(by_selection[selection]["closing_odds"])
                    for selection in ("over", "under")
                }
        markets[source_event_id] = HistoricalOddsMarket(
            source=source,
            source_event_id=source_event_id,
            bookmaker=bookmaker,
            entry_odds_type=entry_odds_type,
            closing_odds_type=close_type,
            entry_odds=entry,
            closing_odds=closing,
            market_probs=market_probs,
            market_overround=sum(1.0 / odd for odd in entry.values()),
            is_real_historical_odds=True,
            is_timestamp_exact=False,
        )
    if not markets:
        raise ValueError(
            f"No complete OU2.5 historical odds found for entry_odds_type={entry_odds_type}"
        )
    return markets


def _historical_odds_family(row: sqlite3.Row) -> str:
    raw = row["metadata_json"]
    if not raw:
        return str(row["entry_odds_type"] or "")
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return str(row["entry_odds_type"] or "")
    return str(parsed.get("entry_column_family") or row["entry_odds_type"] or "")


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
            probs = dict(
                zip(
                    OUTCOME_LABELS,
                    one_x_two_probabilities(model.score_matrix(feature)),
                    strict=True,
                )
            )
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


def _walk_forward_ou25_benchmark_predictions(
    matches: list[HistoricalOddsMatch],
    *,
    odds: dict[str, HistoricalOddsMarket],
    from_date: date,
    to_date: date,
    start_year: int,
    min_train: int,
    calibration: str,
) -> list[Ou25BenchmarkPrediction]:
    from_iso = from_date.isoformat()
    to_iso = to_date.isoformat()
    test_matches = [
        match
        for match in matches
        if from_iso <= match.match_date <= to_iso
        and match.year >= start_year
        and match.source_event_id in odds
    ]
    if not test_matches:
        raise ValueError("No OU2.5 historical matches with complete real odds in requested window")

    features_by_id = {match.source_event_id: match.as_feature() for match in matches}
    by_date: dict[str, list[HistoricalOddsMatch]] = {}
    for match in test_matches:
        by_date.setdefault(match.match_date, []).append(match)

    output: list[Ou25BenchmarkPrediction] = []
    previous_raw: list[tuple[float, int]] = []
    for match_date in sorted(by_date):
        train = [match.as_feature() for match in matches if match.match_date < match_date]
        if len(train) < min_train:
            continue
        calibrator: PlattCalibrator | None = None
        if calibration == "prequential" and previous_raw:
            calibrator = PlattCalibrator().fit(
                [row[0] for row in previous_raw],
                [row[1] for row in previous_raw],
            )
        model = DixonColes().fit(train)
        current_raw: list[tuple[float, int]] = []
        for match in by_date[match_date]:
            feature = features_by_id[match.source_event_id]
            score_grid = model.score_matrix(feature)
            raw_prob = over_probability(score_grid, 2.5)
            label = 1 if (match.home_goals + match.away_goals) > 2.5 else 0
            market = odds[match.source_event_id]
            close_prob = None
            close_overround = None
            if market.closing_odds_type is not None:
                close_over = market.closing_odds.get("over")
                close_under = market.closing_odds.get("under")
                if close_over is not None and close_under is not None:
                    close_probs = _devig_two_way(close_over, close_under)
                    close_prob = close_probs["over"]
                    close_overround = (1.0 / close_over) + (1.0 / close_under)
            output.append(
                Ou25BenchmarkPrediction(
                    match=match,
                    label=label,
                    model_raw_prob=raw_prob,
                    model_calibrated_prob=(
                        calibrator.transform(raw_prob) if calibrator is not None else None
                    ),
                    market_preclose_prob=market.market_probs["over"],
                    market_close_prob=close_prob,
                    entry_over_odds=market.entry_odds["over"],
                    entry_under_odds=market.entry_odds["under"],
                    closing_over_odds=market.closing_odds.get("over"),
                    closing_under_odds=market.closing_odds.get("under"),
                    market_preclose_overround=market.market_overround,
                    market_close_overround=close_overround,
                )
            )
            current_raw.append((raw_prob, label))
        previous_raw.extend(current_raw)
    if not output:
        raise ValueError("OU2.5 benchmark produced no predictions; check min_train/date range")
    return output


def _shrink_real_odds_predictions(
    predictions: list[RealOddsPrediction],
    *,
    alpha: float,
) -> list[RealOddsPrediction]:
    if alpha < 0.0 or alpha > 1.0:
        raise ValueError("market_shrink_alpha must be between 0 and 1")
    if abs(alpha - 1.0) < _EPS:
        return predictions

    output: list[RealOddsPrediction] = []
    for row in predictions:
        model_prob = alpha * row.model_prob + (1.0 - alpha) * row.market_prob
        edge_ev = model_prob * row.entry_odds - 1.0
        edge_prob = model_prob - row.market_prob
        high_edge_reason = _high_edge_reason(
            market=row.market,
            selection=row.selection,
            odds_decimal=row.entry_odds,
            prob_model=model_prob,
            edge_ev=edge_ev,
            edge_prob=edge_prob,
            odds_quality_flags=row.odds_quality_flags,
        )
        recommendation_score = _recommendation_score(
            edge_ev=edge_ev,
            odds_decimal=row.entry_odds,
            market=row.market,
            selection=row.selection,
            odds_quality_flags=row.odds_quality_flags,
            close_match=None,
        )
        output.append(
            replace(
                row,
                model_prob=model_prob,
                edge_ev=edge_ev,
                edge_prob=edge_prob,
                recommendation_score=recommendation_score,
                high_edge_reason=high_edge_reason,
            )
        )
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
    for selection, prob in zip(
        OUTCOME_LABELS,
        one_x_two_probabilities(score_grid),
        strict=True,
    ):
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
    market_shrink_alpha: float,
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
                "market_shrink_alpha": market_shrink_alpha,
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
                    "market_shrink_alpha": market_shrink_alpha,
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
    model_profits = [
        bet.model_stake_fraction * profit
        for bet, profit in zip(bets, flat_profits, strict=True)
    ]
    capped_profits = [
        bet.capped_stake_fraction * profit
        for bet, profit in zip(bets, flat_profits, strict=True)
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


def _market_benchmark_detail_rows(predictions: list[RealOddsPrediction]) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for row in predictions:
        model_logloss = _binary_logloss(row.model_prob, row.won)
        market_logloss = _binary_logloss(row.market_prob, row.won)
        model_brier = (row.model_prob - row.won) ** 2
        market_brier = (row.market_prob - row.won) ** 2
        output.append(
            {
                "match_date": row.match.match_date,
                "league_code": row.match.league_code,
                "season": row.match.season,
                "source_event_id": row.match.source_event_id,
                "home_team": row.match.home_team,
                "away_team": row.match.away_team,
                "market": row.market,
                "selection": row.selection,
                "won": row.won,
                "model_prob": row.model_prob,
                "fair_market_prob": row.market_prob,
                "entry_odds": row.entry_odds,
                "closing_odds": "" if row.closing_odds is None else row.closing_odds,
                "market_overround": row.market_overround,
                "edge_ev": row.edge_ev,
                "edge_prob": row.edge_prob,
                "recommendation_score": row.recommendation_score,
                "model_logloss": model_logloss,
                "market_logloss": market_logloss,
                "model_minus_market_logloss": model_logloss - market_logloss,
                "model_brier": model_brier,
                "market_brier": market_brier,
                "model_minus_market_brier": model_brier - market_brier,
                "model_prob_bucket": prob_bucket(row.model_prob),
                "market_prob_bucket": prob_bucket(row.market_prob),
                "odds_bucket": row.odds_bucket,
                "edge_bucket": row.edge_bucket,
            }
        )
    return output


def _ou25_benchmark_detail_rows(
    predictions: list[Ou25BenchmarkPrediction],
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for row in predictions:
        output.append(
            {
                "match_date": row.match.match_date,
                "league_code": row.match.league_code,
                "season": row.match.season,
                "source_event_id": row.match.source_event_id,
                "home_team": row.match.home_team,
                "away_team": row.match.away_team,
                "market": "OU2.5",
                "label_over": row.label,
                "model_raw_prob": row.model_raw_prob,
                "model_calibrated_prob": (
                    "" if row.model_calibrated_prob is None else row.model_calibrated_prob
                ),
                "market_preclose_prob": row.market_preclose_prob,
                "market_close_prob": (
                    "" if row.market_close_prob is None else row.market_close_prob
                ),
                "entry_over_odds": row.entry_over_odds,
                "entry_under_odds": row.entry_under_odds,
                "closing_over_odds": (
                    "" if row.closing_over_odds is None else row.closing_over_odds
                ),
                "closing_under_odds": (
                    "" if row.closing_under_odds is None else row.closing_under_odds
                ),
                "market_preclose_overround": row.market_preclose_overround,
                "market_close_overround": (
                    "" if row.market_close_overround is None else row.market_close_overround
                ),
                "raw_logloss": binary_logloss(row.model_raw_prob, row.label),
                "market_preclose_logloss": binary_logloss(
                    row.market_preclose_prob,
                    row.label,
                ),
                "raw_minus_preclose_logloss": binary_logloss(
                    row.model_raw_prob,
                    row.label,
                )
                - binary_logloss(row.market_preclose_prob, row.label),
            }
        )
    return output


def _ou25_benchmark_metric_sets(
    predictions: list[Ou25BenchmarkPrediction],
    *,
    bootstrap_samples: int,
) -> list[Ou25MetricSet]:
    metric_sets: list[Ou25MetricSet] = []
    for model_name, model_getter in [
        ("dixon_coles_raw", lambda row: row.model_raw_prob),
        ("dixon_coles_platt_prequential", lambda row: row.model_calibrated_prob),
    ]:
        for reference, market_getter in [
            ("market_preclose_fair", lambda row: row.market_preclose_prob),
            ("market_close_fair", lambda row: row.market_close_prob),
        ]:
            rows = [
                row
                for row in predictions
                if model_getter(row) is not None and market_getter(row) is not None
            ]
            if not rows:
                continue
            metric_sets.append(
                _ou25_metric_set(
                    rows,
                    model_name=model_name,
                    market_reference=reference,
                    model_getter=lambda row, getter=model_getter: float(getter(row)),
                    market_getter=lambda row, getter=market_getter: float(getter(row)),
                    bootstrap_samples=bootstrap_samples,
                )
            )
    return metric_sets


def _ou25_metric_set(
    rows: list[Ou25BenchmarkPrediction],
    *,
    model_name: str,
    market_reference: str,
    model_getter: Callable[[Ou25BenchmarkPrediction], float],
    market_getter: Callable[[Ou25BenchmarkPrediction], float],
    bootstrap_samples: int,
) -> Ou25MetricSet:
    model_loglosses = [binary_logloss(model_getter(row), row.label) for row in rows]
    market_loglosses = [binary_logloss(market_getter(row), row.label) for row in rows]
    model_briers = [binary_brier(model_getter(row), row.label) for row in rows]
    market_briers = [binary_brier(market_getter(row), row.label) for row in rows]
    gaps = [
        model_logloss - market_logloss
        for model_logloss, market_logloss in zip(
            model_loglosses,
            market_loglosses,
            strict=True,
        )
    ]
    interval = _ou25_gap_bootstrap(
        rows,
        gaps=gaps,
        bootstrap_samples=bootstrap_samples,
    )
    delta = _mean(gaps)
    return Ou25MetricSet(
        model_name=model_name,
        market_reference=market_reference,
        num_matches=len(rows),
        num_selection_rows=len(rows) * 2,
        model_logloss=_mean(model_loglosses),
        market_logloss=_mean(market_loglosses),
        model_brier=_mean(model_briers),
        market_brier=_mean(market_briers),
        model_ece=_ou25_binary_ece(rows, model_getter),
        market_ece=_ou25_binary_ece(rows, market_getter),
        delta_logloss=delta,
        delta_brier=_mean(
            [
                model_brier - market_brier
                for model_brier, market_brier in zip(
                    model_briers,
                    market_briers,
                    strict=True,
                )
            ]
        ),
        ci_low=interval["lower"],
        ci_high=interval["upper"],
        bootstrap_blocks=int(interval["n_blocks"]),
        bootstrap_iterations=int(interval["iterations"]),
        benchmark_state=_benchmark_state(
            delta,
            "" if interval["lower"] is None else interval["lower"],
            "" if interval["upper"] is None else interval["upper"],
            len(rows),
        ),
    )


def _ou25_metric_row(metric: Ou25MetricSet) -> dict[str, object]:
    return {
        "group_type": "overall",
        "group": f"{metric.model_name}_vs_{metric.market_reference}",
        "market": "OU2.5",
        "model": metric.model_name,
        "market_reference": metric.market_reference,
        "scoring_unit": "match_binary",
        "num_matches": metric.num_matches,
        "num_selection_rows": metric.num_selection_rows,
        "benchmark_state": metric.benchmark_state,
        "model_logloss": metric.model_logloss,
        "market_logloss": metric.market_logloss,
        "model_minus_market_logloss": metric.delta_logloss,
        "model_minus_market_logloss_ci_low": (
            "" if metric.ci_low is None else metric.ci_low
        ),
        "model_minus_market_logloss_ci_high": (
            "" if metric.ci_high is None else metric.ci_high
        ),
        "model_minus_market_logloss_bootstrap_blocks": metric.bootstrap_blocks,
        "model_minus_market_logloss_bootstrap_iterations": metric.bootstrap_iterations,
        "model_brier": metric.model_brier,
        "market_brier": metric.market_brier,
        "model_minus_market_brier": metric.delta_brier,
        "model_ece": metric.model_ece,
        "market_ece": metric.market_ece,
    }


def _market_benchmark_summary_rows(
    predictions: list[RealOddsPrediction],
) -> list[dict[str, object]]:
    units = _market_benchmark_units(predictions)
    groupers: list[tuple[str, Callable[[BenchmarkUnit], str]]] = [
        ("overall", lambda row: "all"),
        ("selection", lambda row: row.outcome),
        ("model_prob_bucket", lambda row: row.prob_bucket),
        ("market_prob_bucket", lambda row: row.market_prob_bucket),
        ("selection_model_prob_bucket", lambda row: f"{row.outcome}:{row.prob_bucket}"),
        ("odds_bucket", lambda row: row.odds_bucket),
    ]
    output: list[dict[str, object]] = []
    for group_type, key_fn in groupers:
        grouped: dict[str, list[BenchmarkUnit]] = {}
        for row in units:
            grouped.setdefault(key_fn(row), []).append(row)
        for group, rows in sorted(grouped.items()):
            output.append(_market_benchmark_metric_row(group_type, group, rows))
    return output


def _market_benchmark_units(predictions: list[RealOddsPrediction]) -> list[BenchmarkUnit]:
    grouped: dict[tuple[str, str], list[RealOddsPrediction]] = {}
    for row in predictions:
        grouped.setdefault((row.match.source_event_id, row.market), []).append(row)

    units: list[BenchmarkUnit] = []
    for (_event_id, market), rows in sorted(grouped.items()):
        if market == "1X2":
            unit = _one_x_two_benchmark_unit(rows)
            if unit is not None:
                units.append(unit)
            continue
        units.extend(_binary_benchmark_units(rows))
    return units


def _one_x_two_benchmark_unit(rows: list[RealOddsPrediction]) -> BenchmarkUnit | None:
    by_selection = {row.selection: row for row in rows}
    if any(selection not in by_selection for selection in OUTCOME_LABELS):
        return None
    sample = rows[0]
    label = OUTCOME_LABELS.index(sample.match.result_1x2)
    model_probs = tuple(
        normalize_probabilities(
            [by_selection[selection].model_prob for selection in OUTCOME_LABELS]
        )
    )
    market_probs = tuple(
        normalize_probabilities(
            [by_selection[selection].market_prob for selection in OUTCOME_LABELS]
        )
    )
    outcome = sample.match.result_1x2
    outcome_row = by_selection[outcome]
    return BenchmarkUnit(
        match=sample.match,
        market="1X2",
        scoring_unit="match_multiclass",
        outcome=outcome,
        label=label,
        model_probs=model_probs,
        market_probs=market_probs,
        model_prob=model_probs[label],
        market_prob=market_probs[label],
        entry_odds=outcome_row.entry_odds,
        market_overround=outcome_row.market_overround,
        model_logloss=multiclass_logloss(model_probs, label),
        market_logloss=multiclass_logloss(market_probs, label),
        model_brier=multiclass_brier(model_probs, label),
        market_brier=multiclass_brier(market_probs, label),
        model_top_selection=OUTCOME_LABELS[_argmax(model_probs)],
        market_top_selection=OUTCOME_LABELS[_argmax(market_probs)],
        num_selection_rows=len(rows),
    )


def _binary_benchmark_units(rows: list[RealOddsPrediction]) -> list[BenchmarkUnit]:
    by_selection = {row.selection: row for row in rows}
    positive_selection = (
        "over" if "over" in by_selection else "yes" if "yes" in by_selection else None
    )
    if positive_selection is None:
        return [_single_binary_benchmark_unit(row, num_selection_rows=1) for row in rows]
    positive = by_selection[positive_selection]
    return [
        _single_binary_benchmark_unit(
            positive,
            num_selection_rows=len(rows),
            negative_selection="under" if positive_selection == "over" else "no",
        )
    ]


def _single_binary_benchmark_unit(
    row: RealOddsPrediction,
    *,
    num_selection_rows: int,
    negative_selection: str | None = None,
) -> BenchmarkUnit:
    label = int(row.won)
    model_prob = row.model_prob
    market_prob = row.market_prob
    model_top = row.selection if model_prob >= 0.5 else negative_selection or f"not_{row.selection}"
    market_top = (
        row.selection if market_prob >= 0.5 else negative_selection or f"not_{row.selection}"
    )
    outcome = row.selection if label else negative_selection or f"not_{row.selection}"
    return BenchmarkUnit(
        match=row.match,
        market=row.market,
        scoring_unit="match_binary",
        outcome=outcome,
        label=label,
        model_probs=(model_prob,),
        market_probs=(market_prob,),
        model_prob=model_prob,
        market_prob=market_prob,
        entry_odds=row.entry_odds,
        market_overround=row.market_overround,
        model_logloss=_binary_logloss(model_prob, label),
        market_logloss=_binary_logloss(market_prob, label),
        model_brier=(model_prob - label) ** 2,
        market_brier=(market_prob - label) ** 2,
        model_top_selection=model_top,
        market_top_selection=market_top,
        num_selection_rows=num_selection_rows,
    )


def _market_benchmark_metric_row(
    group_type: str,
    group: str,
    rows: list[BenchmarkUnit],
) -> dict[str, object]:
    model_logloss = _mean([row.model_logloss for row in rows])
    market_logloss = _mean([row.market_logloss for row in rows])
    model_brier = _mean([row.model_brier for row in rows])
    market_brier = _mean([row.market_brier for row in rows])
    hit_rate = _mean([int(row.model_top_selection == row.outcome) for row in rows])
    avg_model_prob = _mean([row.model_prob for row in rows])
    avg_market_prob = _mean([row.market_prob for row in rows])
    logloss_gap_bootstrap = _logloss_gap_bootstrap(rows)
    benchmark_state = _benchmark_state(
        model_logloss - market_logloss,
        logloss_gap_bootstrap["lower"],
        logloss_gap_bootstrap["upper"],
        len(rows),
    )
    return {
        "group_type": group_type,
        "group": group,
        "scoring_unit": _scoring_unit_label(rows),
        "num_rows": len(rows),
        "num_matches": len({row.match.source_event_id for row in rows}),
        "num_selection_rows": sum(row.num_selection_rows for row in rows),
        "sample_size_tier": sample_size_tier(len(rows)),
        "decision_gate": decision_gate(len(rows)),
        "benchmark_state": benchmark_state,
        "hit_rate": hit_rate,
        "avg_model_prob": avg_model_prob,
        "avg_market_prob": avg_market_prob,
        "avg_entry_odds": _mean([row.entry_odds for row in rows]),
        "avg_market_overround": _mean([row.market_overround for row in rows]),
        "model_logloss": model_logloss,
        "market_logloss": market_logloss,
        "model_minus_market_logloss": model_logloss - market_logloss,
        "model_minus_market_logloss_ci_low": logloss_gap_bootstrap["lower"],
        "model_minus_market_logloss_ci_high": logloss_gap_bootstrap["upper"],
        "model_minus_market_logloss_bootstrap_blocks": logloss_gap_bootstrap["n_blocks"],
        "model_minus_market_logloss_bootstrap_iterations": logloss_gap_bootstrap[
            "iterations"
        ],
        "model_brier": model_brier,
        "market_brier": market_brier,
        "model_minus_market_brier": model_brier - market_brier,
        "model_calibration_error": hit_rate - avg_model_prob,
        "market_calibration_error": hit_rate - avg_market_prob,
        "model_abs_calibration_error": abs(hit_rate - avg_model_prob),
        "market_abs_calibration_error": abs(hit_rate - avg_market_prob),
        "model_ece": _benchmark_ece(rows, source="model"),
        "market_ece": _benchmark_ece(rows, source="market"),
        "model_better_logloss": int(model_logloss < market_logloss),
        "model_better_brier": int(model_brier < market_brier),
    }


def _logloss_gap_bootstrap(rows: list[BenchmarkUnit]) -> dict[str, object]:
    by_block: dict[str, list[float]] = {}
    for row in rows:
        key = club_week_block(row.match.match_date, row.match.league_code)
        by_block.setdefault(key, []).append(row.model_logloss - row.market_logloss)
    if len(by_block) < 2:
        return {"lower": "", "upper": "", "n_blocks": len(by_block), "iterations": 0}
    interval = block_bootstrap_mean(by_block, iterations=2000, seed=2026)
    return {
        "lower": interval.lower,
        "upper": interval.upper,
        "n_blocks": interval.n_blocks,
        "iterations": interval.iterations,
    }


def _benchmark_state(
    gap: float,
    ci_low: object,
    ci_high: object,
    num_matches: int,
) -> str:
    if num_matches < 300 or ci_low == "" or ci_high == "":
        return "INCONCLUSIVE"
    if float(ci_high) < 0.0:
        return "PROMISING"
    if float(ci_low) > 0.0:
        return "REJECTED_FOR_INDEPENDENT_VALUE"
    if gap > 0.0 and num_matches >= 1000:
        return "REJECTED_FOR_INDEPENDENT_VALUE"
    return "INCONCLUSIVE"


def _ou25_overall_summary(metric_sets: list[Ou25MetricSet]) -> dict[str, object]:
    by_key = {
        (metric.model_name, metric.market_reference): metric
        for metric in metric_sets
    }
    calibrated_preclose = by_key.get(
        ("dixon_coles_platt_prequential", "market_preclose_fair")
    )
    calibrated_close = by_key.get(("dixon_coles_platt_prequential", "market_close_fair"))
    raw_preclose = by_key.get(("dixon_coles_raw", "market_preclose_fair"))
    raw_close = by_key.get(("dixon_coles_raw", "market_close_fair"))
    if calibrated_preclose is None:
        raise ValueError("OU2.5 benchmark has no calibrated preclose comparison")
    states = [calibrated_preclose.benchmark_state]
    if calibrated_close is not None:
        states.append(calibrated_close.benchmark_state)
    benchmark_state = _combined_benchmark_state(states)
    return {
        "benchmark_state": benchmark_state,
        "num_matches": calibrated_preclose.num_matches,
        "num_selection_rows": calibrated_preclose.num_selection_rows,
        "model_raw_logloss": raw_preclose.model_logloss if raw_preclose else "",
        "model_calibrated_logloss": calibrated_preclose.model_logloss,
        "market_preclose_logloss": calibrated_preclose.market_logloss,
        "market_close_logloss": calibrated_close.market_logloss if calibrated_close else "",
        "model_raw_brier": raw_preclose.model_brier if raw_preclose else "",
        "model_calibrated_brier": calibrated_preclose.model_brier,
        "market_preclose_brier": calibrated_preclose.market_brier,
        "market_close_brier": calibrated_close.market_brier if calibrated_close else "",
        "delta_raw_vs_preclose": raw_preclose.delta_logloss if raw_preclose else "",
        "delta_raw_vs_close": raw_close.delta_logloss if raw_close else "",
        "delta_calibrated_vs_preclose": calibrated_preclose.delta_logloss,
        "delta_calibrated_vs_close": calibrated_close.delta_logloss if calibrated_close else "",
        "ci_delta_calibrated_vs_preclose": _metric_ci(calibrated_preclose),
        "ci_delta_calibrated_vs_close": (
            _metric_ci(calibrated_close) if calibrated_close else None
        ),
        "model_calibrated_ece": calibrated_preclose.model_ece,
        "market_preclose_ece": calibrated_preclose.market_ece,
        "market_close_ece": calibrated_close.market_ece if calibrated_close else "",
    }


def _combined_benchmark_state(states: list[str]) -> str:
    if "REJECTED_FOR_INDEPENDENT_VALUE" in states:
        return "REJECTED_FOR_INDEPENDENT_VALUE"
    if states and all(state == "PROMISING" for state in states):
        return "PROMISING"
    return "INCONCLUSIVE"


def _metric_ci(metric: Ou25MetricSet) -> tuple[float, float] | None:
    if metric.ci_low is None or metric.ci_high is None:
        return None
    return (metric.ci_low, metric.ci_high)


def _ou25_gap_bootstrap(
    rows: list[Ou25BenchmarkPrediction],
    *,
    gaps: list[float],
    bootstrap_samples: int,
) -> dict[str, float | int | None]:
    by_block: dict[str, list[float]] = {}
    for row, gap in zip(rows, gaps, strict=True):
        by_block.setdefault(
            club_week_block(row.match.match_date, row.match.league_code),
            [],
        ).append(gap)
    if len(by_block) < 2:
        return {"lower": None, "upper": None, "n_blocks": len(by_block), "iterations": 0}
    interval = block_bootstrap_mean(by_block, iterations=bootstrap_samples, seed=2026)
    return {
        "lower": interval.lower,
        "upper": interval.upper,
        "n_blocks": interval.n_blocks,
        "iterations": interval.iterations,
    }


def _ou25_binary_ece(
    rows: list[Ou25BenchmarkPrediction],
    probability_getter: Callable[[Ou25BenchmarkPrediction], float],
    *,
    n_bins: int = 10,
) -> float:
    total = len(rows)
    if total == 0:
        return 0.0
    ece = 0.0
    for index in range(n_bins):
        lower = index / n_bins
        upper = (index + 1) / n_bins
        selected = [
            row
            for row in rows
            if probability_getter(row) >= lower
            and (probability_getter(row) < upper or index == n_bins - 1)
        ]
        if not selected:
            continue
        avg_prob = _mean([probability_getter(row) for row in selected])
        hit_rate = _mean([row.label for row in selected])
        ece += (len(selected) / total) * abs(avg_prob - hit_rate)
    return ece


def _benchmark_ece(rows: list[BenchmarkUnit], *, source: str) -> float:
    if not rows:
        return 0.0
    if all(row.scoring_unit == "match_multiclass" for row in rows):
        return multiclass_expected_calibration_error(
            [
                (row.model_probs if source == "model" else row.market_probs, row.label)
                for row in rows
            ]
        )
    probability_getter = (
        (lambda row: row.model_prob) if source == "model" else (lambda row: row.market_prob)
    )
    return _binary_unit_ece(rows, probability_getter)


def _binary_unit_ece(
    rows: list[BenchmarkUnit],
    probability_getter: Callable[[BenchmarkUnit], float],
    *,
    n_bins: int = 10,
) -> float:
    total = len(rows)
    ece = 0.0
    for index in range(n_bins):
        lower = index / n_bins
        upper = (index + 1) / n_bins
        selected = [
            row
            for row in rows
            if probability_getter(row) >= lower
            and (probability_getter(row) < upper or index == n_bins - 1)
        ]
        if not selected:
            continue
        ece += (len(selected) / total) * abs(
            _mean([probability_getter(row) for row in selected])
            - _mean([row.label for row in selected])
        )
    return ece


def _scoring_unit_label(rows: list[BenchmarkUnit]) -> str:
    labels = sorted({row.scoring_unit for row in rows})
    return labels[0] if len(labels) == 1 else "mixed"


def _argmax(values: tuple[float, ...]) -> int:
    return max(range(len(values)), key=lambda index: values[index])


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
    return _expected_calibration_error_for_probs(rows, lambda row: row.model_prob, n_bins=n_bins)


def _expected_calibration_error_for_probs(
    rows: list[HistoricalPrediction] | list[RealOddsPrediction],
    prob_getter: Callable[[HistoricalPrediction | RealOddsPrediction], float],
    *,
    n_bins: int = 10,
) -> float:
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
            if prob_getter(row) >= lower
            and (prob_getter(row) < upper or index == n_bins - 1)
        ]
        if not selected:
            continue
        ece += (len(selected) / total) * abs(
            _mean([prob_getter(row) for row in selected]) - _mean([row.won for row in selected])
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
    return binary_logloss(prob, label)


def _devig_two_way(over_odds: float, under_odds: float) -> dict[str, float]:
    if over_odds <= 1.0 or under_odds <= 1.0:
        raise ValueError("Two-way odds must be greater than 1")
    over_inverse = 1.0 / over_odds
    under_inverse = 1.0 / under_odds
    total = over_inverse + under_inverse
    return {"over": over_inverse / total, "under": under_inverse / total}


def _mean(values: Iterable[float | int]) -> float:
    rows = [float(value) for value in values]
    return sum(rows) / len(rows) if rows else 0.0


def _optional_float(value: object) -> float | None:
    if value in ("", None):
        return None
    return float(value)


def _format_optional_metric(value: float | None) -> str:
    return "na" if value is None else f"{value:.6f}"


def _format_ci(value: tuple[float, float] | None) -> str:
    if value is None:
        return "na"
    return f"[{value[0]:.6f},{value[1]:.6f}]"


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


def _record_market_benchmark_quality(
    conn: sqlite3.Connection,
    run_id: int,
    metadata: dict[str, object],
    summary_rows: list[dict[str, object]],
) -> None:
    overall = next(
        (
            row
            for row in summary_rows
            if row["group_type"] == "overall" and row["group"] == "all"
        ),
        None,
    )
    details = {
        **metadata,
        "selection_rows": [
            row for row in summary_rows if row["group_type"] == "selection"
        ],
        "warning": (
            "This benchmark compares model probabilities to historical fair market "
            "probabilities. It does not prove future betting edge by itself."
        ),
    }
    for metric_name, value in [
        ("prediction_rows", metadata["prediction_rows"]),
        ("summary_rows", len(summary_rows)),
        (
            "overall_model_logloss",
            float(overall["model_logloss"]) if overall else 0.0,
        ),
        (
            "overall_market_logloss",
            float(overall["market_logloss"]) if overall else 0.0,
        ),
        (
            "overall_model_minus_market_logloss",
            float(overall["model_minus_market_logloss"]) if overall else 0.0,
        ),
        ("overall_model_ece", float(overall["model_ece"]) if overall else 0.0),
        ("overall_market_ece", float(overall["market_ece"]) if overall else 0.0),
    ]:
        record_quality(
            conn,
            run_id,
            "market_benchmark",
            metric_name,
            float(value),
            json.dumps(details, sort_keys=True),
        )


def _record_ou25_market_benchmark_quality(
    conn: sqlite3.Connection,
    run_id: int,
    metadata: dict[str, object],
    metric_sets: list[Ou25MetricSet],
) -> None:
    details = {
        **metadata,
        "metric_sets": [_ou25_metric_row(metric) for metric in metric_sets],
        "warning": (
            "OU2.5 club benchmark is an architecture sanity check. It does not "
            "validate the World Cup forecast product or create paper candidates."
        ),
    }
    overall = metadata["overall"]
    for metric_name, value in [
        ("benchmark_matches", overall["num_matches"]),
        ("benchmark_selection_rows", overall["num_selection_rows"]),
        ("benchmark_state", 1.0),
        ("delta_calibrated_vs_preclose", overall["delta_calibrated_vs_preclose"]),
        (
            "delta_calibrated_vs_close",
            0.0
            if overall["delta_calibrated_vs_close"] == ""
            else overall["delta_calibrated_vs_close"],
        ),
        ("metric_sets", len(metric_sets)),
    ]:
        record_quality(
            conn,
            run_id,
            "market_benchmark_ou25",
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
