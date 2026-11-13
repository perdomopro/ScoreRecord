"""Bring final scores back into the pipeline.

Two steps close the loop that the fixture ingest leaves open:

1. ``ingest_results_odds_api_io`` asks odds-api.io for settled/cancelled events
   and writes the final score onto the matching ``fixture`` row.
2. ``promote_finished_fixtures`` copies finished club fixtures into
   ``match_history`` so Elo and the league models train on them.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from scorerecord.league_config import CLUB_FORECAST_LEAGUES
from scorerecord.runs import ingest_run
from scorerecord.time_utils import parse_kickoff_utc

FINISHED_STATUSES = frozenset({"settled", "finished", "ended", "complete", "completed"})
CANCELLED_STATUSES = frozenset({"cancelled", "canceled", "abandoned"})
PROMOTED_PREFIX = "fixture:"
DEDUPE_WINDOW_DAYS = 2

FetchJson = Callable[[str, dict[str, str]], Any]


def ingest_results_odds_api_io(
    conn: sqlite3.Connection,
    *,
    api_key: str | None,
    base_url: str,
    sport: str,
    league_code: str,
    league_slugs: tuple[str, ...],
    lookback_days: int = 10,
    dry_run: bool = False,
    now_utc: datetime | None = None,
    fetch_json: FetchJson | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Write final scores for fixtures the provider reports as settled."""
    result: dict[str, Any] = {
        "status": "OK",
        "league": league_code,
        "lookback_days": lookback_days,
        "api_requests_used": 0,
        "events_seen": 0,
        "fixtures_scored": 0,
        "fixtures_cancelled": 0,
        "already_scored": 0,
        "unknown_events": 0,
        "events_without_score": 0,
        "dry_run": dry_run,
    }
    if not api_key:
        result["status"] = "AUTH_MISSING"
        return result
    if fetch_json is None:
        from scorerecord.ingest.odds import _get_json

        fetch_json = _get_json

    now = now_utc or datetime.now(UTC)
    start = now - timedelta(days=lookback_days)
    events: list[dict[str, Any]] = []
    for slug in league_slugs:
        params = {
            "apiKey": api_key,
            "sport": sport,
            "league": slug,
            "status": "settled,cancelled",
            "from": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": "5000",
        }
        try:
            payload = fetch_json(f"{base_url.rstrip('/')}/events", params)
        except Exception as exc:  # network dependent
            result["status"] = "API_ERROR"
            result["error"] = type(exc).__name__
            return result
        result["api_requests_used"] += 1
        if isinstance(payload, dict):
            payload = payload.get("events") or payload.get("data") or []
        if isinstance(payload, list):
            events.extend(event for event in payload if isinstance(event, dict))

    result["events_seen"] = len(events)
    if dry_run:
        _apply_results(conn, events, league_code, result, write=False)
        return result

    with ingest_run(conn, "ingest-results", source="odds-api.io", cwd=cwd):
        _apply_results(conn, events, league_code, result, write=True)
        conn.commit()
    return result


def _apply_results(
    conn: sqlite3.Connection,
    events: list[dict[str, Any]],
    league_code: str,
    result: dict[str, Any],
    *,
    write: bool,
) -> None:
    for event in events:
        event_id = event.get("id") or event.get("eventId") or event.get("event_id")
        if event_id is None:
            continue
        fixture = conn.execute(
            """
            SELECT fixture_id, home_score, away_score, status
            FROM fixture
            WHERE source = 'odds_api_io'
              AND source_event_id = ?
              AND league_code = ?
            """,
            (str(event_id), league_code),
        ).fetchone()
        if fixture is None:
            result["unknown_events"] += 1
            continue
        status = str(event.get("status") or "").strip().lower()
        if status in CANCELLED_STATUSES:
            if fixture["status"] != "cancelled" and fixture["home_score"] is None:
                if write:
                    conn.execute(
                        """
                        UPDATE fixture
                        SET status = 'cancelled', updated_utc = CURRENT_TIMESTAMP
                        WHERE fixture_id = ?
                        """,
                        (fixture["fixture_id"],),
                    )
                result["fixtures_cancelled"] += 1
            continue
        if status not in FINISHED_STATUSES:
            continue
        score = final_score(event)
        if score is None:
            result["events_without_score"] += 1
            continue
        if fixture["home_score"] is not None and fixture["away_score"] is not None:
            result["already_scored"] += 1
            continue
        if write:
            conn.execute(
                """
                UPDATE fixture
                SET home_score = ?, away_score = ?, status = 'finished',
                    updated_utc = CURRENT_TIMESTAMP
                WHERE fixture_id = ?
                """,
                (score[0], score[1], fixture["fixture_id"]),
            )
        result["fixtures_scored"] += 1


def final_score(event: dict[str, Any]) -> tuple[int, int] | None:
    """Return the regulation-time score of a provider event.

    ``scores.periods.ft`` is preferred because the headline ``scores.home`` /
    ``scores.away`` include extra time, and goal markets are settled on 90
    minutes.
    """
    scores = event.get("scores")
    if not isinstance(scores, dict):
        return None
    periods = scores.get("periods")
    candidates = []
    if isinstance(periods, dict) and isinstance(periods.get("ft"), dict):
        candidates.append(periods["ft"])
    candidates.append(scores)
    for candidate in candidates:
        try:
            home = int(candidate["home"])
            away = int(candidate["away"])
        except (KeyError, TypeError, ValueError):
            continue
        if home < 0 or away < 0:
            continue
        return home, away
    return None


def promote_finished_fixtures(
    conn: sqlite3.Connection,
    *,
    leagues: tuple[str, ...] | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Copy finished club fixtures into ``match_history``.

    A fixture is skipped when another source (Football-Data bridge) already has
    the same league, teams and date within ``DEDUPE_WINDOW_DAYS``.
    """
    scope = tuple(leagues) if leagues else tuple(CLUB_FORECAST_LEAGUES)
    placeholders = ",".join("?" for _ in scope)
    rows = conn.execute(
        f"""
        SELECT fixture_id, kickoff_utc, kickoff_local, home_id, away_id,
               home_score, away_score, league_code
        FROM fixture
        WHERE league_code IN ({placeholders})
          AND home_id IS NOT NULL
          AND away_id IS NOT NULL
          AND home_score IS NOT NULL
          AND away_score IS NOT NULL
        ORDER BY kickoff_utc, fixture_id
        """,
        scope,
    ).fetchall()
    summary = {"candidates": len(rows), "inserted": 0, "existing": 0, "duplicates": 0}
    for row in rows:
        source_match_id = f"{PROMOTED_PREFIX}{row['fixture_id']}"
        if conn.execute(
            "SELECT 1 FROM match_history WHERE source_match_id = ?", (source_match_id,)
        ).fetchone():
            summary["existing"] += 1
            continue
        kickoff = parse_kickoff_utc(
            kickoff_utc=row["kickoff_utc"], kickoff_local=row["kickoff_local"]
        )
        if kickoff is None:
            continue
        match_date = kickoff.date()
        if _has_other_source_match(
            conn,
            tournament=str(row["league_code"]),
            home_id=int(row["home_id"]),
            away_id=int(row["away_id"]),
            match_date=match_date,
        ):
            summary["duplicates"] += 1
            continue
        if not dry_run:
            conn.execute(
                """
                INSERT INTO match_history (
                  source_match_id, date, home_id, away_id, home_score, away_score,
                  tournament, city, country, neutral
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0)
                """,
                (
                    source_match_id,
                    match_date.isoformat(),
                    int(row["home_id"]),
                    int(row["away_id"]),
                    int(row["home_score"]),
                    int(row["away_score"]),
                    str(row["league_code"]),
                ),
            )
        summary["inserted"] += 1
    if not dry_run:
        conn.commit()
    return summary


def drop_promoted_duplicates(
    conn: sqlite3.Connection,
    *,
    tournament: str,
    home_id: int,
    away_id: int,
    match_date: str,
) -> int:
    """Remove a fixture-promoted row that another source is about to supersede."""
    try:
        day = date.fromisoformat(str(match_date)[:10])
    except ValueError:
        return 0
    low = (day - timedelta(days=DEDUPE_WINDOW_DAYS)).isoformat()
    high = (day + timedelta(days=DEDUPE_WINDOW_DAYS)).isoformat()
    ids = [
        str(row[0])
        for row in conn.execute(
            """
            SELECT source_match_id
            FROM match_history
            WHERE tournament = ?
              AND home_id = ?
              AND away_id = ?
              AND substr(date, 1, 10) BETWEEN ? AND ?
              AND source_match_id LIKE ?
            """,
            (tournament, home_id, away_id, low, high, f"{PROMOTED_PREFIX}%"),
        ).fetchall()
    ]
    for source_match_id in ids:
        conn.execute(
            "DELETE FROM team_rating_history WHERE source_match_id = ?", (source_match_id,)
        )
        conn.execute("DELETE FROM match_history WHERE source_match_id = ?", (source_match_id,))
    return len(ids)


def _has_other_source_match(
    conn: sqlite3.Connection,
    *,
    tournament: str,
    home_id: int,
    away_id: int,
    match_date: date,
) -> bool:
    low = (match_date - timedelta(days=DEDUPE_WINDOW_DAYS)).isoformat()
    high = (match_date + timedelta(days=DEDUPE_WINDOW_DAYS)).isoformat()
    row = conn.execute(
        """
        SELECT 1
        FROM match_history
        WHERE tournament = ?
          AND home_id = ?
          AND away_id = ?
          AND substr(date, 1, 10) BETWEEN ? AND ?
          AND source_match_id NOT LIKE ?
        LIMIT 1
        """,
        (tournament, home_id, away_id, low, high, f"{PROMOTED_PREFIX}%"),
    ).fetchone()
    return row is not None


def refresh_results_football_data(
    conn: sqlite3.Connection,
    raw_dir: Path,
    *,
    league_code: str,
    season: str,
    dry_run: bool = False,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Download the current Football-Data file of a league and bridge it.

    Football-Data is the results source of record: odds-api.io drops events
    once they finish. Unknown team names are never auto-created here; the
    bridge reports them and the league stays stale until an alias is added.
    """
    from scorerecord.ingest.bridge import bridge_club_history
    from scorerecord.ingest.football_data import ingest_football_data

    result: dict[str, Any] = {"status": "OK", "league": league_code, "season": season}
    try:
        ingest = ingest_football_data(
            conn,
            raw_dir,
            league=league_code,
            season=season,
            refresh=True,
            dry_run=dry_run,
            cwd=cwd,
        )
    except Exception as exc:  # network or parse failure
        result["status"] = "FOOTBALL_DATA_ERROR"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    result["rows_seen"] = ingest.rows_seen
    result["events_upserted"] = ingest.events_upserted

    bridge = bridge_club_history(
        conn,
        league=league_code,
        source="football-data",
        from_season=season,
        to_season=season,
        dry_run=dry_run,
        allow_create_teams=False,
    )
    result["bridge_status"] = bridge.status
    result["matches_inserted"] = bridge.matches_inserted
    result["matches_updated"] = bridge.matches_updated
    result["missing_teams"] = list(bridge.missing_teams)
    if bridge.status not in ("OK", "OK_WITH_SKIPPED_ROWS"):
        result["status"] = f"BRIDGE_{bridge.status}"
    return result


def sync_fixture_scores_from_history(
    conn: sqlite3.Connection,
    *,
    leagues: tuple[str, ...] | None = None,
    dry_run: bool = False,
    now_utc: datetime | None = None,
) -> dict[str, int]:
    """Copy final scores from ``match_history`` onto played fixtures.

    Matching is by league, home team, away team and date within
    ``DEDUPE_WINDOW_DAYS`` (Football-Data dates are local, kickoffs are UTC).
    """
    scope = tuple(leagues) if leagues else tuple(CLUB_FORECAST_LEAGUES)
    placeholders = ",".join("?" for _ in scope)
    now = now_utc or datetime.now(UTC)
    rows = conn.execute(
        f"""
        SELECT fixture_id, kickoff_utc, kickoff_local, home_id, away_id, league_code
        FROM fixture
        WHERE league_code IN ({placeholders})
          AND home_id IS NOT NULL
          AND away_id IS NOT NULL
          AND home_score IS NULL
          AND status != 'cancelled'
        ORDER BY kickoff_utc, fixture_id
        """,
        scope,
    ).fetchall()
    summary = {"pending": 0, "scored": 0}
    for row in rows:
        kickoff = parse_kickoff_utc(
            kickoff_utc=row["kickoff_utc"], kickoff_local=row["kickoff_local"]
        )
        if kickoff is None or kickoff > now:
            continue
        summary["pending"] += 1
        day = kickoff.date()
        match = conn.execute(
            """
            SELECT home_score, away_score
            FROM match_history
            WHERE tournament = ?
              AND home_id = ?
              AND away_id = ?
              AND substr(date, 1, 10) BETWEEN ? AND ?
              AND source_match_id NOT LIKE ?
            ORDER BY ABS(julianday(substr(date, 1, 10)) - julianday(?))
            LIMIT 1
            """,
            (
                str(row["league_code"]),
                int(row["home_id"]),
                int(row["away_id"]),
                (day - timedelta(days=DEDUPE_WINDOW_DAYS)).isoformat(),
                (day + timedelta(days=DEDUPE_WINDOW_DAYS)).isoformat(),
                f"{PROMOTED_PREFIX}%",
                day.isoformat(),
            ),
        ).fetchone()
        if match is None:
            continue
        if not dry_run:
            conn.execute(
                """
                UPDATE fixture
                SET home_score = ?, away_score = ?, status = 'finished',
                    updated_utc = CURRENT_TIMESTAMP
                WHERE fixture_id = ?
                """,
                (int(match["home_score"]), int(match["away_score"]), row["fixture_id"]),
            )
        summary["scored"] += 1
    if not dry_run:
        conn.commit()
    return summary
