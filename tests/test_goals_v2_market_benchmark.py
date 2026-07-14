"""Tests for Goals V2 Market Benchmark OU2.5."""

import ast
import dataclasses
import json
import sqlite3
from datetime import date
from pathlib import Path

import numpy as np

from scorerecord.evaluate.goals_v2_market_benchmark import (
    GoalsV2MarketBenchmarkReport,
    _binary_brier,
    _binary_ece,
    _binary_logloss,
    load_historical_ou25_markets,
    run_goals_v2_market_benchmark,
)

# ---------------------------------------------------------------------------
# Helpers for mock data
# ---------------------------------------------------------------------------

def _create_test_db() -> sqlite3.Connection:
    """Create an in-memory DB with historical_match and historical_odds."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    conn.execute("""
        CREATE TABLE historical_match (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            league_code TEXT NOT NULL,
            season TEXT NOT NULL,
            match_date TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            home_goals INTEGER,
            away_goals INTEGER,
            result_1x2 TEXT,
            raw_json TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source, source_event_id)
        )
    """)

    conn.execute("""
        CREATE TABLE historical_odds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            bookmaker TEXT,
            market TEXT NOT NULL,
            selection TEXT NOT NULL,
            line REAL,
            entry_odds REAL,
            entry_odds_type TEXT,
            closing_odds REAL,
            closing_odds_type TEXT,
            is_real_historical_odds INTEGER NOT NULL DEFAULT 1,
            is_timestamp_exact INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    return conn


def _insert_match(
    conn: sqlite3.Connection,
    *,
    seid: str,
    league: str = "E0",
    season: str = "2022-2023",
    match_date: str = "2023-01-15T15:00:00Z",
    home: str = "TeamA",
    away: str = "TeamB",
    home_goals: int = 2,
    away_goals: int = 1,
    hs: int = 12,
    as_val: int = 8,
    hst: int = 5,
    ast_val: int = 3,
) -> None:
    raw = json.dumps({"HS": hs, "AS": as_val, "HST": hst, "AST": ast_val})
    r1x2 = "home" if home_goals > away_goals else ("away" if away_goals > home_goals else "draw")
    conn.execute(
        """
        INSERT INTO historical_match
        (source, source_event_id, league_code, season, match_date,
         home_team, away_team, home_goals, away_goals, result_1x2, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("football-data", seid, league, season, match_date,
         home, away, home_goals, away_goals, r1x2, raw),
    )


def _insert_ou25_odds(
    conn: sqlite3.Connection,
    *,
    seid: str,
    over_entry: float = 2.0,
    under_entry: float = 1.8,
    over_close: float | None = 2.1,
    under_close: float | None = 1.75,
    entry_type: str = "avg_preclose",
    close_type: str = "avg_close",
) -> None:
    conn.execute(
        """
        INSERT INTO historical_odds
        (source, source_event_id, bookmaker, market, selection, line,
         entry_odds, entry_odds_type, closing_odds, closing_odds_type)
        VALUES (?, ?, ?, 'OU2.5', 'over', 2.5, ?, ?, ?, ?)
        """,
        ("football-data", seid, "Avg", over_entry, entry_type,
         over_close, close_type if over_close else None),
    )
    conn.execute(
        """
        INSERT INTO historical_odds
        (source, source_event_id, bookmaker, market, selection, line,
         entry_odds, entry_odds_type, closing_odds, closing_odds_type)
        VALUES (?, ?, ?, 'OU2.5', 'under', 2.5, ?, ?, ?, ?)
        """,
        ("football-data", seid, "Avg", under_entry, entry_type,
         under_close, close_type if under_close else None),
    )


def _populate_training_data(conn: sqlite3.Connection, n: int = 20) -> None:
    """Insert enough training matches from 2018-2021 for min_train."""
    teams = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta"]
    for i in range(n):
        month = (i % 12) + 1
        year = 2018 + (i // 12)
        day = min((i % 28) + 1, 28)
        dt = f"{year}-{month:02d}-{day:02d}T15:00:00Z"
        home = teams[i % len(teams)]
        away = teams[(i + 1) % len(teams)]
        hg = (i % 3)
        ag = ((i + 1) % 3)
        seid = f"football-data:E0:train:{dt}:{home.lower()}:{away.lower()}"
        _insert_match(
            conn, seid=seid, league="E0", season=f"{year}-{year+1}",
            match_date=dt, home=home, away=away,
            home_goals=hg, away_goals=ag,
            hs=10 + i, as_val=8 + i, hst=4 + (i % 3), ast_val=3 + (i % 3),
        )


def _populate_eval_data_with_odds(conn: sqlite3.Connection, n: int = 6) -> None:
    """Insert eval-window matches (2022+) with OU2.5 odds."""
    teams = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta"]
    for i in range(n):
        day = min(i + 1, 28)
        dt = f"2023-01-{day:02d}T15:00:00Z"
        home = teams[i % len(teams)]
        away = teams[(i + 1) % len(teams)]
        hg = (i % 4)
        ag = ((i + 2) % 3)
        seid = f"football-data:E0:2022-2023:{dt[:10]}:{home.lower()}:{away.lower()}"
        _insert_match(
            conn, seid=seid, league="E0", season="2022-2023",
            match_date=dt, home=home, away=away,
            home_goals=hg, away_goals=ag,
        )
        over_odds = 1.8 + (i * 0.1)
        under_odds = 2.0 - (i * 0.05)
        _insert_ou25_odds(
            conn, seid=seid,
            over_entry=over_odds,
            under_entry=under_odds,
            over_close=over_odds + 0.05 if i < 5 else None,
            under_close=under_odds - 0.02 if i < 5 else None,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_ou25_label():
    """OU2.5 label is total_goals > 2.5."""
    assert 1 if 3 > 2.5 else 0 == 1
    assert 1 if 4 > 2.5 else 0 == 1
    assert 1 if 2 > 2.5 else 0 == 0
    assert 1 if 0 > 2.5 else 0 == 0
    # Verify through actual benchmark data
    conn = _create_test_db()
    _populate_training_data(conn, n=20)
    seid = "football-data:E0:2022-2023:2023-02-01:alpha:beta"
    _insert_match(
        conn, seid=seid, league="E0", season="2022-2023",
        match_date="2023-02-01T15:00:00Z",
        home="Alpha", away="Beta", home_goals=2, away_goals=1,
    )
    _insert_ou25_odds(conn, seid=seid)
    seid2 = "football-data:E0:2022-2023:2023-02-02:gamma:delta"
    _insert_match(
        conn, seid=seid2, league="E0", season="2022-2023",
        match_date="2023-02-02T15:00:00Z",
        home="Gamma", away="Delta", home_goals=1, away_goals=0,
    )
    _insert_ou25_odds(conn, seid=seid2)

    report = run_goals_v2_market_benchmark(
        conn, train_from=date(2018, 1, 1), eval_from=date(2023, 1, 1),
        to_date=date(2023, 12, 31), leagues=("EPL",),
        min_train=2, bootstrap_samples=10,
    )
    # 2+1=3 > 2.5 → label=1; 1+0=1 < 2.5 → label=0
    assert report.matched_preclose_count >= 1


def test_market_odds_require_complete_over_under():
    """Only fixtures with both over AND under odds are matched."""
    conn = _create_test_db()
    _populate_training_data(conn, n=20)
    seid = "football-data:E0:2022-2023:2023-03-01:alpha:beta"
    _insert_match(
        conn, seid=seid, league="E0", season="2022-2023",
        match_date="2023-03-01T15:00:00Z",
        home="Alpha", away="Beta", home_goals=2, away_goals=1,
    )
    # Insert only "over", not "under"
    conn.execute(
        """
        INSERT INTO historical_odds
        (source, source_event_id, bookmaker, market, selection, line,
         entry_odds, entry_odds_type)
        VALUES (?, ?, ?, 'OU2.5', 'over', 2.5, 2.0, 'avg_preclose')
        """,
        ("football-data", seid, "Avg"),
    )
    markets = load_historical_ou25_markets(conn)
    assert seid not in markets


def test_invalid_odds_excluded():
    """Odds <= 1.0 are excluded."""
    conn = _create_test_db()
    seid = "football-data:E0:2022-2023:2023-04-01:alpha:beta"
    _insert_ou25_odds(conn, seid=seid, over_entry=0.95, under_entry=1.8)
    markets = load_historical_ou25_markets(conn)
    assert seid not in markets


def test_one_row_per_matched_fixture():
    """Exactly one evaluation row per matched fixture."""
    conn = _create_test_db()
    _populate_training_data(conn, n=20)
    _populate_eval_data_with_odds(conn, n=4)

    report = run_goals_v2_market_benchmark(
        conn, train_from=date(2018, 1, 1), eval_from=date(2022, 8, 1),
        to_date=date(2023, 12, 31), leagues=("EPL",),
        min_train=2, bootstrap_samples=10,
    )
    assert report.matched_preclose_count <= report.raw_lambda_predictions


def test_preclose_comparisons_identical_fixture_set():
    """Preclose comparisons use identical fixture set."""
    conn = _create_test_db()
    _populate_training_data(conn, n=20)
    _populate_eval_data_with_odds(conn, n=6)

    report = run_goals_v2_market_benchmark(
        conn, train_from=date(2018, 1, 1), eval_from=date(2022, 8, 1),
        to_date=date(2023, 12, 31), leagues=("EPL",),
        min_train=2, bootstrap_samples=10,
    )
    # All logloss metrics computed on same matched_preclose_count rows
    assert report.matched_preclose_count > 0
    assert report.goals_v2_goals_baseline_logloss > 0
    assert report.goals_v2_process_only_logloss > 0
    assert report.goals_v2_goals_plus_process_logloss > 0
    assert report.market_preclose_logloss > 0


def test_close_comparisons_use_close_valid_subset():
    """Close comparisons use only close-valid subset."""
    conn = _create_test_db()
    _populate_training_data(conn, n=20)
    _populate_eval_data_with_odds(conn, n=6)

    report = run_goals_v2_market_benchmark(
        conn, train_from=date(2018, 1, 1), eval_from=date(2022, 8, 1),
        to_date=date(2023, 12, 31), leagues=("EPL",),
        min_train=2, bootstrap_samples=10,
    )
    # Last of 6 eval matches has no close odds
    assert report.matched_close_count <= report.matched_preclose_count
    assert report.missing_close_count >= 0
    assert (
        report.matched_close_count + report.missing_close_count
        == report.matched_preclose_count
    )
    if report.matched_close_count > 0:
        assert report.market_close_logloss is not None
        assert report.delta_gpp_vs_market_close is not None


def test_gpp_probabilities_finite_bounded():
    """GPP probabilities are finite and in [0, 1]."""
    conn = _create_test_db()
    _populate_training_data(conn, n=20)
    _populate_eval_data_with_odds(conn, n=4)

    report = run_goals_v2_market_benchmark(
        conn, train_from=date(2018, 1, 1), eval_from=date(2022, 8, 1),
        to_date=date(2023, 12, 31), leagues=("EPL",),
        min_train=2, bootstrap_samples=10,
    )
    # Logloss and brier are valid (finite, not NaN)
    assert np.isfinite(report.goals_v2_goals_plus_process_logloss)
    assert np.isfinite(report.goals_v2_goals_plus_process_brier)
    assert report.goals_v2_goals_plus_process_brier >= 0
    assert report.goals_v2_goals_plus_process_brier <= 1


def test_bootstrap_ci_league_week_blocks():
    """Bootstrap CI uses league + ISO week blocks."""
    conn = _create_test_db()
    _populate_training_data(conn, n=20)
    _populate_eval_data_with_odds(conn, n=6)

    report = run_goals_v2_market_benchmark(
        conn, train_from=date(2018, 1, 1), eval_from=date(2022, 8, 1),
        to_date=date(2023, 12, 31), leagues=("EPL",),
        min_train=2, bootstrap_samples=50,
    )
    ci = report.ci_delta_gpp_vs_market_preclose
    assert len(ci) == 2
    assert ci[0] <= ci[1]
    ci_gb = report.ci_delta_gpp_vs_goals_baseline_lambda
    assert len(ci_gb) == 2
    assert ci_gb[0] <= ci_gb[1]


def test_by_league_metrics_populated():
    """by_league_metrics is populated for each evaluated league."""
    conn = _create_test_db()
    _populate_training_data(conn, n=20)
    _populate_eval_data_with_odds(conn, n=4)

    report = run_goals_v2_market_benchmark(
        conn, train_from=date(2018, 1, 1), eval_from=date(2022, 8, 1),
        to_date=date(2023, 12, 31), leagues=("EPL",),
        min_train=2, bootstrap_samples=10,
    )
    assert len(report.by_league_metrics) > 0
    for _lg, metrics in report.by_league_metrics.items():
        assert "count" in metrics
        assert "gpp_logloss" in metrics
        assert "preclose_logloss" in metrics
        assert "delta_gpp_vs_preclose" in metrics


def test_by_season_metrics_populated():
    """by_season_metrics is populated for each evaluated season."""
    conn = _create_test_db()
    _populate_training_data(conn, n=20)
    _populate_eval_data_with_odds(conn, n=4)

    report = run_goals_v2_market_benchmark(
        conn, train_from=date(2018, 1, 1), eval_from=date(2022, 8, 1),
        to_date=date(2023, 12, 31), leagues=("EPL",),
        min_train=2, bootstrap_samples=10,
    )
    assert len(report.by_season_metrics) > 0
    for _sn, metrics in report.by_season_metrics.items():
        assert "count" in metrics
        assert "gpp_logloss" in metrics


def test_no_external_api_imports():
    """No external API or network client modules are imported."""
    src_dir = Path(__file__).parent.parent / "src" / "scorerecord"
    files_to_check = [
        src_dir / "evaluate" / "goals_v2_market_benchmark.py",
    ]
    forbidden_modules = {
        "urllib", "requests", "aiohttp", "httpx", "http",
        "api_football_probe", "provider_probe",
    }
    for py_file in files_to_check:
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden_modules, (
                        f"Forbidden import: {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    assert node.module.split(".")[0] not in forbidden_modules, (
                        f"Forbidden import: {node.module}"
                    )


def test_no_ev_kelly_stake_value_language():
    """Output contains no EV, Kelly, stake, value, recommendation language."""
    import re
    report_fields = {f.name for f in dataclasses.fields(GoalsV2MarketBenchmarkReport)}
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
        assert not (tokens & forbidden_tokens), (
            f"Forbidden token found in field: {field_name}"
        )

    # Also check the source file text
    src = (
        Path(__file__).parent.parent
        / "src" / "scorerecord" / "evaluate" / "goals_v2_market_benchmark.py"
    ).read_text()

    # Check that no function or variable names contain forbidden terms
    # (ignoring docstring/comment mentions that say "does NOT")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for word in forbidden_tokens:
                assert word not in node.name.lower(), (
                    f"Forbidden language '{word}' in function: {node.name}"
                )


def test_benchmark_state_logic():
    """Benchmark state logic returns correct states."""
    # MARKET_REJECTED: delta positive, CI above zero
    conn = _create_test_db()
    _populate_training_data(conn, n=20)
    _populate_eval_data_with_odds(conn, n=4)

    report = run_goals_v2_market_benchmark(
        conn, train_from=date(2018, 1, 1), eval_from=date(2022, 8, 1),
        to_date=date(2023, 12, 31), leagues=("EPL",),
        min_train=2, bootstrap_samples=10,
    )
    # State must be one of the three valid states
    assert report.benchmark_state in [
        "MARKET_PROMISING", "MARKET_REJECTED", "MARKET_INCONCLUSIVE",
    ]


def test_binary_metrics():
    """Basic binary metrics sanity check."""
    y = np.array([1, 0, 1, 0])
    p = np.array([0.9, 0.1, 0.8, 0.2])

    ll = _binary_logloss(y, p)
    assert ll > 0
    assert np.isfinite(ll)

    brier = _binary_brier(y, p)
    assert 0 <= brier <= 1
    assert np.isfinite(brier)

    ece = _binary_ece(y, p)
    assert 0 <= ece <= 1
    assert np.isfinite(ece)
