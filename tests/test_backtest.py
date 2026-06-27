import json
import sqlite3
from datetime import date

from scorerecord.db import init_db
from scorerecord.evaluate.backtest import (
    BacktestBet,
    HistoricalPrediction,
    _calibration_rows,
    _select_backtest_bets,
    edge_bucket,
    max_drawdown,
    odds_bucket,
    prob_bucket,
    run_historical_backtest,
)
from scorerecord.features.build import MatchFeature


def _match(match_id: str = "m1") -> MatchFeature:
    return MatchFeature(
        source_match_id=match_id,
        match_date="2022-06-01",
        home_id=1,
        away_id=2,
        home_score=1,
        away_score=0,
        neutral=1,
        tournament="FIFA World Cup",
        elo_home_pre=1600.0,
        elo_away_pre=1550.0,
    )


def _prediction(
    *,
    fixture_id: str = "m1",
    selection: str = "home",
    model_prob: float = 0.55,
    market_prob: float = 0.50,
    edge_ev: float = 0.10,
    score: float = 0.10,
    won: int = 1,
    reason: str = "",
) -> HistoricalPrediction:
    return HistoricalPrediction(
        match=_match(fixture_id),
        market="1X2",
        selection=selection,
        model_prob=model_prob,
        market_prob=market_prob,
        edge_ev=edge_ev,
        edge_prob=model_prob - market_prob,
        recommendation_score=score,
        high_edge_reason=reason,
        odds_quality_flags="",
        won=won,
    )


def test_backtest_buckets_and_drawdown() -> None:
    assert edge_bucket(0.03) == "2.5-5.0%"
    assert odds_bucket(3.4) == "3.00-5.00"
    assert prob_bucket(0.62) == "60-75%"
    assert max_drawdown([1.0, -0.5, -1.0, 0.25]) == -1.5


def test_backtest_selection_compares_edge_and_score_rankings() -> None:
    rows = [
        _prediction(selection="home", edge_ev=0.12, score=0.06),
        _prediction(selection="away", edge_ev=0.08, score=0.08),
    ]

    bets = _select_backtest_bets(
        rows,
        min_edge=0.05,
        max_edge=0.25,
        min_prob=0.15,
        max_picks_per_fixture=1,
    )

    by_strategy = {bet.ranking_strategy: bet.prediction.selection for bet in bets}
    assert by_strategy == {"edge_ev": "home", "recommendation_score": "away"}


def test_backtest_selection_excludes_critical_reason() -> None:
    rows = [
        _prediction(reason="model_market_disagreement,long_odds_tail"),
        _prediction(fixture_id="m2", selection="away", edge_ev=0.08, score=0.08),
    ]

    bets = _select_backtest_bets(
        rows,
        min_edge=0.05,
        max_edge=0.25,
        min_prob=0.15,
        max_picks_per_fixture=1,
    )

    assert all(bet.prediction.match.source_match_id == "m2" for bet in bets)


def test_calibration_rows_flag_large_supported_errors() -> None:
    rows = [
        _prediction(model_prob=0.40, market_prob=0.35, won=0)
        for _ in range(60)
    ]

    calibration = _calibration_rows(rows)

    assert len(calibration) == 1
    assert calibration[0]["problem"] == 1


def test_real_odds_backtest_writes_real_profit_and_clv_outputs(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    for event_id, match_date, home, away, hg, ag, result in [
        ("football-data:E0:2023-2024:2024-01-01:a:b", "2024-01-01", "A", "B", 1, 0, "home"),
        ("football-data:E0:2023-2024:2024-01-02:c:d", "2024-01-02", "C", "D", 0, 1, "away"),
        ("football-data:E0:2023-2024:2024-01-03:a:c", "2024-01-03", "A", "C", 2, 0, "home"),
    ]:
        conn.execute(
            """
            INSERT INTO historical_match (
              source, source_event_id, league_code, season, match_date,
              home_team, away_team, home_goals, away_goals, result_1x2
            )
            VALUES ('football-data', ?, 'E0', '2023-2024', ?, ?, ?, ?, ?, ?)
            """,
            (event_id, match_date, home, away, hg, ag, result),
        )
    for selection, entry, closing in [
        ("home", 2.10, 2.00),
        ("draw", 3.40, 3.20),
        ("away", 3.80, 4.00),
    ]:
        conn.execute(
            """
            INSERT INTO historical_odds (
              source, source_event_id, bookmaker, market, selection,
              entry_odds, entry_odds_type, closing_odds, closing_odds_type,
              is_real_historical_odds, is_timestamp_exact
            )
            VALUES (
              'football-data', 'football-data:E0:2023-2024:2024-01-03:a:c',
              'Avg', '1X2', ?, ?, 'avg_open', ?, 'avg_close', 1, 0
            )
            """,
            (selection, entry, closing),
        )

    summary = run_historical_backtest(
        conn,
        from_date=date(2024, 1, 3),
        to_date=date(2024, 1, 3),
        out_dir=tmp_path,
        start_year=2024,
        min_train=2,
        min_edge=-10.0,
        max_edge=0.25,
        min_prob=0.0,
        odds_mode="historical",
        league="EPL",
        season="2023-2024",
    )

    metadata = json.loads(
        (tmp_path / f"backtest_metadata_real_odds_{summary.run_id}.json").read_text(
            encoding="utf-8"
        )
    )
    recommendations = (
        tmp_path / f"backtest_recommendations_real_odds_{summary.run_id}.csv"
    ).read_text(encoding="utf-8")
    clv = (tmp_path / f"clv_summary_real_odds_{summary.run_id}.csv").read_text(
        encoding="utf-8"
    )

    assert metadata["odds_mode"] == "historical"
    assert metadata["is_real_historical_odds"] is True
    assert "profit_flat_real" in recommendations
    assert "clv_real" in recommendations
    assert "mean_clv_real" in clv
