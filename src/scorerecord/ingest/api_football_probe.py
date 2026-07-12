"""Probe API-FOOTBALL for feasibility."""

import os
import json
import urllib.request
import urllib.error
from urllib.parse import urlencode
from typing import Any


def resolve_aliases(
    leagues_data: list[dict[str, Any]], requested: tuple[str, ...]
) -> dict[str, int]:
    """Resolve ScoreRecord aliases to API-FOOTBALL league IDs."""

    # Heuristics mapping (alias -> (Country, Name substrings to look for))
    heuristics = {
        "EPL": ("England", ["premier league"]),
        "CHAMPIONSHIP": ("England", ["championship"]),
        "LA_LIGA": ("Spain", ["la liga", "primera division"]),
        "SERIE_A": ("Italy", ["serie a"]),
        "BUNDESLIGA": ("Germany", ["bundesliga 1", "bundesliga"]),
        "LIGUE_1": ("France", ["ligue 1"]),
        "MLS": ("USA", ["major league soccer"]),
        "LIGA_MX": ("Mexico", ["liga mx", "primera division"]),
    }

    # Fallback hardcoded IDs for typical API-FOOTBALL IDs as of 2024
    fallbacks = {
        "EPL": 39,
        "LA_LIGA": 140,
        "SERIE_A": 135,
        "BUNDESLIGA": 78,
        "LIGUE_1": 61,
        "CHAMPIONSHIP": 40,
        "MLS": 253,
        "LIGA_MX": 262,
    }

    resolved = {}
    for req in requested:
        req_upper = req.upper()
        found_id = None

        if req_upper in heuristics:
            target_country, target_names = heuristics[req_upper]
            for l_node in leagues_data:
                country = l_node.get("country", {}).get("name", "")
                name = l_node.get("league", {}).get("name", "").lower()

                if country == target_country:
                    if any(tn in name for tn in target_names):
                        found_id = l_node["league"]["id"]
                        break

        if found_id is None and req_upper in fallbacks:
            found_id = fallbacks[req_upper]

        if found_id is not None:
            resolved[req_upper] = found_id

    return resolved


def check_api_errors(response_json: dict) -> str | None:
    """Return error state string if API returned an error, else None."""
    errors = response_json.get("errors")
    if not errors:
        return None

    if isinstance(errors, dict):
        # API-Football errors are sometimes a dict {field: error_msg}
        err_values = " ".join(str(k).lower() + " " + str(v).lower() for k, v in errors.items())
        if "token" in err_values or "unauthorized" in err_values or "access" in err_values:
            return "AUTH_FAILED"
        if "requests" in err_values or "rate" in err_values or "limit" in err_values:
            return "RATE_LIMITED"
        if "plan" in err_values or "subscription" in err_values:
            return "PLAN_RESTRICTED"
    elif isinstance(errors, list) and len(errors) > 0:
        err_values = " ".join(str(v).lower() for v in errors)
        if "token" in err_values or "unauthorized" in err_values or "access" in err_values:
            return "AUTH_FAILED"
        if "requests" in err_values or "rate" in err_values or "limit" in err_values:
            return "RATE_LIMITED"
        if "plan" in err_values or "subscription" in err_values:
            return "PLAN_RESTRICTED"

    return "UNKNOWN_ERROR"


def probe_api_football(
    leagues: tuple[str, ...], season: int, sample_size: int = 3
) -> dict[str, Any]:
    api_key = os.environ.get("API_FOOTBALL_KEY")
    if not api_key:
        return {"provider_state": "NO_ACCESS"}

    base_url = "https://v3.football.api-sports.io"
    headers = {"x-apisports-key": api_key}

    requests_made = 0

    def get(endpoint: str, params: dict | None = None) -> dict:
        nonlocal requests_made
        requests_made += 1
        
        query = f"?{urlencode(params)}" if params else ""
        url = f"{base_url}{endpoint}{query}"
        req = urllib.request.Request(url, headers=headers)
        
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return {"errors": {"token": "auth failed"}}
            if e.code == 429:
                return {"errors": {"requests": "rate limited"}}
            # Check if body has errors
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                return {"errors": {"unknown": f"HTTP {e.code}"}}
        except Exception:
            return {"errors": {"unknown": "JSON decode failed"}}

    # 1. Fetch leagues
    leagues_resp = get("/leagues")
    err = check_api_errors(leagues_resp)
    if err:
        return {
            "provider_state": err,
            "estimated_requests": 1,
            "actual_requests_made": requests_made,
        }

    leagues_data = leagues_resp.get("response", [])
    resolved_leagues = resolve_aliases(leagues_data, leagues)

    if not resolved_leagues:
        return {
            "provider_state": "NO_TARGET_LEAGUES",
            "auth_state": "VALID",
            "season": season,
            "leagues_requested": list(leagues),
            "leagues_found": [],
            "estimated_requests": 1,
            "actual_requests_made": requests_made,
        }

    # Global tracking
    fixtures_sampled = 0
    statistics_found = 0
    shots_found = 0
    sot_found = 0
    total_shots_found = 0
    corners_found = 0
    cards_found = 0
    possession_found = 0
    passes_found = 0
    xg_found = 0
    dates_seen = []

    league_states = {}

    for alias, lg_id in resolved_leagues.items():
        # Get fixtures for the season
        fix_resp = get("/fixtures", {"league": lg_id, "season": season})
        err = check_api_errors(fix_resp)
        if err:
            league_states[alias] = err
            continue

        all_fixtures = fix_resp.get("response", [])

        # Filter for completed fixtures
        finished_statuses = {"FT", "AET", "PEN"}
        finished_fixtures = [
            f
            for f in all_fixtures
            if f.get("fixture", {}).get("status", {}).get("short") in finished_statuses
        ]

        if not finished_fixtures:
            league_states[alias] = "NO_FIXTURES"
            continue

        sampled = finished_fixtures[:sample_size]

        has_stats = False
        for f in sampled:
            f_id = f["fixture"]["id"]
            dt_str = f["fixture"]["date"]
            if dt_str:
                dates_seen.append(dt_str)

            fixtures_sampled += 1

            # Fetch statistics
            stats_resp = get("/fixtures/statistics", {"fixture": f_id})
            err = check_api_errors(stats_resp)
            if err:
                continue

            stats_data = stats_resp.get("response", [])
            if not stats_data:
                continue

            statistics_found += 1
            has_stats = True

            # Examine stats for either team (index 0)
            team_stats = stats_data[0].get("statistics", [])

            for st in team_stats:
                tpe = str(st.get("type", "")).lower().strip()
                val = st.get("value")
                if val is None:
                    continue

                if "shots on goal" in tpe or "shots on target" in tpe:
                    sot_found += 1
                elif "total shots" in tpe:
                    total_shots_found += 1
                elif "corner" in tpe:
                    corners_found += 1
                elif "yellow" in tpe or "red card" in tpe:
                    cards_found += 1
                elif "possession" in tpe:
                    possession_found += 1
                elif "passes" in tpe:
                    passes_found += 1
                elif "expected_goals" in tpe or "expected goals" in tpe or "xg" == tpe:
                    xg_found += 1
                # If they just have "shots" but didn't match total shots, treat as shots found
                if "shots" in tpe:
                    shots_found += 1

        if has_stats:
            league_states[alias] = "STATS_USABLE"
        else:
            league_states[alias] = "NO_STATS"

    dates_seen.sort()

    # Determine final state
    if not league_states:
        provider_state = "NO_TARGET_LEAGUES"
    elif xg_found > 0:
        provider_state = "XG_USABLE_CANDIDATE"
    elif shots_found > 0 or total_shots_found > 0 or corners_found > 0:
        if shots_found > 0 and corners_found == 0:
            provider_state = "STATS_USABLE"
        else:
            provider_state = "CORNERS_USABLE_CANDIDATE"
    elif statistics_found > 0:
        provider_state = "STATS_USABLE"
    elif all(state == "NO_FIXTURES" for state in league_states.values()):
        provider_state = "NO_FIXTURES"
    else:
        provider_state = "NO_STATS"

    return {
        "provider_state": provider_state,
        "auth_state": "VALID",
        "season": season,
        "leagues_requested": list(leagues),
        "leagues_found": list(resolved_leagues.keys()),
        "fixtures_sampled": fixtures_sampled,
        "statistics_found": statistics_found,
        "shots_found": shots_found,
        "sot_found": sot_found,
        "total_shots_found": total_shots_found,
        "corners_found": corners_found,
        "cards_found": cards_found,
        "possession_found": possession_found,
        "passes_found": passes_found,
        "xg_found": xg_found,
        "earliest_match_date_seen": dates_seen[0] if dates_seen else None,
        "latest_match_date_seen": dates_seen[-1] if dates_seen else None,
        "historical_accessible": True if dates_seen else False,
        "estimated_requests": 1 + len(resolved_leagues) + fixtures_sampled,
        "actual_requests_made": requests_made,
        "free_plan_limit_warning": requests_made > 50,
        "league_states": league_states,
    }
