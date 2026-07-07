import sqlite3
from datetime import UTC, datetime

from scorerecord.db import init_db
from scorerecord.odds_coverage import (
    STATUS_COVERAGE_OK,
    STATUS_INCOMPLETE_MARKET,
    STATUS_NO_ODDS,
    STATUS_STALE_ODDS,
    load_fixture_odds_coverage,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute(
        "INSERT INTO team (team_id, canonical, canonical_key) VALUES (1, 'Home', 'home')"
    )
    conn.execute(
        "INSERT INTO team (team_id, canonical, canonical_key) VALUES (2, 'Away', 'away')"
    )
    conn.execute(
        """
        INSERT INTO fixture (fixture_id, kickoff_utc, home_id, away_id)
        VALUES ('fx1', '2026-07-05T20:00:00+00:00', 1, 2)
        """
    )
    return conn


def _insert_snapshot(
    conn: sqlite3.Connection,
    *,
    market: str,
    selection: str,
    odds: float,
    bookmaker: str = "BookA",
    snapshot_at: str = "2026-07-05T18:30:00+00:00",
    line_override: float | None = 2.5,
) -> None:
    line = line_override if market == "OU2.5" else None
    conn.execute(
        """
        INSERT INTO odds_snapshots (
          event_id, fixture_id, bookmaker, market, selection, line,
          decimal_odds, snapshot_at, commence_time, source
        )
        VALUES (
          'event-1', 'fx1', ?, ?, ?, ?, ?,
          ?, '2026-07-05T20:00:00+00:00', 'test'
        )
        """,
        (bookmaker, market, selection, line, odds, snapshot_at),
    )


def _by_market(conn: sqlite3.Connection) -> dict[str, object]:
    coverage = load_fixture_odds_coverage(
        conn,
        "fx1",
        as_of_utc=datetime(2026, 7, 5, 18, 45, tzinfo=UTC),
    )
    return {item.market: item for item in coverage}


def test_no_odds_rows_returns_supported_markets_with_no_odds() -> None:
    conn = _conn()

    coverage = _by_market(conn)

    assert set(coverage) == {"OU2.5", "BTTS"}
    assert coverage["OU2.5"].status == STATUS_NO_ODDS
    assert coverage["BTTS"].status == STATUS_NO_ODDS


def test_ou25_only_one_side_returns_incomplete_market() -> None:
    conn = _conn()
    _insert_snapshot(conn, market="OU2.5", selection="over", odds=1.90)

    ou25 = _by_market(conn)["OU2.5"]

    assert ou25.status == STATUS_INCOMPLETE_MARKET
    assert ou25.bookmaker_count == 1
    assert ou25.complete_bookmaker_count == 0


def test_ou25_over_under_from_same_bookmaker_returns_coverage_ok() -> None:
    conn = _conn()
    _insert_snapshot(conn, market="OU2.5", selection="over", odds=1.90)
    _insert_snapshot(conn, market="OU2.5", selection="under", odds=1.95)

    ou25 = _by_market(conn)["OU2.5"]

    assert ou25.status == STATUS_COVERAGE_OK
    assert ou25.bookmaker_count == 1
    assert ou25.complete_bookmaker_count == 1
    assert [(side.selection, side.best_odds) for side in ou25.sides] == [
        ("over", 1.90),
        ("under", 1.95),
    ]


def test_ou25_null_line_rows_do_not_count_as_coverage_ok() -> None:
    conn = _conn()
    _insert_snapshot(
        conn,
        market="OU2.5",
        selection="over",
        odds=1.90,
        line_override=None,
    )
    _insert_snapshot(
        conn,
        market="OU2.5",
        selection="under",
        odds=1.95,
        line_override=None,
    )

    ou25 = _by_market(conn)["OU2.5"]

    assert ou25.status == STATUS_NO_ODDS
    assert ou25.bookmaker_count == 0
    assert ou25.complete_bookmaker_count == 0


def test_btts_yes_no_from_same_bookmaker_returns_coverage_ok() -> None:
    conn = _conn()
    _insert_snapshot(conn, market="BTTS", selection="yes", odds=1.80)
    _insert_snapshot(conn, market="BTTS", selection="no", odds=2.05)

    btts = _by_market(conn)["BTTS"]

    assert btts.status == STATUS_COVERAGE_OK
    assert btts.bookmaker_count == 1
    assert btts.complete_bookmaker_count == 1


def test_old_complete_snapshot_returns_stale_odds() -> None:
    conn = _conn()
    _insert_snapshot(
        conn,
        market="OU2.5",
        selection="over",
        odds=1.90,
        snapshot_at="2026-07-05T16:00:00+00:00",
    )
    _insert_snapshot(
        conn,
        market="OU2.5",
        selection="under",
        odds=1.95,
        snapshot_at="2026-07-05T16:00:00+00:00",
    )

    ou25 = _by_market(conn)["OU2.5"]

    assert ou25.status == STATUS_STALE_ODDS
    assert ou25.age_minutes == 165.0


def test_best_observed_odds_per_side_uses_highest_decimal_odds() -> None:
    conn = _conn()
    _insert_snapshot(conn, market="OU2.5", selection="over", odds=1.90, bookmaker="BookA")
    _insert_snapshot(conn, market="OU2.5", selection="under", odds=1.95, bookmaker="BookA")
    _insert_snapshot(conn, market="OU2.5", selection="over", odds=1.93, bookmaker="BookB")
    _insert_snapshot(conn, market="OU2.5", selection="under", odds=1.91, bookmaker="BookB")

    ou25 = _by_market(conn)["OU2.5"]
    sides = {side.selection: side for side in ou25.sides}

    assert ou25.status == STATUS_COVERAGE_OK
    assert ou25.bookmaker_count == 2
    assert ou25.complete_bookmaker_count == 2
    assert sides["over"].best_odds == 1.93
    assert sides["over"].bookmaker == "BookB"
    assert sides["under"].best_odds == 1.95
    assert sides["under"].bookmaker == "BookA"
