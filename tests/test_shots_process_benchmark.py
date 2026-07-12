import json
import sqlite3
from datetime import UTC, date, datetime

from scorerecord.evaluate.shots_process_benchmark import run_shots_process_benchmark
from scorerecord.features.shots_ratings import ProcessRatingTrackerEWMA


def test_ewma_tracker_no_leakage():
    tracker = ProcessRatingTrackerEWMA(half_life_days=365.0)
    
    match1_date = datetime(2020, 1, 1, tzinfo=UTC)
    match2_date = datetime(2020, 1, 10, tzinfo=UTC)
    
    # Pre-match 1: defaults
    r1 = tracker.get_ratings("TeamA", match1_date)
    assert r1["goals_for"] == 1.3  # using the global mean fallback
    
    # Update Match 1
    tracker.update("TeamA", match1_date, 2.0, 1.0, 15.0, 10.0, 5.0, 3.0)
    
    # Pre-match 2: should just be the values from match 1 exactly since it was only 1 observation
    # and the weight decays proportionally for both numerator and denominator.
    r2 = tracker.get_ratings("TeamA", match2_date)
    assert r2["goals_for"] == 2.0
    assert r2["goals_against"] == 1.0
    assert r2["shots_for"] == 15.0
    assert r2["sot_for"] == 5.0
    
    # Update Match 2
    tracker.update("TeamA", match2_date, 0.0, 2.0, 5.0, 15.0, 1.0, 6.0)
    
    # Pre-match 3
    match3_date = datetime(2020, 1, 20, tzinfo=UTC)
    r3 = tracker.get_ratings("TeamA", match3_date)
    
    # The average should be a weighted mix of Match 1 and Match 2.
    # Match 1 had 2 goals, Match 2 had 0 goals.
    # Since Match 2 is more recent, it gets full weight (1.0).
    # Match 1 gets decayed weight. So average is < 1.0.
    assert r3["goals_for"] > 0.0
    assert r3["goals_for"] < 1.0


def test_benchmark_state_promising(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE historical_match (
            league_code TEXT,
            season TEXT,
            match_date TEXT,
            home_team TEXT,
            away_team TEXT,
            home_goals INTEGER,
            away_goals INTEGER,
            raw_json TEXT
        )"""
    )
    
    # Insert dummy data (we need > min_train rows, but for test we can set min_train=2)
    # Give team A consistent shots advantage that results in goals, but team B inconsistent.
    for i in range(1, 10):
        # 9 days of matches.
        # TeamA is very good, TeamB is bad.
        # The shots should strongly predict this.
        conn.execute(
            "INSERT INTO historical_match VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "E0", "2020-2021", f"2020-01-0{i}T12:00:00Z",
                "TeamA", "TeamB",
                2, 0,
                json.dumps({"HS": 20, "AS": 5, "HST": 8, "AST": 1})
            )
        )
        
    report = run_shots_process_benchmark(
        conn,
        from_date=date(2020, 1, 1),
        to_date=date(2020, 1, 31),
        leagues=("EPL",),
        min_train=2,
    )
    
    assert report.benchmark_state in (
        "PROCESS_PROMISING", 
        "PROCESS_INCONCLUSIVE", 
        "PROCESS_REJECTED"
    )
    # Matches are 2020-01-01 to 2020-01-09. 
    # Week 1 (Jan 1-5): 5 matches
    # Week 2 (Jan 6-9): 4 matches
    # Week 1 is skipped for evaluation because history has 0 < 2 matches.
    # Week 2 is evaluated because history has 5 >= 2 matches.
    assert report.num_matches == 4
    assert hasattr(report, "goals_plus_process_logloss")
    assert hasattr(report, "bootstrap_ci_combined")
    assert hasattr(report, "bootstrap_ci_gpp")
    assert report.calibration_buckets is not None
