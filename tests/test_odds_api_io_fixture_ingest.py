import sqlite3
import urllib.error

import pytest

from scorerecord.ingest.fixtures_odds_api_io import ingest_fixtures_odds_api_io


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE fixture (fixture_id TEXT PRIMARY KEY, source TEXT, source_event_id TEXT, kickoff_utc TEXT, home_id INTEGER, away_id INTEGER, home_placeholder TEXT, away_placeholder TEXT, league_code TEXT, competition TEXT, season TEXT, status TEXT)")
    c.execute("CREATE TABLE team_alias (team_id INTEGER, alias_key TEXT)")
    yield c
    c.close()

def test_auth_missing(conn):
    res = ingest_fixtures_odds_api_io(conn, None, "http://test", "football", "EPL", "england-premier-league", "EPL", "2026", None)
    assert res["status"] == "AUTH_MISSING"

def test_max_requests_zero(conn):
    res = ingest_fixtures_odds_api_io(conn, "key", "http://test", "football", "EPL", "england-premier-league", "EPL", "2026", None, max_requests=0)
    assert res["status"] == "MAX_REQUESTS_ZERO"

def test_max_requests_too_low(conn):
    res = ingest_fixtures_odds_api_io(conn, "key", "http://test", "football", "EPL", "england-premier-league", "EPL", "2026", None, max_requests=1, verify_league_slug=True)
    assert res["status"] == "MAX_REQUESTS_TOO_LOW"

def test_input_json_zero_network(conn, tmp_path, monkeypatch):
    p = tmp_path / "mock.json"
    p.write_text('[{"id": "1", "home": "A", "away": "B", "date": "2026-07-14T00:00:00Z"}]')
    def mock_urlopen(*args, **kwargs):
        raise RuntimeError("Should not hit network")
    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
    
    res = ingest_fixtures_odds_api_io(conn, "key", "http://test", "football", "EPL", "england-premier-league", "EPL", "2026", None, input_json=p)
    assert res["status"] == "OK"
    assert res["api_requests_used"] == 0
    assert res["fixtures_inserted"] == 1
    
def test_dry_run_zero_db_writes(conn, tmp_path):
    p = tmp_path / "mock.json"
    p.write_text('[{"id": "1", "home": "A", "away": "B", "date": "2026-07-14T00:00:00Z"}]')
    res = ingest_fixtures_odds_api_io(conn, "key", "http://test", "football", "EPL", "england-premier-league", "EPL", "2026", None, input_json=p, dry_run=True)
    assert res["status"] == "OK"
    assert res["fixtures_inserted"] == 0
    c = conn.execute("SELECT COUNT(*) FROM fixture").fetchone()[0]
    assert c == 0

def test_http_401_auth_failed(conn, monkeypatch):
    class MockResp:
        def __init__(self):
            self.code = 401
            self.msg = "Unauthorized"
            self.hdrs = {}
            self.fp = None
            
    def mock_urlopen(*args, **kwargs):
        raise urllib.error.HTTPError("http://test", 401, "Unauthorized", {}, None)
    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
    
    res = ingest_fixtures_odds_api_io(conn, "key", "http://test", "football", "EPL", "england-premier-league", "EPL", "2026", None)
    assert res["status"] == "AUTH_FAILED"

def test_http_429_rate_limited(conn, monkeypatch):
    def mock_urlopen(*args, **kwargs):
        raise urllib.error.HTTPError("http://test", 429, "Too Many Requests", {}, None)
    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
    
    res = ingest_fixtures_odds_api_io(conn, "key", "http://test", "football", "EPL", "england-premier-league", "EPL", "2026", None)
    assert res["status"] == "RATE_LIMITED"

def test_invalid_json(conn, monkeypatch):
    class MockResp:
        def __init__(self, data):
            self.data = data
        def read(self):
            return self.data
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    def mock_urlopen(*args, **kwargs):
        return MockResp(b"bad json")
    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
    
    res = ingest_fixtures_odds_api_io(conn, "key", "http://test", "football", "EPL", "england-premier-league", "EPL", "2026", None)
    assert res["status"] == "INVALID_RESPONSE"

def test_event_payload_flat_fields(conn, tmp_path):
    p = tmp_path / "mock.json"
    p.write_text('[{"id": "ev1", "home": "Team A", "away": "Team B", "date": "2026-07-14T00:00:00Z"}]')
    res = ingest_fixtures_odds_api_io(conn, "key", "http://test", "football", "EPL", "england-premier-league", "EPL", "2026", None, input_json=p)
    assert res["status"] == "OK"
    assert res["fixtures_inserted"] == 1
    row = conn.execute("SELECT * FROM fixture WHERE source_event_id='ev1'").fetchone()
    assert row[6] == "Team A" # home_placeholder
    assert row[7] == "Team B" # away_placeholder

def test_event_payload_camel_case(conn, tmp_path):
    p = tmp_path / "mock.json"
    p.write_text('[{"eventId": "ev2", "homeTeam": "Team C", "awayTeam": "Team D", "startTime": "2026-07-14T00:00:00Z"}]')
    res = ingest_fixtures_odds_api_io(conn, "key", "http://test", "football", "EPL", "england-premier-league", "EPL", "2026", None, input_json=p)
    assert res["status"] == "OK"
    assert res["fixtures_inserted"] == 1
    row = conn.execute("SELECT * FROM fixture WHERE source_event_id='ev2'").fetchone()
    assert row[6] == "Team C"

def test_event_payload_nested_teams(conn, tmp_path):
    p = tmp_path / "mock.json"
    p.write_text('[{"id": "ev3", "teams": {"home": {"name": "Team E"}, "away": {"name": "Team F"}}, "kickoff": "2026-07-14T00:00:00Z"}]')
    res = ingest_fixtures_odds_api_io(conn, "key", "http://test", "football", "EPL", "england-premier-league", "EPL", "2026", None, input_json=p)
    assert res["status"] == "OK"
    assert res["fixtures_inserted"] == 1
    row = conn.execute("SELECT * FROM fixture WHERE source_event_id='ev3'").fetchone()
    assert row[6] == "Team E"

def test_ambiguous_participants_skipped(conn, tmp_path):
    p = tmp_path / "mock.json"
    p.write_text('[{"id": "ev4", "participants": [{"name": "A"}, {"name": "B"}], "date": "2026-07-14T00:00:00Z"}]')
    res = ingest_fixtures_odds_api_io(conn, "key", "http://test", "football", "EPL", "england-premier-league", "EPL", "2026", None, input_json=p)
    assert res["status"] == "INVALID_RESPONSE"
    assert res["events_valid"] == 0

def test_unmapped_teams_reported(conn, tmp_path):
    p = tmp_path / "mock.json"
    p.write_text('[{"id": "1", "home": "A", "away": "B", "date": "2026-07-14T00:00:00Z"}]')
    res = ingest_fixtures_odds_api_io(conn, "key", "http://test", "football", "EPL", "england-premier-league", "EPL", "2026", None, input_json=p)
    assert res["teams_mapped"] == 0
    assert res["teams_unmapped"] == 2
    assert set(res["unmapped_names"]) == {"A", "B"}

def test_idempotent_upsert(conn, tmp_path):
    p = tmp_path / "mock.json"
    p.write_text('[{"id": "1", "home": "A", "away": "B", "date": "2026-07-14T00:00:00Z"}]')
    ingest_fixtures_odds_api_io(conn, "key", "http://test", "football", "EPL", "england-premier-league", "EPL", "2026", None, input_json=p)
    
    p2 = tmp_path / "mock2.json"
    p2.write_text('[{"id": "1", "home": "A_mod", "away": "B_mod", "date": "2026-07-15T00:00:00Z"}]')
    res = ingest_fixtures_odds_api_io(conn, "key", "http://test", "football", "EPL", "england-premier-league", "EPL", "2026", None, input_json=p2)
    assert res["fixtures_inserted"] == 0
    assert res["fixtures_updated"] == 1
    
    row = conn.execute("SELECT home_placeholder, kickoff_utc FROM fixture WHERE source_event_id='1'").fetchone()
    assert row[0] == "A_mod"
    assert row[1] == "2026-07-15T00:00:00Z"

def test_league_slug_not_found(conn, monkeypatch):
    class MockResp:
        def __init__(self, data):
            self.data = data
        def read(self):
            return self.data
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    def mock_urlopen(req, *args, **kwargs):
        if "leagues" in req.full_url:
            return MockResp(b'[{"slug": "other-league"}]')
        raise RuntimeError("Should not reach events")
    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
    
    res = ingest_fixtures_odds_api_io(conn, "key", "http://test", "football", "EPL", "england-premier-league", "EPL", "2026", None, max_requests=2, verify_league_slug=True)
    assert res["status"] == "LEAGUE_SLUG_NOT_FOUND"
