import sqlite3

from scorerecord.db import init_db
from scorerecord.evaluate.edge import compute_edge, compute_edges, devig_1x2, devig_binary
from scorerecord.models.manifest import MODEL_VERSION

FUTURE_KICKOFF_LOCAL = "2099-06-11 13:00 UTC-6"
FUTURE_GENERATED_UTC = "2099-06-10T00:00:00+00:00"
FUTURE_CAPTURED_UTC = "2099-06-10T01:00:00+00:00"
STALE_CAPTURED_UTC = "2026-06-10T01:00:00+00:00"


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


def test_devig_helpers_normalize_implied_probabilities() -> None:
    assert round(sum(devig_1x2(2.10, 3.20, 3.60)), 8) == 1.0
    over, under = devig_binary(1.91, 1.91)
    assert round(over, 8) == 0.5
    assert round(under, 8) == 0.5


def test_compute_edge_returns_ev_and_probability_gap() -> None:
    edge_ev, edge_prob = compute_edge(0.57, 0.50, 2.00)
    assert round(edge_ev, 8) == 0.14
    assert round(edge_prob, 8) == 0.07


def test_compute_edges_persists_complete_market_rows() -> None:
    conn = _conn()
    prediction_run_id = _run(conn, "predict-fixtures")
    conn.execute(
        "INSERT INTO team (team_id, canonical, canonical_key) VALUES (1, 'Home', 'home')"
    )
    conn.execute(
        "INSERT INTO team (team_id, canonical, canonical_key) VALUES (2, 'Away', 'away')"
    )
    conn.execute(
        """
        INSERT INTO fixture (fixture_id, kickoff_local, home_id, away_id)
        VALUES ('m1', ?, 1, 2)
        """,
        (FUTURE_KICKOFF_LOCAL,),
    )
    conn.execute(
        """
        INSERT INTO team_rating (team_id, rating_system, rating, as_of)
        VALUES (1, 'elo', 1600.0, '2026-01-01')
        """
    )
    conn.execute(
        """
        INSERT INTO team_rating (team_id, rating_system, rating, as_of)
        VALUES (2, 'elo', 1540.0, '2026-01-01')
        """
    )
    for selection, prob in [("home", 0.60), ("draw", 0.25), ("away", 0.15)]:
        conn.execute(
            """
            INSERT INTO prediction (
              run_id, fixture_id, model, market, selection, prob,
              fair_odds, generated_utc, model_version
            )
            VALUES (?, 'm1', 'dixon_coles', '1X2', ?, ?, ?, ?, ?)
            """,
            (prediction_run_id, selection, prob, 1.0 / prob, FUTURE_GENERATED_UTC, MODEL_VERSION),
        )
    for selection, odds in [("home", 2.40), ("draw", 3.50), ("away", 4.00)]:
        conn.execute(
            """
            INSERT INTO odds (
              fixture_id, market, selection, decimal_odds, bookmaker, captured_utc
            )
            VALUES ('m1', '1X2', ?, ?, 'Bet365', ?)
            """,
            (selection, odds, STALE_CAPTURED_UTC),
        )

    summary = compute_edges(conn)

    assert summary.rows_analyzed == 3
    assert conn.execute("SELECT COUNT(*) FROM edge_analysis").fetchone()[0] == 3
    row = conn.execute(
        """
        SELECT elo_diff, close_match, raw_implied_prob, market_overround,
               odds_books, odds_quality_flags, high_edge_reason,
               recommendation_score
        FROM edge_analysis
        WHERE selection = 'home'
        """
    ).fetchone()
    assert round(float(row["elo_diff"]), 8) == 60.0
    assert int(row["close_match"]) == 1
    assert round(float(row["raw_implied_prob"]), 8) == round(1 / 2.4, 8)
    assert float(row["market_overround"]) > 0
    assert int(row["odds_books"]) == 1
    assert "low_market_depth" in row["odds_quality_flags"]
    assert "stale_odds" in row["odds_quality_flags"]
    assert "model_market_disagreement" in row["high_edge_reason"]
    assert float(row["recommendation_score"]) > 0
    alert = conn.execute(
        """
        SELECT metric_value, details_json
        FROM data_quality
        WHERE check_name = 'edge_sanity'
          AND metric_name = 'high_edge_alert'
        """
    ).fetchone()
    assert alert["metric_value"] == 1.0
    assert "severity" in alert["details_json"]


def test_compute_edges_groups_bookmaker_variants_for_market_depth() -> None:
    conn = _conn()
    prediction_run_id = _run(conn, "predict-fixtures")
    conn.execute(
        "INSERT INTO fixture (fixture_id, kickoff_local) VALUES ('m2', ?)",
        (FUTURE_KICKOFF_LOCAL,),
    )
    for selection, prob in [("home", 0.50), ("draw", 0.30), ("away", 0.20)]:
        conn.execute(
            """
            INSERT INTO prediction (
              run_id, fixture_id, model, market, selection, prob,
              fair_odds, generated_utc, model_version
            )
            VALUES (?, 'm2', 'dixon_coles', '1X2', ?, ?, ?, ?, ?)
            """,
            (prediction_run_id, selection, prob, 1.0 / prob, FUTURE_GENERATED_UTC, MODEL_VERSION),
        )
    for bookmaker, home_odds in [
        ("Bet365", 2.00),
        ("Bet365 (no latency)", 2.02),
        ("Unibet", 2.05),
    ]:
        for selection, odds in [("home", home_odds), ("draw", 3.50), ("away", 4.00)]:
            conn.execute(
                """
                INSERT INTO odds (
                  fixture_id, market, selection, decimal_odds, bookmaker, captured_utc
                )
                VALUES ('m2', '1X2', ?, ?, ?, ?)
                """,
                (selection, odds, bookmaker, FUTURE_CAPTURED_UTC),
            )

    compute_edges(conn)

    row = conn.execute(
        """
        SELECT odds_books
        FROM edge_analysis
        WHERE fixture_id = 'm2'
          AND selection = 'home'
          AND bookmaker = 'Unibet'
        """
    ).fetchone()
    assert int(row["odds_books"]) == 2
