from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scorerecord.aliases import get_or_create_team, normalize_name, seed_team_aliases
from scorerecord.sources import OPENFOOTBALL_2026_URL, fetch_text


def ingest_worldcup_2026(
    conn: sqlite3.Connection,
    raw_dir: Path,
    *,
    refresh: bool = False,
) -> int:
    seed_team_aliases(conn)
    payload = json.loads(fetch_text(OPENFOOTBALL_2026_URL, raw_dir, refresh=refresh))
    matches = payload.get("matches", [])
    upserts = 0

    for index, match in enumerate(matches, start=1):
        team1 = normalize_name(match.get("team1"))
        team2 = normalize_name(match.get("team2"))
        home_id = _team_id_or_none(conn, team1)
        away_id = _team_id_or_none(conn, team2)
        score = match.get("score") or {}
        full_time = score.get("ft") if isinstance(score, dict) else None
        home_score = full_time[0] if full_time and len(full_time) == 2 else None
        away_score = full_time[1] if full_time and len(full_time) == 2 else None
        status = "finished" if home_score is not None and away_score is not None else "scheduled"
        fixture_id = _fixture_id(match, index)
        kickoff_local = _kickoff_local(match)

        conn.execute(
            """
            INSERT INTO fixture (
              fixture_id, kickoff_local, kickoff_utc, stage, group_name,
              home_id, away_id, home_placeholder, away_placeholder, status,
              home_score, away_score, ground, source, updated_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'openfootball', CURRENT_TIMESTAMP)
            ON CONFLICT(fixture_id) DO UPDATE SET
              kickoff_local = excluded.kickoff_local,
              kickoff_utc = excluded.kickoff_utc,
              stage = excluded.stage,
              group_name = excluded.group_name,
              home_id = excluded.home_id,
              away_id = excluded.away_id,
              home_placeholder = excluded.home_placeholder,
              away_placeholder = excluded.away_placeholder,
              status = excluded.status,
              home_score = excluded.home_score,
              away_score = excluded.away_score,
              ground = excluded.ground,
              updated_utc = CURRENT_TIMESTAMP
            """,
            (
                fixture_id,
                kickoff_local,
                None,
                match.get("round"),
                match.get("group"),
                home_id,
                away_id,
                None if home_id else team1,
                None if away_id else team2,
                status,
                home_score,
                away_score,
                match.get("ground"),
            ),
        )
        upserts += 1

    conn.commit()
    return upserts


def _team_id_or_none(conn: sqlite3.Connection, name: str | None) -> int | None:
    if not name or _is_placeholder(name):
        return None
    return get_or_create_team(conn, name)


def _is_placeholder(name: str) -> bool:
    return name.startswith(("W", "L")) and name[1:].isdigit()


def _fixture_id(match: dict[str, object], index: int) -> str:
    parts = [
        "wc2026",
        str(match.get("date") or "unknown-date"),
        str(match.get("time") or "unknown-time"),
        str(match.get("team1") or f"home-{index}"),
        str(match.get("team2") or f"away-{index}"),
    ]
    slug = "-".join(_slug(part) for part in parts)
    return slug[:180]


def _kickoff_local(match: dict[str, object]) -> str | None:
    date = match.get("date")
    time = match.get("time")
    if not date:
        return None
    if not time:
        return str(date)
    return f"{date} {time}"


def _slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")
