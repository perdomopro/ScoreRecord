import sqlite3

from scorerecord.db import init_db
from scorerecord.evaluate.edge import compute_edge, compute_edges, devig_1x2, devig_binary
from scorerecord.models.manifest import MODEL_VERSION


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


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
    conn.execute(
        "INSERT INTO team (team_id, canonical, canonical_key) VALUES (1, 'Home', 'home')"
    )
    conn.execute(
        "INSERT INTO team (team_id, canonical, canonical_key) VALUES (2, 'Away', 'away')"
    )
    conn.execute(
        """
        INSERT INTO fixture (fixture_id, kickoff_local, home_id, away_id)
        VALUES ('m1', '2026-06-11 13:00 UTC-6', 1, 2)
        """
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
            VALUES (NULL, 'm1', 'dixon_coles', '1X2', ?, ?, ?,
                    '2026-06-10T00:00:00+00:00', ?)
            """,
            (selection, prob, 1.0 / prob, MODEL_VERSION),
        )
    for selection, odds in [("home", 2.00), ("draw", 3.50), ("away", 4.00)]:
        conn.execute(
            """
            INSERT INTO odds (
              fixture_id, market, selection, decimal_odds, bookmaker, captured_utc
            )
            VALUES ('m1', '1X2', ?, ?, 'Bet365', '2026-06-10T01:00:00+00:00')
            """,
            (selection, odds),
        )

    summary = compute_edges(conn)

    assert summary.rows_analyzed == 3
    assert conn.execute("SELECT COUNT(*) FROM edge_analysis").fetchone()[0] == 3
    row = conn.execute(
        "SELECT elo_diff, close_match FROM edge_analysis WHERE selection = 'home'"
    ).fetchone()
    assert round(float(row["elo_diff"]), 8) == 60.0
    assert int(row["close_match"]) == 1
