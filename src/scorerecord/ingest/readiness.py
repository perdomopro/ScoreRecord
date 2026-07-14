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

    for league in leagues:
        # historical_match checks
        hist_count = conn.execute("SELECT count(*) FROM historical_match WHERE league_code = ?", (league,)).fetchone()[0]
        match_hist_count = 0
        if has_match_history:
            match_hist_count = conn.execute("SELECT count(*) FROM match_history WHERE tournament = ?", (league,)).fetchone()[0]
            # Since historical_match matches tournament in some models, or we just rely on historical_match. 
            # Actually historical_match has league_code. We use hist_count.
        
        latest_date = None
        if hist_count > 0:
            latest_date = conn.execute("SELECT max(match_date) FROM historical_match WHERE league_code = ?", (league,)).fetchone()[0]
            
        # teams
        unique_team_count = 0
        if hist_count > 0:
            unique_team_count = conn.execute("""
                SELECT count(DISTINCT team) FROM (
                    SELECT home_team as team FROM historical_match WHERE league_code = ?
                    UNION
                    SELECT away_team as team FROM historical_match WHERE league_code = ?
                )
            """, (league, league)).fetchone()[0]
            
        team_alias_count = 0 # Not easily isolated by league unless joined
        team_rating_count = 0
        rating_coverage = 0.0
        
        if unique_team_count > 0:
            if _table_exists(conn, "team_alias") and _table_exists(conn, "team"):
                mapped_teams = conn.execute("""
                    SELECT count(DISTINCT t.team_id)
                    FROM team_alias a
                    JOIN team t ON a.team_id = t.team_id
                    WHERE a.alias IN (
                        SELECT home_team FROM historical_match WHERE league_code = ?
                        UNION
                        SELECT away_team FROM historical_match WHERE league_code = ?
                    )
                """, (league, league)).fetchone()[0]
                team_alias_count = mapped_teams
            
            if _table_exists(conn, "team_rating") and _table_exists(conn, "team_alias"):
                rated_teams = conn.execute("""
                    SELECT count(DISTINCT tr.team_id)
                    FROM team_rating tr
                    JOIN team_alias a ON tr.team_id = a.team_id
                    WHERE a.alias IN (
                        SELECT home_team FROM historical_match WHERE league_code = ?
                        UNION
                        SELECT away_team FROM historical_match WHERE league_code = ?
                    )
                """, (league, league)).fetchone()[0]
                team_rating_count = rated_teams
            
            rating_coverage = round(team_rating_count / unique_team_count, 3) if unique_team_count > 0 else 0.0

        # fixtures
        # Since fixture lacks league_code, we can't accurately count future_fixture_count per league yet.
        # If schema is blocked, future_fixture_count is 0 reliably.
        future_fixture_count = 0
        unresolved_fixture_count = 0
        if not fixture_schema_blocker:
            col = "league_code" if schema_readiness.fixture_has_league_code else "competition_id"
            future_fixture_count = conn.execute(f"SELECT count(*) FROM fixture WHERE {col} = ?", (league,)).fetchone()[0]
            unresolved_fixture_count = conn.execute(f"SELECT count(*) FROM fixture WHERE {col} = ? AND (home_id IS NULL OR away_id IS NULL)", (league,)).fetchone()[0]

        prediction_rows_possible = 0
        odds_snapshot_count = 0
        external_event_mapping_count = 0
        
        blockers = []
        next_action = ""
        readiness_state = "NOT_SUPPORTED"
        
        if hist_count == 0:
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
        elif team_alias_count < unique_team_count * 0.8: # Arbitrary threshold for mapping
            readiness_state = "NEEDS_TEAM_MAPPING"
            blockers.append("Teams are not fully mapped.")
            next_action = "Map team aliases."
            alias_blocker = True
        elif rating_coverage < 0.8:
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
            "odds_mapping_feasible": False, # Requires external_event_map and Odds API implementation
            "blockers": blockers,
            "next_action": next_action
        })

    recommended_pilot_league = None
    if ready_count > 0:
        recommended_pilot_league = [r["league"] for r in reports if r["readiness_state"] == "READY_FOR_PILOT"][0]
        
    if fixture_schema_blocker:
        recommended_next_phase = "Phase 13.1 — Fixture schema + provider selection"
    elif all(r["future_fixture_count"] == 0 for r in reports if r["historical_match_count"] > 0):
        recommended_next_phase = "Phase 13.1 — Fixture provider selection"
    elif alias_blocker:
        recommended_next_phase = "Phase 13.1 — Team alias mapping hardening"
    elif ready_count == 1:
        recommended_next_phase = "Phase 13.1 — Single-league pilot"
    elif ready_count > 1:
        recommended_next_phase = "Phase 13.1 — Single-league pilot"
    else:
        recommended_next_phase = "Phase 13.1 — Fixture provider selection"

    return {
        "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "leagues_requested": leagues,
        "schema_readiness": schema_readiness.as_dict(),
        "league_reports": reports,
        "global_blockers": global_blockers,
        "recommended_pilot_league": recommended_pilot_league,
        "recommended_next_phase": recommended_next_phase
    }
