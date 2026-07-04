from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from scorerecord.aliases import (
    get_or_create_team,
    normalize_key,
    normalize_name,
    seed_team_aliases,
)
from scorerecord.sources import OPENFOOTBALL_2026_URL, fetch_text
from scorerecord.time_utils import format_utc, parse_kickoff_utc

_PLACEHOLDER_RE = re.compile(r"^(?:[WL]\d+|[12][A-L]|3[A-L](?:/(?:3?[A-L]))+)$")


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
        kickoff_local = _kickoff_local(match)
        kickoff_utc = format_utc(parse_kickoff_utc(kickoff_local=kickoff_local))
        fixture_id = _resolve_fixture_id(conn, match, index, kickoff_utc=kickoff_utc)

        conn.execute(
            """
            INSERT INTO fixture (
              fixture_id, kickoff_local, kickoff_utc, stage, group_name,
              home_id, away_id, home_placeholder, away_placeholder, status,
              home_score, away_score, ground, source, updated_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'openfootball', CURRENT_TIMESTAMP)
            ON CONFLICT(fixture_id) DO UPDATE SET
              kickoff_local = COALESCE(excluded.kickoff_local, fixture.kickoff_local),
              kickoff_utc = COALESCE(excluded.kickoff_utc, fixture.kickoff_utc),
              stage = excluded.stage,
              group_name = excluded.group_name,
              home_id = COALESCE(excluded.home_id, fixture.home_id),
              away_id = COALESCE(excluded.away_id, fixture.away_id),
              home_placeholder = CASE
                WHEN COALESCE(excluded.home_id, fixture.home_id) IS NULL
                THEN excluded.home_placeholder
                ELSE NULL
              END,
              away_placeholder = CASE
                WHEN COALESCE(excluded.away_id, fixture.away_id) IS NULL
                THEN excluded.away_placeholder
                ELSE NULL
              END,
              status = CASE
                WHEN COALESCE(excluded.home_score, fixture.home_score) IS NOT NULL
                 AND COALESCE(excluded.away_score, fixture.away_score) IS NOT NULL
                THEN 'finished'
                ELSE excluded.status
              END,
              home_score = COALESCE(excluded.home_score, fixture.home_score),
              away_score = COALESCE(excluded.away_score, fixture.away_score),
              ground = COALESCE(excluded.ground, fixture.ground),
              updated_utc = CURRENT_TIMESTAMP
            """,
            (
                fixture_id,
                kickoff_local,
                kickoff_utc,
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


def resolve_fixture_teams(
    conn: sqlite3.Connection,
    *,
    fixture_id: str,
    home: str,
    away: str,
) -> None:
    home_id = _existing_team_id(conn, home)
    away_id = _existing_team_id(conn, away)
    missing = []
    if home_id is None:
        missing.append(home)
    if away_id is None:
        missing.append(away)
    if missing:
        raise ValueError(f"Unknown team alias(es): {', '.join(missing)}")

    cursor = conn.execute(
        """
        UPDATE fixture
        SET home_id = ?,
            away_id = ?,
            home_placeholder = NULL,
            away_placeholder = NULL,
            status = 'scheduled',
            updated_utc = CURRENT_TIMESTAMP
        WHERE fixture_id = ?
        """,
        (home_id, away_id, fixture_id),
    )
    if cursor.rowcount == 0:
        raise ValueError(f"Unknown fixture_id: {fixture_id}")
    conn.commit()


def _team_id_or_none(conn: sqlite3.Connection, name: str | None) -> int | None:
    if not name or _is_placeholder(name):
        return None
    return get_or_create_team(conn, name)


def _existing_team_id(conn: sqlite3.Connection, name: str) -> int | None:
    key = normalize_key(name)
    if not key:
        return None
    row = conn.execute(
        """
        SELECT team_id
        FROM team
        WHERE canonical_key = ?
        UNION
        SELECT team_id
        FROM team_alias
        WHERE alias_key = ?
        ORDER BY team_id
        LIMIT 1
        """,
        (key, key),
    ).fetchone()
    return int(row["team_id"]) if row else None


def _resolve_fixture_id(
    conn: sqlite3.Connection,
    match: dict[str, object],
    index: int,
    *,
    kickoff_utc: str | None,
) -> str:
    fixture_id = _fixture_id(match, index)
    if _fixture_exists(conn, fixture_id):
        return fixture_id

    existing_id = _legacy_fixture_id(match, index)
    if not _fixture_exists(conn, existing_id):
        existing_id = _fixture_id_by_fingerprint(conn, match, kickoff_utc=kickoff_utc)
    if existing_id is None:
        return fixture_id

    if not _fixture_has_references(conn, existing_id):
        conn.execute(
            "UPDATE fixture SET fixture_id = ? WHERE fixture_id = ?",
            (fixture_id, existing_id),
        )
        return fixture_id
    return existing_id


def _fixture_exists(conn: sqlite3.Connection, fixture_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM fixture WHERE fixture_id = ?",
        (fixture_id,),
    ).fetchone()
    return row is not None


def _fixture_id_by_fingerprint(
    conn: sqlite3.Connection,
    match: dict[str, object],
    *,
    kickoff_utc: str | None,
) -> str | None:
    row = conn.execute(
        """
        SELECT fixture_id
        FROM fixture
        WHERE source = 'openfootball'
          AND COALESCE(kickoff_utc, '') = COALESCE(?, '')
          AND COALESCE(stage, '') = COALESCE(?, '')
          AND COALESCE(group_name, '') = COALESCE(?, '')
          AND COALESCE(ground, '') = COALESCE(?, '')
        ORDER BY fixture_id
        LIMIT 1
        """,
        (kickoff_utc, match.get("round"), match.get("group"), match.get("ground")),
    ).fetchone()
    return str(row["fixture_id"]) if row else None


def _fixture_has_references(conn: sqlite3.Connection, fixture_id: str) -> bool:
    tables = (
        "fixture_stats",
        "odds",
        "odds_snapshots",
        "edge_analysis",
        "player_prop",
        "prediction",
        "prediction_result",
        "recommendation",
        "recommendation_result",
    )
    for table in tables:
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE fixture_id = ? LIMIT 1",
            (fixture_id,),
        ).fetchone()
        if row:
            return True
    return False


def _is_placeholder(name: str) -> bool:
    return bool(_PLACEHOLDER_RE.match(name))


def _fixture_id(match: dict[str, object], index: int) -> str:
    raw_match_no = match.get("num") or match.get("number") or match.get("match")
    try:
        match_no = int(raw_match_no) if raw_match_no is not None else index
    except (TypeError, ValueError):
        match_no = index
    return f"wc2026-match-{match_no:03d}"


def _legacy_fixture_id(match: dict[str, object], index: int) -> str:
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
