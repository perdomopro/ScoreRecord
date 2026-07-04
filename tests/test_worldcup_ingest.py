import sqlite3

from scorerecord.aliases import seed_team_aliases
from scorerecord.db import init_db
from scorerecord.ingest.worldcup import _is_placeholder, resolve_fixture_teams


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    seed_team_aliases(conn)
    return conn


def test_worldcup_knockout_placeholders_are_not_teams() -> None:
    for name in ["W73", "L101", "1B", "2L", "3A/B/C/D/F", "3C/3D/3F/3G/3H"]:
        assert _is_placeholder(name)


def test_worldcup_real_team_names_are_not_placeholders() -> None:
    for name in ["Brazil", "Canada", "South Africa", "Cote d'Ivoire"]:
        assert not _is_placeholder(name)


def test_resolve_fixture_teams_uses_aliases() -> None:
    conn = _conn()
    conn.execute(
        """
        INSERT INTO fixture (fixture_id, kickoff_utc, home_placeholder, away_placeholder)
        VALUES ('knockout-1', '2026-07-04T20:00:00+00:00', 'W73', 'L101')
        """
    )

    resolve_fixture_teams(
        conn,
        fixture_id="knockout-1",
        home="MEX",
        away="United States of America",
    )

    row = conn.execute(
        """
        SELECT ht.canonical AS home, at.canonical AS away
        FROM fixture f
        JOIN team ht ON ht.team_id = f.home_id
        JOIN team at ON at.team_id = f.away_id
        WHERE f.fixture_id = 'knockout-1'
        """
    ).fetchone()
    assert row["home"] == "Mexico"
    assert row["away"] == "United States"


def test_resolve_fixture_teams_rejects_unknown_team() -> None:
    conn = _conn()
    conn.execute(
        """
        INSERT INTO fixture (fixture_id, kickoff_utc, home_placeholder, away_placeholder)
        VALUES ('knockout-2', '2026-07-04T20:00:00+00:00', 'W73', 'L101')
        """
    )

    try:
        resolve_fixture_teams(
            conn,
            fixture_id="knockout-2",
            home="Definitely Not A Team",
            away="Mexico",
        )
    except ValueError as exc:
        assert "Unknown team alias" in str(exc)
    else:
        raise AssertionError("Expected unknown team alias to be rejected")
