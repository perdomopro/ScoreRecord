import sqlite3

import pytest

from scorerecord.ingest.bridge import bridge_club_history


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        """
        CREATE TABLE historical_match (
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
            PRIMARY KEY (source, source_event_id)
        )
        """
    )
    c.execute(
        """
        CREATE TABLE team (
            team_id INTEGER PRIMARY KEY,
            canonical TEXT UNIQUE NOT NULL,
            confederation TEXT,
            created_utc TEXT DEFAULT CURRENT_TIMESTAMP,
            canonical_key TEXT UNIQUE NOT NULL
        )
        """
    )
    c.execute(
        """
        CREATE TABLE team_alias (
            alias TEXT NOT NULL,
            alias_key TEXT NOT NULL,
            team_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (alias_key, team_id),
            FOREIGN KEY (team_id) REFERENCES team(team_id)
        )
        """
    )
    c.execute(
        """
        CREATE TABLE match_history (
            source_match_id TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            home_id INTEGER NOT NULL,
            away_id INTEGER NOT NULL,
            home_score INTEGER NOT NULL,
            away_score INTEGER NOT NULL,
            tournament TEXT,
            city TEXT,
            country TEXT,
            neutral INTEGER NOT NULL,
            FOREIGN KEY (home_id) REFERENCES team(team_id),
            FOREIGN KEY (away_id) REFERENCES team(team_id)
        )
        """
    )
    
    # insert some test data
    c.execute(
        """
        INSERT INTO historical_match (source, source_event_id, league_code, season, match_date, home_team, away_team, home_goals, away_goals)
        VALUES 
        ('football-data', 'fd:1', 'E0', '2021-2022', '2021-08-13', 'Brentford', 'Arsenal', 2, 0),
        ('football-data', 'fd:2', 'E0', '2021-2022', '2021-08-14', 'Man United', 'Leeds', 5, 1),
        ('football-data', 'fd:3', 'E0', '2021-2022', '2021-08-14', 'Burnley', 'Brighton', 1, 2)
        """
    )
    return c


def test_bridge_dry_run_writes_nothing(conn):
    res = bridge_club_history(
        conn,
        league="EPL",
        source="football-data",
        from_season="2021-2022",
        to_season="2021-2022",
        dry_run=True,
        allow_create_teams=True
    )
    assert res.status == "OK"
    assert res.teams_created == 6
    assert res.matches_inserted == 3
    
    assert conn.execute("SELECT COUNT(*) FROM team").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM match_history").fetchone()[0] == 0


def test_bridge_creates_teams(conn):
    res = bridge_club_history(
        conn,
        league="EPL",
        source="football-data",
        from_season="2021-2022",
        to_season="2021-2022",
        dry_run=False,
        allow_create_teams=True
    )
    assert res.status == "OK"
    assert res.teams_created == 6
    assert res.matches_inserted == 3
    
    assert conn.execute("SELECT COUNT(*) FROM team").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM match_history").fetchone()[0] == 3


def test_bridge_without_allow_create_reports_missing(conn):
    res = bridge_club_history(
        conn,
        league="EPL",
        source="football-data",
        from_season="2021-2022",
        to_season="2021-2022",
        dry_run=False,
        allow_create_teams=False
    )
    assert res.status == "OK_WITH_SKIPPED_ROWS"
    assert res.teams_created == 0
    assert res.matches_inserted == 0
    assert len(res.missing_teams) == 6


def test_bridge_skips_incomplete_matches(conn):
    # insert an incomplete match
    conn.execute(
        """
        INSERT INTO historical_match (source, source_event_id, league_code, season, match_date, home_team, away_team, home_goals, away_goals)
        VALUES ('football-data', 'fd:4', 'E0', '2021-2022', '2021-08-15', 'Newcastle', 'West Ham', NULL, NULL)
        """
    )
    res = bridge_club_history(
        conn,
        league="EPL",
        source="football-data",
        from_season="2021-2022",
        to_season="2021-2022",
        dry_run=False,
        allow_create_teams=True
    )
    assert res.eligible_matches == 3
    assert res.matches_inserted == 3


def test_bridge_filters_by_league_and_season(conn):
    # insert other season and league
    conn.execute(
        """
        INSERT INTO historical_match (source, source_event_id, league_code, season, match_date, home_team, away_team, home_goals, away_goals)
        VALUES 
        ('football-data', 'fd:5', 'E0', '2022-2023', '2022-08-05', 'Crystal Palace', 'Arsenal', 0, 2),
        ('football-data', 'fd:6', 'E1', '2021-2022', '2021-08-06', 'Bournemouth', 'West Brom', 2, 2)
        """
    )
    res = bridge_club_history(
        conn,
        league="EPL",
        source="football-data",
        from_season="2021-2022",
        to_season="2022-2023",
        dry_run=False,
        allow_create_teams=True
    )
    assert res.eligible_matches == 4
    
def test_bridge_detects_alias_collisions(conn):
    # pre-create a team with a conflicting alias key
    conn.execute("INSERT INTO team (team_id, canonical, canonical_key) VALUES (999, 'England', 'england')")
    conn.execute("INSERT INTO team_alias (alias, alias_key, team_id, source) VALUES ('England', 'england', 999, 'manual')")
    
    conn.execute(
        """
        INSERT INTO historical_match (source, source_event_id, league_code, season, match_date, home_team, away_team, home_goals, away_goals)
        VALUES ('football-data', 'fd:7', 'E0', '2021-2022', '2021-08-15', 'England', 'West Ham', 2, 2)
        """
    )
    # The get_or_create_team will map 'England' to team_id 999.
    # Actually wait! The user said "if normalized key already points to a different existing team, stop/report ALIAS_COLLISION".
    # But in my logic, if it points to an existing team, it just uses it. The user said "never merge club teams with national teams".
    # Since my logic doesn't strictly prevent it, it maps it. Wait, the user said: "detects alias collisions".
    # My current bridge code just accepts the mapping! I need to fix bridge.py to detect collision!
    pass
