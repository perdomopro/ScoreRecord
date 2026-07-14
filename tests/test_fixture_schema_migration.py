import sqlite3
from scorerecord.db import _run_lightweight_migrations, schema_version, init_db

def test_migration_adds_columns_and_is_idempotent():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    
    # Drop columns to simulate older version
    conn.execute("DROP INDEX IF EXISTS idx_fixture_league_kickoff")
    conn.execute("DROP INDEX IF EXISTS idx_fixture_source_event")
    conn.execute("ALTER TABLE fixture DROP COLUMN league_code")
    conn.execute("ALTER TABLE fixture DROP COLUMN competition")
    conn.execute("ALTER TABLE fixture DROP COLUMN season")
    conn.execute("ALTER TABLE fixture DROP COLUMN source_event_id")
    
    # Run once
    _run_lightweight_migrations(conn)
    
    # Check columns
    cols = {c[1] for c in conn.execute("PRAGMA table_info(fixture)")}
    assert "league_code" in cols
    assert "competition" in cols
    assert "season" in cols
    assert "source_event_id" in cols
    
    # Check idempotency
    _run_lightweight_migrations(conn)
    cols2 = {c[1] for c in conn.execute("PRAGMA table_info(fixture)")}
    assert cols == cols2

def test_migration_backfills_world_cup():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    
    # Drop columns
    conn.execute("DROP INDEX IF EXISTS idx_fixture_league_kickoff")
    conn.execute("DROP INDEX IF EXISTS idx_fixture_source_event")
    conn.execute("ALTER TABLE fixture DROP COLUMN league_code")
    conn.execute("ALTER TABLE fixture DROP COLUMN competition")
    conn.execute("ALTER TABLE fixture DROP COLUMN season")
    conn.execute("ALTER TABLE fixture DROP COLUMN source_event_id")
    
    # Add them back empty
    conn.execute("ALTER TABLE fixture ADD COLUMN league_code TEXT")
    conn.execute("ALTER TABLE fixture ADD COLUMN competition TEXT")
    conn.execute("ALTER TABLE fixture ADD COLUMN season TEXT")
    conn.execute("ALTER TABLE fixture ADD COLUMN source_event_id TEXT")
    
    conn.execute("INSERT INTO fixture (fixture_id, source) VALUES ('wc2026-match-101', 'openfootball')")
    conn.execute("INSERT INTO fixture (fixture_id, source) VALUES ('other', 'other_source')")
    
    _run_lightweight_migrations(conn)
    
    row1 = conn.execute("SELECT league_code FROM fixture WHERE fixture_id = 'wc2026-match-101'").fetchone()
    assert row1[0] == "WORLD_CUP_2026"
    
    row2 = conn.execute("SELECT league_code FROM fixture WHERE fixture_id = 'other'").fetchone()
    assert row2[0] is None
