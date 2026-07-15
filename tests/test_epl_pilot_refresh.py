import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from scorerecord.ingest.epl_pilot_refresh import run_epl_pilot_refresh


@pytest.fixture
def mem_db():
    conn = sqlite3.connect(":memory:")
    # Initialize basic schema needed for tests
    conn.execute(
        """
        CREATE TABLE fixture (
            fixture_id INTEGER PRIMARY KEY,
            league_code TEXT,
            source_event_id TEXT,
            home_id INTEGER,
            away_id INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE prediction (
            prediction_id INTEGER PRIMARY KEY,
            fixture_id INTEGER,
            model TEXT,
            market TEXT,
            selection TEXT,
            prob REAL,
            fair_odds REAL
        )
        """
    )
    # create match_history
    conn.execute(
        """
        CREATE TABLE match_history (
            match_id INTEGER PRIMARY KEY,
            tournament TEXT,
            home_id INTEGER,
            away_id INTEGER
        )
        """
    )
    yield conn
    conn.close()


@patch("scorerecord.ingest.epl_pilot_refresh._health_status", return_value="OK")
@patch("scorerecord.ingest.epl_pilot_refresh.ingest_fixtures_odds_api_io")
@patch("scorerecord.ingest.epl_pilot_refresh.rebuild_elo")
@patch("scorerecord.ingest.epl_pilot_refresh.publish_fixture_predictions")
def test_dry_run_skips_writes(
    mock_publish, mock_rebuild, mock_ingest, mock_health, mem_db
):
    mock_ingest.return_value = {
        "status": "OK",
        "api_requests_used": 1,
        "unresolved_fixtures": 0
    }
    
    result = run_epl_pilot_refresh(
        conn=mem_db,
        api_key="fake-key",
        base_url="http://fake",
        dry_run=True,
    )
    
    assert result["status"] == "OK"
    assert result["build_elo_status"] == "SKIPPED_DRY_RUN"
    assert result["would_publish_predictions"] is True
    
    mock_rebuild.assert_not_called()
    mock_publish.assert_not_called()


@patch("scorerecord.ingest.epl_pilot_refresh._health_status", return_value="OK")
@patch("scorerecord.ingest.epl_pilot_refresh.ingest_fixtures_odds_api_io")
@patch("scorerecord.ingest.epl_pilot_refresh.publish_fixture_predictions")
def test_max_requests_enforced(
    mock_publish, mock_ingest, mock_health, mem_db
):
    mock_ingest.return_value = {
        "status": "OK",
        "unresolved_fixtures": 0
    }
    mock_publish.return_value = MagicMock(predicted_fixtures=10, inserted_predictions=20)
    
    run_epl_pilot_refresh(
        conn=mem_db,
        api_key="fake-key",
        base_url="http://fake",
        max_requests=1,
    )
    
    mock_ingest.assert_called_once()
    assert mock_ingest.call_args.kwargs["max_requests"] == 1


@patch("scorerecord.ingest.epl_pilot_refresh._health_status", return_value="OK")
@patch("scorerecord.ingest.epl_pilot_refresh.ingest_fixtures_odds_api_io")
@patch("scorerecord.ingest.epl_pilot_refresh.publish_fixture_predictions")
def test_api_error_halts(mock_publish, mock_ingest, mock_health, mem_db):
    mock_ingest.return_value = {
        "status": "AUTH_FAILED",
        "unresolved_fixtures": 0
    }
    
    result = run_epl_pilot_refresh(
        conn=mem_db,
        api_key="fake-key",
        base_url="http://fake",
    )
    
    assert result["status"] == "INGEST_FAILED"
    mock_publish.assert_not_called()


@patch("scorerecord.ingest.epl_pilot_refresh._health_status", return_value="OK")
@patch("scorerecord.ingest.epl_pilot_refresh.ingest_fixtures_odds_api_io")
@patch("scorerecord.ingest.epl_pilot_refresh.publish_fixture_predictions")
def test_unresolved_fixtures_halts(mock_publish, mock_ingest, mock_health, mem_db):
    mock_ingest.return_value = {
        "status": "OK",
        "unresolved_fixtures": 5
    }
    
    result = run_epl_pilot_refresh(
        conn=mem_db,
        api_key="fake-key",
        base_url="http://fake",
    )
    
    assert result["status"] == "UNRESOLVED_FIXTURES"
    mock_publish.assert_not_called()


@patch("scorerecord.ingest.epl_pilot_refresh._health_status", return_value="OK")
@patch("scorerecord.ingest.epl_pilot_refresh.ingest_fixtures_odds_api_io")
@patch("scorerecord.ingest.epl_pilot_refresh.publish_fixture_predictions")
def test_duplicate_source_event_ids_halts(mock_publish, mock_ingest, mock_health, mem_db):
    mock_ingest.return_value = {
        "status": "OK",
        "unresolved_fixtures": 0
    }
    
    mem_db.execute("INSERT INTO fixture (league_code, source_event_id) VALUES ('EPL', 'event1')")
    mem_db.execute("INSERT INTO fixture (league_code, source_event_id) VALUES ('EPL', 'event1')")
    
    result = run_epl_pilot_refresh(
        conn=mem_db,
        api_key="fake-key",
        base_url="http://fake",
    )
    
    assert result["status"] == "DUPLICATE_SOURCE_EVENT_IDS"
    mock_publish.assert_not_called()


@patch("scorerecord.ingest.epl_pilot_refresh._health_status", return_value="OK")
@patch("scorerecord.ingest.epl_pilot_refresh.ingest_fixtures_odds_api_io")
@patch("scorerecord.ingest.epl_pilot_refresh.rebuild_elo")
@patch("scorerecord.ingest.epl_pilot_refresh.publish_fixture_predictions")
def test_epl_scoping(
    mock_publish, mock_rebuild, mock_ingest, mock_health, mem_db
):
    mock_ingest.return_value = {
        "status": "OK",
        "unresolved_fixtures": 0
    }
    mock_publish.return_value = MagicMock(predicted_fixtures=10, inserted_predictions=20)
    
    run_epl_pilot_refresh(
        conn=mem_db,
        api_key="fake-key",
        base_url="http://fake",
        league_code="EPL",
    )
    
    mock_publish.assert_called_once()
    assert mock_publish.call_args.kwargs.get("league") == "EPL"
