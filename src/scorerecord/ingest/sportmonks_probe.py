from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_FIXTURE_INCLUDES = "participants;league;venue;state;scores"


def probe_sportmonks_fixture(
    *,
    fixture_id: int | str,
    api_token: str | None,
    base_url: str,
    includes: str = DEFAULT_FIXTURE_INCLUDES,
    input_json: Path | None = None,
) -> dict[str, Any]:
    if input_json is None and not api_token:
        return {"status": "AUTH_MISSING", "fixture_id": str(fixture_id)}

    if input_json is not None:
        try:
            payload = json.loads(input_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"status": "INVALID_RESPONSE", "fixture_id": str(fixture_id), "error": str(exc)}
    else:
        query = urllib.parse.urlencode({"api_token": api_token, "include": includes})
        url = f"{base_url.rstrip('/')}/fixtures/{fixture_id}?{query}"
        request = urllib.request.Request(url, headers={"User-Agent": "ScoreRecord/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return {"status": "AUTH_FAILED", "fixture_id": str(fixture_id)}
            if exc.code == 429:
                return {"status": "RATE_LIMITED", "fixture_id": str(fixture_id)}
            return {"status": "API_ERROR", "fixture_id": str(fixture_id), "http_status": exc.code}
        except (OSError, json.JSONDecodeError) as exc:
            return {"status": "INVALID_RESPONSE", "fixture_id": str(fixture_id), "error": str(exc)}

    fixture = payload.get("data", payload) if isinstance(payload, dict) else {}
    if not isinstance(fixture, dict):
        return {"status": "INVALID_RESPONSE", "fixture_id": str(fixture_id)}

    home, away = _home_away_from_participants(fixture.get("participants"))
    league = fixture.get("league") if isinstance(fixture.get("league"), dict) else {}
    venue = fixture.get("venue") if isinstance(fixture.get("venue"), dict) else {}
    state = fixture.get("state") if isinstance(fixture.get("state"), dict) else {}

    return {
        "status": "OK",
        "provider": "sportmonks",
        "fixture_id": str(fixture.get("id") or fixture_id),
        "league_id": fixture.get("league_id"),
        "league_name": league.get("name"),
        "season_id": fixture.get("season_id"),
        "round_id": fixture.get("round_id"),
        "name": fixture.get("name"),
        "starting_at": fixture.get("starting_at"),
        "starting_at_timestamp": fixture.get("starting_at_timestamp"),
        "home_team": home,
        "away_team": away,
        "venue_name": venue.get("name"),
        "state_name": state.get("name"),
        "has_odds": bool(fixture.get("has_odds")),
        "has_premium_odds": bool(fixture.get("has_premium_odds")),
        "raw_participant_count": len(fixture.get("participants") or []),
    }


def _home_away_from_participants(raw: object) -> tuple[str | None, str | None]:
    if not isinstance(raw, list):
        return None, None
    home = None
    away = None
    for item in raw:
        if not isinstance(item, dict):
            continue
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        location = str(meta.get("location") or "").lower()
        if location == "home":
            home = item.get("name")
        elif location == "away":
            away = item.get("name")
    return str(home) if home else None, str(away) if away else None
