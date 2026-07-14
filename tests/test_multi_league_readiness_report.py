import sqlite3
from scorerecord.ingest.readiness import audit_leagues

def test_no_data_returns_not_supported():
    conn = sqlite3.connect(":memory:")
    # Needs some basic schema
    conn.execute("CREATE TABLE historical_match (league_code TEXT, home_team TEXT, away_team TEXT, match_date TEXT)")
    conn.execute("CREATE TABLE fixture (fixture_id TEXT)")
    conn.execute("CREATE TABLE odds_snapshots (fixture_id TEXT)")
    conn.execute("CREATE TABLE prediction (fixture_id TEXT)")
    conn.execute("CREATE TABLE model_artifact (market_validation_status TEXT)")
    
    report = audit_leagues(conn, ["EPL"])
    assert report["leagues_requested"] == ["EPL"]
    assert len(report["league_reports"]) == 1
    
    epl = report["league_reports"][0]
    assert epl["league"] == "EPL"
    assert epl["readiness_state"] == "NOT_SUPPORTED"
    assert epl["historical_match_count"] == 0
    
    # Check schema readiness structure
    assert report["schema_readiness"]["fixture_has_league_code"] is False

def test_fixture_schema_blocker():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE historical_match (league_code TEXT, home_team TEXT, away_team TEXT, match_date TEXT)")
    conn.execute("CREATE TABLE fixture (fixture_id TEXT)") # NO league_code
    
    conn.execute("INSERT INTO historical_match (league_code, match_date, home_team, away_team) VALUES ('LA_LIGA', '2026-01-01', 'A', 'B')")
    
    report = audit_leagues(conn, ["LA_LIGA"])
    
    la_liga = report["league_reports"][0]
    assert la_liga["readiness_state"] == "NEEDS_FIXTURE_SCHEMA"
    assert "Fixture table lacks league_code" in la_liga["blockers"][0]
    assert report["recommended_next_phase"] == "Phase 13.1 — Fixture schema + provider selection"

def test_needs_fixture_source():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE historical_match (league_code TEXT, home_team TEXT, away_team TEXT, match_date TEXT)")
    conn.execute("CREATE TABLE fixture (fixture_id TEXT, league_code TEXT, home_id INTEGER, away_id INTEGER)")
    
    conn.execute("INSERT INTO historical_match (league_code, match_date, home_team, away_team) VALUES ('SERIE_A', '2026-01-01', 'A', 'B')")
    # No fixtures inserted
    
    report = audit_leagues(conn, ["SERIE_A"])
    serie_a = report["league_reports"][0]
    assert serie_a["readiness_state"] == "NEEDS_FIXTURE_SOURCE"
    assert report["recommended_next_phase"] == "Phase 13.1 — Fixture provider selection"

def test_needs_team_mapping():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE historical_match (league_code TEXT, home_team TEXT, away_team TEXT, match_date TEXT)")
    conn.execute("CREATE TABLE fixture (fixture_id TEXT, league_code TEXT, home_id INTEGER, away_id INTEGER)")
    conn.execute("CREATE TABLE team_alias (alias TEXT, team_id INTEGER)")
    conn.execute("CREATE TABLE team (team_id INTEGER)")
    
    conn.execute("INSERT INTO historical_match (league_code, match_date, home_team, away_team) VALUES ('LIGA_MX', '2026-01-01', 'America', 'Chivas')")
    conn.execute("INSERT INTO fixture (fixture_id, league_code, home_id, away_id) VALUES ('f1', 'LIGA_MX', 1, 2)")
    # No aliases mapped
    
    report = audit_leagues(conn, ["LIGA_MX"])
    mx = report["league_reports"][0]
    assert mx["readiness_state"] == "NEEDS_TEAM_MAPPING"
    assert mx["team_alias_count"] == 0
    assert mx["unique_team_count"] == 2
    assert report["recommended_next_phase"] == "Phase 13.1 — Team alias mapping hardening"

def test_ready_for_pilot():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE historical_match (league_code TEXT, home_team TEXT, away_team TEXT, match_date TEXT)")
    conn.execute("CREATE TABLE fixture (fixture_id TEXT, league_code TEXT, home_id INTEGER, away_id INTEGER)")
    conn.execute("CREATE TABLE team_alias (alias TEXT, team_id INTEGER)")
    conn.execute("CREATE TABLE team (team_id INTEGER)")
    conn.execute("CREATE TABLE team_rating (team_id INTEGER)")
    
    conn.execute("INSERT INTO historical_match (league_code, match_date, home_team, away_team) VALUES ('EPL', '2026-01-01', 'Arsenal', 'Chelsea')")
    conn.execute("INSERT INTO fixture (fixture_id, league_code, home_id, away_id) VALUES ('f1', 'EPL', 1, 2)")
    
    # Map teams
    conn.execute("INSERT INTO team (team_id) VALUES (1)")
    conn.execute("INSERT INTO team (team_id) VALUES (2)")
    conn.execute("INSERT INTO team_alias (alias, team_id) VALUES ('Arsenal', 1)")
    conn.execute("INSERT INTO team_alias (alias, team_id) VALUES ('Chelsea', 2)")
    
    # Add ratings
    conn.execute("INSERT INTO team_rating (team_id) VALUES (1)")
    conn.execute("INSERT INTO team_rating (team_id) VALUES (2)")
    
    report = audit_leagues(conn, ["EPL"])
    epl = report["league_reports"][0]
    assert epl["readiness_state"] == "READY_FOR_PILOT"
    assert epl["rating_coverage"] == 1.0
    assert report["recommended_next_phase"] == "Phase 13.1 — Single-league pilot"

def test_no_ev_language_in_output():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE historical_match (league_code TEXT, home_team TEXT, away_team TEXT, match_date TEXT)")
    conn.execute("CREATE TABLE fixture (fixture_id TEXT)")
    report = audit_leagues(conn, ["EPL"])
    report_str = str(report).upper()
    assert "'EV'" not in report_str
    assert "KELLY" not in report_str
    assert "STAKE" not in report_str
    assert "VALIDATED_RECOMMENDATION" not in report_str
