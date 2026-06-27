import sqlite3

from scorerecord.db import init_db
from scorerecord.evaluate.recommendations import (
    compute_clv,
    compute_recommendation_clv,
    generate_recommendations,
    kelly_stake,
)
from scorerecord.models.manifest import MODEL_VERSION


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _run(conn: sqlite3.Connection, job_name: str) -> int:
    cursor = conn.execute(
        "INSERT INTO ingest_run (job_name, status) VALUES (?, 'succeeded')",
        (job_name,),
    )
    return int(cursor.lastrowid)


def test_kelly_stake_is_fractional_and_capped() -> None:
    assert round(kelly_stake(0.57, 2.0, fraction=0.25, max_stake=1.0), 8) == 0.035
    assert kelly_stake(0.90, 10.0, fraction=1.0, max_stake=0.5) == 0.5


def test_generate_recommendations_uses_latest_edge_run() -> None:
    conn = _conn()
    edge_run_id = _run(conn, "compute-edge")
    conn.execute(
        "INSERT INTO fixture (fixture_id, kickoff_local) VALUES ('m1', '2026-06-11 13:00 UTC-6')"
    )
    conn.execute(
        """
        INSERT INTO edge_analysis (
          fixture_id, run_id, model_version, market, selection, prob_model,
          odds_decimal, prob_market_devig, edge_ev, edge_prob, bookmaker,
          captured_utc, generated_utc
        )
        VALUES ('m1', ?, ?, 'OU2.5', 'over', 0.57, 2.00, 0.50,
                0.14, 0.07, 'Bet365', '2026-06-10T01:00:00+00:00',
                '2026-06-10T02:00:00+00:00')
        """,
        (edge_run_id, MODEL_VERSION),
    )

    summary = generate_recommendations(conn)

    assert summary.recommendations == 1
    row = conn.execute(
        "SELECT edge, stake_units, confidence, edge_run_id, recommendation_score FROM recommendation"
    ).fetchone()
    assert round(row["edge"], 8) == 0.14
    assert round(row["stake_units"], 8) == 0.035
    assert row["confidence"] == "high"
    assert row["edge_run_id"] == edge_run_id
    assert round(row["recommendation_score"], 8) == 0.14


def test_generate_recommendations_excludes_implausibly_high_edges_by_default() -> None:
    conn = _conn()
    edge_run_id = _run(conn, "compute-edge")
    conn.execute(
        "INSERT INTO fixture (fixture_id, kickoff_local) VALUES ('m3', '2026-06-11 13:00 UTC-6')"
    )
    conn.execute(
        """
        INSERT INTO edge_analysis (
          fixture_id, run_id, model_version, market, selection, prob_model,
          odds_decimal, prob_market_devig, edge_ev, edge_prob, bookmaker,
          captured_utc, generated_utc
        )
        VALUES ('m3', ?, ?, '1X2', 'home', 0.35, 4.00, 0.25,
                0.40, 0.10, 'Bet365', '2026-06-10T01:00:00+00:00',
                '2026-06-10T02:00:00+00:00')
        """,
        (edge_run_id, MODEL_VERSION),
    )

    summary = generate_recommendations(conn)

    assert summary.candidates_seen == 0
    assert summary.recommendations == 0


def test_generate_recommendations_excludes_flagged_odds_quality() -> None:
    conn = _conn()
    edge_run_id = _run(conn, "compute-edge")
    conn.execute(
        "INSERT INTO fixture (fixture_id, kickoff_local) VALUES ('m4', '2026-06-11 13:00 UTC-6')"
    )
    conn.execute(
        """
        INSERT INTO edge_analysis (
          fixture_id, run_id, model_version, market, selection, prob_model,
          odds_decimal, prob_market_devig, edge_ev, edge_prob, bookmaker,
          captured_utc, generated_utc, odds_books, odds_quality_flags,
          recommendation_score
        )
        VALUES ('m4', ?, ?, 'BTTS', 'yes', 0.55, 2.00, 0.50,
                0.10, 0.05, 'Bet365', '2026-06-10T01:00:00+00:00',
                '2026-06-10T02:00:00+00:00', 1, 'low_market_depth',
                0.05)
        """,
        (edge_run_id, MODEL_VERSION),
    )

    summary = generate_recommendations(conn)

    assert summary.candidates_seen == 0
    assert summary.recommendations == 0


def test_generate_recommendations_limits_correlated_fixture_picks() -> None:
    conn = _conn()
    edge_run_id = _run(conn, "compute-edge")
    conn.execute(
        "INSERT INTO fixture (fixture_id, kickoff_local) VALUES ('m5', '2026-06-11 13:00 UTC-6')"
    )
    for market, selection, edge, score in [
        ("1X2", "home", 0.10, 0.07),
        ("BTTS", "yes", 0.08, 0.08),
    ]:
        conn.execute(
            """
            INSERT INTO edge_analysis (
              fixture_id, run_id, model_version, market, selection, prob_model,
              odds_decimal, prob_market_devig, edge_ev, edge_prob, bookmaker,
              captured_utc, generated_utc, odds_books, odds_quality_flags,
              recommendation_score
            )
            VALUES ('m5', ?, ?, ?, ?, 0.55, 2.00, 0.50,
                    ?, 0.05, 'Bet365', '2026-06-10T01:00:00+00:00',
                    '2026-06-10T02:00:00+00:00', 2, '',
                    ?)
            """,
            (edge_run_id, MODEL_VERSION, market, selection, edge, score),
        )

    summary = generate_recommendations(conn)

    assert summary.candidates_seen == 2
    assert summary.recommendations == 1
    row = conn.execute("SELECT market, selection FROM recommendation").fetchone()
    assert (row["market"], row["selection"]) == ("BTTS", "yes")


def test_compute_recommendation_clv_matches_closing_odds() -> None:
    conn = _conn()
    recommendation_run_id = _run(conn, "recommend")
    conn.execute(
        "INSERT INTO fixture (fixture_id, kickoff_local) VALUES ('m2', '2026-06-11 13:00 UTC-6')"
    )
    conn.execute(
        """
        INSERT INTO recommendation (
          run_id, fixture_id, market, selection, model_prob, market_odds,
          edge, edge_prob, kelly_fraction, stake_units, confidence, bookmaker,
          model_version, odds_captured_utc, paper, generated_utc
        )
        VALUES (?, 'm2', 'OU2.5', 'over', 0.57, 2.00, 0.14, 0.07,
                0.25, 0.035, 'high', 'Bet365', ?,
                '2026-06-10T01:00:00+00:00', 1, '2026-06-10T02:00:00+00:00')
        """,
        (recommendation_run_id, MODEL_VERSION),
    )
    conn.execute(
        """
        INSERT INTO odds (
          fixture_id, market, selection, decimal_odds, bookmaker, captured_utc, is_closing
        )
        VALUES ('m2', 'OU2.5', 'over', 1.85, 'Bet365',
                '2026-06-11T18:00:00+00:00', 1)
        """
    )

    summary = compute_recommendation_clv(conn)

    assert summary.clv_rows == 1
    assert round(summary.mean_clv, 8) == round(compute_clv(2.00, 1.85), 8)
    assert conn.execute("SELECT COUNT(*) FROM recommendation_result").fetchone()[0] == 1
