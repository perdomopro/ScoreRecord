import json

from scorerecord.ingest.sportmonks_probe import probe_sportmonks_fixture


def test_probe_sportmonks_fixture_parses_participants(tmp_path):
    payload = {
        "data": {
            "id": 19439627,
            "league_id": 564,
            "season_id": 25659,
            "round_id": 373247,
            "name": "Real Madrid vs Athletic Club",
            "starting_at": "2026-05-23 19:00:00",
            "has_odds": True,
            "has_premium_odds": True,
            "participants": [
                {"name": "Athletic Club", "meta": {"location": "away"}},
                {"name": "Real Madrid", "meta": {"location": "home"}},
            ],
            "league": {"name": "La Liga"},
            "venue": {"name": "Estadio Santiago Bernabeu"},
            "state": {"name": "FT"},
        }
    }
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = probe_sportmonks_fixture(
        fixture_id=19439627,
        api_token=None,
        base_url="https://example.invalid",
        input_json=path,
    )

    assert result["status"] == "OK"
    assert result["home_team"] == "Real Madrid"
    assert result["away_team"] == "Athletic Club"
    assert result["league_name"] == "La Liga"
    assert result["has_odds"] is True
