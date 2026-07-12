"""Read-only feasibility probes for external sports data APIs."""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field

BIGBALLS_BASE_URL = "https://api.bigballsdata.com"


@dataclass(frozen=True)
class ProviderProbeReport:
    provider: str
    leagues_requested: tuple[str, ...]
    provider_state: str
    earliest_match_date_seen: str | None
    latest_match_date_seen: str | None
    historical_accessible: bool
    xg_status: str
    matches_sampled: int
    matches_with_shots: int
    matches_with_sot: int
    matches_with_possession: int
    matches_with_passes: int
    matches_with_native_xg: int
    league_states: dict[str, str] = field(default_factory=dict)


def probe_bigballs_api(
    leagues: tuple[str, ...],
    sample_size: int = 20,
) -> ProviderProbeReport:
    """Probe the BigBalls Sports Data API for xG and shots feasibility."""
    api_key = os.environ.get("BIGBALLS_API_KEY")
    if not api_key:
        return ProviderProbeReport(
            provider="bigballs",
            leagues_requested=leagues,
            provider_state="NO_ACCESS",
            earliest_match_date_seen=None,
            latest_match_date_seen=None,
            historical_accessible=False,
            xg_status="missing",
            matches_sampled=0,
            matches_with_shots=0,
            matches_with_sot=0,
            matches_with_possession=0,
            matches_with_passes=0,
            matches_with_native_xg=0,
            league_states={},
        )

    def _fetch(path: str) -> dict | list:
        url = f"{BIGBALLS_BASE_URL}{path}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise PermissionError(f"HTTP {e.code} on {path}")
            return {"error": f"HTTP {e.code}"}
        except Exception as e:
            return {"error": str(e)}

    try:
        leagues_resp = _fetch("/v1/leagues?sport=football")
    except PermissionError:
        return ProviderProbeReport(
            provider="bigballs",
            leagues_requested=leagues,
            provider_state="AUTH_FAILED",
            earliest_match_date_seen=None,
            latest_match_date_seen=None,
            historical_accessible=False,
            xg_status="missing",
            matches_sampled=0,
            matches_with_shots=0,
            matches_with_sot=0,
            matches_with_possession=0,
            matches_with_passes=0,
            matches_with_native_xg=0,
            league_states={},
        )
        
    if isinstance(leagues_resp, dict) and "error" in leagues_resp:
        return ProviderProbeReport(
            provider="bigballs",
            leagues_requested=leagues,
            provider_state="UNEXPECTED_SCHEMA",
            earliest_match_date_seen=None,
            latest_match_date_seen=None,
            historical_accessible=False,
            xg_status="missing",
            matches_sampled=0,
            matches_with_shots=0,
            matches_with_sot=0,
            matches_with_possession=0,
            matches_with_passes=0,
            matches_with_native_xg=0,
            league_states={"error": leagues_resp["error"]},
        )

    provider_leagues = leagues_resp.get("data", []) if isinstance(leagues_resp, dict) else leagues_resp
    if not provider_leagues:
        return ProviderProbeReport(
            provider="bigballs",
            leagues_requested=leagues,
            provider_state="EMPTY_LEAGUES",
            earliest_match_date_seen=None,
            latest_match_date_seen=None,
            historical_accessible=False,
            xg_status="missing",
            matches_sampled=0,
            matches_with_shots=0,
            matches_with_sot=0,
            matches_with_possession=0,
            matches_with_passes=0,
            matches_with_native_xg=0,
            league_states={},
        )

    provider_league_keys = []
    for l in provider_leagues:
        if isinstance(l, dict) and "key" in l:
            provider_league_keys.append(l["key"])
        elif isinstance(l, str):
            provider_league_keys.append(l)

    target_league_keys = []
    for req_league in leagues:
        req_norm = req_league.lower()
        matched = False
        for pl in provider_league_keys:
            if req_norm in pl.lower() or pl.lower() in req_norm:
                target_league_keys.append(pl)
                matched = True
                break

    if not target_league_keys:
        return ProviderProbeReport(
            provider="bigballs",
            leagues_requested=leagues,
            provider_state="NO_TARGET_LEAGUES",
            earliest_match_date_seen=None,
            latest_match_date_seen=None,
            historical_accessible=False,
            xg_status="missing",
            matches_sampled=0,
            matches_with_shots=0,
            matches_with_sot=0,
            matches_with_possession=0,
            matches_with_passes=0,
            matches_with_native_xg=0,
            league_states={},
        )

    sampled_match_ids = []
    match_dates = []

    for l_key in target_league_keys:
        matches_resp = _fetch(f"/v1/matches?sport=football&league={l_key}&limit={sample_size}")
        m_data = matches_resp.get("data", []) if isinstance(matches_resp, dict) else matches_resp
        for m in m_data:
            if isinstance(m, dict) and "id" in m:
                sampled_match_ids.append(m["id"])
                if "kickoff_utc" in m:
                    match_dates.append(m["kickoff_utc"])
            if len(sampled_match_ids) >= sample_size:
                break
        if len(sampled_match_ids) >= sample_size:
            break

    if not sampled_match_ids:
        return ProviderProbeReport(
            provider="bigballs",
            leagues_requested=leagues,
            provider_state="NO_MATCHES",
            earliest_match_date_seen=None,
            latest_match_date_seen=None,
            historical_accessible=False,
            xg_status="missing",
            matches_sampled=0,
            matches_with_shots=0,
            matches_with_sot=0,
            matches_with_possession=0,
            matches_with_passes=0,
            matches_with_native_xg=0,
            league_states={},
        )

    shots_count = 0
    sot_count = 0
    poss_count = 0
    passes_count = 0
    xg_count = 0

    xg_shape = "missing"

    for mid in sampled_match_ids:
        stats_resp = _fetch(f"/v1/stored/matches/{mid}/stats")
        s_data = stats_resp.get("data", {}) if isinstance(stats_resp, dict) else stats_resp
        
        has_shots = False
        has_sot = False
        has_poss = False
        has_passes = False
        has_xg = False

        if isinstance(s_data, dict):
            # Check common keys
            keys_str = str(s_data).lower()
            if "shots" in keys_str or "total_shots" in keys_str:
                has_shots = True
            if "shots_on_target" in keys_str or "shots_on_goal" in keys_str:
                has_sot = True
            if "possession" in keys_str:
                has_poss = True
            if "passes" in keys_str:
                has_passes = True
            if "xg" in keys_str or "expected_goals" in keys_str:
                has_xg = True

                if "players" in keys_str and "xg" in keys_str:
                    xg_shape = "player-level"
                elif "home" in keys_str and "away" in keys_str:
                    xg_shape = "team-level"
                else:
                    xg_shape = "unknown shape"

        if has_shots: shots_count += 1
        if has_sot: sot_count += 1
        if has_poss: poss_count += 1
        if has_passes: passes_count += 1
        if has_xg: xg_count += 1

    if match_dates:
        earliest = min(match_dates)
        latest = max(match_dates)
        import datetime
        now = datetime.datetime.now(datetime.UTC)
        try:
            earliest_dt = datetime.datetime.fromisoformat(earliest.replace("Z", "+00:00"))
            historical_accessible = (now - earliest_dt).days > 30
        except Exception:
            historical_accessible = False
    else:
        earliest = None
        latest = None
        historical_accessible = False

    state = "NO_STATS"
    if xg_count > 0 and shots_count > 0:
        state = "XG_USABLE_CANDIDATE"
    elif xg_count > 0:
        state = "PARTIAL_XG"
    elif shots_count > 0:
        state = "SHOTS_ONLY"

    return ProviderProbeReport(
        provider="bigballs",
        leagues_requested=leagues,
        provider_state=state,
        earliest_match_date_seen=earliest,
        latest_match_date_seen=latest,
        historical_accessible=historical_accessible,
        xg_status=xg_shape,
        matches_sampled=len(sampled_match_ids),
        matches_with_shots=shots_count,
        matches_with_sot=sot_count,
        matches_with_possession=poss_count,
        matches_with_passes=passes_count,
        matches_with_native_xg=xg_count,
        league_states={l: "sampled" for l in target_league_keys},
    )
