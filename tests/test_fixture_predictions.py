import sqlite3

from scorerecord.db import init_db
from scorerecord.predict.fixtures import _fixture_as_match_feature, _latest_elo


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
        INSERT INTO fixture (fixture_id, kickoff_local, home_id, away_id)
        VALUES ('f1', '2026-06-27 17:00 UTC-4', 1, 2)
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
        INSERT INTO fixture (fixture_id, kickoff_local, home_id, away_id)
        VALUES ('f1', '2026-06-27 17:00 UTC-4', 1, 2)
        """
    )
    fixture = conn.execute("SELECT * FROM fixture WHERE fixture_id = 'f1'").fetchone()

    match = _fixture_as_match_feature(conn, fixture)

    assert match.elo_home_pre == 1500.0
    assert match.elo_away_pre == 1500.0
