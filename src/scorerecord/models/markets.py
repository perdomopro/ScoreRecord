"""Market aggregations derived from a score-probability matrix."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ExactScore:
    home_goals: int
    away_goals: int
    prob: float

    @property
    def selection(self) -> str:
        return f"{self.home_goals}-{self.away_goals}"


def normalize_score_grid(score_grid: np.ndarray) -> np.ndarray:
    total = float(score_grid.sum())
    if total <= 0:
        raise ValueError("Score grid must have positive total probability")
    return score_grid / total


def one_x_two_probabilities(score_grid: np.ndarray) -> list[float]:
    grid = normalize_score_grid(score_grid)
    p_home = float(np.tril(grid, -1).sum())
    p_draw = float(np.trace(grid))
    p_away = float(np.triu(grid, 1).sum())
    return [p_home, p_draw, p_away]


def over_probability(score_grid: np.ndarray, line: float) -> float:
    grid = normalize_score_grid(score_grid)
    home_goals, away_goals = np.indices(grid.shape)
    return float(grid[(home_goals + away_goals) > line].sum())


def under_probability(score_grid: np.ndarray, line: float) -> float:
    return 1.0 - over_probability(score_grid, line)


def btts_probability(score_grid: np.ndarray) -> float:
    grid = normalize_score_grid(score_grid)
    home_goals, away_goals = np.indices(grid.shape)
    return float(grid[(home_goals >= 1) & (away_goals >= 1)].sum())


def double_chance_probabilities(score_grid: np.ndarray) -> dict[str, float]:
    p_home, p_draw, p_away = one_x_two_probabilities(score_grid)
    return {
        "1X": p_home + p_draw,
        "12": p_home + p_away,
        "X2": p_draw + p_away,
    }


def top_exact_scores(score_grid: np.ndarray, *, top_n: int = 5) -> list[ExactScore]:
    grid = normalize_score_grid(score_grid)
    flat_indexes = np.argsort(grid, axis=None)[::-1][:top_n]
    scores: list[ExactScore] = []
    for flat_index in flat_indexes:
        home_goals, away_goals = np.unravel_index(flat_index, grid.shape)
        scores.append(
            ExactScore(
                home_goals=int(home_goals),
                away_goals=int(away_goals),
                prob=float(grid[home_goals, away_goals]),
            )
        )
    return scores
