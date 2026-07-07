import contextlib
import io
import sqlite3
import tempfile
from pathlib import Path

from scorerecord.cli import main
from scorerecord.db import connect, init_db
from scorerecord.odds_coverage import (
    STATUS_COVERAGE_OK,
    STATUS_INCOMPLETE_MARKET,
    STATUS_NO_ODDS,
)
from scorerecord.odds_horizons import (
    DEFAULT_HORIZONS,
    load_date_odds_horizon_coverage,
    load_fixture_odds_horizon_coverage,
)


def _conn(*, kickoff_utc: str | None = "2026-07-05T20:00:00+00:00") -> sqlite3.Connection:
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
        VALUES ('fx1', ?, 1, 2)
        """,
        (kickoff_utc,),
    )
    return conn


def _insert_snapshot(
    conn: sqlite3.Connection,
    *,
    market: str = "OU2.5",
    selection: str = "over",
    odds: float = 1.90,
    bookmaker: str = "BookA",
    snapshot_at: str = "2026-07-05T18:30:00+00:00",
    fixture_id: str = "fx1",
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
          'event-1', ?, ?, ?, ?, ?, ?,
          ?, '2026-07-05T20:00:00+00:00', 'test'
        )
        """,
        (fixture_id, bookmaker, market, selection, line, odds, snapshot_at),
    )


def _coverage_by_key(conn: sqlite3.Connection) -> dict[tuple[str, str], object]:
    coverage = load_fixture_odds_horizon_coverage(conn, "fx1")
    return {(item.market, item.horizon): item for item in coverage}


def test_fixture_without_kickoff_returns_no_odds_missing_kickoff() -> None:
    conn = _conn(kickoff_utc=None)

    coverage = _coverage_by_key(conn)

    assert coverage[("OU2.5", "T1H")].status == STATUS_NO_ODDS
    assert coverage[("OU2.5", "T1H")].details == "missing kickoff"


def test_no_odds_before_horizon_returns_no_odds() -> None:
    conn = _conn()

    coverage = _coverage_by_key(conn)

    assert coverage[("OU2.5", "T1H")].status == STATUS_NO_ODDS


def test_snapshot_after_target_horizon_is_not_selected() -> None:
    conn = _conn()
    _insert_snapshot(conn, selection="over", snapshot_at="2026-07-05T19:30:00+00:00")
    _insert_snapshot(conn, selection="under", snapshot_at="2026-07-05T19:30:00+00:00")

    coverage = _coverage_by_key(conn)

    assert coverage[("OU2.5", "T1H")].status == STATUS_NO_ODDS


def test_latest_snapshot_before_target_horizon_is_selected() -> None:
    conn = _conn()
    for snapshot_at, over_odds in [
        ("2026-07-05T18:00:00+00:00", 1.80),
        ("2026-07-05T18:45:00+00:00", 1.90),
    ]:
        _insert_snapshot(conn, selection="over", odds=over_odds, snapshot_at=snapshot_at)
        _insert_snapshot(conn, selection="under", odds=2.00, snapshot_at=snapshot_at)

    coverage = _coverage_by_key(conn)
    t1h = coverage[("OU2.5", "T1H")]

    assert t1h.status == STATUS_COVERAGE_OK
    assert t1h.selected_snapshot_at == "2026-07-05T18:45:00+00:00"
    assert t1h.minutes_from_target == 15.0
    assert t1h.minutes_before_kickoff == 75.0


def test_last_pre_kickoff_excludes_snapshot_at_or_after_kickoff() -> None:
    conn = _conn()
    _insert_snapshot(conn, selection="over", snapshot_at="2026-07-05T19:50:00+00:00")
    _insert_snapshot(conn, selection="under", snapshot_at="2026-07-05T19:50:00+00:00")
    _insert_snapshot(conn, selection="over", odds=1.50, snapshot_at="2026-07-05T20:00:00+00:00")
    _insert_snapshot(conn, selection="under", odds=2.50, snapshot_at="2026-07-05T20:00:00+00:00")

    coverage = _coverage_by_key(conn)
    last = coverage[("OU2.5", "LAST_PRE_KICKOFF")]

    assert last.status == STATUS_COVERAGE_OK
    assert last.selected_snapshot_at == "2026-07-05T19:50:00+00:00"
    assert last.minutes_before_kickoff == 10.0


def test_ou25_requires_line_25() -> None:
    conn = _conn()
    _insert_snapshot(
        conn,
        selection="over",
        snapshot_at="2026-07-05T18:45:00+00:00",
        line_override=None,
    )
    _insert_snapshot(
        conn,
        selection="under",
        snapshot_at="2026-07-05T18:45:00+00:00",
        line_override=None,
    )

    coverage = _coverage_by_key(conn)

    assert coverage[("OU2.5", "T1H")].status == STATUS_NO_ODDS


def test_ou25_same_bookmaker_same_snapshot_returns_coverage_ok() -> None:
    conn = _conn()
    _insert_snapshot(conn, selection="over", snapshot_at="2026-07-05T18:45:00+00:00")
    _insert_snapshot(conn, selection="under", snapshot_at="2026-07-05T18:45:00+00:00")

    coverage = _coverage_by_key(conn)

    assert coverage[("OU2.5", "T1H")].status == STATUS_COVERAGE_OK
    assert coverage[("OU2.5", "T1H")].complete_bookmaker_count == 1


def test_ou25_different_snapshot_at_does_not_count_complete() -> None:
    conn = _conn()
    _insert_snapshot(conn, selection="over", snapshot_at="2026-07-05T18:45:00+00:00")
    _insert_snapshot(conn, selection="under", snapshot_at="2026-07-05T18:30:00+00:00")

    coverage = _coverage_by_key(conn)

    assert coverage[("OU2.5", "T1H")].status == STATUS_INCOMPLETE_MARKET
    assert coverage[("OU2.5", "T1H")].complete_bookmaker_count == 0


def test_btts_yes_no_same_bookmaker_same_snapshot_returns_coverage_ok() -> None:
    conn = _conn()
    _insert_snapshot(
        conn,
        market="BTTS",
        selection="yes",
        odds=1.80,
        snapshot_at="2026-07-05T18:45:00+00:00",
    )
    _insert_snapshot(
        conn,
        market="BTTS",
        selection="no",
        odds=2.05,
        snapshot_at="2026-07-05T18:45:00+00:00",
    )

    coverage = _coverage_by_key(conn)

    assert coverage[("BTTS", "T1H")].status == STATUS_COVERAGE_OK


def test_incomplete_one_sided_market_returns_incomplete() -> None:
    conn = _conn()
    _insert_snapshot(conn, selection="over", snapshot_at="2026-07-05T18:45:00+00:00")

    coverage = _coverage_by_key(conn)

    assert coverage[("OU2.5", "T1H")].status == STATUS_INCOMPLETE_MARKET


def test_date_report_uses_mexico_city_local_date() -> None:
    conn = _conn(kickoff_utc="2026-07-06T02:00:00+00:00")

    local_day = load_date_odds_horizon_coverage(conn, "2026-07-05")
    utc_day = load_date_odds_horizon_coverage(conn, "2026-07-06")

    assert len(local_day) == len(DEFAULT_HORIZONS) * 2
    assert utc_day == []


def test_cli_odds_horizon_report_prints_all_horizons() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / "horizons.db"
        conn = connect(db_path)
        try:
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
            _insert_snapshot(conn, selection="over", snapshot_at="2026-07-05T18:45:00+00:00")
            _insert_snapshot(conn, selection="under", snapshot_at="2026-07-05T18:45:00+00:00")
            conn.commit()
        finally:
            conn.close()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = main(["--db-path", str(db_path), "odds-horizon-report", "--fixture-id", "fx1"])

    text = output.getvalue()
    assert rc == 0
    for horizon in DEFAULT_HORIZONS:
        assert f"horizon={horizon}" in text
    assert "market=OU2.5" in text
    assert "status=COVERAGE_OK" in text
