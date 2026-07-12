"""Tests for API-FOOTBALL provider probe."""

import json
import os
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from scorerecord.ingest.api_football_probe import probe_api_football


@pytest.fixture
def mock_env():
    with patch.dict(os.environ, {"API_FOOTBALL_KEY": "test_key"}):
        yield


def _mock_response(json_data: dict) -> MagicMock:
    mock = MagicMock()
    encoded = json.dumps(json_data).encode("utf-8")
    mock.read.return_value = encoded
    # urlopen acts as a context manager
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = None
    return mock


def _mock_http_error(code: int, json_data: dict | None = None) -> urllib.error.HTTPError:
    encoded = json.dumps(json_data or {}).encode("utf-8")
    fp = BytesIO(encoded)
    return urllib.error.HTTPError(url="", code=code, msg="", hdrs=None, fp=fp)


def test_missing_key():
    with patch.dict(os.environ, {}, clear=True):
        result = probe_api_football(leagues=("EPL",), season=2024, sample_size=1)
    assert result["provider_state"] == "NO_ACCESS"
    assert "test_key" not in str(result)
    assert "fouls_found" in result
    assert "blocked_shots_found" in result
    assert "shots_inside_box_found" in result


def test_auth_failed(mock_env):
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = _mock_http_error(401)
        result = probe_api_football(leagues=("EPL",), season=2024, sample_size=1)

    assert result["provider_state"] == "AUTH_FAILED"
    assert "test_key" not in str(result)


def test_rate_limited(mock_env):
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = _mock_http_error(429)
        result = probe_api_football(leagues=("EPL",), season=2024, sample_size=1)

    assert result["provider_state"] == "RATE_LIMITED"


def test_plan_restricted(mock_env):
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(
            {"errors": {"plan": "upgrade your subscription"}}
        )
        result = probe_api_football(leagues=("EPL",), season=2024, sample_size=1)

    assert result["provider_state"] == "PLAN_RESTRICTED"


def test_no_target_leagues(mock_env):
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(
            {
                "response": [
                    {"league": {"id": 99, "name": "Random League"}, "country": {"name": "Random"}}
                ]
            }
        )
        result = probe_api_football(leagues=("XYZ",), season=2024, sample_size=1)

    assert result["provider_state"] == "NO_TARGET_LEAGUES"
    assert result["leagues_found"] == []


def test_no_fixtures(mock_env):
    def side_effect(req, **kwargs):
        url = req.full_url
        if "/leagues" in url:
            return _mock_response(
                {
                    "response": [
                        {
                            "league": {"id": 39, "name": "Premier League"},
                            "country": {"name": "England"},
                        }
                    ]
                }
            )
        if "/fixtures" in url:
            return _mock_response({"response": []})
        return _mock_response({})

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = side_effect
        result = probe_api_football(leagues=("EPL",), season=2024, sample_size=1)

    assert result["provider_state"] == "NO_FIXTURES"
    assert result["league_states"]["EPL"] == "NO_FIXTURES"


def test_no_stats(mock_env):
    def side_effect(req, **kwargs):
        url = req.full_url
        if "/leagues" in url:
            return _mock_response(
                {
                    "response": [
                        {
                            "league": {"id": 39, "name": "Premier League"},
                            "country": {"name": "England"},
                        }
                    ]
                }
            )
        if "/fixtures?" in url:
            return _mock_response(
                {
                    "response": [
                        {"fixture": {"id": 123, "date": "2024-01-01", "status": {"short": "FT"}}}
                    ]
                }
            )
        if "/statistics" in url:
            return _mock_response({"response": []})
        return _mock_response({})

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = side_effect
        result = probe_api_football(leagues=("EPL",), season=2024, sample_size=1)

    assert result["provider_state"] == "NO_STATS"
    assert result["league_states"]["EPL"] == "NO_STATS"


def test_stats_usable_shots_and_corners(mock_env):
    def side_effect(req, **kwargs):
        url = req.full_url
        if "/leagues" in url:
            return _mock_response(
                {
                    "response": [
                        {
                            "league": {"id": 39, "name": "Premier League"},
                            "country": {"name": "England"},
                        }
                    ]
                }
            )
        if "/fixtures?" in url:
            return _mock_response(
                {
                    "response": [
                        {"fixture": {"id": 123, "date": "2024-01-01", "status": {"short": "FT"}}}
                    ]
                }
            )
        if "/statistics" in url:
            return _mock_response(
                {
                    "response": [
                        {
                            "statistics": [
                                {"type": "Shots on Goal", "value": 5},
                                {"type": "Corner Kicks", "value": 8},
                                {"type": "Fouls", "value": 10},
                                {"type": "Blocked Shots", "value": 2},
                            ]
                        }
                    ]
                }
            )
        return _mock_response({})

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = side_effect
        result = probe_api_football(leagues=("EPL",), season=2024, sample_size=1)

    assert result["provider_state"] == "CORNERS_USABLE_CANDIDATE"
    assert result["shots_found"] > 0
    assert result["sot_found"] == 1
    assert result["corners_found"] == 1
    assert result["fouls_found"] == 1
    assert result["blocked_shots_found"] == 1


def test_xg_usable_candidate(mock_env):
    def side_effect(req, **kwargs):
        url = req.full_url
        if "/leagues" in url:
            return _mock_response(
                {
                    "response": [
                        {
                            "league": {"id": 39, "name": "Premier League"},
                            "country": {"name": "England"},
                        }
                    ]
                }
            )
        if "/fixtures?" in url:
            return _mock_response(
                {
                    "response": [
                        {"fixture": {"id": 123, "date": "2024-01-01", "status": {"short": "FT"}}}
                    ]
                }
            )
        if "/statistics" in url:
            return _mock_response(
                {"response": [{"statistics": [{"type": "expected_goals", "value": "1.5"}]}]}
            )
        return _mock_response({})

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = side_effect
        result = probe_api_football(leagues=("EPL",), season=2024, sample_size=1)

    assert result["provider_state"] == "XG_USABLE_CANDIDATE"
    assert result["xg_found"] == 1
