import json
import sqlite3
import urllib.error
from unittest import mock

from scorerecord.db import _run_lightweight_migrations, init_db
from scorerecord.ingest.fixtures_odds_api import ingest_fixtures_odds_api

def test_ingest_fixtures_odds_api_dry_run():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    
    conn.execute("INSERT INTO team (team_id, canonical) VALUES (1, 'Manchester United')")
    conn.execute("INSERT INTO team_alias (alias, alias_key, team_id, source) VALUES ('manchester united', 'manchesterunited', 1, 'seed')")
    conn.commit()
    
    mock_events = [
        {
            "id": "event_123",
            "commence_time": "2026-08-16T19:00:00Z",
            "home_team": "Manchester United",
            "away_team": "Unknown Team"
        }
    ]
    
    summary = ingest_fixtures_odds_api(
        conn=conn,
        api_key="test",
        sport_key="soccer_epl",
        league_code="EPL",
        competition="English Premier League",
        season="2026-2027",
        raw_dir=None,
        max_requests=1,
        dry_run=True,
        mock_events=mock_events
    )
    
    assert summary["events_seen"] == 1
    assert summary["teams_mapped"] == 1
    assert summary["teams_unmapped"] == 1
    assert "Unknown Team" in summary["unmapped_names"]
    
    # Dry run should not insert
    row = conn.execute("SELECT COUNT(*) FROM fixture WHERE source_event_id = 'event_123'").fetchone()
    assert row[0] == 0

def test_ingest_fixtures_odds_api_insert():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    
    mock_events = [
        {
            "id": "event_123",
            "commence_time": "2026-08-16T19:00:00Z",
            "home_team": "Manchester United",
            "away_team": "Fulham"
        }
    ]
    
    summary = ingest_fixtures_odds_api(
        conn=conn,
        api_key="test",
        sport_key="soccer_epl",
        league_code="EPL",
        competition="English Premier League",
        season="2026-2027",
        raw_dir=None,
        max_requests=1,
        dry_run=False,
        mock_events=mock_events
    )
    
    assert summary["events_seen"] == 1
    assert summary["fixtures_inserted"] == 1
    assert summary["teams_mapped"] == 0
    assert summary["teams_unmapped"] == 2
    
    row = conn.execute("SELECT home_placeholder, away_placeholder, status FROM fixture WHERE source_event_id = 'event_123'").fetchone()
    assert row["home_placeholder"] == "Manchester United"
    assert row["away_placeholder"] == "Fulham"
    assert row["status"] == "scheduled"

def test_ingest_max_requests_zero():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    summary = ingest_fixtures_odds_api(
        conn=conn, api_key="test", sport_key="soccer_epl", league_code="EPL", competition="EPL", season="2026", raw_dir=None, max_requests=0
    )
    assert summary["status"] == "MAX_REQUESTS_ZERO"
    assert summary["api_requests_used"] == 0

def test_ingest_auth_missing():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    summary = ingest_fixtures_odds_api(
        conn=conn, api_key=None, sport_key="soccer_epl", league_code="EPL", competition="EPL", season="2026", raw_dir=None, max_requests=1
    )
    assert summary["status"] == "AUTH_MISSING"
    assert summary["api_requests_used"] == 0

@mock.patch("urllib.request.urlopen")
def test_ingest_auth_failed_401(mock_urlopen):
    mock_urlopen.side_effect = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    summary = ingest_fixtures_odds_api(
        conn=conn, api_key="bad_key", sport_key="soccer_epl", league_code="EPL", competition="EPL", season="2026", raw_dir=None, max_requests=1
    )
    assert summary["status"] == "AUTH_FAILED"
    assert summary["api_requests_used"] == 1

@mock.patch("urllib.request.urlopen")
def test_ingest_rate_limited_429(mock_urlopen):
    mock_urlopen.side_effect = urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    summary = ingest_fixtures_odds_api(
        conn=conn, api_key="key", sport_key="soccer_epl", league_code="EPL", competition="EPL", season="2026", raw_dir=None, max_requests=1
    )
    assert summary["status"] == "RATE_LIMITED"
    assert summary["api_requests_used"] == 1

@mock.patch("urllib.request.urlopen")
def test_ingest_invalid_json(mock_urlopen):
    mock_response = mock.MagicMock()
    mock_response.read.return_value = b"invalid json"
    mock_response.headers = {}
    mock_urlopen.return_value.__enter__.return_value = mock_response
    
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    summary = ingest_fixtures_odds_api(
        conn=conn, api_key="key", sport_key="soccer_epl", league_code="EPL", competition="EPL", season="2026", raw_dir=None, max_requests=1
    )
    assert summary["status"] == "INVALID_RESPONSE"
    assert summary["api_requests_used"] == 1

def test_idempotent_insert():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    mock_events = [{"id": "ev1", "commence_time": "2026-08-16T19:00:00Z", "home_team": "H", "away_team": "A"}]
    
    # First insert
    s1 = ingest_fixtures_odds_api(
        conn, "test", "soccer_epl", "EPL", "English Premier League", "2026-2027", None, mock_events=mock_events
    )
    assert s1["fixtures_inserted"] == 1
    assert s1["fixtures_updated"] == 0
    assert s1["status"] == "OK"
    
    # Second update
    s2 = ingest_fixtures_odds_api(
        conn, "test", "soccer_epl", "EPL", "English Premier League", "2026-2027", None, mock_events=mock_events
    )
    assert s2["fixtures_inserted"] == 0
    assert s2["fixtures_updated"] == 1

    row = conn.execute("SELECT competition FROM fixture WHERE source_event_id = 'ev1'").fetchone()
    assert row["competition"] == "English Premier League"
