"""Tests for Goals V2 Lambda Modeling."""

import ast
import sqlite3
from datetime import date
from pathlib import Path

import numpy as np

from scorerecord.evaluate.goals_v2_lambda_benchmark import run_goals_v2_lambda_benchmark
from scorerecord.models.goals_v2_lambdas import (
    PoissonRegressor,
    compute_independent_probabilities,
)


def test_no_network_imports():
    """Ensure no external API or network clients are used."""
    src_dir = Path(__file__).parent.parent / "src" / "scorerecord"
    
    files_to_check = [
        src_dir / "models" / "goals_v2_lambdas.py",
        src_dir / "evaluate" / "goals_v2_lambda_benchmark.py"
    ]
    
    forbidden_modules = {
        "urllib", "requests", "aiohttp", "httpx", "http",
        "api_football_probe", "provider_probe"
    }
    
    for py_file in files_to_check:
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split('.')[0] not in forbidden_modules
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    assert node.module.split('.')[0] not in forbidden_modules


def test_independent_probabilities():
    """Verify independent Poisson probability logic."""
    lam_h = np.array([1.5, 0.5])
    lam_a = np.array([1.0, 2.0])
    
    res = compute_independent_probabilities(lam_h, lam_a)
    
    # 0 <= p <= 1
    for k in res:
        assert np.all(res[k] >= 0.0)
        assert np.all(res[k] <= 1.0)
        
    # BTTS logic
    np.testing.assert_allclose(res["p_btts_yes"] + res["p_btts_no"], 1.0)
    
    # Monotonicity
    assert np.all(res["p_over_15"] >= res["p_over_25"])
    assert np.all(res["p_over_25"] >= res["p_over_35"])


def test_poisson_regressor_warm_start():
    """Test warm_start behavior resets or keeps weights on repeated fit."""
    X_train1 = np.array([[100.0, 1.0], [200.0, 2.0], [300.0, 3.0]])
    y_train1 = np.array([1, 2, 3])
    
    # 1. warm_start=False (default behavior)
    mod_no_warm = PoissonRegressor(n_features=2, learning_rate=0.01, epochs=10, warm_start=False)
    mod_no_warm.fit(X_train1, y_train1)
    w1 = mod_no_warm.w.copy()
    
    # Fit on same data again
    mod_no_warm.fit(X_train1, y_train1)
    w2 = mod_no_warm.w.copy()
    # It resets weights and runs 10 epochs again, so w1 should exactly equal w2 (deterministic GD)
    np.testing.assert_allclose(w1, w2)
    
    # Fit on different data
    X_train2 = np.array([[50.0, 0.5], [150.0, 1.5]])
    y_train2 = np.array([1, 1])
    mod_no_warm.fit(X_train2, y_train2)
    w3 = mod_no_warm.w.copy()
    # Now w3 should be entirely different and based solely on X_train2, not building on w2
    assert not np.allclose(w2, w3)
    
    # 2. warm_start=True
    mod_warm = PoissonRegressor(n_features=2, learning_rate=0.01, epochs=10, warm_start=True)
    mod_warm.fit(X_train1, y_train1)
    w_warm_1 = mod_warm.w.copy()
    
    # Fit on same data again
    mod_warm.fit(X_train1, y_train1)
    w_warm_2 = mod_warm.w.copy()
    # It built on top of w_warm_1, doing 10 MORE epochs, so w_warm_1 and w_warm_2 should differ
    assert not np.allclose(w_warm_1, w_warm_2)
    
    # Mean and std scaling are train-only and should update
    np.testing.assert_allclose(mod_warm.mean_x, [200.0, 2.0])
    pred = mod_warm.predict(np.array([[200.0, 2.0]]))
    assert len(pred) == 1
    assert pred[0] > 0


def test_goals_v2_lambda_benchmark_walk_forward():
    """Test walk forward prequential block logic on mock data."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE historical_match (
            id INTEGER PRIMARY KEY,
            league_code TEXT,
            season TEXT,
            match_date TEXT,
            home_team TEXT,
            away_team TEXT,
            home_goals INTEGER,
            away_goals INTEGER,
            raw_json TEXT
        )
        """
    )
    
    # Insert a few days of data
    rows = []
    for day in range(1, 15):
        rows.append((
            f"2023-01-{day:02d}T15:00:00Z", 
            "E0", "2022-2023", "TeamA", "TeamB", 1, 1, 
            '{"HS": 10, "AS": 5, "HST": 4, "AST": 2}'
        ))
    
    # Row with missing stats (should be skipped by benchmark)
    rows.append((
        "2023-01-16T15:00:00Z", 
        "E0", "2022-2023", "TeamA", "TeamB", 1, 1, 
        '{"HS": null}'
    ))
    
    conn.executemany(
        """
        INSERT INTO historical_match 
        (match_date, league_code, season, home_team, away_team, home_goals, away_goals, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows
    )
    
    report = run_goals_v2_lambda_benchmark(
        conn,
        from_date=date(2023, 1, 1),
        to_date=date(2023, 1, 31),
        leagues=("EPL",),
        min_train=2,
        bootstrap_samples=10,
    )
    
    assert hasattr(report, "goals_baseline_joint_home_away_ll")
    assert hasattr(report, "goals_baseline_total_goals_pseudo_ll_excl_factorial")
    assert report.num_evaluated_matches > 0
    assert report.raw_matches_loaded == 15
    assert report.usable_matches_after_missing_stats_filter == 14
    assert report.skipped_missing_stats == 1
    assert hasattr(report, "lambda_total_calibration_buckets")
    assert report.benchmark_state in ["LAMBDA_PROMISING", "LAMBDA_REJECTED", "LAMBDA_INCONCLUSIVE"]
    assert "goals_baseline" in report.lambda_total_calibration_buckets
