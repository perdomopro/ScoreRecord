import json
import sqlite3
import urllib.request
from datetime import datetime, UTC
from typing import Any
from pathlib import Path

from scorerecord.aliases import normalize_key

API_URL = "https://api.the-odds-api.com/v4/sports/{sport_key}/events"

def _resolve_team_id(conn: sqlite3.Connection, name: str) -> int | None:
    norm = normalize_key(name)
    if not norm:
        return None
    row = conn.execute(
        "SELECT team_id FROM team_alias WHERE alias_key = ?", (norm,)
    ).fetchone()
    return int(row["team_id"]) if row else None

def ingest_fixtures_odds_api(
    conn: sqlite3.Connection,
    api_key: str | None,
    sport_key: str,
    league_code: str,
    competition: str,
    season: str,
    raw_dir: Path | None,
    base_url: str = "https://api.the-odds-api.com/v4",
    max_requests: int = 1,
    dry_run: bool = False,
    mock_events: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    
    empty_result = {
        "api_requests_used": 0,
        "events_seen": 0,
        "fixtures_inserted": 0,
        "fixtures_updated": 0,
        "teams_mapped": 0,
        "teams_unmapped": 0,
        "unmapped_names": [],
        "unresolved_fixtures": 0,
        "request_budget_remaining": -1,
        "dry_run": dry_run,
    }

    if mock_events is None and max_requests < 1:
        return {"status": "MAX_REQUESTS_ZERO", **empty_result}

    if mock_events is None and not api_key:
        return {"status": "AUTH_MISSING", **empty_result}

    if mock_events is not None:
        events = mock_events
        remaining = -1
        api_requests = 0
    else:
        url = f"{base_url.rstrip('/')}/sports/{sport_key}/events?apiKey={api_key}"
        req = urllib.request.Request(url, headers={"User-Agent": "ScoreRecord/1.0"})
        api_requests = 1
        try:
            with urllib.request.urlopen(req) as response:
                events = json.loads(response.read())
                headers = response.headers
                remaining_str = headers.get("x-requests-remaining", "-1")
                remaining = int(remaining_str)
        except urllib.error.HTTPError as e:
            status = "API_ERROR"
            if e.code in (401, 403):
                status = "AUTH_FAILED"
            elif e.code == 429:
                status = "RATE_LIMITED"
            return {"status": status, **empty_result, "api_requests_used": api_requests}
        except json.JSONDecodeError:
            return {"status": "INVALID_RESPONSE", **empty_result, "api_requests_used": api_requests}
        
        if raw_dir:
            raw_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            raw_file = raw_dir / f"odds_api_events_{sport_key}_{timestamp}.json"
            raw_file.write_text(json.dumps(events, indent=2), encoding="utf-8")

    fixtures_inserted = 0
    fixtures_updated = 0
    teams_mapped = set()
    teams_unmapped = set()
    unresolved_fixtures = 0
    
    for event in events:
        event_id = event["id"]
        commence_time = event["commence_time"]
        home_name = event["home_team"]
        away_name = event["away_team"]
        
        home_id = _resolve_team_id(conn, home_name)
        away_id = _resolve_team_id(conn, away_name)
        
        if home_id is not None:
            teams_mapped.add(home_name)
        else:
            teams_unmapped.add(home_name)
            
        if away_id is not None:
            teams_mapped.add(away_name)
        else:
            teams_unmapped.add(away_name)
            
        home_ph = None if home_id is not None else home_name
        away_ph = None if away_id is not None else away_name
        
        if home_id is None or away_id is None:
            unresolved_fixtures += 1
            
        if not dry_run:
            # Check if exists
            row = conn.execute(
                "SELECT fixture_id FROM fixture WHERE source = 'odds_api' AND source_event_id = ?",
                (event_id,)
            ).fetchone()
            
            if row:
                conn.execute(
                    """
                    UPDATE fixture SET
                      kickoff_utc = ?, home_id = ?, away_id = ?,
                      home_placeholder = ?, away_placeholder = ?,
                      league_code = ?, competition = ?, season = ?
                    WHERE source = 'odds_api' AND source_event_id = ?
                    """,
                    (commence_time, home_id, away_id, home_ph, away_ph, league_code, competition, season, event_id)
                )
                fixtures_updated += 1
            else:
                fixture_id = f"odds-{event_id}"
                conn.execute(
                    """
                    INSERT INTO fixture (
                      fixture_id, source, source_event_id, kickoff_utc,
                      home_id, away_id, home_placeholder, away_placeholder,
                      league_code, competition, season, status
                    ) VALUES (?, 'odds_api', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled')
                    """,
                    (fixture_id, event_id, commence_time, home_id, away_id, home_ph, away_ph, league_code, competition, season)
                )
                fixtures_inserted += 1

    if not dry_run:
        conn.commit()

    return {
        "status": "OK",
        "events_seen": len(events),
        "fixtures_inserted": fixtures_inserted,
        "fixtures_updated": fixtures_updated,
        "teams_mapped": len(teams_mapped),
        "teams_unmapped": len(teams_unmapped),
        "unmapped_names": list(teams_unmapped),
        "unresolved_fixtures": unresolved_fixtures,
        "api_requests_used": api_requests,
        "request_budget_remaining": remaining,
        "dry_run": dry_run,
    }
