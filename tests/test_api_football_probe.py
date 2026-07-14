import json
import sqlite3
import urllib.error
from pathlib import Path
from unittest import mock

import pytest
from scorerecord.db import init_db
from scorerecord.ingest.api_football_probe import probe_api_football_fixtures

@pytest.fixture
def mem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    # Insert a mapped team for mapping tests
    conn.execute("INSERT INTO team (team_id, canonical) VALUES (1, 'Arsenal')")
    conn.execute("INSERT INTO team_alias (alias, alias_key, team_id, source) VALUES ('arsenal', 'arsenal', 1, 'seed')")
    conn.commit()
    return conn

def test_probe_auth_missing(mem_db):
    res = probe_api_football_fixtures(
        conn=mem_db, api_key=None, base_url="http://mock", league_code="EPL",
        api_football_league_id=39, season=2026, max_requests=3
    )
    assert res["status"] == "AUTH_MISSING"
    assert res["api_requests_used"] == 0

def test_probe_max_requests_too_low(mem_db):
    res = probe_api_football_fixtures(
        conn=mem_db, api_key="key", base_url="http://mock", league_code="EPL",
        api_football_league_id=39, season=2026, max_requests=1, include_status=False
    )
    assert res["status"] == "MAX_REQUESTS_TOO_LOW"
    assert res["api_requests_used"] == 0

def test_probe_input_json_no_network(mem_db, tmp_path):
    mock_json = tmp_path / "mock.json"
    mock_json.write_text(json.dumps({
        "teams": {"response": [{"team": {"name": "Arsenal"}}]},
        "fixtures": {"response": []}
    }))
    
    res = probe_api_football_fixtures(
        conn=mem_db, api_key=None, base_url="http://mock", league_code="EPL",
        api_football_league_id=39, season=2026, max_requests=0, input_json=mock_json
    )
    assert res["status"] == "OK"
    assert res["api_requests_used"] == 0
    assert res["mapped_team_count"] == 1
    assert res["unmapped_team_count"] == 0

@mock.patch("urllib.request.urlopen")
def test_probe_auth_failed_401(mock_urlopen, mem_db):
    mock_urlopen.side_effect = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
    res = probe_api_football_fixtures(
        conn=mem_db, api_key="key", base_url="http://mock", league_code="EPL",
        api_football_league_id=39, season=2026, max_requests=3
    )
    assert res["status"] == "AUTH_FAILED"
    assert res["api_requests_used"] == 1

@mock.patch("urllib.request.urlopen")
def test_probe_rate_limited_429(mock_urlopen, mem_db):
    mock_urlopen.side_effect = urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)
    res = probe_api_football_fixtures(
        conn=mem_db, api_key="key", base_url="http://mock", league_code="EPL",
        api_football_league_id=39, season=2026, max_requests=3
    )
    assert res["status"] == "RATE_LIMITED"
    assert res["api_requests_used"] == 1

@mock.patch("urllib.request.urlopen")
def test_probe_invalid_json(mock_urlopen, mem_db):
    mock_response = mock.MagicMock()
    mock_response.read.return_value = b"invalid json"
    mock_response.headers = {}
    mock_urlopen.return_value.__enter__.return_value = mock_response
    
    res = probe_api_football_fixtures(
        conn=mem_db, api_key="key", base_url="http://mock", league_code="EPL",
        api_football_league_id=39, season=2026, max_requests=3
    )
    assert res["status"] == "INVALID_RESPONSE"
    assert res["api_requests_used"] == 1

@mock.patch("urllib.request.urlopen")
def test_probe_errors_object_checked(mock_urlopen, mem_db):
    mock_response = mock.MagicMock()
    # Payload has response but also has errors
    mock_response.read.return_value = json.dumps({"errors": {"token": "bad"}, "response": []}).encode("utf-8")
    mock_response.headers = {}
    mock_urlopen.return_value.__enter__.return_value = mock_response
    
    res = probe_api_football_fixtures(
        conn=mem_db, api_key="key", base_url="http://mock", league_code="EPL",
        api_football_league_id=39, season=2026, max_requests=3
    )
    assert res["status"] == "API_FOOTBALL_ERROR"

@mock.patch("urllib.request.urlopen")
def test_probe_successful_flow_with_quota_and_pagination(mock_urlopen, mem_db):
    # Mocking two calls (teams, fixtures)
    mock_teams = mock.MagicMock()
    mock_teams.read.return_value = json.dumps({
        "errors": [], "paging": {"total": 1},
        "response": [{"team": {"name": "Arsenal"}}, {"team": {"name": "Unknown Team"}}]
    }).encode("utf-8")
    mock_teams.headers = {"x-ratelimit-requests-remaining": "99", "x-ratelimit-limit": "100"}
    
    mock_fixtures = mock.MagicMock()
    mock_fixtures.read.return_value = json.dumps({
        "errors": [], "paging": {"total": 2}, # Triggers pagination_detected
        "response": [{"fixture": {"id": 101}, "teams": {"home": {"name": "Arsenal"}, "away": {"name": "Unknown Team 2"}}}]
    }).encode("utf-8")
    mock_fixtures.headers = {"x-ratelimit-requests-remaining": "98", "x-ratelimit-limit": "100"}
    
    # Return mock_teams then mock_fixtures
    mock_urlopen.side_effect = [
        mock.MagicMock(__enter__=mock.MagicMock(return_value=mock_teams)),
        mock.MagicMock(__enter__=mock.MagicMock(return_value=mock_fixtures))
    ]
    
    res = probe_api_football_fixtures(
        conn=mem_db, api_key="key", base_url="http://mock", league_code="EPL",
        api_football_league_id=39, season=2026, max_requests=2, include_status=False
    )
    
    assert res["status"] == "OK"
    assert res["api_requests_used"] == 2
    assert res["quota_remaining"] == 98
    assert res["quota_limit"] == 100
    assert res["teams_seen"] == 2
    assert res["fixtures_seen"] == 1
    assert res["pagination_detected"] is True
    
    assert res["mapped_team_count"] == 1
    assert res["unmapped_team_count"] == 2
    assert "Unknown Team" in res["unmapped_team_names"]
    assert "Unknown Team 2" in res["unmapped_team_names"]
    
    # Assert nothing was written to fixture table (no DB writes)
    assert mem_db.execute("SELECT COUNT(*) FROM fixture").fetchone()[0] == 0
