from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scorerecord.odds_coverage import (
    STATUS_COVERAGE_OK,
    STATUS_INCOMPLETE_MARKET,
    STATUS_NO_ODDS,
    OddsSideCoverage,
)
from scorerecord.time_utils import format_utc, parse_datetime_utc, parse_kickoff_utc

DEFAULT_DISPLAY_TZ = "America/Mexico_City"
DEFAULT_HORIZONS = ("T24H", "T6H", "T1H", "T15M", "LAST_PRE_KICKOFF")
SUPPORTED_MARKETS = ("OU2.5", "BTTS")

_HORIZON_OFFSETS = {
    "T24H": timedelta(hours=24),
    "T6H": timedelta(hours=6),
    "T1H": timedelta(hours=1),
    "T15M": timedelta(minutes=15),
    "LAST_PRE_KICKOFF": timedelta(0),
}
_EXPECTED_SELECTIONS = {
    "OU2.5": ("over", "under"),
    "BTTS": ("yes", "no"),
}
_MARKET_LINE = {
    "OU2.5": 2.5,
    "BTTS": None,
}


@dataclass(frozen=True)
class OddsHorizonCoverage:
    fixture_id: str
    market: str
    horizon: str
    target_utc: str | None
    selected_snapshot_at: str | None
    minutes_from_target: float | None
    minutes_before_kickoff: float | None
    status: str
    bookmaker_count: int
    complete_bookmaker_count: int
    best_sides: list[OddsSideCoverage]
    details: str


def load_fixture_odds_horizon_coverage(
    conn: sqlite3.Connection,
    fixture_id: str,
    horizons: tuple[str, ...] = DEFAULT_HORIZONS,
) -> list[OddsHorizonCoverage]:
    """Return pre-kickoff odds coverage observations by horizon.

    This is observability only. It does not derive closing odds, de-vig
    probabilities, EV, stakes, or recommendations.
    """

    kickoff = _fixture_kickoff_utc(conn, fixture_id)
    output: list[OddsHorizonCoverage] = []
    for market in SUPPORTED_MARKETS:
        for horizon in horizons:
            output.append(
                _horizon_coverage(
                    conn,
                    fixture_id=fixture_id,
                    market=market,
                    horizon=horizon,
                    kickoff_utc=kickoff,
                )
            )
    return output


def load_date_odds_horizon_coverage(
    conn: sqlite3.Connection,
    target_date: str,
    display_tz_name: str = DEFAULT_DISPLAY_TZ,
) -> list[OddsHorizonCoverage]:
    display_tz = _display_tz(display_tz_name)
    rows = conn.execute(
        """
        SELECT fixture_id, kickoff_utc, kickoff_local
        FROM fixture
        ORDER BY kickoff_utc, fixture_id
        """
    ).fetchall()
    output: list[OddsHorizonCoverage] = []
    for row in rows:
        kickoff = parse_kickoff_utc(
            kickoff_utc=row["kickoff_utc"],
            kickoff_local=row["kickoff_local"],
        )
        if kickoff is None:
            continue
        if kickoff.astimezone(display_tz).date().isoformat() != target_date:
            continue
        output.extend(load_fixture_odds_horizon_coverage(conn, str(row["fixture_id"])))
    return output


def _display_tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_DISPLAY_TZ)


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


def _horizon_coverage(
    conn: sqlite3.Connection,
    *,
    fixture_id: str,
    market: str,
    horizon: str,
    kickoff_utc: datetime | None,
) -> OddsHorizonCoverage:
    if kickoff_utc is None:
        return _empty_coverage(
            fixture_id=fixture_id,
            market=market,
            horizon=horizon,
            target_utc=None,
            details="missing kickoff",
        )
    target = _target_time(kickoff_utc, horizon)
    snapshot_at = _selected_snapshot_at(
        conn,
        fixture_id=fixture_id,
        market=market,
        horizon=horizon,
        target_utc=target,
    )
    if snapshot_at is None:
        return _empty_coverage(
            fixture_id=fixture_id,
            market=market,
            horizon=horizon,
            target_utc=format_utc(target),
            details="No odds snapshots at or before horizon target.",
        )
    rows = _snapshot_rows(
        conn,
        fixture_id=fixture_id,
        market=market,
        snapshot_at=snapshot_at,
    )
    selected_dt = parse_datetime_utc(snapshot_at)
    bookmaker_count = len({str(row["bookmaker"]) for row in rows})
    complete_count = len(_complete_bookmakers(market, rows))
    status = STATUS_COVERAGE_OK if complete_count > 0 else STATUS_INCOMPLETE_MARKET
    details = (
        "Complete two-way snapshot found."
        if complete_count > 0
        else "Snapshot exists, but no bookmaker has both sides at this horizon."
    )
    return OddsHorizonCoverage(
        fixture_id=fixture_id,
        market=market,
        horizon=horizon,
        target_utc=format_utc(target),
        selected_snapshot_at=snapshot_at,
        minutes_from_target=_minutes_between(target, selected_dt),
        minutes_before_kickoff=_minutes_between(kickoff_utc, selected_dt),
        status=status,
        bookmaker_count=bookmaker_count,
        complete_bookmaker_count=complete_count,
        best_sides=_side_coverage(market, rows),
        details=details,
    )


def _empty_coverage(
    *,
    fixture_id: str,
    market: str,
    horizon: str,
    target_utc: str | None,
    details: str,
) -> OddsHorizonCoverage:
    return OddsHorizonCoverage(
        fixture_id=fixture_id,
        market=market,
        horizon=horizon,
        target_utc=target_utc,
        selected_snapshot_at=None,
        minutes_from_target=None,
        minutes_before_kickoff=None,
        status=STATUS_NO_ODDS,
        bookmaker_count=0,
        complete_bookmaker_count=0,
        best_sides=_side_coverage(market, []),
        details=details,
    )


def _target_time(kickoff_utc: datetime, horizon: str) -> datetime:
    if horizon not in _HORIZON_OFFSETS:
        raise ValueError(f"Unsupported odds horizon: {horizon}")
    return kickoff_utc.astimezone(UTC) - _HORIZON_OFFSETS[horizon]


def _selected_snapshot_at(
    conn: sqlite3.Connection,
    *,
    fixture_id: str,
    market: str,
    horizon: str,
    target_utc: datetime,
) -> str | None:
    comparator = "<" if horizon == "LAST_PRE_KICKOFF" else "<="
    line_filter = "AND line = 2.5" if market == "OU2.5" else ""
    row = conn.execute(
        f"""
        SELECT snapshot_at
        FROM odds_snapshots
        WHERE fixture_id = ?
          AND market = ?
          {line_filter}
          AND datetime(snapshot_at) {comparator} datetime(?)
        GROUP BY snapshot_at
        ORDER BY datetime(snapshot_at) DESC
        LIMIT 1
        """,
        (fixture_id, market, target_utc.isoformat(timespec="seconds")),
    ).fetchone()
    return str(row["snapshot_at"]) if row is not None else None


def _snapshot_rows(
    conn: sqlite3.Connection,
    *,
    fixture_id: str,
    market: str,
    snapshot_at: str,
) -> list[sqlite3.Row]:
    line_filter = "AND line = 2.5" if market == "OU2.5" else ""
    return conn.execute(
        f"""
        SELECT fixture_id, bookmaker, market, selection, line, decimal_odds, snapshot_at
        FROM odds_snapshots
        WHERE fixture_id = ?
          AND market = ?
          AND snapshot_at = ?
          {line_filter}
        ORDER BY bookmaker, selection
        """,
        (fixture_id, market, snapshot_at),
    ).fetchall()


def _complete_bookmakers(market: str, rows: list[sqlite3.Row]) -> set[str]:
    expected = set(_EXPECTED_SELECTIONS[market])
    grouped: dict[str, set[str]] = {}
    for row in rows:
        selection = str(row["selection"]).lower()
        if selection not in expected:
            continue
        grouped.setdefault(str(row["bookmaker"]), set()).add(selection)
    return {bookmaker for bookmaker, selections in grouped.items() if selections >= expected}


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
    return max(matching, key=lambda row: (float(row["decimal_odds"]), str(row["bookmaker"])))


def _minutes_between(later: datetime, earlier: datetime | None) -> float | None:
    if earlier is None:
        return None
    return max((later.astimezone(UTC) - earlier.astimezone(UTC)).total_seconds() / 60.0, 0.0)
