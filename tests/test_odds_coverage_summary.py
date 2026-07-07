import contextlib
import io
import sqlite3
import tempfile
from pathlib import Path

from scorerecord.cli import main
from scorerecord.db import connect, init_db
from scorerecord.odds_coverage_summary import (
    GRADE_NO_ODDS,
    GRADE_PARTIAL,
    GRADE_READY,
    GRADE_WEAK,
    build_odds_coverage_summary_for_date,
    build_odds_coverage_summary_for_fixture,
)


def _conn(*, fixture_id: str = "fx1", kickoff_utc: str = "2026-07-05T20:00:00+00:00"):
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
        VALUES (?, ?, 1, 2)
        """,
        (fixture_id, kickoff_utc),
    )
    return conn


def _insert_snapshot(
    conn: sqlite3.Connection,
    *,
    fixture_id: str = "fx1",
    market: str = "OU2.5",
    selection: str = "over",
    bookmaker: str = "BookA",
    snapshot_at: str = "2026-07-05T18:50:00+00:00",
    odds: float = 1.90,
) -> None:
    line = 2.5 if market == "OU2.5" else None
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


def _insert_complete_pair(
    conn: sqlite3.Connection,
    *,
    fixture_id: str = "fx1",
    market: str = "OU2.5",
    bookmaker: str = "BookA",
    snapshot_at: str = "2026-07-05T18:50:00+00:00",
) -> None:
    first, second = ("over", "under") if market == "OU2.5" else ("yes", "no")
    _insert_snapshot(
        conn,
        fixture_id=fixture_id,
        market=market,
        selection=first,
        bookmaker=bookmaker,
        snapshot_at=snapshot_at,
    )
    _insert_snapshot(
        conn,
        fixture_id=fixture_id,
        market=market,
        selection=second,
        bookmaker=bookmaker,
        snapshot_at=snapshot_at,
        odds=1.95,
    )


def _summary_by_market(conn: sqlite3.Connection) -> dict[str, object]:
    report = build_odds_coverage_summary_for_fixture(conn, "fx1")
    return {item.market: item for item in report.fixtures}


def test_no_horizon_coverage_returns_no_odds() -> None:
    conn = _conn()

    summary = _summary_by_market(conn)

    assert summary["OU2.5"].coverage_grade == GRADE_NO_ODDS
    assert summary["OU2.5"].horizons_covered == 0
    assert summary["OU2.5"].horizons_missing == 5


def test_last_and_near_horizons_with_two_books_returns_ready() -> None:
    conn = _conn()
    for bookmaker in ("BookA", "BookB"):
        _insert_complete_pair(conn, bookmaker=bookmaker, snapshot_at="2026-07-05T18:50:00+00:00")
        _insert_complete_pair(conn, bookmaker=bookmaker, snapshot_at="2026-07-05T19:50:00+00:00")

    ou25 = _summary_by_market(conn)["OU2.5"]

    assert ou25.coverage_grade == GRADE_READY
    assert ou25.complete_bookmaker_max == 2
    assert ou25.has_last_pre_kickoff is True
    assert ou25.has_t1h_or_t15m is True


def test_one_covered_horizon_only_returns_partial() -> None:
    conn = _conn()
    for bookmaker in ("BookA", "BookB"):
        _insert_complete_pair(conn, bookmaker=bookmaker, snapshot_at="2026-07-04T19:50:00+00:00")
    for snapshot_at in (
        "2026-07-05T13:50:00+00:00",
        "2026-07-05T18:50:00+00:00",
        "2026-07-05T19:40:00+00:00",
        "2026-07-05T19:50:00+00:00",
    ):
        _insert_snapshot(conn, snapshot_at=snapshot_at, selection="over")

    ou25 = _summary_by_market(conn)["OU2.5"]

    assert ou25.coverage_grade == GRADE_PARTIAL
    assert ou25.horizons_covered == 1
    assert ou25.horizons_incomplete == 4
    assert ou25.complete_bookmaker_max == 2


def test_incomplete_only_horizons_return_weak() -> None:
    conn = _conn()
    _insert_snapshot(conn, selection="over", snapshot_at="2026-07-04T19:50:00+00:00")

    ou25 = _summary_by_market(conn)["OU2.5"]

    assert ou25.coverage_grade == GRADE_WEAK
    assert ou25.horizons_incomplete == 5
    assert ou25.blocking_reason == "only_incomplete_horizons"


def test_complete_bookmaker_max_is_computed_across_horizons() -> None:
    conn = _conn()
    _insert_complete_pair(conn, bookmaker="BookA", snapshot_at="2026-07-05T18:50:00+00:00")
    for bookmaker in ("BookA", "BookB"):
        _insert_complete_pair(conn, bookmaker=bookmaker, snapshot_at="2026-07-05T19:50:00+00:00")

    ou25 = _summary_by_market(conn)["OU2.5"]

    assert ou25.complete_bookmaker_max == 2


def test_last_pre_kickoff_flag_requires_covered_last_horizon() -> None:
    conn = _conn()
    _insert_complete_pair(conn, snapshot_at="2026-07-05T18:50:00+00:00")
    _insert_snapshot(conn, snapshot_at="2026-07-05T19:50:00+00:00", selection="over")

    ou25 = _summary_by_market(conn)["OU2.5"]

    assert ou25.has_t1h_or_t15m is True
    assert ou25.has_last_pre_kickoff is False


def test_near_kickoff_flag_uses_t1h_or_t15m() -> None:
    conn = _conn()
    _insert_complete_pair(conn, snapshot_at="2026-07-05T19:40:00+00:00")

    ou25 = _summary_by_market(conn)["OU2.5"]

    assert ou25.has_t1h_or_t15m is True


def test_date_summary_uses_mexico_city_local_date() -> None:
    conn = _conn(kickoff_utc="2026-07-06T02:00:00+00:00")

    local_day = build_odds_coverage_summary_for_date(conn, "2026-07-05")
    utc_day = build_odds_coverage_summary_for_date(conn, "2026-07-06")

    assert local_day.total_fixture_markets == 2
    assert utc_day.total_fixture_markets == 0


def test_cli_fixture_id_prints_rows() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / "summary.db"
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
            _insert_complete_pair(conn)
            conn.commit()
        finally:
            conn.close()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = main(["--db-path", str(db_path), "odds-coverage-summary", "--fixture-id", "fx1"])

    output.seek(0)
    contents = output.read()
    assert rc == 0
    assert "total_fixture_markets=2" in contents
    assert "market=OU2.5" in contents
    assert "grade=" in contents


def test_cli_date_prints_aggregate_totals() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / "summary.db"
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
            conn.commit()
        finally:
            conn.close()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = main(["--db-path", str(db_path), "odds-coverage-summary", "--date", "2026-07-05"])

    output.seek(0)
    contents = output.read()
    assert rc == 0
    assert "date=2026-07-05 total_fixture_markets=2" in contents
    assert "no_odds=2" in contents


def test_cli_output_uses_operational_language() -> None:
    conn = _conn()
    report = build_odds_coverage_summary_for_fixture(conn, "fx1")
    text = "\n".join([report.as_header(), *(row.as_row() for row in report.fixtures)])
    blocked_terms = [
        "e" + "v",
        "ed" + "ge",
        "de" + "-vig",
        "kel" + "ly",
        "sta" + "ke",
        "val" + "ue",
        "recomm" + "endation",
        "p" + "ick",
        "paper" + "_candidate",
    ]

    lower_text = text.casefold()

    assert all(term not in lower_text for term in blocked_terms)
