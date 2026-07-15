import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from scorerecord.ingest.epl_pilot_refresh import run_epl_pilot_refresh


from scorerecord.db import init_db

@pytest.fixture
def mem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    yield conn
    conn.close()


@patch("scorerecord.ingest.epl_pilot_refresh._health_status", return_value="OK")
@patch("scorerecord.ingest.epl_pilot_refresh.ingest_fixtures_odds_api_io")
@patch("scorerecord.ingest.epl_pilot_refresh.rebuild_elo")
@patch("scorerecord.ingest.epl_pilot_refresh.publish_fixture_predictions")
def test_dry_run_skips_writes(
    mock_publish, mock_rebuild, mock_ingest, mock_health, mem_db, tmp_path
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
        outputs_dir=tmp_path,
    )
    
    assert result["status"] == "OK"
    assert result["build_elo_status"] == "SKIPPED_DRY_RUN"
    assert result["would_publish_predictions"] is True
    
    artifacts = list(tmp_path.glob("benchmarks/epl_pilot_refresh_*.json"))
    assert len(artifacts) == 1
    
    mock_rebuild.assert_not_called()
    mock_publish.assert_not_called()


@patch("scorerecord.ingest.epl_pilot_refresh._health_status", return_value="OK")
@patch("scorerecord.ingest.epl_pilot_refresh.ingest_fixtures_odds_api_io")
@patch("scorerecord.ingest.epl_pilot_refresh.publish_fixture_predictions")
def test_max_requests_enforced(
    mock_publish, mock_ingest, mock_health, mem_db, tmp_path
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
        outputs_dir=tmp_path,
    )
    
    mock_ingest.assert_called_once()
    assert mock_ingest.call_args.kwargs["max_requests"] == 1
    artifacts = list(tmp_path.glob("benchmarks/epl_pilot_refresh_*.json"))
    assert len(artifacts) == 1


@patch("scorerecord.ingest.epl_pilot_refresh._health_status", return_value="OK")
@patch("scorerecord.ingest.epl_pilot_refresh.ingest_fixtures_odds_api_io")
@patch("scorerecord.ingest.epl_pilot_refresh.publish_fixture_predictions")
def test_api_error_halts(mock_publish, mock_ingest, mock_health, mem_db, tmp_path):
    mock_ingest.return_value = {
        "status": "AUTH_FAILED",
        "unresolved_fixtures": 0
    }
    
    result = run_epl_pilot_refresh(
        conn=mem_db,
        api_key="fake-key",
        base_url="http://fake",
        outputs_dir=tmp_path,
    )
    
    assert result["status"] == "INGEST_FAILED"
    mock_publish.assert_not_called()
    assert len(list(tmp_path.glob("benchmarks/epl_pilot_refresh_*.json"))) == 1


@patch("scorerecord.ingest.epl_pilot_refresh._health_status", return_value="OK")
@patch("scorerecord.ingest.epl_pilot_refresh.ingest_fixtures_odds_api_io")
@patch("scorerecord.ingest.epl_pilot_refresh.publish_fixture_predictions")
def test_unresolved_fixtures_halts(mock_publish, mock_ingest, mock_health, mem_db, tmp_path):
    mock_ingest.return_value = {
        "status": "OK",
        "unresolved_fixtures": 5
    }
    
    result = run_epl_pilot_refresh(
        conn=mem_db,
        api_key="fake-key",
        base_url="http://fake",
        outputs_dir=tmp_path,
    )
    
    assert result["status"] == "UNRESOLVED_FIXTURES"
    mock_publish.assert_not_called()
    assert len(list(tmp_path.glob("benchmarks/epl_pilot_refresh_*.json"))) == 1


@patch("scorerecord.ingest.epl_pilot_refresh._health_status", return_value="OK")
@patch("scorerecord.ingest.epl_pilot_refresh.ingest_fixtures_odds_api_io")
@patch("scorerecord.ingest.epl_pilot_refresh.publish_fixture_predictions")
def test_duplicate_source_event_ids_halts(mock_publish, mock_ingest, mock_health, mem_db, tmp_path):
    mock_ingest.return_value = {
        "status": "OK",
        "unresolved_fixtures": 0
    }
    mem_db.execute("DROP INDEX IF EXISTS idx_fixture_source_event")

    mem_db.execute(
        "INSERT INTO fixture (league_code, source, source_event_id) "
        "VALUES ('EPL', 'odds_api_io', 'event1')"
    )
    mem_db.execute(
        "INSERT INTO fixture (league_code, source, source_event_id) "
        "VALUES ('EPL', 'odds_api_io', 'event1')"
    )
    
    result = run_epl_pilot_refresh(
        conn=mem_db,
        api_key="fake-key",
        base_url="http://fake",
        outputs_dir=tmp_path,
    )
    
    assert result["status"] == "DUPLICATE_SOURCE_EVENT_IDS"
    mock_publish.assert_not_called()
    assert len(list(tmp_path.glob("benchmarks/epl_pilot_refresh_*.json"))) == 1


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
    
    result = run_epl_pilot_refresh(
        conn=mem_db,
        api_key="fake-key",
        base_url="http://fake",
        league_code="EPL",
    )
    assert result["status"] == "OK", result["build_elo_status"]  
    
    mock_publish.assert_called_once()
    assert mock_publish.call_args.kwargs.get("league") == "EPL"
