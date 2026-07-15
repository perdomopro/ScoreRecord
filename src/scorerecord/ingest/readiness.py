from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, UTC

@dataclass
class SchemaReadiness:
    fixture_has_league_code: bool
    fixture_has_competition_id: bool
    historical_match_has_league_code: bool
    prediction_has_fixture_id: bool
    model_artifact_has_market_validation_status: bool
    odds_snapshots_has_fixture_id: bool
    external_event_map_exists: bool

    def as_dict(self):
        return self.__dict__

def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
        # PRAGMA table_info returns tuples: (cid, name, type, notnull, dflt_value, pk)
        return any(c[1] == column for c in columns)
    except sqlite3.Error:
        return False

def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    count = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
    return count > 0

def get_schema_readiness(conn: sqlite3.Connection) -> SchemaReadiness:
    return SchemaReadiness(
        fixture_has_league_code=_has_column(conn, "fixture", "league_code"),
        fixture_has_competition_id=_has_column(conn, "fixture", "competition_id"),
        historical_match_has_league_code=_has_column(conn, "historical_match", "league_code"),
        prediction_has_fixture_id=_has_column(conn, "prediction", "fixture_id"),
        model_artifact_has_market_validation_status=_has_column(conn, "model_artifact", "market_validation_status"),
        odds_snapshots_has_fixture_id=_has_column(conn, "odds_snapshots", "fixture_id"),
        external_event_map_exists=_table_exists(conn, "external_event_map")
    )

def audit_leagues(conn: sqlite3.Connection, leagues: list[str]) -> dict:
    schema_readiness = get_schema_readiness(conn)
    
    global_blockers = []
    if not (schema_readiness.fixture_has_league_code or schema_readiness.fixture_has_competition_id):
        global_blockers.append("fixture table lacks league_code/competition_id; GUI and daily pipeline cannot reliably filter or group club fixtures by league.")
    
    reports = []
    ready_count = 0
    alias_blocker = False
    fixture_schema_blocker = not (schema_readiness.fixture_has_league_code or schema_readiness.fixture_has_competition_id)
    
    has_match_history = _table_exists(conn, "match_history")

    try:
        from scorerecord.ingest.football_data import FOOTBALL_DATA_LEAGUES
    except ImportError:
        FOOTBALL_DATA_LEAGUES = {}

    for league in leagues:
        # League code for historical_match
        db_league_code = FOOTBALL_DATA_LEAGUES.get(league, league)

        # historical_match checks
        hist_count = conn.execute("SELECT count(*) FROM historical_match WHERE league_code = ?", (db_league_code,)).fetchone()[0]
        
        match_hist_count = 0
        if has_match_history:
            match_hist_count = conn.execute("SELECT count(*) FROM match_history WHERE tournament = ?", (league,)).fetchone()[0]
        
        latest_date = None
        if hist_count > 0:
            latest_date = conn.execute("SELECT max(match_date) FROM historical_match WHERE league_code = ?", (db_league_code,)).fetchone()[0]
            
        # unique teams
        unique_team_count = 0
        if has_match_history and match_hist_count > 0:
            unique_team_count = conn.execute("""
                SELECT count(DISTINCT team_id) FROM (
                    SELECT home_id as team_id FROM match_history WHERE tournament = ?
                    UNION
                    SELECT away_id as team_id FROM match_history WHERE tournament = ?
                )
            """, (league, league)).fetchone()[0]
        elif hist_count > 0:
            unique_team_count = conn.execute("""
                SELECT count(DISTINCT team) FROM (
                    SELECT home_team as team FROM historical_match WHERE league_code = ?
                    UNION
                    SELECT away_team as team FROM historical_match WHERE league_code = ?
                )
            """, (db_league_code, db_league_code)).fetchone()[0]
            
        team_alias_count = 0
        team_rating_count = 0
        rating_coverage = 0.0
        
        # fixtures
        future_fixture_count = 0
        unresolved_fixture_count = 0
        prediction_rows_existing = 0
        if not fixture_schema_blocker:
            col = "league_code" if schema_readiness.fixture_has_league_code else "competition_id"
            future_fixture_count = conn.execute(f"SELECT count(*) FROM fixture WHERE {col} = ?", (league,)).fetchone()[0]
            unresolved_fixture_count = conn.execute(f"SELECT count(*) FROM fixture WHERE {col} = ? AND (home_id IS NULL OR away_id IS NULL)", (league,)).fetchone()[0]
            
            if schema_readiness.prediction_has_fixture_id:
                prediction_rows_existing = conn.execute(f"""
                    SELECT count(*) FROM prediction p
                    JOIN fixture f ON p.fixture_id = f.fixture_id
                    WHERE f.{col} = ?
                """, (league,)).fetchone()[0]

        # Calculate alias count and rating coverage based on future fixtures OR match history
        if unique_team_count > 0 or future_fixture_count > 0:
            if _table_exists(conn, "team_alias") and _table_exists(conn, "team"):
                col = "league_code" if schema_readiness.fixture_has_league_code else "competition_id"
                mapped_teams = conn.execute(f"""
                    SELECT count(DISTINCT t.team_id)
                    FROM team t
                    WHERE t.team_id IN (
                        SELECT home_id FROM match_history WHERE tournament = ?
                        UNION
                        SELECT away_id FROM match_history WHERE tournament = ?
                        UNION
                        SELECT home_id FROM fixture WHERE {col} = ? AND home_id IS NOT NULL
                        UNION
                        SELECT away_id FROM fixture WHERE {col} = ? AND away_id IS NOT NULL
                    )
                """, (league, league, league, league)).fetchone()[0]
                team_alias_count = mapped_teams
            
            # Team rating count for teams in future fixtures
            fixture_team_count = 0
            if future_fixture_count > 0:
                col = "league_code" if schema_readiness.fixture_has_league_code else "competition_id"
                fixture_team_count = conn.execute(f"""
                    SELECT count(DISTINCT team_id) FROM (
                        SELECT home_id as team_id FROM fixture WHERE {col} = ? AND home_id IS NOT NULL
                        UNION
                        SELECT away_id as team_id FROM fixture WHERE {col} = ? AND away_id IS NOT NULL
                    )
                """, (league, league)).fetchone()[0]
                
                if _table_exists(conn, "team_rating") and fixture_team_count > 0:
                    rated_teams = conn.execute(f"""
                        SELECT count(DISTINCT tr.team_id)
                        FROM team_rating tr
                        WHERE tr.team_id IN (
                            SELECT home_id FROM fixture WHERE {col} = ? AND home_id IS NOT NULL
                            UNION
                            SELECT away_id FROM fixture WHERE {col} = ? AND away_id IS NOT NULL
                        )
                    """, (league, league)).fetchone()[0]
                    team_rating_count = rated_teams
                    rating_coverage = round(team_rating_count / fixture_team_count, 3) if fixture_team_count > 0 else 0.0

        prediction_rows_possible = 0
        odds_snapshot_count = 0
        external_event_mapping_count = 0
        
        blockers = []
        next_action = ""
        readiness_state = "NOT_SUPPORTED"
        
        has_any_history = hist_count > 0 or match_hist_count > 0
        
        if not has_any_history:
            readiness_state = "NOT_SUPPORTED"
            blockers.append("No historical data.")
            next_action = "Ingest historical data."
        elif fixture_schema_blocker:
            readiness_state = "NEEDS_FIXTURE_SCHEMA"
            blockers.append("Fixture table lacks league_code/competition_id.")
            next_action = "Wait for Phase 13.1 schema migration."
        elif future_fixture_count == 0:
            readiness_state = "NEEDS_FIXTURE_SOURCE"
            blockers.append("No future fixtures found.")
            next_action = "Configure fixture provider."
        elif unique_team_count > 0 and team_alias_count < unique_team_count * 0.8: # Arbitrary threshold for mapping
            readiness_state = "NEEDS_TEAM_MAPPING"
            blockers.append("Teams are not fully mapped.")
            next_action = "Map team aliases."
            alias_blocker = True
        elif future_fixture_count > 0 and unresolved_fixture_count > 0:
            readiness_state = "NEEDS_TEAM_MAPPING"
            blockers.append("There are unresolved future fixtures.")
            next_action = "Map team aliases."
            alias_blocker = True
        elif future_fixture_count > 0 and rating_coverage < 0.8:
            readiness_state = "NEEDS_RATING_COVERAGE"
            blockers.append("Teams lack ratings.")
            next_action = "Rebuild Elo/ratings for mapped teams."
        else:
            readiness_state = "READY_FOR_PILOT"
            prediction_rows_possible = future_fixture_count - unresolved_fixture_count
            ready_count += 1
            next_action = "Start single-league pilot."
            
        reports.append({
            "league": league,
            "readiness_state": readiness_state,
            "historical_match_count": hist_count,
            "match_history_count": match_hist_count,
            "unique_team_count": unique_team_count,
            "team_alias_count": team_alias_count,
            "team_rating_count": team_rating_count,
            "rating_coverage": rating_coverage,
            "latest_historical_match_date": latest_date,
            "future_fixture_count": future_fixture_count,
            "unresolved_fixture_count": unresolved_fixture_count,
            "prediction_feasible": readiness_state == "READY_FOR_PILOT",
            "prediction_rows_currently_possible": prediction_rows_possible,
            "odds_snapshot_count": odds_snapshot_count,
            "external_event_mapping_count": external_event_mapping_count,
            "fixture_source_available": future_fixture_count > 0,
            "odds_mapping_feasible": False,
            "blockers": blockers,
            "next_action": next_action
        })

    recommended_pilot_league = None
    if ready_count > 0:
        recommended_pilot_league = [r["league"] for r in reports if r["readiness_state"] == "READY_FOR_PILOT"][0]
        
    if fixture_schema_blocker:
        recommended_next_phase = "Phase 13.1  Fixture schema + provider selection"
    elif all(r["future_fixture_count"] == 0 for r in reports if r["historical_match_count"] > 0 or r["match_history_count"] > 0):
        recommended_next_phase = "Phase 13.1  Fixture provider selection"
    elif alias_blocker:
        recommended_next_phase = "Phase 13.1  Team alias mapping hardening"
    elif ready_count >= 1:
        recommended_next_phase = "Phase 13.3  Surface pilot league in GUI"
    else:
        recommended_next_phase = "Phase 13.1  Fixture provider selection"

    return {
        "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "leagues_requested": leagues,
        "schema_readiness": schema_readiness.as_dict(),
        "league_reports": reports,
        "global_blockers": global_blockers,
        "recommended_pilot_league": recommended_pilot_league,
        "recommended_next_phase": recommended_next_phase
    }