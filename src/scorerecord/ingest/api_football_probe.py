"""Probe API-FOOTBALL for feasibility."""

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode

import sqlite3
from datetime import datetime, UTC
from pathlib import Path
from scorerecord.aliases import normalize_key


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

            # Pass 1: exact match
            for l_node in leagues_data:
                country = l_node.get("country", {}).get("name", "")
                name = l_node.get("league", {}).get("name", "").lower()
                if country.lower() == target_country.lower():
                    if name in target_names:
                        found_id = l_node["league"]["id"]
                        break

            # Pass 2: substring match if not found
            if found_id is None:
                for l_node in leagues_data:
                    country = l_node.get("country", {}).get("name", "")
                    name = l_node.get("league", {}).get("name", "").lower()
                    if country.lower() == target_country.lower():
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
    leagues: tuple[str, ...], season: int, sample_size: int = 3, sleep_seconds: float = 0.0
) -> dict[str, Any]:

    def _base_report(state="UNKNOWN") -> dict[str, Any]:
        return {
            "provider_state": state,
            "auth_state": "VALID" if state != "NO_ACCESS" and state != "AUTH_FAILED" else state,
            "season": season,
            "leagues_requested": list(leagues),
            "leagues_found": [],
            "fixtures_sampled": 0,
            "statistics_found": 0,
            "shots_found": 0,
            "sot_found": 0,
            "total_shots_found": 0,
            "blocked_shots_found": 0,
            "shots_inside_box_found": 0,
            "shots_outside_box_found": 0,
            "corners_found": 0,
            "cards_found": 0,
            "fouls_found": 0,
            "possession_found": 0,
            "passes_found": 0,
            "xg_found": 0,
            "earliest_match_date_seen": None,
            "latest_match_date_seen": None,
            "historical_accessible": False,
            "estimated_requests": 0,
            "actual_requests_made": 0,
            "free_plan_limit_warning": False,
            "league_states": {},
        }

    report = _base_report()

    api_key = os.environ.get("API_FOOTBALL_KEY")
    if not api_key:
        report["provider_state"] = "NO_ACCESS"
        report["auth_state"] = "NO_ACCESS"
        return report

    base_url = "https://v3.football.api-sports.io"
    headers = {"x-apisports-key": api_key}

    requests_made = 0

    def get(endpoint: str, params: dict | None = None) -> dict:
        nonlocal requests_made
        if requests_made > 0 and sleep_seconds > 0:
            time.sleep(sleep_seconds)
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
    report["actual_requests_made"] = requests_made
    report["estimated_requests"] = 1

    err = check_api_errors(leagues_resp)
    if err:
        report["provider_state"] = err
        if err == "AUTH_FAILED":
            report["auth_state"] = "AUTH_FAILED"
        return report

    leagues_data = leagues_resp.get("response", [])
    resolved_leagues = resolve_aliases(leagues_data, leagues)
    report["leagues_found"] = list(resolved_leagues.keys())

    if not resolved_leagues:
        report["provider_state"] = "NO_TARGET_LEAGUES"
        return report

    # Global tracking
    dates_seen = []

    for alias, lg_id in resolved_leagues.items():
        # Get fixtures for the season
        fix_resp = get("/fixtures", {"league": lg_id, "season": season})
        err = check_api_errors(fix_resp)
        if err:
            report["league_states"][alias] = err
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
            report["league_states"][alias] = "NO_FIXTURES"
            continue

        sampled = finished_fixtures[:sample_size]

        has_stats = False
        for f in sampled:
            f_id = f["fixture"]["id"]
            dt_str = f["fixture"]["date"]
            if dt_str:
                dates_seen.append(dt_str)

            report["fixtures_sampled"] += 1

            # Fetch statistics
            stats_resp = get("/fixtures/statistics", {"fixture": f_id})
            err = check_api_errors(stats_resp)
            if err:
                continue

            stats_data = stats_resp.get("response", [])
            if not stats_data:
                continue

            stats_found_in_fixture = {
                "shots": False,
                "sot": False,
                "total_shots": False,
                "corners": False,
                "cards": False,
                "possession": False,
                "passes": False,
                "xg": False,
                "blocked_shots": False,
                "shots_inside_box": False,
                "shots_outside_box": False,
                "fouls": False,
            }

            fixture_has_stats = False
            for team_data in stats_data:
                team_stats = team_data.get("statistics", [])
                if team_stats:
                    fixture_has_stats = True
                    has_stats = True

                for st in team_stats:
                    tpe = str(st.get("type", "")).lower().strip()
                    val = st.get("value")
                    if val is None:
                        continue

                    if "shots on goal" in tpe or "shots on target" in tpe:
                        stats_found_in_fixture["sot"] = True
                    elif "total shots" in tpe:
                        stats_found_in_fixture["total_shots"] = True
                    elif "blocked shots" in tpe:
                        stats_found_in_fixture["blocked_shots"] = True
                    elif "shots insidebox" in tpe or "shots inside box" in tpe:
                        stats_found_in_fixture["shots_inside_box"] = True
                    elif "shots outsidebox" in tpe or "shots outside box" in tpe:
                        stats_found_in_fixture["shots_outside_box"] = True
                    elif "corner" in tpe:
                        stats_found_in_fixture["corners"] = True
                    elif "foul" in tpe:
                        stats_found_in_fixture["fouls"] = True
                    elif "yellow" in tpe or "red card" in tpe:
                        stats_found_in_fixture["cards"] = True
                    elif "possession" in tpe:
                        stats_found_in_fixture["possession"] = True
                    elif "passes" in tpe:
                        stats_found_in_fixture["passes"] = True
                    elif "expected_goals" in tpe or "expected goals" in tpe or "xg" == tpe:
                        stats_found_in_fixture["xg"] = True
                    if "shots" in tpe:
                        stats_found_in_fixture["shots"] = True

            if fixture_has_stats:
                report["statistics_found"] += 1
                if stats_found_in_fixture["shots"]:
                    report["shots_found"] += 1
                if stats_found_in_fixture["sot"]:
                    report["sot_found"] += 1
                if stats_found_in_fixture["total_shots"]:
                    report["total_shots_found"] += 1
                if stats_found_in_fixture["blocked_shots"]:
                    report["blocked_shots_found"] += 1
                if stats_found_in_fixture["shots_inside_box"]:
                    report["shots_inside_box_found"] += 1
                if stats_found_in_fixture["shots_outside_box"]:
                    report["shots_outside_box_found"] += 1
                if stats_found_in_fixture["corners"]:
                    report["corners_found"] += 1
                if stats_found_in_fixture["fouls"]:
                    report["fouls_found"] += 1
                if stats_found_in_fixture["cards"]:
                    report["cards_found"] += 1
                if stats_found_in_fixture["possession"]:
                    report["possession_found"] += 1
                if stats_found_in_fixture["passes"]:
                    report["passes_found"] += 1
                if stats_found_in_fixture["xg"]:
                    report["xg_found"] += 1

        if has_stats:
            report["league_states"][alias] = "STATS_USABLE"
        else:
            report["league_states"][alias] = "NO_STATS"

    dates_seen.sort()

    if dates_seen:
        report["earliest_match_date_seen"] = dates_seen[0]
        report["latest_match_date_seen"] = dates_seen[-1]
        report["historical_accessible"] = True

    # Determine final state
    if not report["league_states"]:
        provider_state = "NO_TARGET_LEAGUES"
    elif report["xg_found"] > 0:
        provider_state = "XG_USABLE_CANDIDATE"
    elif (
        report["shots_found"] > 0 or report["total_shots_found"] > 0 or report["corners_found"] > 0
    ):
        if report["shots_found"] > 0 and report["corners_found"] == 0:
            provider_state = "STATS_USABLE"
        else:
            provider_state = "CORNERS_USABLE_CANDIDATE"
    elif report["statistics_found"] > 0:
        provider_state = "STATS_USABLE"
    elif all(state == "NO_FIXTURES" for state in report["league_states"].values()):
        provider_state = "NO_FIXTURES"
    else:
        provider_state = "NO_STATS"

    report["provider_state"] = provider_state
    report["estimated_requests"] = 1 + len(resolved_leagues) + report["fixtures_sampled"]
    report["actual_requests_made"] = requests_made
    report["free_plan_limit_warning"] = requests_made > 50

    return report


def probe_api_football_fixtures(
    conn: sqlite3.Connection,
    api_key: str | None,
    base_url: str,
    league_code: str,
    api_football_league_id: int,
    season: int,
    max_requests: int,
    include_status: bool = False,
    input_json: Path | None = None,
    raw_dir: Path | None = None,
) -> dict[str, Any]:
    
    empty_result = {
        "status": "OK",
        "provider": "api_football",
        "league": league_code,
        "api_football_league_id": api_football_league_id,
        "season": season,
        "api_requests_used": 0,
        "quota_remaining": -1,
        "quota_limit": -1,
        "teams_seen": 0,
        "fixtures_seen": 0,
        "sample_fixture_ids": [],
        "sample_team_names": [],
        "mapped_team_count": 0,
        "unmapped_team_count": 0,
        "unmapped_team_names": [],
        "pagination_detected": False,
        "raw_files_written": [],
        "recommended_provider_role": "NOT_USABLE"
    }

    if not input_json and not api_key:
        return {**empty_result, "status": "AUTH_MISSING"}

    required_requests = 3 if include_status else 2
    if not input_json and max_requests < required_requests:
        return {**empty_result, "status": "MAX_REQUESTS_TOO_LOW"}

    api_requests_used = 0
    quota_remaining = -1
    quota_limit = -1
    pagination_detected = False
    raw_files_written = []

    def save_raw(name: str, data: dict) -> None:
        if raw_dir:
            raw_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            path = raw_dir / f"api_football_{name}_{timestamp}.json"
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            raw_files_written.append(str(path))

    def check_resp(resp_dict: dict) -> tuple[str, list]:
        nonlocal pagination_detected
        errors = resp_dict.get("errors")
        if errors:
            return "API_FOOTBALL_ERROR", []
        
        response = resp_dict.get("response")
        if response is None:
            return "INVALID_RESPONSE", []
            
        paging = resp_dict.get("paging", {})
        if paging.get("total", 0) > 1:
            pagination_detected = True
            
        return "OK", response

    if input_json:
        try:
            data = json.loads(input_json.read_text(encoding="utf-8"))
            teams_data = data.get("teams", {})
            fixtures_data = data.get("fixtures", {})
        except Exception:
            return {**empty_result, "status": "INVALID_RESPONSE"}
    else:
        def fetch(endpoint: str) -> dict | str:
            nonlocal api_requests_used, quota_remaining, quota_limit
            url = f"{base_url.rstrip('/')}{endpoint}"
            req = urllib.request.Request(url, headers={"x-apisports-key": api_key})
            api_requests_used += 1
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    headers = resp.headers
                    rem = headers.get("x-ratelimit-requests-remaining") or headers.get("X-Ratelimit-Remaining")
                    lim = headers.get("x-ratelimit-limit") or headers.get("x-ratelimit-requests-limit")
                    if rem is not None:
                        quota_remaining = int(rem)
                    if lim is not None:
                        quota_limit = int(lim)
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    return "AUTH_FAILED"
                if e.code == 429:
                    return "RATE_LIMITED"
                return "API_ERROR"
            except Exception:
                return "INVALID_RESPONSE"

        if include_status:
            status_data = fetch("/status")
            if isinstance(status_data, str):
                return {**empty_result, "status": status_data, "api_requests_used": api_requests_used}
            save_raw("status", status_data)

        teams_data = fetch(f"/teams?league={api_football_league_id}&season={season}")
        if isinstance(teams_data, str):
            return {**empty_result, "status": teams_data, "api_requests_used": api_requests_used}
        save_raw("teams", teams_data)

        fixtures_data = fetch(f"/fixtures?league={api_football_league_id}&season={season}")
        if isinstance(fixtures_data, str):
            return {**empty_result, "status": fixtures_data, "api_requests_used": api_requests_used}
        save_raw("fixtures", fixtures_data)

    t_status, t_list = check_resp(teams_data)
    if t_status != "OK":
        return {**empty_result, "status": t_status, "api_requests_used": api_requests_used, "quota_remaining": quota_remaining, "quota_limit": quota_limit, "raw_files_written": raw_files_written}

    f_status, f_list = check_resp(fixtures_data)
    if f_status != "OK":
        return {**empty_result, "status": f_status, "api_requests_used": api_requests_used, "quota_remaining": quota_remaining, "quota_limit": quota_limit, "raw_files_written": raw_files_written}

    all_names = set()
    sample_teams = set()
    sample_fixtures = []

    for t_node in t_list:
        name = t_node.get("team", {}).get("name")
        if name:
            all_names.add(name)
            if len(sample_teams) < 3:
                sample_teams.add(name)

    for f_node in f_list:
        sample_fixtures.append(f_node.get("fixture", {}).get("id"))
        t_home = f_node.get("teams", {}).get("home", {}).get("name")
        t_away = f_node.get("teams", {}).get("away", {}).get("name")
        if t_home:
            all_names.add(t_home)
        if t_away:
            all_names.add(t_away)

    mapped_count = 0
    unmapped_names = []
    
    for name in sorted(all_names):
        norm = normalize_key(name)
        row = None
        if norm:
            row = conn.execute("SELECT team_id FROM team_alias WHERE alias_key = ?", (norm,)).fetchone()
        if row:
            mapped_count += 1
        else:
            unmapped_names.append(name)

    return {
        "status": "OK",
        "provider": "api_football",
        "league": league_code,
        "api_football_league_id": api_football_league_id,
        "season": season,
        "api_requests_used": api_requests_used,
        "quota_remaining": quota_remaining,
        "quota_limit": quota_limit,
        "teams_seen": len(t_list),
        "fixtures_seen": len(f_list),
        "sample_fixture_ids": sample_fixtures[:3],
        "sample_team_names": list(sample_teams),
        "mapped_team_count": mapped_count,
        "unmapped_team_count": len(unmapped_names),
        "unmapped_team_names": unmapped_names,
        "pagination_detected": pagination_detected,
        "raw_files_written": raw_files_written,
        "recommended_provider_role": "BACKUP"
    }
