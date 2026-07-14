import json
import sqlite3
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scorerecord.aliases import normalize_key


def _get_event_id(event: dict) -> str | None:
    if "id" in event:
        return str(event["id"])
    if "eventId" in event:
        return str(event["eventId"])
    if "event_id" in event:
        return str(event["event_id"])
    return None

def _get_home_name(event: dict) -> str | None:
    if "home" in event and isinstance(event["home"], str): return event["home"]
    if "homeTeam" in event and isinstance(event["homeTeam"], str): return event["homeTeam"]
    if "home_team" in event and isinstance(event["home_team"], str): return event["home_team"]
    
    if isinstance(event.get("home"), dict) and "name" in event["home"]: return event["home"]["name"]
    if isinstance(event.get("homeTeam"), dict) and "name" in event["homeTeam"]: return event["homeTeam"]["name"]
    
    if isinstance(event.get("teams"), dict):
        if isinstance(event["teams"].get("home"), dict) and "name" in event["teams"]["home"]:
            return event["teams"]["home"]["name"]
            
    # Deterministic participants logic (only if strict ordering can be implied, which is risky)
    # We will avoid guessing participants unless explicit home/away is present.
    return None

def _get_away_name(event: dict) -> str | None:
    if "away" in event and isinstance(event["away"], str): return event["away"]
    if "awayTeam" in event and isinstance(event["awayTeam"], str): return event["awayTeam"]
    if "away_team" in event and isinstance(event["away_team"], str): return event["away_team"]
    
    if isinstance(event.get("away"), dict) and "name" in event["away"]: return event["away"]["name"]
    if isinstance(event.get("awayTeam"), dict) and "name" in event["awayTeam"]: return event["awayTeam"]["name"]
    
    if isinstance(event.get("teams"), dict):
        if isinstance(event["teams"].get("away"), dict) and "name" in event["teams"]["away"]:
            return event["teams"]["away"]["name"]
            
    return None

def _get_kickoff_utc(event: dict) -> str | None:
    for key in ["date", "startTime", "start_time", "commence_time", "kickoff"]:
        if key in event and isinstance(event[key], str):
            return event[key]
    if isinstance(event.get("fixture"), dict) and "date" in event["fixture"]:
        return event["fixture"]["date"]
    return None

def ingest_fixtures_odds_api_io(
    conn: sqlite3.Connection,
    api_key: str | None,
    base_url: str,
    sport: str,
    league_code: str,
    league_slug: str,
    competition: str,
    season: str,
    raw_dir: Path | None,
    max_requests: int = 1,
    dry_run: bool = False,
    input_json: Path | None = None,
    verify_league_slug: bool = False,
) -> dict[str, Any]:

    empty_result = {
        "status": "OK",
        "provider": "odds_api_io",
        "league": league_code,
        "sport": sport,
        "league_slug": league_slug,
        "competition": competition,
        "season": season,
        "api_requests_used": 0,
        "events_seen": 0,
        "events_valid": 0,
        "invalid_events": 0,
        "skipped_events": 0,
        "fixtures_inserted": 0,
        "fixtures_updated": 0,
        "teams_mapped": 0,
        "teams_unmapped": 0,
        "unmapped_names": [],
        "unresolved_fixtures": 0,
        "raw_files_written": [],
        "dry_run": dry_run,
    }

    if not input_json and not api_key:
        return {**empty_result, "status": "AUTH_MISSING"}

    required_requests = 2 if verify_league_slug else 1
    if not input_json and max_requests < required_requests:
        status = "MAX_REQUESTS_TOO_LOW" if max_requests > 0 else "MAX_REQUESTS_ZERO"
        return {**empty_result, "status": status}

    api_requests_used = 0
    raw_files_written = []
    
    def save_raw(name: str, data: Any) -> None:
        if raw_dir:
            raw_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            path = raw_dir / f"odds_api_io_{name}_{sport}_{timestamp}.json"
            if name == "events":
                path = raw_dir / f"odds_api_io_{name}_{sport}_{league_slug}_{timestamp}.json"
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            raw_files_written.append(str(path))

    def fetch(endpoint: str) -> Any | str:
        nonlocal api_requests_used
        api_requests_used += 1
        url = f"{base_url.rstrip('/')}{endpoint}"
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return "AUTH_FAILED"
            if e.code == 429:
                return "RATE_LIMITED"
            return "API_ERROR"
        except Exception:
            return "INVALID_RESPONSE"

    events = []
    if input_json:
        try:
            data = json.loads(input_json.read_text(encoding="utf-8"))
            if isinstance(data, list):
                events = data
            elif isinstance(data, dict) and "events" in data:
                events = data["events"]
            else:
                events = data # Assume it might be iterable somehow, fallback is handled later
        except Exception:
            return {**empty_result, "status": "INVALID_RESPONSE"}
    else:
        if verify_league_slug:
            leagues_data = fetch(f"/leagues?apiKey={api_key}&sport={sport}")
            if isinstance(leagues_data, str):
                return {**empty_result, "status": leagues_data, "api_requests_used": api_requests_used}
            save_raw("leagues", leagues_data)
            
            if not isinstance(leagues_data, list):
                return {**empty_result, "status": "INVALID_RESPONSE", "api_requests_used": api_requests_used, "raw_files_written": raw_files_written}
            
            found = False
            for l in leagues_data:
                if isinstance(l, dict) and l.get("slug") == league_slug:
                    found = True
                    break
            
            if not found:
                return {**empty_result, "status": "LEAGUE_SLUG_NOT_FOUND", "api_requests_used": api_requests_used, "raw_files_written": raw_files_written}

        events_data = fetch(f"/events?apiKey={api_key}&sport={sport}&league={league_slug}")
        if isinstance(events_data, str):
            return {**empty_result, "status": events_data, "api_requests_used": api_requests_used, "raw_files_written": raw_files_written}
        save_raw("events", events_data)
        
        if isinstance(events_data, list):
            events = events_data
        elif isinstance(events_data, dict) and "events" in events_data:
            events = events_data["events"]
        else:
            return {**empty_result, "status": "INVALID_RESPONSE", "api_requests_used": api_requests_used, "raw_files_written": raw_files_written}

    if not isinstance(events, list):
        return {**empty_result, "status": "INVALID_RESPONSE", "api_requests_used": api_requests_used, "raw_files_written": raw_files_written}

    events_seen = len(events)
    if events_seen == 0:
        return {**empty_result, "status": "NO_EVENTS_RETURNED", "api_requests_used": api_requests_used, "raw_files_written": raw_files_written}

    events_valid = 0
    invalid_events = 0
    fixtures_inserted = 0
    fixtures_updated = 0
    teams_mapped = set()
    teams_unmapped = set()
    unresolved_fixtures = 0
    
    for event in events:
        if not isinstance(event, dict):
            invalid_events += 1
            continue
            
        e_id = _get_event_id(event)
        h_name = _get_home_name(event)
        a_name = _get_away_name(event)
        dt = _get_kickoff_utc(event)
        
        if not all([e_id, h_name, a_name, dt]):
            invalid_events += 1
            continue
            
        events_valid += 1
        
        norm_h = normalize_key(h_name)
        norm_a = normalize_key(a_name)
        
        home_id = None
        away_id = None
        
        if norm_h:
            h_row = conn.execute("SELECT team_id FROM team_alias WHERE alias_key = ?", (norm_h,)).fetchone()
            if h_row:
                home_id = h_row["team_id"]
        
        if norm_a:
            a_row = conn.execute("SELECT team_id FROM team_alias WHERE alias_key = ?", (norm_a,)).fetchone()
            if a_row:
                away_id = a_row["team_id"]
                
        if home_id is not None:
            teams_mapped.add(h_name)
        else:
            teams_unmapped.add(h_name)
            
        if away_id is not None:
            teams_mapped.add(a_name)
        else:
            teams_unmapped.add(a_name)
            
        home_ph = None if home_id is not None else h_name
        away_ph = None if away_id is not None else a_name
        
        if home_id is None or away_id is None:
            unresolved_fixtures += 1
            
        if not dry_run:
            row = conn.execute(
                "SELECT fixture_id FROM fixture WHERE source = 'odds_api_io' AND source_event_id = ?",
                (e_id,)
            ).fetchone()
            
            if row:
                conn.execute(
                    """
                    UPDATE fixture SET
                      kickoff_utc = ?, home_id = ?, away_id = ?,
                      home_placeholder = ?, away_placeholder = ?,
                      league_code = ?, competition = ?, season = ?
                    WHERE source = 'odds_api_io' AND source_event_id = ?
                    """,
                    (dt, home_id, away_id, home_ph, away_ph, league_code, competition, season, e_id)
                )
                fixtures_updated += 1
            else:
                fixture_id = f"odds_io-{e_id}"
                conn.execute(
                    """
                    INSERT INTO fixture (
                      fixture_id, source, source_event_id, kickoff_utc,
                      home_id, away_id, home_placeholder, away_placeholder,
                      league_code, competition, season, status
                    ) VALUES (?, 'odds_api_io', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled')
                    """,
                    (fixture_id, e_id, dt, home_id, away_id, home_ph, away_ph, league_code, competition, season)
                )
                fixtures_inserted += 1

    if not dry_run:
        conn.commit()
        
    final_status = "OK"
    if events_valid == 0 and events_seen > 0:
        final_status = "INVALID_RESPONSE"
    elif invalid_events > 0:
        final_status = "OK_WITH_SKIPPED_EVENTS"

    return {
        "status": final_status,
        "provider": "odds_api_io",
        "league": league_code,
        "sport": sport,
        "league_slug": league_slug,
        "competition": competition,
        "season": season,
        "api_requests_used": api_requests_used,
        "events_seen": events_seen,
        "events_valid": events_valid,
        "invalid_events": invalid_events,
        "skipped_events": invalid_events,
        "fixtures_inserted": fixtures_inserted,
        "fixtures_updated": fixtures_updated,
        "teams_mapped": len(teams_mapped),
        "teams_unmapped": len(teams_unmapped),
        "unmapped_names": list(teams_unmapped),
        "unresolved_fixtures": unresolved_fixtures,
        "raw_files_written": raw_files_written,
        "dry_run": dry_run,
    }
