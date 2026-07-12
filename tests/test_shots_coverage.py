import json
import sqlite3
from pathlib import Path

from scorerecord.shots_coverage import audit_shots_coverage


def test_shots_coverage_states(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE historical_match (
            source TEXT,
            source_event_id TEXT,
            league_code TEXT,
            season TEXT,
            raw_json TEXT
        )
        """
    )
    
    # 1. NO_SOURCE
    # Insert nothing for E0 (EPL) in 2021-2022
    
    # 2. RAW_AVAILABLE_NOT_INGESTED
    # Insert DB row without shots metadata, but we will mock a raw CSV with HS, AS
    conn.execute(
        "INSERT INTO historical_match VALUES ('football-data', '1', 'D1', '2021-2022', '{}')"
    )
    
    # Mock raw file
    raw_dir = tmp_path / "data"
    d1_dir = raw_dir / "football-data" / "2122"
    d1_dir.mkdir(parents=True)
    with open(d1_dir / "D1.csv", "w", encoding="utf-8") as f:
        f.write("Div,Date,HomeTeam,AwayTeam,HS,AS\n")
        
    # 3. SHOTS_ONLY
    conn.execute(
        "INSERT INTO historical_match VALUES ('football-data', '2', 'I1', '2021-2022', ?)",
        (json.dumps({"HS": 10, "AS": 5}),)
    )
    
    # 4. SHOTS_AND_SOT
    conn.execute(
        "INSERT INTO historical_match VALUES ('football-data', '3', 'F1', '2021-2022', ?)",
        (json.dumps({"HS": 10, "AS": 5, "HST": 4, "AST": 2}),)
    )
    
    # 5. NATIVE_XG_AVAILABLE
    conn.execute(
        "INSERT INTO historical_match VALUES ('football-data', '4', 'SP1', '2021-2022', ?)",
        (json.dumps({"HS": 10, "AS": 5, "HST": 4, "AST": 2, "xG_home": 1.5, "expected_goals_away": 0.8}),)
    )
    
    # 6. V2_ELIGIBLE
    # Needs >= 3 seasons, >= 80% coverage, >= 800 matches.
    # Let's insert 800 matches across 3 seasons for E1
    for s in ("2020-2021", "2021-2022", "2022-2023"):
        for i in range(300):
            conn.execute(
                "INSERT INTO historical_match VALUES ('football-data', ?, 'E1', ?, ?)",
                (f"E1_{s}_{i}", s, json.dumps({"HS": 10, "AS": 5, "HST": 4, "AST": 2}))
            )
            
    report = audit_shots_coverage(
        conn,
        raw_dir,
        leagues=("EPL", "BUNDESLIGA", "SERIE_A", "LIGUE_1", "LA_LIGA", "CHAMPIONSHIP", "INVALID_LEAGUE"),
    )
    
    # Check NO_SOURCE (EPL -> E0)
    e0_rows = [r for r in report.rows if r.league == "E0"]
    assert len(e0_rows) == 1
    assert e0_rows[0].coverage_state == "NO_SOURCE"
    
    # Check RAW_AVAILABLE_NOT_INGESTED (BUNDESLIGA -> D1)
    d1_rows = [r for r in report.rows if r.league == "D1"]
    assert len(d1_rows) == 1
    assert d1_rows[0].coverage_state == "RAW_AVAILABLE_NOT_INGESTED"
    assert d1_rows[0].raw_has_shot_columns is True
    assert d1_rows[0].db_has_shot_columns is False
    
    # Check SHOTS_ONLY (SERIE_A -> I1)
    i1_rows = [r for r in report.rows if r.league == "I1"]
    assert len(i1_rows) == 1
    assert i1_rows[0].coverage_state == "SHOTS_ONLY"
    
    # Check SHOTS_AND_SOT (LIGUE_1 -> F1)
    f1_rows = [r for r in report.rows if r.league == "F1"]
    assert len(f1_rows) == 1
    assert f1_rows[0].coverage_state == "SHOTS_AND_SOT"
    
    # Check NATIVE_XG_AVAILABLE (LA_LIGA -> SP1)
    sp1_rows = [r for r in report.rows if r.league == "SP1"]
    assert len(sp1_rows) == 1
    assert sp1_rows[0].coverage_state == "NATIVE_XG_AVAILABLE"
    
    # Check V2_ELIGIBLE (CHAMPIONSHIP -> E1)
    e1_rows = [r for r in report.rows if r.league == "E1"]
    assert len(e1_rows) == 3
    for r in e1_rows:
        assert r.coverage_state == "V2_ELIGIBLE"
        
    # Check INVALID_LEAGUE
    inv_rows = [r for r in report.rows if r.league == "INVALID_LEAGUE"]
    assert len(inv_rows) == 1
    assert inv_rows[0].coverage_state == "SHOTS_UNAVAILABLE"
