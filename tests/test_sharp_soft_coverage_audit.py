import dataclasses
import re
import sqlite3

import pytest

from scorerecord.evaluate.sharp_soft_coverage_audit import (
    SharpSoftCoverageAuditReport,
    is_sharp,
    normalize_bookmaker,
    run_sharp_soft_audit,
)


@pytest.fixture
def conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE historical_match (source TEXT, source_event_id TEXT, league_code TEXT, match_date TEXT)"
    )
    conn.execute(
        "CREATE TABLE historical_odds (source TEXT, source_event_id TEXT, bookmaker TEXT, market TEXT)"
    )
    conn.execute("CREATE TABLE fixture (fixture_id TEXT, kickoff_utc TEXT)")
    conn.execute(
        "CREATE TABLE odds_snapshots (fixture_id TEXT, bookmaker TEXT, market TEXT, snapshot_at TEXT, source TEXT)"
    )
    return conn


def test_normalize_bookmaker():
    assert normalize_bookmaker(None) == "aggregate"
    assert normalize_bookmaker("Avg") == "aggregate"
    assert normalize_bookmaker("Pinnacle") == "pinnacle"
    assert normalize_bookmaker("Pinnacle (PS3838)") == "pinnacle"
    assert normalize_bookmaker("Betfair Exchange") == "betfair_exchange"
    assert normalize_bookmaker("Betfair") == "betfair"
    assert normalize_bookmaker("188 Bet") == "188bet"
    assert normalize_bookmaker("SBOBET") == "sbobet"


def test_is_sharp():
    sharp_books = ["Pinnacle", "Betfair Exchange", "Matchbook"]
    assert is_sharp("pinnacle", sharp_books)
    assert is_sharp("betfair_exchange", sharp_books)
    assert not is_sharp("betfair", sharp_books)
    assert not is_sharp("bet365", sharp_books)


def test_sharp_soft_feasible_volume(conn):
    # Create 200 fixtures with both sharp and soft in the same horizon + close
    for i in range(200):
        fix_id = f"f{i}"
        kickoff = "2025-01-01T20:00:00Z"
        conn.execute(
            "INSERT INTO fixture (fixture_id, kickoff_utc) VALUES (?, ?)", (fix_id, kickoff)
        )

        # historical match to link league
        conn.execute(
            "INSERT INTO historical_match (source, source_event_id, league_code, match_date) VALUES (?, ?, ?, ?)",
            ("src", fix_id, "EPL", "2025-01-01"),
        )

        # sharp preclose (T24H)
        conn.execute(
            "INSERT INTO odds_snapshots (fixture_id, bookmaker, market, snapshot_at, source) VALUES (?, ?, ?, ?, ?)",
            (fix_id, "Pinnacle", "OU2.5", "2024-12-31T20:00:00Z", "src"),
        )
        # soft preclose (T24H)
        conn.execute(
            "INSERT INTO odds_snapshots (fixture_id, bookmaker, market, snapshot_at, source) VALUES (?, ?, ?, ?, ?)",
            (fix_id, "Bet365", "OU2.5", "2024-12-31T20:00:00Z", "src"),
        )

        # sharp close
        conn.execute(
            "INSERT INTO odds_snapshots (fixture_id, bookmaker, market, snapshot_at, source) VALUES (?, ?, ?, ?, ?)",
            (fix_id, "Pinnacle", "OU2.5", "2025-01-01T19:55:00Z", "src"),
        )

    report = run_sharp_soft_audit(
        conn,
        date_from="2024-01-01",
        date_to="2026-01-01",
        markets=["OU2.5"],
        leagues=["EPL"],
        sharp_books=["Pinnacle", "Betfair Exchange"],
    )

    assert report.odds_snapshots_state == "SHARP_SOFT_FEASIBLE"
    assert report.overall_state == "SHARP_SOFT_FEASIBLE"
    assert report.edge_detection_feasible
    assert report.clv_feasible
    assert report.bookmaker_level_available
    assert "pinnacle" in report.sharp_candidates_found
    assert "bet365" in report.soft_candidates_found
    assert "EPL_OU2.5" in report.market_league_coverage
    assert (
        report.source_coverage["odds_snapshots"]["fixtures_with_sharp_and_soft_same_horizon"] == 200
    )
    assert report.source_coverage["odds_snapshots"]["fixtures_with_last_pre_kickoff_close"] == 200


def test_partial_coverage_below_volume(conn):
    # Only 10 fixtures
    for i in range(10):
        fix_id = f"f{i}"
        kickoff = "2025-01-01T20:00:00Z"
        conn.execute(
            "INSERT INTO fixture (fixture_id, kickoff_utc) VALUES (?, ?)", (fix_id, kickoff)
        )
        conn.execute(
            "INSERT INTO odds_snapshots (fixture_id, bookmaker, market, snapshot_at, source) VALUES (?, ?, ?, ?, ?)",
            (fix_id, "Pinnacle", "OU2.5", "2025-01-01T19:55:00Z", "src"),
        )
        conn.execute(
            "INSERT INTO odds_snapshots (fixture_id, bookmaker, market, snapshot_at, source) VALUES (?, ?, ?, ?, ?)",
            (fix_id, "Bet365", "OU2.5", "2025-01-01T19:55:00Z", "src"),
        )

    report = run_sharp_soft_audit(
        conn, "2024-01-01", "2026-01-01", ["OU2.5"], ["EPL"], ["Pinnacle"]
    )
    assert report.odds_snapshots_state == "PARTIAL_COVERAGE"
    assert report.overall_state == "PARTIAL_COVERAGE"
    assert "Volume too low (10 fixtures)" in " ".join(report.blockers)


def test_aggregate_only_not_feasible(conn):
    fix_id = "f1"
    conn.execute(
        "INSERT INTO fixture (fixture_id, kickoff_utc) VALUES (?, ?)",
        (fix_id, "2025-01-01T20:00:00Z"),
    )
    conn.execute(
        "INSERT INTO historical_match (source, source_event_id, league_code, match_date) VALUES (?, ?, ?, ?)",
        ("src", fix_id, "EPL", "2025-01-01"),
    )
    conn.execute(
        "INSERT INTO historical_odds (source, source_event_id, bookmaker, market) VALUES (?, ?, ?, ?)",
        ("src", fix_id, "Avg", "OU2.5"),
    )

    report = run_sharp_soft_audit(
        conn, "2024-01-01", "2026-01-01", ["OU2.5"], ["EPL"], ["Pinnacle"]
    )

    assert report.historical_odds_state == "NOT_FEASIBLE"
    assert report.overall_state == "NOT_FEASIBLE"
    assert not report.edge_detection_feasible
    assert "aggregate" in report.aggregate_candidates_found
    assert len(report.sharp_candidates_found) == 0


def test_missing_sharp_reference(conn):
    fix_id = "f1"
    conn.execute(
        "INSERT INTO fixture (fixture_id, kickoff_utc) VALUES (?, ?)",
        (fix_id, "2025-01-01T20:00:00Z"),
    )
    conn.execute(
        "INSERT INTO odds_snapshots (fixture_id, bookmaker, market, snapshot_at, source) VALUES (?, ?, ?, ?, ?)",
        (fix_id, "Bet365", "OU2.5", "2025-01-01T19:55:00Z", "src"),
    )

    report = run_sharp_soft_audit(
        conn, "2024-01-01", "2026-01-01", ["OU2.5"], ["EPL"], ["Pinnacle"]
    )
    assert report.overall_state == "NOT_FEASIBLE"
    assert not report.edge_detection_feasible
    assert len(report.sharp_candidates_found) == 0
    assert "bet365" in report.soft_candidates_found


def test_missing_close_timestamps(conn):
    for i in range(200):
        fix_id = f"f{i}"
        kickoff = "2025-01-01T20:00:00Z"
        conn.execute(
            "INSERT INTO fixture (fixture_id, kickoff_utc) VALUES (?, ?)", (fix_id, kickoff)
        )

        # Only preclose (T24H), NO close (<1h)
        conn.execute(
            "INSERT INTO odds_snapshots (fixture_id, bookmaker, market, snapshot_at, source) VALUES (?, ?, ?, ?, ?)",
            (fix_id, "Pinnacle", "OU2.5", "2024-12-31T20:00:00Z", "src"),
        )
        conn.execute(
            "INSERT INTO odds_snapshots (fixture_id, bookmaker, market, snapshot_at, source) VALUES (?, ?, ?, ?, ?)",
            (fix_id, "Bet365", "OU2.5", "2024-12-31T20:00:00Z", "src"),
        )

    report = run_sharp_soft_audit(
        conn, "2024-01-01", "2026-01-01", ["OU2.5"], ["EPL"], ["Pinnacle"]
    )

    assert report.overall_state == "PARTIAL_COVERAGE"
    assert report.edge_detection_feasible
    assert not report.clv_feasible
    assert "Missing robust last_pre_kickoff close timing data." in " ".join(report.blockers)


def test_null_kickoff_utc(conn):
    fix_id = "f1"
    # missing kickoff
    conn.execute("INSERT INTO fixture (fixture_id, kickoff_utc) VALUES (?, ?)", (fix_id, None))
    conn.execute(
        "INSERT INTO odds_snapshots (fixture_id, bookmaker, market, snapshot_at, source) VALUES (?, ?, ?, ?, ?)",
        (fix_id, "Pinnacle", "OU2.5", "2025-01-01T19:55:00Z", "src"),
    )

    report = run_sharp_soft_audit(
        conn, "2024-01-01", "2026-01-01", ["OU2.5"], ["EPL"], ["Pinnacle"]
    )
    assert report.source_coverage["odds_snapshots"]["fixtures_with_null_kickoff_utc"] == 1


def test_forbidden_language():
    report_fields = {f.name for f in dataclasses.fields(SharpSoftCoverageAuditReport)}
    forbidden_tokens = {
        "ev",
        "expected_value",
        "kelly",
        "stake",
        "value",
        "recommendation",
        "paper_candidate",
    }

    for field_name in report_fields:
        lower = field_name.lower()
        tokens = set(re.split(r"[^a-z0-9]+", lower))
        assert not (tokens & forbidden_tokens), f"Forbidden language found in field: {field_name}"
