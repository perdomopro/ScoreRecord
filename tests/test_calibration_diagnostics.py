import csv
import math
import sqlite3
from datetime import date

from scorerecord.db import init_db
from scorerecord.evaluate.backtest import HistoricalOddsMatch, RealOddsPrediction
from scorerecord.evaluate.calibration_diagnostics import (
    _overconfidence_rows,
    _segment_rows,
    run_calibration_diagnostics,
)


def _real_prediction(
    *,
    model_prob: float = 0.80,
    market_prob: float = 0.50,
    won: int = 0,
) -> RealOddsPrediction:
    return RealOddsPrediction(
        match=HistoricalOddsMatch(
            source="football-data",
            source_event_id="e1",
            league_code="E0",
            season="2023-2024",
            match_date="2024-01-01",
            home_team="A",
            away_team="B",
            home_goals=0,
            away_goals=1,
            result_1x2="away",
            home_id=1,
            away_id=2,
        ),
        market="1X2",
        selection="home",
        bookmaker="Avg",
        entry_odds=2.0,
        entry_odds_type="avg_open",
        closing_odds=1.90,
        closing_odds_type="avg_close",
        model_prob=model_prob,
        market_prob=market_prob,
        market_overround=1.05,
        edge_ev=model_prob * 2.0 - 1.0,
        edge_prob=model_prob - market_prob,
        recommendation_score=model_prob * 2.0 - 1.0,
        high_edge_reason="",
        odds_quality_flags="",
        won=won,
        is_real_historical_odds=True,
        is_timestamp_exact=False,
    )


def test_segment_diagnostics_surface_overconfidence_and_market_gap() -> None:
    rows = [_real_prediction() for _ in range(120)]

    segments = _segment_rows(rows, [("overall", lambda row: "all")])
    overconfidence = _overconfidence_rows(segments, min_segment_rows=100)

    assert segments[0]["model_minus_market_logloss"] > 0
    assert math.isclose(segments[0]["overconfidence"], 0.80, rel_tol=1e-5)
    assert overconfidence[0]["severity"] == "critical"
    assert overconfidence[0]["model_worse_than_market"] == 1


def test_run_calibration_diagnostics_writes_segment_csvs(tmp_path) -> None:
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

    summary = run_calibration_diagnostics(
        conn,
        from_date=date(2024, 1, 3),
        to_date=date(2024, 1, 3),
        leagues=("EPL",),
        out_dir=tmp_path,
        start_year=2024,
        min_train=2,
        min_segment_rows=1,
    )

    assert summary.prediction_rows == 3
    assert (tmp_path / f"calibration_by_league_{summary.run_id}.csv").exists()
    assert (tmp_path / f"model_vs_market_by_bucket_{summary.run_id}.csv").exists()
    overconfidence_path = tmp_path / f"overconfidence_report_{summary.run_id}.csv"
    with overconfidence_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows
    quality = conn.execute(
        """
        SELECT COUNT(*)
        FROM data_quality
        WHERE check_name = 'calibration_diagnostics'
        """
    ).fetchone()[0]
    assert quality > 0
