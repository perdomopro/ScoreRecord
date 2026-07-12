"""Audits Football-Data CSVs and ScoreRecord DB for Shots/SOT/xG feasibility."""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from scorerecord.ingest.football_data import (
    FOOTBALL_DATA_LEAGUES,
    resolve_league_code,
)


@dataclass(frozen=True)
class ShotsCoverageRow:
    league: str
    season: str
    source: str
    raw_files_found: bool
    db_rows_found: bool
    matches_total: int
    matches_with_shots: int
    shots_coverage_pct: float
    matches_with_shots_on_target: int
    sot_coverage_pct: float
    matches_with_native_xg: int
    native_xg_coverage_pct: float
    raw_has_shot_columns: bool
    raw_has_sot_columns: bool
    db_has_shot_columns: bool
    db_has_sot_columns: bool
    ingestion_gap: bool
    coverage_state: str
    recommended_next_step: str


@dataclass(frozen=True)
class ShotsCoverageReport:
    date_range: str
    leagues_requested: str
    rows: list[ShotsCoverageRow]


def audit_shots_coverage(
    conn: sqlite3.Connection,
    raw_dir: Path,
    from_date: str | None = None,
    to_date: str | None = None,
    leagues: tuple[str, ...] | None = None,
) -> ShotsCoverageReport:
    """Audit Shots/SOT/xG coverage across raw files and DB."""
    
    # Simple season parsing if passed as full dates (e.g. 2018-08-01 to 2025-05-31)
    # The prompt actually uses --from 2018-08-01 --to 2025-05-31
    # We need to map dates to seasons or query historical_match by date.
    # The requirement is per league/season.
    
    # Let's extract all matches within date range grouped by league and season.
    # Or just query all matches and filter by date.
    
    # We will query historical_match
    
    query = """
        SELECT
            league_code,
            season,
            source,
            source_event_id,
            raw_json
        FROM historical_match
        WHERE 1=1
    """
    params: list[object] = []
    
    if from_date:
        query += " AND match_date >= ?"
        params.append(from_date)
    if to_date:
        query += " AND match_date <= ?"
        params.append(to_date)
        
    conn.row_factory = sqlite3.Row
    db_rows = conn.execute(query, params).fetchall()
    
    # Group by league_code, season
    # If specific leagues requested, map them
    requested_codes = set()
    if leagues:
        for lg in leagues:
            try:
                code = resolve_league_code(lg)
                requested_codes.add(code)
            except ValueError:
                # Unsupported alias
                requested_codes.add(lg)  # Keep original to report it
                
    grouped_db_matches = defaultdict(list)
    for r in db_rows:
        lg = r["league_code"]
        if requested_codes and lg not in requested_codes:
            continue
        grouped_db_matches[(lg, r["season"])].append(r)
        
    # Make sure we have entries for requested leagues even if no DB rows
    # so we can report NO_SOURCE
    if leagues:
        for lg in leagues:
            try:
                code = resolve_league_code(lg)
            except ValueError:
                code = lg
                
            # If we don't have any season for this code, add a dummy season to ensure it shows up in report
            has_seasons = any(k[0] == code for k in grouped_db_matches.keys())
            if not has_seasons:
                grouped_db_matches[(code, "unknown")].append(None)

    out_rows = []
    
    for (league_code, season), matches in grouped_db_matches.items():
        if matches and matches[0] is None:
            matches = []
            
        matches_total = len(matches)
        
        matches_with_shots = 0
        matches_with_sot = 0
        matches_with_native_xg = 0
        
        db_has_shot_columns = False
        db_has_sot_columns = False
        
        for m in matches:
            meta = json.loads(m["raw_json"]) if m["raw_json"] else {}
            
            # Check for shots (HS, AS)
            if "HS" in meta and "AS" in meta:
                matches_with_shots += 1
                db_has_shot_columns = True
                
            # Check for SOT (HST, AST)
            if "HST" in meta and "AST" in meta:
                matches_with_sot += 1
                db_has_sot_columns = True
                
            # Check for xG (e.g. xG_home, xG_away) - we are looking for native xG, not our proxy
            keys = str(meta).lower()
            if "xg" in keys or "expected_goals" in keys:
                matches_with_native_xg += 1
        
        # Now check raw files
        raw_files_found = False
        raw_has_shot_columns = False
        raw_has_sot_columns = False
        
        if season != "unknown" and league_code in FOOTBALL_DATA_LEAGUES.values():
            season_code = season.replace("-", "")[2:4] + season.replace("-", "")[6:8]
            raw_path = raw_dir / "football-data" / season_code / f"{league_code}.csv"
            if not raw_path.exists():
                # Try new current feed format
                raw_path = raw_dir / "football-data" / "new" / f"{league_code}.csv"
                
            if raw_path.exists():
                raw_files_found = True
                try:
                    with open(raw_path, encoding="utf-8", errors="replace") as f:
                        header = next(csv.reader(f), [])
                        if "HS" in header and "AS" in header:
                            raw_has_shot_columns = True
                        if "HST" in header and "AST" in header:
                            raw_has_sot_columns = True
                except Exception:
                    pass

        shots_pct = matches_with_shots / matches_total if matches_total > 0 else 0.0
        sot_pct = matches_with_sot / matches_total if matches_total > 0 else 0.0
        xg_pct = matches_with_native_xg / matches_total if matches_total > 0 else 0.0
        
        ingestion_gap = raw_has_shot_columns and not db_has_shot_columns
        
        # Determine state
        state = "UNKNOWN"
        next_step = "Investigate"
        
        if not raw_files_found and matches_total == 0:
            state = "NO_SOURCE"
            next_step = "Find new provider"
            if league_code not in FOOTBALL_DATA_LEAGUES.values() and league_code not in FOOTBALL_DATA_LEAGUES:
                state = "SHOTS_UNAVAILABLE"
                next_step = "Unsupported alias"
        elif ingestion_gap:
            state = "RAW_AVAILABLE_NOT_INGESTED"
            next_step = "Update DB ingestion schema"
        elif matches_with_native_xg > 0:
            state = "NATIVE_XG_AVAILABLE"
            next_step = "Proceed to Phase 10.1 (Native xG)"
        elif matches_with_shots > 0 and matches_with_sot > 0:
            state = "SHOTS_AND_SOT"
            next_step = "Evaluate for V2_ELIGIBLE"
        elif matches_with_shots > 0:
            state = "SHOTS_ONLY"
            next_step = "Limited use (no SOT)"
        else:
            state = "NO_SHOTS"
            next_step = "Cannot use for Phase 10"
            
        out_rows.append(
            ShotsCoverageRow(
                league=league_code,
                season=season,
                source="football-data",
                raw_files_found=raw_files_found,
                db_rows_found=matches_total > 0,
                matches_total=matches_total,
                matches_with_shots=matches_with_shots,
                shots_coverage_pct=shots_pct,
                matches_with_shots_on_target=matches_with_sot,
                sot_coverage_pct=sot_pct,
                matches_with_native_xg=matches_with_native_xg,
                native_xg_coverage_pct=xg_pct,
                raw_has_shot_columns=raw_has_shot_columns,
                raw_has_sot_columns=raw_has_sot_columns,
                db_has_shot_columns=db_has_shot_columns,
                db_has_sot_columns=db_has_sot_columns,
                ingestion_gap=ingestion_gap,
                coverage_state=state,
                recommended_next_step=next_step,
            )
        )
        
    # Determine V2_ELIGIBLE at league level
    league_season_counts = defaultdict(int)
    league_match_counts = defaultdict(int)
    league_shots_counts = defaultdict(int)
    league_sot_counts = defaultdict(int)
    
    for r in out_rows:
        if r.matches_total > 0:
            league_season_counts[r.league] += 1
            league_match_counts[r.league] += r.matches_total
            league_shots_counts[r.league] += r.matches_with_shots
            league_sot_counts[r.league] += r.matches_with_shots_on_target
            
    # Apply V2_ELIGIBLE state modification
    for i, r in enumerate(out_rows):
        if r.coverage_state in ("SHOTS_AND_SOT", "NATIVE_XG_AVAILABLE"):
            lg = r.league
            seasons = league_season_counts[lg]
            matches = league_match_counts[lg]
            shots_pct = league_shots_counts[lg] / matches if matches > 0 else 0
            sot_pct = league_sot_counts[lg] / matches if matches > 0 else 0
            
            if seasons >= 3 and shots_pct >= 0.8 and sot_pct >= 0.8 and matches >= 800:
                if r.coverage_state != "NATIVE_XG_AVAILABLE":
                    out_rows[i] = ShotsCoverageRow(
                        **{**r.__dict__, "coverage_state": "V2_ELIGIBLE", "recommended_next_step": "Ready for Phase 10.1"}
                    )
                    
    # Sort
    out_rows.sort(key=lambda x: (x.league, x.season))
    
    date_range_str = "All DB Dates"
    if from_date or to_date:
        date_range_str = f"{from_date or 'Any'} to {to_date or 'Any'}"

    return ShotsCoverageReport(
        date_range=date_range_str,
        leagues_requested=",".join(leagues) if leagues else "ALL",
        rows=out_rows,
    )
