"""Leak-free pre-match feature extraction for the 1X2 market."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date


OUTCOME_HOME = 0
OUTCOME_DRAW = 1
OUTCOME_AWAY = 2
OUTCOME_LABELS = ("home", "draw", "away")

FEATURE_NAMES = ("elo_diff", "home_field")


@dataclass(frozen=True)
class MatchFeature:
    source_match_id: str
    match_date: str
    home_id: int
    away_id: int
    home_score: int
    away_score: int
    neutral: int
    tournament: str | None
    elo_home_pre: float
    elo_away_pre: float

    @property
    def elo_diff(self) -> float:
        return self.elo_home_pre - self.elo_away_pre

    @property
    def home_field(self) -> float:
        return 0.0 if self.neutral else 1.0

    @property
    def outcome(self) -> int:
        if self.home_score > self.away_score:
            return OUTCOME_HOME
        if self.home_score == self.away_score:
            return OUTCOME_DRAW
        return OUTCOME_AWAY

    @property
    def year(self) -> int:
        return int(self.match_date[:4])

    def features(self) -> list[float]:
        return [self.elo_diff, self.home_field]


def load_match_features(
    conn: sqlite3.Connection,
    *,
    start_date: date | None = None,
    rating_system: str = "elo",
) -> list[MatchFeature]:
    """Return historical matches with both home and away pre-match Elo ratings."""
    params: list[object] = [rating_system, rating_system]
    where = ""
    if start_date is not None:
        where = "WHERE m.date >= ?"
        params.append(start_date.isoformat())

    rows = conn.execute(
        f"""
        SELECT
          m.source_match_id, m.date, m.home_id, m.away_id,
          m.home_score, m.away_score, m.neutral, m.tournament,
          hr.rating_pre AS elo_home_pre,
          ar.rating_pre AS elo_away_pre
        FROM match_history m
        JOIN team_rating_history hr
          ON hr.source_match_id = m.source_match_id
         AND hr.team_id = m.home_id
         AND hr.rating_system = ?
        JOIN team_rating_history ar
          ON ar.source_match_id = m.source_match_id
         AND ar.team_id = m.away_id
         AND ar.rating_system = ?
        {where}
        ORDER BY m.date, m.source_match_id
        """,
        params,
    ).fetchall()

    return [
        MatchFeature(
            source_match_id=row["source_match_id"],
            match_date=row["date"],
            home_id=int(row["home_id"]),
            away_id=int(row["away_id"]),
            home_score=int(row["home_score"]),
            away_score=int(row["away_score"]),
            neutral=int(row["neutral"]),
            tournament=row["tournament"],
            elo_home_pre=float(row["elo_home_pre"]),
            elo_away_pre=float(row["elo_away_pre"]),
        )
        for row in rows
    ]
