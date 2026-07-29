"""Read-only provider coverage probe for match stats and corners."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from scorerecord.league_config import league_config, league_label


@dataclass(frozen=True)
class ProviderLeagueStatsCoverage:
    provider: str
    league: str
    provider_league_id: str | None
    status: str
    matches_seen: int
    matches_sampled: int
    matches_with_stats: int
    matches_with_corners: int
    corners_coverage_rate: float
    stats_coverage_rate: float
    date_from: str
    date_to: str
    notes: str

    def as_row(self) -> str:
        return (
            f"{self.provider:<12} {self.league:<12} status={self.status:<22} "
            f"sampled={self.matches_sampled:<3} corners={self.matches_with_corners:<3} "
            f"rate={self.corners_coverage_rate:.2%}"
        )


def run_provider_stats_probe(
    *,
    leagues: tuple[str, ...],
    providers: tuple[str, ...],
    sportmonks_key: str | None,
    sportmonks_base_url: str,
    thestatsapi_key: str | None,
    thestatsapi_base_url: str,
    date_from: date | None = None,
    date_to: date | None = None,
    sample_size: int = 12,
    out_path: Path | None = None,
) -> list[ProviderLeagueStatsCoverage]:
    end = date_to or datetime.now(UTC).date()
    start = date_from or (end - timedelta(days=45))
    reports: list[ProviderLeagueStatsCoverage] = []
    for provider in providers:
        normalized_provider = provider.strip().casefold()
        for league in leagues:
            if normalized_provider == "sportmonks":
                reports.append(
                    _probe_sportmonks_league(
                        league=league,
                        api_token=sportmonks_key,
                        base_url=sportmonks_base_url,
                        date_from=start,
                        date_to=end,
                        sample_size=sample_size,
                    )
                )
            elif normalized_provider in {"thestatsapi", "the_stats_api"}:
                reports.append(
                    _probe_thestatsapi_league(
                        league=league,
                        api_key=thestatsapi_key,
                        base_url=thestatsapi_base_url,
                        date_from=start,
                        date_to=end,
                        sample_size=sample_size,
                    )
                )
            else:
                reports.append(
                    ProviderLeagueStatsCoverage(
                        provider=provider,
                        league=league,
                        provider_league_id=None,
                        status="UNSUPPORTED_PROVIDER",
                        matches_seen=0,
                        matches_sampled=0,
                        matches_with_stats=0,
                        matches_with_corners=0,
                        corners_coverage_rate=0.0,
                        stats_coverage_rate=0.0,
                        date_from=start.isoformat(),
                        date_to=end.isoformat(),
                        notes="supported providers: sportmonks, thestatsapi",
                    )
                )
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps([_asdict(report) for report in reports], indent=2, sort_keys=True),
            encoding="utf-8",
        )
    return reports


def _probe_sportmonks_league(
    *,
    league: str,
    api_token: str | None,
    base_url: str,
    date_from: date,
    date_to: date,
    sample_size: int,
) -> ProviderLeagueStatsCoverage:
    if not api_token:
        return _empty_report(
            "sportmonks",
            league,
            "AUTH_MISSING",
            date_from,
            date_to,
            "SPORTMONKS_API_TOKEN is not configured",
        )
    try:
        provider_league_id = _sportmonks_league_id(
            league,
            api_token=api_token,
            base_url=base_url,
        )
        if provider_league_id is None:
            return _empty_report(
                "sportmonks",
                league,
                "LEAGUE_NOT_FOUND",
                date_from,
                date_to,
                "league search returned no usable provider id",
            )
        query = urllib.parse.urlencode(
            {
                "api_token": api_token,
                "include": "statistics;participants;league;state",
                "filters": f"fixtureLeagues:{provider_league_id}",
                "per_page": str(max(1, min(sample_size, 50))),
                "order": "desc",
            }
        )
        payload = _fetch_json(
            f"{base_url.rstrip('/')}/fixtures/between/{date_from.isoformat()}/{date_to.isoformat()}?{query}",
            headers={"User-Agent": "ScoreRecord/1.0"},
        )
    except ProviderProbeError as exc:
        return _empty_report(
            "sportmonks",
            league,
            exc.status,
            date_from,
            date_to,
            exc.message,
        )
    fixtures = _payload_list(payload)
    sampled = fixtures[:sample_size]
    with_stats = sum(1 for fixture in sampled if bool(fixture.get("statistics")))
    with_corners = sum(1 for fixture in sampled if _sportmonks_has_corners(fixture))
    return _coverage_report(
        provider="sportmonks",
        league=league,
        provider_league_id=str(provider_league_id),
        matches_seen=len(fixtures),
        matches_sampled=len(sampled),
        matches_with_stats=with_stats,
        matches_with_corners=with_corners,
        date_from=date_from,
        date_to=date_to,
        notes="fixture statistics include; corners type_id=34",
    )


def _probe_thestatsapi_league(
    *,
    league: str,
    api_key: str | None,
    base_url: str,
    date_from: date,
    date_to: date,
    sample_size: int,
) -> ProviderLeagueStatsCoverage:
    if not api_key:
        return _empty_report(
            "thestatsapi",
            league,
            "AUTH_MISSING",
            date_from,
            date_to,
            "THESTATSAPI_KEY is not configured",
        )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "ScoreRecord/1.0",
    }
    try:
        competition_id = _thestatsapi_competition_id(
            league,
            api_key=api_key,
            base_url=base_url,
        )
        if competition_id is None:
            return _empty_report(
                "thestatsapi",
                league,
                "LEAGUE_NOT_FOUND",
                date_from,
                date_to,
                "competition search returned no usable provider id",
            )
        query = urllib.parse.urlencode(
            {
                "competition_id": competition_id,
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "status": "finished",
                "per_page": str(max(1, min(sample_size, 50))),
            }
        )
        matches_payload = _fetch_json(
            f"{base_url.rstrip('/')}/football/matches?{query}",
            headers=headers,
        )
        matches = _payload_list(matches_payload)
        sampled = matches[:sample_size]
        with_stats = 0
        with_corners = 0
        for match in sampled:
            match_id = match.get("id")
            if not match_id:
                continue
            stats_payload = _fetch_json(
                f"{base_url.rstrip('/')}/football/matches/{urllib.parse.quote(str(match_id))}/stats",
                headers=headers,
            )
            if _payload_has_stats(stats_payload):
                with_stats += 1
            if _thestatsapi_has_corners(stats_payload):
                with_corners += 1
    except ProviderProbeError as exc:
        return _empty_report(
            "thestatsapi",
            league,
            exc.status,
            date_from,
            date_to,
            exc.message,
        )
    return _coverage_report(
        provider="thestatsapi",
        league=league,
        provider_league_id=str(competition_id),
        matches_seen=len(matches),
        matches_sampled=len(sampled),
        matches_with_stats=with_stats,
        matches_with_corners=with_corners,
        date_from=date_from,
        date_to=date_to,
        notes="match stats endpoint; corners detected in stats payload",
    )


def _sportmonks_league_id(
    league: str,
    *,
    api_token: str,
    base_url: str,
) -> str | None:
    messages: list[str] = []
    for search_name in _provider_search_names(league):
        query = urllib.parse.urlencode(
            {"api_token": api_token, "include": "country;currentSeason", "per_page": "10"}
        )
        search = urllib.parse.quote(search_name)
        payload = _fetch_json(
            f"{base_url.rstrip('/')}/leagues/search/{search}?{query}",
            headers={"User-Agent": "ScoreRecord/1.0"},
        )
        rows = _payload_list(payload)
        if rows:
            return _best_provider_id(rows, search_name)
        if isinstance(payload, dict):
            messages.append(str(payload.get("message") or ""))
    if any("subscription" in message.casefold() for message in messages):
        raise ProviderProbeError(
            "PROVIDER_ACCESS_MISSING",
            "league search returned no rows; provider says results may be outside subscription",
        )
    return None


def _thestatsapi_competition_id(
    league: str,
    *,
    api_key: str,
    base_url: str,
) -> str | None:
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    for search_name in _provider_search_names(league):
        query = urllib.parse.urlencode({"search": search_name, "per_page": "10"})
        payload = _fetch_json(
            f"{base_url.rstrip('/')}/football/competitions?{query}",
            headers=headers,
        )
        rows = _payload_list(payload)
        if rows:
            return _best_provider_id(rows, search_name)
    return None


def _provider_search_name(league: str) -> str:
    return _provider_search_names(league)[0]


def _provider_search_names(league: str) -> tuple[str, ...]:
    try:
        config = league_config(league)
    except ValueError:
        return (league_label(league),)
    if config.code == "EPL":
        return ("Premier League", "English Premier League", "EPL")
    if config.code == "MLS":
        return ("Major League Soccer", "MLS")
    if config.code == "LIGA_MX":
        return ("Liga MX", "Mexico Liga MX", "Liga BBVA MX")
    if config.code == "BRASILEIRAO":
        return ("Brasileirao Serie A", "Brazil Serie A", "Serie A Brazil")
    if config.code == "ARGENTINA":
        return ("Argentina Primera", "Liga Profesional Argentina", "Primera Division Argentina")
    return (config.label,)


def _best_provider_id(rows: list[dict[str, Any]], search_name: str) -> str | None:
    if not rows:
        return None
    needle = _norm(search_name)
    ranked = sorted(
        rows,
        key=lambda row: (
            int(needle in _norm(str(row.get("name") or row.get("competition") or ""))),
            int(bool(row.get("active", True))),
        ),
        reverse=True,
    )
    value = ranked[0].get("id")
    return str(value) if value is not None else None


def _sportmonks_has_corners(fixture: dict[str, Any]) -> bool:
    for stat in fixture.get("statistics") or []:
        if not isinstance(stat, dict):
            continue
        if int(stat.get("type_id") or 0) == 34:
            return True
        stat_type = stat.get("type") if isinstance(stat.get("type"), dict) else {}
        code = str(stat_type.get("code") or stat_type.get("developer_name") or "").casefold()
        if "corner" in code:
            return True
    return False


def _thestatsapi_has_corners(payload: Any) -> bool:
    return "corner" in json.dumps(payload, sort_keys=True).casefold()


def _payload_has_stats(payload: Any) -> bool:
    data = payload.get("data") if isinstance(payload, dict) else payload
    if data in (None, [], {}):
        return False
    return True


def _payload_list(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


def _coverage_report(
    *,
    provider: str,
    league: str,
    provider_league_id: str | None,
    matches_seen: int,
    matches_sampled: int,
    matches_with_stats: int,
    matches_with_corners: int,
    date_from: date,
    date_to: date,
    notes: str,
) -> ProviderLeagueStatsCoverage:
    corners_rate = matches_with_corners / matches_sampled if matches_sampled else 0.0
    stats_rate = matches_with_stats / matches_sampled if matches_sampled else 0.0
    if matches_sampled == 0:
        status = "NO_MATCHES"
    elif corners_rate >= 0.80 and matches_sampled >= 5:
        status = "CORNERS_COVERAGE_OK"
    elif corners_rate > 0:
        status = "PARTIAL_CORNERS_COVERAGE"
    elif stats_rate > 0:
        status = "STATS_WITHOUT_CORNERS"
    else:
        status = "NO_STATS"
    return ProviderLeagueStatsCoverage(
        provider=provider,
        league=league,
        provider_league_id=provider_league_id,
        status=status,
        matches_seen=matches_seen,
        matches_sampled=matches_sampled,
        matches_with_stats=matches_with_stats,
        matches_with_corners=matches_with_corners,
        corners_coverage_rate=corners_rate,
        stats_coverage_rate=stats_rate,
        date_from=date_from.isoformat(),
        date_to=date_to.isoformat(),
        notes=notes,
    )


def _empty_report(
    provider: str,
    league: str,
    status: str,
    date_from: date,
    date_to: date,
    notes: str,
) -> ProviderLeagueStatsCoverage:
    return ProviderLeagueStatsCoverage(
        provider=provider,
        league=league,
        provider_league_id=None,
        status=status,
        matches_seen=0,
        matches_sampled=0,
        matches_with_stats=0,
        matches_with_corners=0,
        corners_coverage_rate=0.0,
        stats_coverage_rate=0.0,
        date_from=date_from.isoformat(),
        date_to=date_to.isoformat(),
        notes=notes,
    )


class ProviderProbeError(Exception):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _fetch_json(url: str, *, headers: dict[str, str]) -> Any:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ProviderProbeError("AUTH_FAILED", f"HTTP {exc.code}")
        if exc.code == 429:
            raise ProviderProbeError("RATE_LIMITED", "HTTP 429")
        raise ProviderProbeError("API_ERROR", f"HTTP {exc.code}")
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderProbeError("INVALID_RESPONSE", str(exc))


def _asdict(report: ProviderLeagueStatsCoverage) -> dict[str, Any]:
    return {
        "provider": report.provider,
        "league": report.league,
        "provider_league_id": report.provider_league_id,
        "status": report.status,
        "matches_seen": report.matches_seen,
        "matches_sampled": report.matches_sampled,
        "matches_with_stats": report.matches_with_stats,
        "matches_with_corners": report.matches_with_corners,
        "corners_coverage_rate": report.corners_coverage_rate,
        "stats_coverage_rate": report.stats_coverage_rate,
        "date_from": report.date_from,
        "date_to": report.date_to,
        "notes": report.notes,
    }


def _norm(value: str) -> str:
    return "".join(ch for ch in value.casefold() if ch.isalnum())
