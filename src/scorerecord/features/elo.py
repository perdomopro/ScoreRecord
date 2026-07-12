from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass

BASE_RATING = 1500.0


@dataclass
class EloConfig:
    k_base: float = 28.0
    home_advantage: float = 45.0


def rebuild_elo(conn: sqlite3.Connection, *, config: EloConfig | None = None) -> int:
    config = config or EloConfig()
    ratings: dict[int, float] = {}
    rows = conn.execute(
        """
        SELECT source_match_id, date, home_id, away_id, home_score, away_score, neutral, tournament
        FROM match_history
        ORDER BY date, source_match_id
        """
    ).fetchall()

    conn.execute("DELETE FROM team_rating WHERE rating_system = 'elo'")
    conn.execute("DELETE FROM team_rating_history WHERE rating_system = 'elo'")

    metadata_json = json.dumps({"base": BASE_RATING, "config": config.__dict__}, sort_keys=True)

    for row in rows:
        home_id = int(row["home_id"])
        away_id = int(row["away_id"])
        home_rating = ratings.get(home_id, BASE_RATING)
        away_rating = ratings.get(away_id, BASE_RATING)
        home_adjusted = home_rating if row["neutral"] else home_rating + config.home_advantage
        expected_home = _expected(home_adjusted, away_rating)
        actual_home = _actual(row["home_score"], row["away_score"])
        k = config.k_base * _importance_multiplier(row["tournament"]) * _goal_multiplier(
            row["home_score"],
            row["away_score"],
        )
        delta = k * (actual_home - expected_home)
        home_post = home_rating + delta
        away_post = away_rating - delta

        conn.execute(
            """
            INSERT OR REPLACE INTO team_rating_history (
              source_match_id, team_id, rating_system, as_of, rating_pre, rating_post,
              opponent_id, is_home, expected_result, actual_result, metadata_json
            )
            VALUES (?, ?, 'elo', ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                row["source_match_id"],
                home_id,
                row["date"],
                round(home_rating, 3),
                round(home_post, 3),
                away_id,
                round(expected_home, 6),
                actual_home,
                metadata_json,
            ),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO team_rating_history (
              source_match_id, team_id, rating_system, as_of, rating_pre, rating_post,
              opponent_id, is_home, expected_result, actual_result, metadata_json
            )
            VALUES (?, ?, 'elo', ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                row["source_match_id"],
                away_id,
                row["date"],
                round(away_rating, 3),
                round(away_post, 3),
                home_id,
                round(1.0 - expected_home, 6),
                1.0 - actual_home,
                metadata_json,
            ),
        )
        ratings[home_id] = home_post
        ratings[away_id] = away_post

    as_of = rows[-1]["date"] if rows else "1970-01-01"
    for team_id, rating in ratings.items():
        conn.execute(
            """
            INSERT INTO team_rating (team_id, rating_system, rating, as_of, metadata_json)
            VALUES (?, 'elo', ?, ?, ?)
            """,
            (
                team_id,
                round(rating, 3),
                as_of,
                metadata_json,
            ),
        )
    conn.commit()
    return len(ratings)


def _expected(rating_a: float, rating_b: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))


def _actual(goals_a: int, goals_b: int) -> float:
    if goals_a > goals_b:
        return 1.0
    if goals_a == goals_b:
        return 0.5
    return 0.0


def _goal_multiplier(goals_a: int, goals_b: int) -> float:
    diff = abs(goals_a - goals_b)
    if diff <= 1:
        return 1.0
    return math.log(diff + 1.0) * 1.15


def _importance_multiplier(tournament: str | None) -> float:
    label = (tournament or "").lower()
    if "world cup" in label:
        return 1.25
    if "qualif" in label or "uefa euro" in label or "copa am" in label:
        return 1.1
    if "friendly" in label:
        return 0.75
    return 1.0
