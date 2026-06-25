"""Tune Dixon-Coles time decay with walk-forward close-match log-loss."""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from scorerecord.evaluate.walk_forward import walk_forward
from scorerecord.features.build import MatchFeature, load_match_features
from scorerecord.models.dixon_coles import DixonColes
from scorerecord.runs import ingest_run, record_quality


DEFAULT_HALF_LIVES = (0.5, 1.0, 1.5, 2.0, 3.0)


def half_life_to_xi(years: float) -> float:
    if years <= 0:
        raise ValueError("Half-life must be positive")
    return math.log(2.0) / (years * 365.0)


@dataclass(frozen=True)
class DecayTrial:
    half_life_years: float
    xi: float
    n: int
    logloss: float
    close_logloss: float | None
    close_n: int
    ece: float

    def as_row(self) -> str:
        close = "na" if self.close_logloss is None else f"{self.close_logloss:.4f}"
        return (
            f"half_life={self.half_life_years:>4}y xi={self.xi:.5f} "
            f"n={self.n:<6} logloss={self.logloss:.4f} close_ll={close} ece={self.ece:.4f}"
        )


def tune_decay(
    matches: list[MatchFeature],
    *,
    start_year: int,
    half_lives: tuple[float, ...] = DEFAULT_HALF_LIVES,
    l2: float = 1.0,
) -> list[DecayTrial]:
    trials: list[DecayTrial] = []
    for half_life in half_lives:
        xi = half_life_to_xi(half_life)

        def factory(xi: float = xi, l2: float = l2) -> DixonColes:
            return DixonColes(xi=xi, l2=l2)

        result = walk_forward(matches, factory, start_year=start_year)
        trials.append(
            DecayTrial(
                half_life_years=half_life,
                xi=xi,
                n=result.n,
                logloss=result.logloss,
                close_logloss=result.close_logloss,
                close_n=result.close_n,
                ece=result.ece,
            )
        )
    return trials


def best_trial(trials: list[DecayTrial]) -> DecayTrial:
    if not trials:
        raise ValueError("Cannot select best trial from an empty sweep")
    with_close = [trial for trial in trials if trial.close_logloss is not None]
    if with_close:
        return min(with_close, key=lambda trial: trial.close_logloss or float("inf"))
    return min(trials, key=lambda trial: trial.logloss)


def run_decay_sweep(
    conn: sqlite3.Connection,
    *,
    start_year: int = 2021,
    half_lives: tuple[float, ...] = DEFAULT_HALF_LIVES,
    l2: float = 1.0,
    cwd: Path | None = None,
) -> tuple[list[DecayTrial], DecayTrial]:
    matches = load_match_features(conn)
    with ingest_run(conn, "tune-decay", source="match_history", cwd=cwd) as run_id:
        trials = tune_decay(matches, start_year=start_year, half_lives=half_lives, l2=l2)
        for trial in trials:
            record_quality(
                conn,
                run_id,
                "tune_decay",
                f"hl_{trial.half_life_years}y_close_logloss",
                trial.close_logloss,
                json.dumps(
                    {
                        "half_life_years": trial.half_life_years,
                        "xi": trial.xi,
                        "n": trial.n,
                        "logloss": trial.logloss,
                        "close_logloss": trial.close_logloss,
                        "close_n": trial.close_n,
                        "ece": trial.ece,
                    },
                    sort_keys=True,
                ),
            )
        best = best_trial(trials)
        record_quality(
            conn,
            run_id,
            "tune_decay",
            "best_half_life_years",
            best.half_life_years,
            json.dumps(
                {
                    "xi": best.xi,
                    "logloss": best.logloss,
                    "close_logloss": best.close_logloss,
                    "close_n": best.close_n,
                    "ece": best.ece,
                },
                sort_keys=True,
            ),
        )
        conn.commit()
    return trials, best
