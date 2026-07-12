import sqlite3

from scorerecord.db import init_db
from scorerecord.evaluate.recommendations import (
    capped_real_stake,
    compute_clv,
    compute_recommendation_clv,
    generate_recommendations,
    kelly_stake,
)
from scorerecord.models.manifest import MODEL_VERSION

FUTURE_KICKOFF_LOCAL = "2099-06-11 13:00 UTC-6"
FUTURE_KICKOFF_UTC = "2099-06-11T18:00:00+00:00"
FUTURE_CAPTURED_UTC = "2099-06-10T01:00:00+00:00"
FUTURE_GENERATED_UTC = "2099-06-10T02:00:00+00:00"


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


def test_capped_real_stake_is_more_conservative_than_model_stake() -> None:
    assert capped_real_stake(
        model_stake=0.041,
        odds_decimal=1.91,
        market="1X2",
        selection="home",
    ) == 0.01
    assert capped_real_stake(
        model_stake=0.02,
        odds_decimal=3.50,
        market="1X2",
        selection="home",
    ) == 0.0075
    assert capped_real_stake(
        model_stake=0.02,
        odds_decimal=5.50,
        market="1X2",
        selection="draw",
    ) == 0.0025


def test_generate_recommendations_uses_latest_edge_run() -> None:
    conn = _conn()
    edge_run_id = _run(conn, "compute-edge")
    conn.execute(
        "INSERT INTO fixture (fixture_id, kickoff_local) VALUES ('m1', ?)",
        (FUTURE_KICKOFF_LOCAL,),
    )
    conn.execute(
        """
        INSERT INTO edge_analysis (
          fixture_id, run_id, model_version, market, selection, prob_model,
          odds_decimal, prob_market_devig, edge_ev, edge_prob, bookmaker,
          captured_utc, generated_utc
        )
        VALUES ('m1', ?, ?, 'OU2.5', 'over', 0.57, 2.00, 0.50,
                0.14, 0.07, 'Bet365', ?, ?)
        """,
        (edge_run_id, MODEL_VERSION, FUTURE_CAPTURED_UTC, FUTURE_GENERATED_UTC),
    )

    summary = generate_recommendations(conn)

    assert summary.recommendations == 1
    row = conn.execute(
        """
        SELECT edge, stake_units, capped_stake_units, confidence, edge_run_id,
               recommendation_score
        FROM recommendation
        """
    ).fetchone()
    assert round(row["edge"], 8) == 0.14
    assert round(row["stake_units"], 8) == 0.035
    assert round(row["capped_stake_units"], 8) == 0.01
    assert row["confidence"] == "high"
    assert row["edge_run_id"] == edge_run_id
    assert round(row["recommendation_score"], 8) == 0.14


def test_generate_recommendations_excludes_implausibly_high_edges_by_default() -> None:
    conn = _conn()
    edge_run_id = _run(conn, "compute-edge")
    conn.execute(
        "INSERT INTO fixture (fixture_id, kickoff_local) VALUES ('m3', ?)",
        (FUTURE_KICKOFF_LOCAL,),
    )
    conn.execute(
        """
        INSERT INTO edge_analysis (
          fixture_id, run_id, model_version, market, selection, prob_model,
          odds_decimal, prob_market_devig, edge_ev, edge_prob, bookmaker,
          captured_utc, generated_utc
        )
        VALUES ('m3', ?, ?, '1X2', 'home', 0.35, 4.00, 0.25,
                0.40, 0.10, 'Bet365', ?, ?)
        """,
        (edge_run_id, MODEL_VERSION, FUTURE_CAPTURED_UTC, FUTURE_GENERATED_UTC),
    )

    summary = generate_recommendations(conn)

    assert summary.candidates_seen == 0
    assert summary.recommendations == 0


def test_generate_recommendations_excludes_flagged_odds_quality() -> None:
    conn = _conn()
    edge_run_id = _run(conn, "compute-edge")
    conn.execute(
        "INSERT INTO fixture (fixture_id, kickoff_local) VALUES ('m4', ?)",
        (FUTURE_KICKOFF_LOCAL,),
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
                0.10, 0.05, 'Bet365', ?, ?, 1, 'low_market_depth', 0.05)
        """,
        (edge_run_id, MODEL_VERSION, FUTURE_CAPTURED_UTC, FUTURE_GENERATED_UTC),
    )

    summary = generate_recommendations(conn)

    assert summary.candidates_seen == 0
    assert summary.recommendations == 0


def test_generate_recommendations_excludes_critical_high_edge_reasons() -> None:
    conn = _conn()
    edge_run_id = _run(conn, "compute-edge")
    conn.execute(
        "INSERT INTO fixture (fixture_id, kickoff_local) VALUES ('m6', ?)",
        (FUTURE_KICKOFF_LOCAL,),
    )
    conn.execute(
        """
        INSERT INTO edge_analysis (
          fixture_id, run_id, model_version, market, selection, prob_model,
          odds_decimal, prob_market_devig, edge_ev, edge_prob, bookmaker,
          captured_utc, generated_utc, odds_books, odds_quality_flags,
          high_edge_reason, recommendation_score
        )
        VALUES ('m6', ?, ?, '1X2', 'draw', 0.28, 4.00, 0.20,
                0.12, 0.08, 'Bet365', ?, ?, 2, '',
                'model_market_disagreement,long_odds_tail', 0.07)
        """,
        (edge_run_id, MODEL_VERSION, FUTURE_CAPTURED_UTC, FUTURE_GENERATED_UTC),
    )

    summary = generate_recommendations(conn)

    assert summary.candidates_seen == 0
    assert summary.recommendations == 0


def test_generate_recommendations_allows_model_market_disagreement_alone() -> None:
    conn = _conn()
    edge_run_id = _run(conn, "compute-edge")
    conn.execute(
        "INSERT INTO fixture (fixture_id, kickoff_local) VALUES ('m7', ?)",
        (FUTURE_KICKOFF_LOCAL,),
    )
    conn.execute(
        """
        INSERT INTO edge_analysis (
          fixture_id, run_id, model_version, market, selection, prob_model,
          odds_decimal, prob_market_devig, edge_ev, edge_prob, bookmaker,
          captured_utc, generated_utc, odds_books, odds_quality_flags,
          high_edge_reason, recommendation_score
        )
        VALUES ('m7', ?, ?, '1X2', 'home', 0.40, 2.80, 0.32,
                0.12, 0.08, 'Bet365', ?, ?, 2, '',
                'model_market_disagreement', 0.08)
        """,
        (edge_run_id, MODEL_VERSION, FUTURE_CAPTURED_UTC, FUTURE_GENERATED_UTC),
    )

    summary = generate_recommendations(conn)

    assert summary.candidates_seen == 1
    assert summary.recommendations == 1


def test_generate_recommendations_limits_correlated_fixture_picks() -> None:
    conn = _conn()
    edge_run_id = _run(conn, "compute-edge")
    conn.execute(
        "INSERT INTO fixture (fixture_id, kickoff_local) VALUES ('m5', ?)",
        (FUTURE_KICKOFF_LOCAL,),
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
                    ?, 0.05, 'Bet365', ?, ?, 2, '', ?)
            """,
            (
                edge_run_id,
                MODEL_VERSION,
                market,
                selection,
                edge,
                FUTURE_CAPTURED_UTC,
                FUTURE_GENERATED_UTC,
                score,
            ),
        )

    summary = generate_recommendations(conn)

    assert summary.candidates_seen == 2
    assert summary.recommendations == 1
    row = conn.execute("SELECT market, selection FROM recommendation").fetchone()
    assert (row["market"], row["selection"]) == ("BTTS", "yes")


def test_generate_recommendations_excludes_low_recommendation_score() -> None:
    conn = _conn()
    edge_run_id = _run(conn, "compute-edge")
    conn.execute(
        "INSERT INTO fixture (fixture_id, kickoff_local) VALUES ('m8', ?)",
        (FUTURE_KICKOFF_LOCAL,),
    )
    conn.execute(
        """
        INSERT INTO edge_analysis (
          fixture_id, run_id, model_version, market, selection, prob_model,
          odds_decimal, prob_market_devig, edge_ev, edge_prob, bookmaker,
          captured_utc, generated_utc, odds_books, odds_quality_flags,
          recommendation_score
        )
        VALUES ('m8', ?, ?, '1X2', 'home', 0.45, 2.00, 0.10,
                0.05, 0.35, 'Bet365', ?, ?, 2, '', 0.04)
        """,
        (edge_run_id, MODEL_VERSION, FUTURE_CAPTURED_UTC, FUTURE_GENERATED_UTC),
    )

    summary = generate_recommendations(conn, min_score=0.05)

    assert summary.candidates_seen == 0
    assert summary.recommendations == 0


def test_generate_recommendations_limits_daily_volume() -> None:
    conn = _conn()
    edge_run_id = _run(conn, "compute-edge")
    for fixture_id, score in [("d1", 0.09), ("d2", 0.12)]:
        conn.execute(
            """
            INSERT INTO fixture (fixture_id, kickoff_utc)
            VALUES (?, ?)
            """,
            (fixture_id, FUTURE_KICKOFF_UTC),
        )
        conn.execute(
            """
            INSERT INTO edge_analysis (
              fixture_id, run_id, model_version, market, selection, prob_model,
              odds_decimal, prob_market_devig, edge_ev, edge_prob, bookmaker,
              captured_utc, generated_utc, odds_books, odds_quality_flags,
              recommendation_score
            )
            VALUES (?, ?, ?, '1X2', 'home', 0.45, 2.00, 0.10,
                    0.05, 0.35, 'Bet365', ?, ?, 2, '', ?)
            """,
            (
                fixture_id,
                edge_run_id,
                MODEL_VERSION,
                FUTURE_CAPTURED_UTC,
                FUTURE_GENERATED_UTC,
                score,
            ),
        )

    summary = generate_recommendations(conn, max_picks_per_day=1)

    assert summary.candidates_seen == 2
    assert summary.recommendations == 1
    row = conn.execute("SELECT fixture_id FROM recommendation").fetchone()
    assert row["fixture_id"] == "d2"


def test_compute_recommendation_clv_matches_closing_odds() -> None:
    conn = _conn()
    recommendation_run_id = _run(conn, "recommend")
    conn.execute(
        """
        INSERT INTO fixture (fixture_id, kickoff_local, kickoff_utc)
        VALUES ('m2', '2026-06-11 13:00 UTC-6', '2026-06-11T19:00:00+00:00')
        """
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
    for selection, odds in [("over", 1.85), ("under", 2.05)]:
        conn.execute(
            """
            INSERT INTO odds_snapshots (
              event_id, fixture_id, bookmaker, market, selection, line,
              decimal_odds, snapshot_at, commence_time, source
            )
            VALUES ('event-m2', 'm2', 'Bet365', 'OU2.5', ?, 2.5,
                    ?, '2026-06-11T18:00:00+00:00',
                    '2026-06-11T19:00:00Z', 'odds-api.io')
            """,
            (selection, odds),
        )

    summary = compute_recommendation_clv(conn)

    assert summary.clv_rows == 1
    assert round(summary.mean_clv, 8) == round(compute_clv(2.00, 1.85), 8)
    row = conn.execute("SELECT * FROM recommendation_result").fetchone()
    assert row["closing_snapshot_at"] == "2026-06-11T18:00:00+00:00"
    assert row["clv_log"] is not None
    assert row["clv_fair_ev"] is not None
