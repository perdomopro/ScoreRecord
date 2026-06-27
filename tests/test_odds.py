import sqlite3
from datetime import datetime, timedelta, timezone

from scorerecord.db import init_db
from scorerecord.ingest.odds import _candidate_fixtures, parse_odds_payload


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def test_parse_odds_api_io_documented_shape() -> None:
    payload = {
        "id": 123456,
        "home": "Argentina",
        "away": "Austria",
        "date": "2026-06-11T19:00:00Z",
        "bookmakers": {
            "Bet365": [
                {
                    "name": "ML",
                    "odds": [{"home": "2.10", "draw": "3.40", "away": "3.20"}],
                    "updatedAt": "2026-06-10T10:30:00Z",
                },
                {
                    "name": "Over/Under",
                    "odds": [{"max": 2.5, "over": "1.90", "under": "1.90"}],
                    "updatedAt": "2026-06-10T10:31:00Z",
                },
                {
                    "name": "Both Teams to Score",
                    "odds": [{"yes": "1.83", "no": "1.95"}],
                    "updatedAt": "2026-06-10T10:32:00Z",
                },
            ],
        },
    }

    rows = parse_odds_payload(
        payload,
        fixture_id="m1",
        captured_utc="2026-06-10T10:00:00+00:00",
        is_closing=False,
        requested_markets={"1X2", "OU", "BTTS"},
    )

    keyed = {(row.market, row.selection): row.decimal_odds for row in rows}
    assert keyed[("1X2", "home")] == 2.10
    assert keyed[("1X2", "draw")] == 3.40
    assert keyed[("1X2", "away")] == 3.20
    assert keyed[("OU2.5", "over")] == 1.90
    assert keyed[("OU2.5", "under")] == 1.90
    assert keyed[("BTTS", "yes")] == 1.83
    assert keyed[("BTTS", "no")] == 1.95


def test_closing_candidate_fixtures_limit_to_upcoming_window() -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO team (team_id, canonical, canonical_key) VALUES (1, 'Home', 'home')"
    )
    conn.execute(
        "INSERT INTO team (team_id, canonical, canonical_key) VALUES (2, 'Away', 'away')"
    )
    near = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    far = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    conn.execute(
        """
        INSERT INTO fixture (fixture_id, kickoff_utc, home_id, away_id)
        VALUES ('near', ?, 1, 2)
        """,
        (near,),
    )
    conn.execute(
        """
        INSERT INTO fixture (fixture_id, kickoff_utc, home_id, away_id)
        VALUES ('far', ?, 1, 2)
        """,
        (far,),
    )

    rows = _candidate_fixtures(conn, fixture_id=None, closing_window_minutes=60)

    assert [row["fixture_id"] for row in rows] == ["near"]
