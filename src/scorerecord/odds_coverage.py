from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from scorerecord.time_utils import parse_datetime_utc, parse_kickoff_utc

STATUS_NO_ODDS = "NO_ODDS"
STATUS_INCOMPLETE_MARKET = "INCOMPLETE_MARKET"
STATUS_STALE_ODDS = "STALE_ODDS"
STATUS_COVERAGE_OK = "COVERAGE_OK"

SUPPORTED_MARKETS = ("OU2.5", "BTTS")
_EXPECTED_SELECTIONS = {
    "OU2.5": ("over", "under"),
    "BTTS": ("yes", "no"),
}
_MARKET_LINE = {
    "OU2.5": 2.5,
    "BTTS": None,
}


@dataclass(frozen=True)
class OddsSideCoverage:
    selection: str
    best_odds: float | None
    bookmaker: str | None
    captured_utc: str | None


@dataclass(frozen=True)
class OddsMarketCoverage:
    fixture_id: str
    market: str
    line: float | None
    status: str
    latest_captured_utc: str | None
    age_minutes: float | None
    bookmaker_count: int
    complete_bookmaker_count: int
    sides: list[OddsSideCoverage]
    details: str


def load_fixture_odds_coverage(
    conn: sqlite3.Connection,
    fixture_id: str,
    as_of_utc: datetime | None = None,
    stale_minutes: float = 90.0,
) -> list[OddsMarketCoverage]:
    """Return read-only odds coverage for the forecast GUI.

    This function only reports whether captured bookmaker rows exist and whether
    they form complete two-way markets. It deliberately avoids de-vig, EV, stake,
    or recommendation calculations.
    """

    as_of = (as_of_utc or datetime.now(UTC)).astimezone(UTC)
    kickoff = _fixture_kickoff_utc(conn, fixture_id)
    fixture_started = bool(kickoff and as_of >= kickoff)
    return [
        _market_coverage(
            conn,
            fixture_id=fixture_id,
            market=market,
            kickoff_utc=kickoff,
            fixture_started=fixture_started,
            as_of_utc=as_of,
            stale_minutes=stale_minutes,
        )
        for market in SUPPORTED_MARKETS
    ]


def _fixture_kickoff_utc(conn: sqlite3.Connection, fixture_id: str) -> datetime | None:
    row = conn.execute(
        """
        SELECT kickoff_utc, kickoff_local
        FROM fixture
        WHERE fixture_id = ?
        """,
        (fixture_id,),
    ).fetchone()
    if row is None:
        return None
    return parse_kickoff_utc(
        kickoff_utc=row["kickoff_utc"],
        kickoff_local=row["kickoff_local"],
    )


def _market_coverage(
    conn: sqlite3.Connection,
    *,
    fixture_id: str,
    market: str,
    kickoff_utc: datetime | None,
    fixture_started: bool,
    as_of_utc: datetime,
    stale_minutes: float,
) -> OddsMarketCoverage:
    rows = _odds_rows(conn, fixture_id=fixture_id, market=market, kickoff_utc=kickoff_utc)
    sides = _side_coverage(market, rows)
    latest_captured_utc = _latest_snapshot_at(rows)
    age_minutes = _age_minutes(latest_captured_utc, as_of_utc)
    bookmaker_count = len({str(row["bookmaker"]) for row in rows})
    complete_bookmakers = _complete_bookmakers(market, rows)
    complete_bookmaker_count = len(complete_bookmakers)
    status = _status(
        rows=rows,
        complete_bookmaker_count=complete_bookmaker_count,
        fixture_started=fixture_started,
        age_minutes=age_minutes,
        stale_minutes=stale_minutes,
    )
    details = _details(status)
    return OddsMarketCoverage(
        fixture_id=fixture_id,
        market=market,
        line=_MARKET_LINE[market],
        status=status,
        latest_captured_utc=latest_captured_utc,
        age_minutes=age_minutes,
        bookmaker_count=bookmaker_count,
        complete_bookmaker_count=complete_bookmaker_count,
        sides=sides,
        details=details,
    )


def _odds_rows(
    conn: sqlite3.Connection,
    *,
    fixture_id: str,
    market: str,
    kickoff_utc: datetime | None,
) -> list[sqlite3.Row]:
    params: list[object] = [fixture_id, market]
    kickoff_filter = ""
    if kickoff_utc is not None:
        kickoff_filter = "AND datetime(snapshot_at) <= datetime(?)"
        params.append(kickoff_utc.isoformat(timespec="seconds"))
    line_filter = ""
    if market == "OU2.5":
        line_filter = "AND (line = 2.5 OR line IS NULL)"
    return conn.execute(
        f"""
        SELECT fixture_id, bookmaker, market, selection, line, decimal_odds, snapshot_at
        FROM odds_snapshots
        WHERE fixture_id = ?
          AND market = ?
          {line_filter}
          {kickoff_filter}
        ORDER BY datetime(snapshot_at) DESC, bookmaker, selection
        """,
        params,
    ).fetchall()


def _side_coverage(market: str, rows: list[sqlite3.Row]) -> list[OddsSideCoverage]:
    output: list[OddsSideCoverage] = []
    for selection in _EXPECTED_SELECTIONS[market]:
        best = _best_side_row(rows, selection)
        output.append(
            OddsSideCoverage(
                selection=selection,
                best_odds=float(best["decimal_odds"]) if best is not None else None,
                bookmaker=str(best["bookmaker"]) if best is not None else None,
                captured_utc=str(best["snapshot_at"]) if best is not None else None,
            )
        )
    return output


def _best_side_row(rows: list[sqlite3.Row], selection: str) -> sqlite3.Row | None:
    matching = [row for row in rows if str(row["selection"]).lower() == selection]
    if not matching:
        return None
    return max(
        matching,
        key=lambda row: (
            float(row["decimal_odds"]),
            _timestamp_sort_value(row["snapshot_at"]),
            str(row["bookmaker"]),
        ),
    )


def _complete_bookmakers(market: str, rows: list[sqlite3.Row]) -> set[str]:
    expected = set(_EXPECTED_SELECTIONS[market])
    grouped: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        selection = str(row["selection"]).lower()
        if selection not in expected:
            continue
        key = (str(row["bookmaker"]), str(row["snapshot_at"]))
        grouped.setdefault(key, set()).add(selection)
    return {bookmaker for (bookmaker, _), selections in grouped.items() if selections >= expected}


def _latest_snapshot_at(rows: list[sqlite3.Row]) -> str | None:
    if not rows:
        return None
    return max(
        (str(row["snapshot_at"]) for row in rows),
        key=_timestamp_sort_value,
    )


def _timestamp_sort_value(value: object) -> datetime:
    return parse_datetime_utc(value) or datetime.min.replace(tzinfo=UTC)


def _age_minutes(snapshot_at: str | None, as_of_utc: datetime) -> float | None:
    parsed = parse_datetime_utc(snapshot_at)
    if parsed is None:
        return None
    return max((as_of_utc - parsed).total_seconds() / 60.0, 0.0)


def _status(
    *,
    rows: list[sqlite3.Row],
    complete_bookmaker_count: int,
    fixture_started: bool,
    age_minutes: float | None,
    stale_minutes: float,
) -> str:
    if not rows:
        return STATUS_NO_ODDS
    if complete_bookmaker_count <= 0:
        return STATUS_INCOMPLETE_MARKET
    if fixture_started:
        return STATUS_STALE_ODDS
    if age_minutes is None or age_minutes > stale_minutes:
        return STATUS_STALE_ODDS
    return STATUS_COVERAGE_OK


def _details(status: str) -> str:
    if status == STATUS_NO_ODDS:
        return "Sin cuotas capturadas."
    if status == STATUS_INCOMPLETE_MARKET:
        return "Hay cuotas, pero falta algun lado del mercado."
    if status == STATUS_STALE_ODDS:
        return "La captura existe, pero no es fresca para decision operativa."
    return "Mercado completo con captura reciente."
