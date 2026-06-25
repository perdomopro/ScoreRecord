from __future__ import annotations

import csv
import sqlite3
from datetime import date
from io import StringIO
from pathlib import Path

from scorerecord.aliases import (
    get_or_create_player,
    get_or_create_team,
    normalize_name,
    seed_team_aliases,
)
from scorerecord.sources import MARTJ42_GOALSCORERS_URL, MARTJ42_RESULTS_URL, fetch_text


DEFAULT_START_DATE = date(2018, 1, 1)


def ingest_history(
    conn: sqlite3.Connection,
    raw_dir: Path,
    *,
    start_date: date = DEFAULT_START_DATE,
    refresh: bool = False,
) -> tuple[int, int]:
    seed_team_aliases(conn)
    conn.execute("DELETE FROM team_rating WHERE rating_system = 'elo'")
    conn.execute("DELETE FROM team_rating_history WHERE rating_system = 'elo'")
    conn.execute("DELETE FROM goal_history")
    conn.execute("DELETE FROM match_history")
    _ingest_results(conn, raw_dir, start_date=start_date, refresh=refresh)
    _ingest_goals(conn, raw_dir, start_date=start_date, refresh=refresh)
    conn.commit()
    match_count = int(conn.execute("SELECT COUNT(*) FROM match_history").fetchone()[0])
    goal_count = int(conn.execute("SELECT COUNT(*) FROM goal_history").fetchone()[0])
    return match_count, goal_count


def _ingest_results(
    conn: sqlite3.Connection,
    raw_dir: Path,
    *,
    start_date: date,
    refresh: bool,
) -> int:
    text = fetch_text(MARTJ42_RESULTS_URL, raw_dir, refresh=refresh)
    rows = csv.DictReader(StringIO(text))
    count = 0
    for row in rows:
        match_date = date.fromisoformat(row["date"])
        if match_date < start_date:
            continue
        if not _is_int(row.get("home_score")) or not _is_int(row.get("away_score")):
            continue

        home_id = get_or_create_team(conn, row["home_team"])
        away_id = get_or_create_team(conn, row["away_team"])
        source_match_id = _match_id(row)
        conn.execute(
            """
            INSERT INTO match_history (
              source_match_id, date, home_id, away_id, home_score, away_score,
              tournament, city, country, neutral
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_match_id) DO UPDATE SET
              home_id = excluded.home_id,
              away_id = excluded.away_id,
              home_score = excluded.home_score,
              away_score = excluded.away_score,
              tournament = excluded.tournament,
              city = excluded.city,
              country = excluded.country,
              neutral = excluded.neutral
            """,
            (
                source_match_id,
                row["date"],
                home_id,
                away_id,
                int(row["home_score"]),
                int(row["away_score"]),
                row.get("tournament"),
                row.get("city"),
                row.get("country"),
                1 if _truthy(row.get("neutral")) else 0,
            ),
        )
        count += 1
    return count


def _ingest_goals(
    conn: sqlite3.Connection,
    raw_dir: Path,
    *,
    start_date: date,
    refresh: bool,
) -> int:
    text = fetch_text(MARTJ42_GOALSCORERS_URL, raw_dir, refresh=refresh)
    rows = csv.DictReader(StringIO(text))
    count = 0
    for index, row in enumerate(rows, start=1):
        match_date = date.fromisoformat(row["date"])
        if match_date < start_date:
            continue

        source_match_id = _match_id(row)
        if not conn.execute(
            "SELECT 1 FROM match_history WHERE source_match_id = ?",
            (source_match_id,),
        ).fetchone():
            continue

        team_name = normalize_name(row.get("team"))
        team_id = get_or_create_team(conn, team_name) if team_name else None
        scorer = normalize_name(row.get("scorer"))
        scorer_id = get_or_create_player(conn, scorer, team_id) if scorer else None
        minute = _parse_minute(row.get("minute"))
        source_goal_id = f"{source_match_id}:{index}:{scorer or 'unknown'}:{minute or 'na'}"

        conn.execute(
            """
            INSERT OR REPLACE INTO goal_history (
              source_goal_id, source_match_id, scorer_id, team_id, minute, own_goal, penalty
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_goal_id,
                source_match_id,
                scorer_id,
                team_id,
                minute,
                1 if _truthy(row.get("own_goal")) else 0,
                1 if _truthy(row.get("penalty")) else 0,
            ),
        )
        count += 1
    return count


def _match_id(row: dict[str, str]) -> str:
    return "|".join(
        [
            row["date"],
            normalize_name(row["home_team"]) or "",
            normalize_name(row["away_team"]) or "",
        ]
    )


def _truthy(value: str | None) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def _is_int(value: str | None) -> bool:
    try:
        int(str(value))
    except (TypeError, ValueError):
        return False
    return True


def _parse_minute(value: str | None) -> int | None:
    if not value:
        return None
    first = str(value).split("+", 1)[0].strip()
    try:
        return int(first)
    except ValueError:
        return None
