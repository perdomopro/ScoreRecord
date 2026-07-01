"""Odds ingestion for Odds-API.io.

The module stores raw bookmaker prices only. De-vig, edge, and recommendations
are intentionally handled in later steps so each layer stays auditable.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scorerecord.aliases import normalize_key
from scorerecord.runs import ingest_run, record_quality
from scorerecord.time_utils import parse_datetime_utc, parse_kickoff_utc


API_MARKET_MAP = {
    "h2h": "1X2",
    "ml": "1X2",
    "moneyline": "1X2",
    "match result": "1X2",
    "totals": "OU",
    "over/under": "OU",
    "total goals": "OU",
    "both_teams_to_score": "BTTS",
    "both teams to score": "BTTS",
    "btts": "BTTS",
}

DEFAULT_MIN_INTERVAL_SECONDS = 36.0
DEFAULT_MAX_REQUESTS = 90


@dataclass(frozen=True)
class OddsFetchSummary:
    fixtures_seen: int
    matched_events: int
    missing_fixture: int
    rows_inserted: int
    snapshot_rows_inserted: int
    api_requests_used: int
    bookmakers_seen: int
    markets_seen: int
    dry_run: bool

    def as_row(self) -> str:
        return (
            f"fixtures_seen={self.fixtures_seen} "
            f"matched_events={self.matched_events} "
            f"missing_fixture={self.missing_fixture} "
            f"rows_inserted={self.rows_inserted} "
            f"snapshot_rows_inserted={self.snapshot_rows_inserted} "
            f"api_requests_used={self.api_requests_used} "
            f"bookmakers_seen={self.bookmakers_seen} "
            f"markets_seen={self.markets_seen} "
            f"dry_run={self.dry_run}"
        )


@dataclass(frozen=True)
class OddsRow:
    fixture_id: str
    bookmaker: str
    market: str
    selection: str
    decimal_odds: float
    captured_utc: str
    is_closing: int
    source_event_id: str | None = None
    commence_time: str | None = None
    raw_json: str | None = None


@dataclass(frozen=True)
class OddsSnapshotCoverageRow:
    fixture_id: str
    home_name: str
    away_name: str
    kickoff_utc: str
    total_snapshots: int
    unique_bookmakers: int
    unique_markets: int
    first_snapshot_at: str
    last_snapshot_at: str

    def as_row(self) -> str:
        return (
            f"fixture_id={self.fixture_id} "
            f"match={self.home_name} vs {self.away_name} "
            f"kickoff_utc={self.kickoff_utc} "
            f"total_snapshots={self.total_snapshots} "
            f"unique_bookmakers={self.unique_bookmakers} "
            f"unique_markets={self.unique_markets} "
            f"first_snapshot_at={self.first_snapshot_at} "
            f"last_snapshot_at={self.last_snapshot_at}"
        )


def fetch_live_odds(
    conn: sqlite3.Connection,
    *,
    api_key: str | None,
    base_url: str = "https://api.odds-api.io/v3",
    sport: str = "soccer",
    league: str | None = "international-fifa-world-cup",
    markets: str = "h2h,totals,btts",
    bookmakers: str = "Bet365,Unibet",
    fixture_id: str | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    dry_run: bool = False,
    is_closing: bool = False,
    closing_window_minutes: int | None = 60,
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
    cwd: Path | None = None,
) -> OddsFetchSummary:
    fixtures = _candidate_fixtures(
        conn,
        fixture_id=fixture_id,
        closing_window_minutes=closing_window_minutes if is_closing else None,
    )
    if dry_run:
        planned_odds_requests = min(len(fixtures), max_requests)
        planned_requests = planned_odds_requests + (1 if fixtures else 0)
        return OddsFetchSummary(
            fixtures_seen=len(fixtures),
            matched_events=0,
            missing_fixture=0,
            rows_inserted=0,
            snapshot_rows_inserted=0,
            api_requests_used=planned_requests,
            bookmakers_seen=0,
            markets_seen=0,
            dry_run=True,
        )
    if not api_key:
        raise ValueError("ODDS_API_IO_KEY is required for fetch-odds without --dry-run")

    requested_markets = _requested_markets(markets)
    with ingest_run(conn, "fetch-odds", source="odds-api.io", cwd=cwd) as run_id:
        event_index, event_requests = _fetch_event_index(
            api_key=api_key,
            base_url=base_url,
            sport=sport,
            league=league,
            fixtures=fixtures,
        )
        rows_inserted = 0
        snapshot_rows_inserted = 0
        api_requests_used = event_requests
        missing_fixture = 0
        matched_events = 0
        bookmakers_seen: set[str] = set()
        markets_seen: set[str] = set()

        for fixture in fixtures[:max_requests]:
            event_id = _match_event_id(fixture, event_index)
            if event_id is None:
                missing_fixture += 1
                continue
            matched_events += 1
            payload = _fetch_odds_payload(
                api_key=api_key,
                base_url=base_url,
                event_id=event_id,
                bookmakers=bookmakers,
            )
            api_requests_used += 1
            captured_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
            parsed_rows = parse_odds_payload(
                payload,
                fixture_id=str(fixture["fixture_id"]),
                captured_utc=captured_utc,
                is_closing=False,
                requested_markets=requested_markets,
            )
            for row in parsed_rows:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO odds (
                      fixture_id, bookmaker, market, selection,
                      decimal_odds, captured_utc, is_closing
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row.fixture_id,
                        row.bookmaker,
                        row.market,
                        row.selection,
                        row.decimal_odds,
                        row.captured_utc,
                        row.is_closing,
                    ),
                )
                rows_inserted += cursor.rowcount
                snapshot_rows_inserted += _insert_odds_snapshot(conn, row)
                bookmakers_seen.add(row.bookmaker)
                markets_seen.add(row.market)
            conn.commit()
            if min_interval_seconds > 0 and api_requests_used < max_requests:
                time.sleep(min_interval_seconds)

        summary = OddsFetchSummary(
            fixtures_seen=len(fixtures),
            matched_events=matched_events,
            missing_fixture=missing_fixture,
            rows_inserted=rows_inserted,
            snapshot_rows_inserted=snapshot_rows_inserted,
            api_requests_used=api_requests_used,
            bookmakers_seen=len(bookmakers_seen),
            markets_seen=len(markets_seen),
            dry_run=False,
        )
        _record_odds_quality(conn, run_id, summary, bookmakers_seen, markets_seen)
        _record_snapshot_coverage_quality(conn, run_id)
        conn.commit()
    return summary


def odds_snapshot_coverage_report(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
) -> list[OddsSnapshotCoverageRow]:
    rows = conn.execute(
        """
        SELECT
          os.fixture_id,
          COALESCE(ht.canonical, '') AS home_name,
          COALESCE(at.canonical, '') AS away_name,
          COALESCE(f.kickoff_utc, '') AS kickoff_utc,
          COUNT(*) AS total_snapshots,
          COUNT(DISTINCT os.bookmaker) AS unique_bookmakers,
          COUNT(DISTINCT os.market) AS unique_markets,
          MIN(os.snapshot_at) AS first_snapshot_at,
          MAX(os.snapshot_at) AS last_snapshot_at
        FROM odds_snapshots os
        LEFT JOIN fixture f ON f.fixture_id = os.fixture_id
        LEFT JOIN team ht ON ht.team_id = f.home_id
        LEFT JOIN team at ON at.team_id = f.away_id
        GROUP BY os.fixture_id
        ORDER BY last_snapshot_at DESC, os.fixture_id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        OddsSnapshotCoverageRow(
            fixture_id=str(row["fixture_id"]),
            home_name=str(row["home_name"]),
            away_name=str(row["away_name"]),
            kickoff_utc=str(row["kickoff_utc"]),
            total_snapshots=int(row["total_snapshots"]),
            unique_bookmakers=int(row["unique_bookmakers"]),
            unique_markets=int(row["unique_markets"]),
            first_snapshot_at=str(row["first_snapshot_at"]),
            last_snapshot_at=str(row["last_snapshot_at"]),
        )
        for row in rows
    ]


def _insert_odds_snapshot(conn: sqlite3.Connection, row: OddsRow) -> int:
    cursor = conn.execute(
        """
        INSERT INTO odds_snapshots (
          event_id, fixture_id, bookmaker, market, selection, line,
          decimal_odds, snapshot_at, commence_time, source, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row.source_event_id or row.fixture_id,
            row.fixture_id,
            row.bookmaker,
            row.market,
            row.selection,
            _market_line(row.market),
            row.decimal_odds,
            row.captured_utc,
            row.commence_time,
            "odds-api.io",
            row.raw_json,
        ),
    )
    return cursor.rowcount


def parse_odds_payload(
    payload: Any,
    *,
    fixture_id: str,
    captured_utc: str,
    is_closing: bool,
    requested_markets: set[str] | None = None,
) -> list[OddsRow]:
    requested = requested_markets or {"1X2", "OU", "BTTS"}
    events = payload if isinstance(payload, list) else [payload]
    rows: list[OddsRow] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        source_event_id = str(
            event.get("id") or event.get("eventId") or event.get("event_id") or ""
        )
        commence_time = str(
            event.get("date")
            or event.get("startTime")
            or event.get("commence_time")
            or ""
        )
        home_name = str(event.get("home") or event.get("home_team") or "home")
        away_name = str(event.get("away") or event.get("away_team") or "away")
        bookmakers = event.get("bookmakers") or {}
        if isinstance(bookmakers, dict):
            bookmaker_items = bookmakers.items()
        elif isinstance(bookmakers, list):
            bookmaker_items = (
                (
                    book.get("key") or book.get("name") or book.get("title"),
                    book.get("markets") or book,
                )
                for book in bookmakers
                if isinstance(book, dict)
            )
        else:
            bookmaker_items = []
        for bookmaker, market_entries in bookmaker_items:
            if not bookmaker:
                continue
            if isinstance(market_entries, dict):
                market_entries = market_entries.get("markets") or market_entries.get("odds") or []
            for market_entry in _as_list(market_entries):
                if not isinstance(market_entry, dict):
                    continue
                market_name = str(
                    market_entry.get("name")
                    or market_entry.get("key")
                    or market_entry.get("market")
                    or ""
                )
                market_key = _market_key(market_name)
                if market_key is None:
                    continue
                if market_key == "1X2" and "1X2" not in requested:
                    continue
                if market_key == "OU" and "OU" not in requested:
                    continue
                if market_key == "BTTS" and "BTTS" not in requested:
                    continue
                updated_at = str(market_entry.get("updatedAt") or captured_utc)
                for odds_entry in _as_list(
                    market_entry.get("odds") or market_entry.get("outcomes") or market_entry
                ):
                    if not isinstance(odds_entry, dict):
                        continue
                    rows.extend(
                        _parse_market_entry(
                            odds_entry,
                            fixture_id=fixture_id,
                            bookmaker=str(bookmaker),
                            market_key=market_key,
                            home_name=home_name,
                            away_name=away_name,
                            captured_utc=updated_at,
                            is_closing=is_closing,
                            source_event_id=source_event_id or None,
                            commence_time=commence_time or None,
                            raw_json=json.dumps(odds_entry, sort_keys=True),
                        )
                    )
    return rows


def _parse_market_entry(
    odds_entry: dict[str, Any],
    *,
    fixture_id: str,
    bookmaker: str,
    market_key: str,
    home_name: str,
    away_name: str,
    captured_utc: str,
    is_closing: bool,
    source_event_id: str | None = None,
    commence_time: str | None = None,
    raw_json: str | None = None,
) -> list[OddsRow]:
    rows: list[OddsRow] = []
    if market_key == "1X2":
        mapping = {
            "home": odds_entry.get("home"),
            "draw": odds_entry.get("draw"),
            "away": odds_entry.get("away"),
        }
        if not any(value is not None for value in mapping.values()):
            mapping = _outcome_mapping(odds_entry, home_name=home_name, away_name=away_name)
        for selection, value in mapping.items():
            price = _decimal(value)
            if price:
                rows.append(
                    OddsRow(
                        fixture_id,
                        bookmaker,
                        "1X2",
                        selection,
                        price,
                        captured_utc,
                        int(is_closing),
                        source_event_id,
                        commence_time,
                        raw_json,
                    )
                )
        return rows

    if market_key == "OU":
        line = _decimal(
            odds_entry.get("max")
            or odds_entry.get("line")
            or odds_entry.get("point")
            or odds_entry.get("total")
        )
        if line is None:
            return rows
        market = f"OU{line:g}"
        for selection in ("over", "under"):
            price = _decimal(odds_entry.get(selection))
            if price:
                rows.append(
                    OddsRow(
                        fixture_id,
                        bookmaker,
                        market,
                        selection,
                        price,
                        captured_utc,
                        int(is_closing),
                        source_event_id,
                        commence_time,
                        raw_json,
                    )
                )
        return rows

    if market_key == "BTTS":
        mapping = {
            "yes": odds_entry.get("yes") or odds_entry.get("Yes"),
            "no": odds_entry.get("no") or odds_entry.get("No"),
        }
        if not any(value is not None for value in mapping.values()):
            mapping = _outcome_mapping(odds_entry, home_name="yes", away_name="no")
        for selection, value in mapping.items():
            if selection not in {"yes", "no"}:
                continue
            price = _decimal(value)
            if price:
                rows.append(
                    OddsRow(
                        fixture_id,
                        bookmaker,
                        "BTTS",
                        selection,
                        price,
                        captured_utc,
                        int(is_closing),
                        source_event_id,
                        commence_time,
                        raw_json,
                    )
                )
        return rows

    return rows


def _candidate_fixtures(
    conn: sqlite3.Connection,
    *,
    fixture_id: str | None,
    closing_window_minutes: int | None,
) -> list[sqlite3.Row]:
    filter_sql = ""
    params: tuple[object, ...] = ()
    if fixture_id:
        filter_sql = "AND f.fixture_id = ?"
        params = (fixture_id,)
    rows = conn.execute(
        f"""
        SELECT
          f.fixture_id, f.kickoff_local, f.kickoff_utc,
          ht.canonical AS home_name, at.canonical AS away_name
        FROM fixture f
        JOIN team ht ON ht.team_id = f.home_id
        JOIN team at ON at.team_id = f.away_id
        WHERE f.home_id IS NOT NULL
          AND f.away_id IS NOT NULL
          AND f.home_score IS NULL
          AND f.away_score IS NULL
          {filter_sql}
        ORDER BY f.kickoff_local, f.fixture_id
        """,
        params,
    ).fetchall()
    now_utc = datetime.now(timezone.utc)
    closing_deadline = (
        now_utc + timedelta(minutes=closing_window_minutes)
        if closing_window_minutes is not None and fixture_id is None
        else None
    )
    candidates = []
    for row in rows:
        kickoff_utc = _row_kickoff_utc(row)
        if kickoff_utc is not None and kickoff_utc <= now_utc:
            continue
        if closing_deadline is not None:
            if kickoff_utc is None or kickoff_utc > closing_deadline:
                continue
        candidates.append(row)
    return candidates


def _fetch_event_index(
    *,
    api_key: str,
    base_url: str,
    sport: str,
    league: str | None,
    fixtures: list[sqlite3.Row],
) -> tuple[dict[tuple[str, str, str], str], int]:
    if not fixtures:
        return {}, 0
    dates = [_fixture_utc_date(row) for row in fixtures]
    earliest = min(date for date in dates if date)
    latest = max(date for date in dates if date)
    params = {
        "apiKey": api_key,
        "sport": _sport_slug(sport),
        "limit": str(max(100, min(5000, len(fixtures) * 8))),
    }
    if earliest:
        params["from"] = f"{earliest}T00:00:00Z"
    if latest:
        params["to"] = f"{latest}T23:59:59Z"
    if league:
        params["league"] = league
    payload = _get_json(f"{base_url.rstrip('/')}/events", params)
    events = payload.get("data") if isinstance(payload, dict) else payload
    index: dict[tuple[str, str, str], str] = {}
    for event in _as_list(events):
        if not isinstance(event, dict):
            continue
        home = normalize_key(str(event.get("home") or event.get("home_team") or ""))
        away = normalize_key(str(event.get("away") or event.get("away_team") or ""))
        event_date = str(
            event.get("date") or event.get("startTime") or event.get("commence_time") or ""
        )[:10]
        event_id = event.get("id") or event.get("eventId") or event.get("event_id")
        if home and away and event_date and event_id:
            index[(event_date, home, away)] = str(event_id)
    return index, 1


def _match_event_id(fixture: sqlite3.Row, index: dict[tuple[str, str, str], str]) -> str | None:
    fixture_date = _fixture_utc_date(fixture)
    home = normalize_key(str(fixture["home_name"]))
    away = normalize_key(str(fixture["away_name"]))
    return index.get((fixture_date, home, away)) or index.get((fixture_date, away, home))


def _fixture_utc_date(row: sqlite3.Row) -> str:
    kickoff = _row_kickoff_utc(row)
    if kickoff is not None:
        return kickoff.date().isoformat()
    return str(row["kickoff_local"] or row["kickoff_utc"] or "")[:10]


def _row_kickoff_utc(row: sqlite3.Row) -> datetime | None:
    return parse_kickoff_utc(kickoff_utc=row["kickoff_utc"], kickoff_local=row["kickoff_local"])


def _parse_datetime(value: str) -> datetime | None:
    return parse_datetime_utc(value)


def _fetch_odds_payload(
    *,
    api_key: str,
    base_url: str,
    event_id: str,
    bookmakers: str,
) -> Any:
    params = {
        "apiKey": api_key,
        "eventId": event_id,
        "bookmakers": bookmakers,
    }
    return _get_json(f"{base_url.rstrip('/')}/odds", params)


def _get_json(url: str, params: dict[str, str]) -> Any:
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(full_url, headers={"User-Agent": "ScoreRecord/0.1"})
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:  # pragma: no cover
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # pragma: no cover - network dependent
            last_error = exc
            time.sleep(2**attempt)
    raise RuntimeError(f"Could not fetch Odds-API.io endpoint {url}: {last_error}")


def _record_odds_quality(
    conn: sqlite3.Connection,
    run_id: int,
    summary: OddsFetchSummary,
    bookmakers_seen: set[str],
    markets_seen: set[str],
) -> None:
    details = {
        "fixtures_seen": summary.fixtures_seen,
        "matched_events": summary.matched_events,
        "missing_fixture": summary.missing_fixture,
        "rows_inserted": summary.rows_inserted,
        "snapshot_rows_inserted": summary.snapshot_rows_inserted,
        "api_requests_used": summary.api_requests_used,
        "bookmakers_seen": sorted(bookmakers_seen),
        "markets_seen": sorted(markets_seen),
        "closing_semantics": "raw capture only; closing is derived later from odds_snapshots",
    }
    for metric_name, value in [
        ("odds_fixtures_seen", summary.fixtures_seen),
        ("odds_matched_events", summary.matched_events),
        ("odds_missing_fixture", summary.missing_fixture),
        ("odds_rows_inserted", summary.rows_inserted),
        ("odds_snapshot_rows_inserted", summary.snapshot_rows_inserted),
        ("api_requests_used", summary.api_requests_used),
        ("bookmakers_seen", summary.bookmakers_seen),
        ("markets_seen", summary.markets_seen),
    ]:
        record_quality(
            conn,
            run_id,
            "odds_ingest",
            metric_name,
            float(value),
            json.dumps(details, sort_keys=True),
        )


def _record_snapshot_coverage_quality(conn: sqlite3.Connection, run_id: int) -> None:
    rows = odds_snapshot_coverage_report(conn, limit=100000)
    total_snapshots = sum(row.total_snapshots for row in rows)
    avg_snapshots = total_snapshots / len(rows) if rows else 0.0
    max_snapshots = max((row.total_snapshots for row in rows), default=0)
    fixtures_with_3plus = sum(1 for row in rows if row.total_snapshots >= 3)
    details = {
        "fixtures_with_snapshots": len(rows),
        "total_snapshots": total_snapshots,
        "avg_snapshots_per_fixture": avg_snapshots,
        "max_snapshots_per_fixture": max_snapshots,
        "fixtures_with_3plus_snapshots": fixtures_with_3plus,
        "recent_fixture_rows": [row.__dict__ for row in rows[:25]],
    }
    for metric_name, value in [
        ("snapshot_fixtures_with_rows", len(rows)),
        ("snapshot_total_rows", total_snapshots),
        ("snapshot_avg_rows_per_fixture", avg_snapshots),
        ("snapshot_max_rows_per_fixture", max_snapshots),
        ("snapshot_fixtures_with_3plus", fixtures_with_3plus),
    ]:
        record_quality(
            conn,
            run_id,
            "odds_snapshot_coverage",
            metric_name,
            float(value),
            json.dumps(details, sort_keys=True),
        )


def _requested_markets(markets: str) -> set[str]:
    requested = set()
    for raw_market in markets.split(","):
        market = raw_market.strip()
        mapped = _market_key(market)
        if mapped:
            requested.add(mapped)
        elif market.startswith("OU"):
            requested.add("OU")
        elif market in {"1X2", "BTTS"}:
            requested.add(market)
    return requested or {"1X2", "OU", "BTTS"}


def _market_key(name: str) -> str | None:
    normalized = name.strip().lower().replace("_", " ")
    return API_MARKET_MAP.get(normalized) or API_MARKET_MAP.get(name.strip().lower())


def _outcome_mapping(
    odds_entry: dict[str, Any],
    *,
    home_name: str,
    away_name: str,
) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    outcomes = odds_entry.get("outcomes")
    if not isinstance(outcomes, list):
        return mapping
    home_key = normalize_key(home_name)
    away_key = normalize_key(away_name)
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue
        name = str(outcome.get("name") or outcome.get("selection") or "")
        key = normalize_key(name)
        value = outcome.get("price") or outcome.get("odds") or outcome.get("decimal_odds")
        if key == home_key:
            mapping["home"] = value
        elif key == away_key:
            mapping["away"] = value
        elif key in {"draw", "tie", "x"}:
            mapping["draw"] = value
        elif key in {"yes", "y"}:
            mapping["yes"] = value
        elif key in {"no", "n"}:
            mapping["no"] = value
    return mapping


def _decimal(value: Any) -> float | None:
    if value is None:
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if price <= 1.0:
        return None
    return price


def _market_line(market: str) -> float | None:
    if not market.startswith("OU"):
        return None
    try:
        return float(market[2:])
    except ValueError:
        return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _sport_slug(sport: str) -> str:
    if sport.lower() in {"soccer", "football"}:
        return "football"
    return sport
