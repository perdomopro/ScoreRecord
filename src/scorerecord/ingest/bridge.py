from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from scorerecord.aliases import get_or_create_team, normalize_key, normalize_name
from scorerecord.ingest.football_data import football_data_season_range


@dataclass(frozen=True)
class BridgeClubHistoryResult:
    status: str
    source: str
    league: str
    from_season: str
    to_season: str
    dry_run: bool
    allow_create_teams: bool
    historical_rows_seen: int
    eligible_matches: int
    matches_inserted: int
    matches_updated: int
    matches_skipped_existing: int
    teams_created: int
    aliases_created: int
    missing_teams: list[str]
    invalid_rows: int
    alias_collisions: list[str]
    status_reason: str | None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "source": self.source,
            "league": self.league,
            "from_season": self.from_season,
            "to_season": self.to_season,
            "dry_run": self.dry_run,
            "allow_create_teams": self.allow_create_teams,
            "historical_rows_seen": self.historical_rows_seen,
            "eligible_matches": self.eligible_matches,
            "matches_inserted": self.matches_inserted,
            "matches_updated": self.matches_updated,
            "matches_skipped_existing": self.matches_skipped_existing,
            "teams_created": self.teams_created,
            "aliases_created": self.aliases_created,
            "missing_teams": self.missing_teams,
            "invalid_rows": self.invalid_rows,
            "alias_collisions": self.alias_collisions,
            "status_reason": self.status_reason,
        }


def bridge_club_history(
    conn: sqlite3.Connection,
    *,
    league: str,
    source: str,
    from_season: str,
    to_season: str,
    dry_run: bool = False,
    allow_create_teams: bool = False,
) -> BridgeClubHistoryResult:
    try:
        seasons = football_data_season_range(from_season, to_season)
    except ValueError as e:
        return _error_result(str(e), league, source, from_season, to_season, dry_run, allow_create_teams)

    # Use provider-specific league code
    if source == "football-data":
        from scorerecord.ingest.football_data import FOOTBALL_DATA_LEAGUES
        db_league_code = FOOTBALL_DATA_LEAGUES.get(league, league)
    else:
        db_league_code = league

    # 1. Fetch eligible rows
    placeholders = ",".join("?" for _ in seasons)
    query = f"""
        SELECT 
            source_event_id, 
            match_date, 
            home_team, 
            away_team, 
            home_goals, 
            away_goals,
            season
        FROM historical_match
        WHERE source = ?
          AND league_code = ?
          AND season IN ({placeholders})
          AND home_goals IS NOT NULL
          AND away_goals IS NOT NULL
          AND match_date IS NOT NULL
    """
    rows = conn.execute(query, (source, db_league_code, *seasons)).fetchall()

    historical_rows_seen = len(rows)
    if not historical_rows_seen:
        return BridgeClubHistoryResult(
            status="NO_ROWS_FOUND",
            source=source,
            league=league,
            from_season=from_season,
            to_season=to_season,
            dry_run=dry_run,
            allow_create_teams=allow_create_teams,
            historical_rows_seen=0,
            eligible_matches=0,
            matches_inserted=0,
            matches_updated=0,
            matches_skipped_existing=0,
            teams_created=0,
            aliases_created=0,
            missing_teams=[],
            invalid_rows=0,
            alias_collisions=[],
            status_reason="No completed matches found for filter criteria",
        )

    missing_teams_set: set[str] = set()
    alias_collisions: set[str] = set()
    invalid_rows = 0
    teams_created = 0
    aliases_created = 0

    # Process teams
    unique_team_names = set()
    for row in rows:
        unique_team_names.add(row["home_team"])
        unique_team_names.add(row["away_team"])

    for team_name in unique_team_names:
        canonical = normalize_name(team_name)
        if not canonical:
            continue

        alias_key = normalize_key(canonical)
        existing_alias_row = conn.execute(
            """
            SELECT ta.team_id, t.canonical
            FROM team_alias ta
            JOIN team t ON ta.team_id = t.team_id
            WHERE ta.alias_key = ?
            """,
            (alias_key,),
        ).fetchone()
        if not existing_alias_row:
            continue

        existing_canonical = existing_alias_row["canonical"]
        if existing_canonical != canonical and normalize_key(existing_canonical) != alias_key:
            alias_collisions.add(f"{team_name} points to {existing_canonical}")

    if alias_collisions:
        return BridgeClubHistoryResult(
            status="ALIAS_COLLISION",
            source=source,
            league=league,
            from_season=from_season,
            to_season=to_season,
            dry_run=dry_run,
            allow_create_teams=allow_create_teams,
            historical_rows_seen=historical_rows_seen,
            eligible_matches=historical_rows_seen - invalid_rows,
            matches_inserted=0,
            matches_updated=0,
            matches_skipped_existing=0,
            teams_created=0,
            aliases_created=0,
            missing_teams=[],
            invalid_rows=invalid_rows,
            alias_collisions=list(alias_collisions),
            status_reason="Alias collisions detected",
        )
        
    team_mapping: dict[str, int] = {}
    for team_name in unique_team_names:
        canonical = normalize_name(team_name)
        if not canonical:
            invalid_rows += 1
            continue
        
        alias_key = normalize_key(canonical)
        
        # Check collision
        existing_alias_row = conn.execute(
            """
            SELECT ta.team_id, t.canonical 
            FROM team_alias ta
            JOIN team t ON ta.team_id = t.team_id
            WHERE ta.alias_key = ?
            """, 
            (alias_key,)
        ).fetchone()
        
        if existing_alias_row:
            existing_canonical = existing_alias_row["canonical"]
            if existing_canonical != canonical:
                if normalize_key(existing_canonical) == alias_key:
                    team_mapping[team_name] = int(existing_alias_row["team_id"])
                    continue
                # Check if this is an explicitly seeded alias. If team_name is in the DB but points to a DIFFERENT canonical, it's a collision
                alias_collisions.add(f"{team_name} points to {existing_canonical}")
                continue
                
            team_mapping[team_name] = int(existing_alias_row["team_id"])
            continue

        if not allow_create_teams:
            missing_teams_set.add(team_name)
            continue
            
        if dry_run:
            teams_created += 1
            aliases_created += 1
            team_mapping[team_name] = -1  # Fake ID for dry run
            continue
            
        # Create team
        try:
            team_id = get_or_create_team(conn, team_name)
            team_mapping[team_name] = team_id
            # Note: get_or_create_team handles insertion into team and team_alias
            # We assume it created a team if we got here and didn't find the alias earlier
            teams_created += 1
            aliases_created += 1
        except ValueError:
            invalid_rows += 1

    if alias_collisions:
        return BridgeClubHistoryResult(
            status="ALIAS_COLLISION",
            source=source,
            league=league,
            from_season=from_season,
            to_season=to_season,
            dry_run=dry_run,
            allow_create_teams=allow_create_teams,
            historical_rows_seen=historical_rows_seen,
            eligible_matches=historical_rows_seen - invalid_rows,
            matches_inserted=0,
            matches_updated=0,
            matches_skipped_existing=0,
            teams_created=0,
            aliases_created=0,
            missing_teams=list(missing_teams_set),
            invalid_rows=invalid_rows,
            alias_collisions=list(alias_collisions),
            status_reason="Alias collisions detected",
        )

    if missing_teams_set:
        status = "DRY_RUN_ONLY_MISSING_TEAMS" if dry_run else "OK_WITH_SKIPPED_ROWS"
        return BridgeClubHistoryResult(
            status=status,
            source=source,
            league=league,
            from_season=from_season,
            to_season=to_season,
            dry_run=dry_run,
            allow_create_teams=allow_create_teams,
            historical_rows_seen=historical_rows_seen,
            eligible_matches=historical_rows_seen - invalid_rows,
            matches_inserted=0,
            matches_updated=0,
            matches_skipped_existing=0,
            teams_created=0,
            aliases_created=0,
            missing_teams=list(missing_teams_set),
            invalid_rows=invalid_rows,
            alias_collisions=[],
            status_reason="Missing teams require --allow-create-teams",
        )

    # Insert match_history
    matches_inserted = 0
    matches_updated = 0
    matches_skipped_existing = 0
    eligible_matches = 0
    
    for row in rows:
        home_team = row["home_team"]
        away_team = row["away_team"]
        home_id = team_mapping.get(home_team)
        away_id = team_mapping.get(away_team)
        
        if home_id is None or away_id is None:
            # Should only happen with missing teams or invalid rows
            continue
            
        eligible_matches += 1
        
        # generate deterministic match id
        # football-data:EPL:2021-2022:2021-08-13:brentford:arsenal
        match_date = row["match_date"]
        # Use existing source_event_id if present, but we might want to make sure it matches the new format?
        # The user requested: "use an existing historical_match id if available."
        source_match_id = row["source_event_id"]
        
        # check if exists
        existing = conn.execute(
            "SELECT home_score, away_score FROM match_history WHERE source_match_id = ?",
            (source_match_id,)
        ).fetchone()
        
        home_score = int(row["home_goals"])
        away_score = int(row["away_goals"])
        
        if existing:
            if int(existing["home_score"]) == home_score and int(existing["away_score"]) == away_score:
                matches_skipped_existing += 1
                continue
                
            if not dry_run:
                conn.execute(
                    """
                    UPDATE match_history SET
                        date = ?, home_id = ?, away_id = ?, home_score = ?, away_score = ?,
                        tournament = ?
                    WHERE source_match_id = ?
                    """,
                    (match_date, home_id, away_id, home_score, away_score, league, source_match_id)
                )
            matches_updated += 1
        else:
            if not dry_run:
                conn.execute(
                    """
                    INSERT INTO match_history (
                        source_match_id, date, home_id, away_id, home_score, away_score,
                        tournament, city, country, neutral
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0)
                    """,
                    (source_match_id, match_date, home_id, away_id, home_score, away_score, league)
                )
            matches_inserted += 1

    if not dry_run:
        conn.commit()

    return BridgeClubHistoryResult(
        status="OK",
        source=source,
        league=league,
        from_season=from_season,
        to_season=to_season,
        dry_run=dry_run,
        allow_create_teams=allow_create_teams,
        historical_rows_seen=historical_rows_seen,
        eligible_matches=eligible_matches,
        matches_inserted=matches_inserted,
        matches_updated=matches_updated,
        matches_skipped_existing=matches_skipped_existing,
        teams_created=teams_created,
        aliases_created=aliases_created,
        missing_teams=[],
        invalid_rows=invalid_rows,
        alias_collisions=[],
        status_reason=None,
    )


def _error_result(msg: str, league: str, source: str, from_season: str, to_season: str, dry_run: bool, allow_create_teams: bool) -> BridgeClubHistoryResult:
    return BridgeClubHistoryResult(
        status="INVALID_SOURCE_DATA",
        source=source,
        league=league,
        from_season=from_season,
        to_season=to_season,
        dry_run=dry_run,
        allow_create_teams=allow_create_teams,
        historical_rows_seen=0,
        eligible_matches=0,
        matches_inserted=0,
        matches_updated=0,
        matches_skipped_existing=0,
        teams_created=0,
        aliases_created=0,
        missing_teams=[],
        invalid_rows=0,
        alias_collisions=[],
        status_reason=msg,
    )
