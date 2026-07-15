import sqlite3

from scorerecord.db import init_db
from scorerecord.predict.fixtures import (
    _fixture_as_match_feature,
    _latest_elo,
    _record_prediction_sanity,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def test_fixture_feature_uses_latest_team_rating_elo() -> None:
    conn = _conn()
    conn.execute("INSERT INTO team (team_id, canonical, canonical_key) VALUES (1, 'A', 'a')")
    conn.execute("INSERT INTO team (team_id, canonical, canonical_key) VALUES (2, 'B', 'b')")
    conn.execute(
        """
        INSERT INTO fixture (fixture_id, kickoff_local, home_id, away_id, league_code, competition)
        VALUES ('f1', '2026-06-27 17:00 UTC-4', 1, 2, 'WORLD_CUP_2026', 'FIFA World Cup')
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
        VALUES (2, 'elo', 1450.0, '2026-01-01')
        """
    )
    fixture = conn.execute("SELECT * FROM fixture WHERE fixture_id = 'f1'").fetchone()

    match = _fixture_as_match_feature(conn, fixture)

    assert _latest_elo(conn, 1) == 1600.0
    assert match.elo_home_pre == 1600.0
    assert match.elo_away_pre == 1450.0
    assert match.elo_diff == 150.0
    assert match.neutral == 1


def test_fixture_feature_falls_back_to_1500_when_elo_missing() -> None:
    conn = _conn()
    conn.execute("INSERT INTO team (team_id, canonical, canonical_key) VALUES (1, 'A', 'a')")
    conn.execute("INSERT INTO team (team_id, canonical, canonical_key) VALUES (2, 'B', 'b')")
    conn.execute(
        """
        INSERT INTO fixture (fixture_id, kickoff_local, home_id, away_id, league_code, competition)
        VALUES ('f1', '2026-06-27 17:00 UTC-4', 1, 2, 'WORLD_CUP_2026', 'FIFA World Cup')
        """
    )
    fixture = conn.execute("SELECT * FROM fixture WHERE fixture_id = 'f1'").fetchone()

    match = _fixture_as_match_feature(conn, fixture)

    assert match.elo_home_pre == 1500.0
    assert match.elo_away_pre == 1500.0


def test_fixture_feature_epl_neutrality() -> None:
    conn = _conn()
    conn.execute("INSERT INTO team (team_id, canonical, canonical_key) VALUES (1, 'A', 'a')")
    conn.execute("INSERT INTO team (team_id, canonical, canonical_key) VALUES (2, 'B', 'b')")
    conn.execute(
        """
        INSERT INTO fixture (fixture_id, kickoff_local, home_id, away_id, league_code, competition)
        VALUES ('f1', '2026-08-15 15:00 UTC+1', 1, 2, 'EPL', 'English Premier League')
        """
    )
    fixture = conn.execute("SELECT * FROM fixture WHERE fixture_id = 'f1'").fetchone()

    match = _fixture_as_match_feature(conn, fixture)

    assert match.neutral == 0
    assert match.tournament == 'EPL'


def test_prediction_sanity_records_ou_monotonicity_violations() -> None:
    conn = _conn()
    conn.execute("INSERT INTO team (team_id, canonical, canonical_key) VALUES (1, 'A', 'a')")
    conn.execute("INSERT INTO team (team_id, canonical, canonical_key) VALUES (2, 'B', 'b')")
    conn.execute(
        """
        INSERT INTO fixture (fixture_id, kickoff_utc, home_id, away_id)
        VALUES ('f1', '2026-07-04T20:00:00+00:00', 1, 2)
        """
    )
    run_id = conn.execute(
        "INSERT INTO ingest_run (job_name, status) VALUES ('predict-fixtures', 'succeeded')"
    ).lastrowid
    for market, prob in [("OU1.5", 0.61), ("OU2.5", 0.70), ("OU3.5", 0.30)]:
        conn.execute(
            """
            INSERT INTO prediction (
              run_id, fixture_id, model, market, selection, prob,
              fair_odds, generated_utc, model_version
            )
            VALUES (?, 'f1', 'dixon_coles', ?, 'over', ?, ?, '2026-07-04T12:00:00+00:00', 'v')
            """,
            (run_id, market, prob, 1 / prob),
        )

    _record_prediction_sanity(conn, int(run_id))

    row = conn.execute(
        """
        SELECT metric_value
        FROM data_quality
        WHERE run_id = ?
          AND check_name = 'prediction_sanity'
          AND metric_name = 'ou_monotonicity_violations'
        """,
        (run_id,),
    ).fetchone()
    assert row["metric_value"] == 1.0
