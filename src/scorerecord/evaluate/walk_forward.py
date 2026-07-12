"""Expanding-window walk-forward evaluation for the 1X2 market."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from scorerecord.evaluate.scoring import (
    ONE_X_TWO_LABELS,
    multiclass_brier,
    multiclass_logloss,
)
from scorerecord.features.build import OUTCOME_LABELS, MatchFeature, load_match_features
from scorerecord.models.baseline import MarginalBaseline, OneXTwoBaseline, UniformBaseline
from scorerecord.models.dixon_coles import DixonColes
from scorerecord.runs import ingest_run, record_quality

N_CLASSES = len(ONE_X_TWO_LABELS)


class Model(Protocol):
    name: str

    def fit(self, matches: list[MatchFeature]) -> Model: ...

    def predict_proba(self, matches: list[MatchFeature]) -> list[list[float]]: ...


ModelFactory = Callable[[], Model]


@dataclass(frozen=True)
class WalkForwardResult:
    model: str
    market: str
    n: int
    brier: float
    logloss: float
    ece: float
    close_n: int
    close_logloss: float | None
    competitive_n: int
    competitive_logloss: float | None
    friendly_n: int
    friendly_logloss: float | None
    calibration_bins: list[dict[str, float]]
    class_calibration_bins: dict[str, list[dict[str, float]]]

    def as_row(self) -> str:
        close = "na" if self.close_logloss is None else f"{self.close_logloss:.4f}"
        comp = "na" if self.competitive_logloss is None else f"{self.competitive_logloss:.4f}"
        friendly = "na" if self.friendly_logloss is None else f"{self.friendly_logloss:.4f}"
        return (
            f"{self.model:<20} n={self.n:<6} "
            f"brier={self.brier:.4f} logloss={self.logloss:.4f} "
            f"ece={self.ece:.4f} close_ll={close} comp_ll={comp} friendly_ll={friendly}"
        )


@dataclass(frozen=True)
class PredictionRecord:
    match: MatchFeature
    prob: list[float]


def expected_calibration_error(
    records: list[PredictionRecord],
    *,
    n_bins: int = 10,
) -> tuple[float, list[dict[str, float]]]:
    """Average one-vs-rest ECE across home/draw/away."""
    if not records:
        raise ValueError("Cannot compute ECE with no predictions")

    class_bins = class_reliability_bins(records, n_bins=n_bins)
    class_eces: list[float] = []
    details: list[dict[str, float]] = []
    total_per_class = float(len(records))
    for label, bins in class_bins.items():
        class_ece = 0.0
        for row in bins:
            n = row["n"]
            if n == 0:
                details.append({"class": label, **row})
                continue
            gap = row["gap"]
            class_ece += (n / total_per_class) * gap
            details.append({"class": label, **row})
        class_eces.append(class_ece)
    return sum(class_eces) / len(class_eces), details


def class_reliability_bins(
    records: list[PredictionRecord],
    *,
    n_bins: int = 10,
) -> dict[str, list[dict[str, float]]]:
    """Per-outcome reliability bins for debugging calibration shape."""
    output: dict[str, list[dict[str, float]]] = {}
    for class_index, label in enumerate(OUTCOME_LABELS):
        bins = [{"n": 0.0, "prob_sum": 0.0, "hit_sum": 0.0} for _ in range(n_bins)]
        for record in records:
            probability = record.prob[class_index]
            bin_index = min(n_bins - 1, int(probability * n_bins))
            bins[bin_index]["n"] += 1.0
            bins[bin_index]["prob_sum"] += probability
            bins[bin_index]["hit_sum"] += (
                1.0 if record.match.outcome == class_index else 0.0
            )
        details: list[dict[str, float]] = []
        for index, row in enumerate(bins):
            n = row["n"]
            lower = index / n_bins
            upper = (index + 1) / n_bins
            if n == 0:
                details.append(
                    {"bin": float(index), "lower": lower, "upper": upper, "n": 0.0}
                )
                continue
            avg_probability = row["prob_sum"] / n
            frequency = row["hit_sum"] / n
            details.append(
                {
                    "bin": float(index),
                    "lower": lower,
                    "upper": upper,
                    "n": n,
                    "avg_probability": avg_probability,
                    "frequency": frequency,
                    "gap": abs(avg_probability - frequency),
                }
            )
        output[label] = details
    return output


def walk_forward(
    matches: list[MatchFeature],
    model_factory: ModelFactory,
    *,
    start_year: int,
    min_train: int = 200,
) -> WalkForwardResult:
    """For each year Y >= start_year, train on years before Y and test on Y."""
    by_year: dict[int, list[MatchFeature]] = {}
    for match in matches:
        by_year.setdefault(match.year, []).append(match)
    if not by_year:
        raise ValueError("No matches with features to evaluate")

    last_year = max(by_year)
    brier_sum = 0.0
    logloss_sum = 0.0
    n_predictions = 0
    model_name = model_factory().name
    records: list[PredictionRecord] = []

    for year in range(start_year, last_year + 1):
        test = by_year.get(year, [])
        if not test:
            continue
        train = [match for match in matches if match.year < year]
        if len(train) < min_train:
            continue

        model = model_factory().fit(train)
        probs = model.predict_proba(test)
        for match, prob in zip(test, probs):
            brier_sum += multiclass_brier(prob, match.outcome)
            logloss_sum += multiclass_logloss(prob, match.outcome)
            n_predictions += 1
            records.append(PredictionRecord(match=match, prob=prob))

    if n_predictions == 0:
        raise ValueError("Walk-forward produced no test predictions; check start_year/min_train")

    ece, calibration_bins = expected_calibration_error(records)
    class_bins = class_reliability_bins(records)
    close_logloss, close_n = _segment_logloss(
        records,
        lambda record: abs(record.match.elo_diff) < 75.0,
    )
    competitive_logloss, competitive_n = _segment_logloss(
        records,
        lambda record: not _is_friendly(record.match.tournament),
    )
    friendly_logloss, friendly_n = _segment_logloss(
        records,
        lambda record: _is_friendly(record.match.tournament),
    )

    return WalkForwardResult(
        model=model_name,
        market="1X2",
        n=n_predictions,
        brier=brier_sum / n_predictions,
        logloss=logloss_sum / n_predictions,
        ece=ece,
        close_n=close_n,
        close_logloss=close_logloss,
        competitive_n=competitive_n,
        competitive_logloss=competitive_logloss,
        friendly_n=friendly_n,
        friendly_logloss=friendly_logloss,
        calibration_bins=calibration_bins,
        class_calibration_bins=class_bins,
    )


_FACTORIES: dict[str, ModelFactory] = {
    "baseline_uniform": UniformBaseline,
    "baseline_marginal": MarginalBaseline,
    "baseline_logistic": OneXTwoBaseline,
    "dixon_coles": DixonColes,
}


def _segment_logloss(
    records: list[PredictionRecord],
    predicate: Callable[[PredictionRecord], bool],
) -> tuple[float | None, int]:
    selected = [record for record in records if predicate(record)]
    if not selected:
        return None, 0
    total = sum(multiclass_logloss(record.prob, record.match.outcome) for record in selected)
    return total / len(selected), len(selected)


def _is_friendly(tournament: str | None) -> bool:
    return "friendly" in (tournament or "").casefold()


def run_walk_forward(
    conn: sqlite3.Connection,
    *,
    start_year: int = 2021,
    models: list[str] | None = None,
    cwd: Path | None = None,
) -> list[WalkForwardResult]:
    """Run the harness, persist metrics to eval_metric, and return results."""
    selected = models or list(_FACTORIES)
    unknown = sorted(set(selected) - set(_FACTORIES))
    if unknown:
        raise ValueError(f"Unknown model(s): {', '.join(unknown)}")

    matches = load_match_features(conn)
    as_of = datetime.now(UTC).isoformat(timespec="seconds")

    results: list[WalkForwardResult] = []
    with ingest_run(conn, "walk-forward", source="match_history", cwd=cwd) as run_id:
        record_quality(
            conn,
            run_id,
            "walk_forward",
            "feature_rows",
            float(len(matches)),
            json.dumps({"start_year": start_year, "models": selected}, sort_keys=True),
        )
        for model_key in selected:
            result = walk_forward(matches, _FACTORIES[model_key], start_year=start_year)
            results.append(result)
            detail_payload = {
                "ece": result.ece,
                "calibration_bins": result.calibration_bins,
                "class_calibration_bins": result.class_calibration_bins,
                "segments": {
                    "close": {"n": result.close_n, "logloss": result.close_logloss},
                    "competitive": {
                        "n": result.competitive_n,
                        "logloss": result.competitive_logloss,
                    },
                    "friendly": {"n": result.friendly_n, "logloss": result.friendly_logloss},
                },
            }
            record_quality(
                conn,
                run_id,
                "walk_forward",
                f"{result.model}_ece",
                result.ece,
                json.dumps(detail_payload, sort_keys=True),
            )
            if result.close_logloss is not None:
                record_quality(
                    conn,
                    run_id,
                    "walk_forward",
                    f"{result.model}_close_logloss",
                    result.close_logloss,
                    json.dumps({"n": result.close_n, "max_abs_elo_diff": 75}, sort_keys=True),
                )
            if result.competitive_logloss is not None:
                record_quality(
                    conn,
                    run_id,
                    "walk_forward",
                    f"{result.model}_competitive_logloss",
                    result.competitive_logloss,
                    json.dumps({"n": result.competitive_n}, sort_keys=True),
                )
            if result.friendly_logloss is not None:
                record_quality(
                    conn,
                    run_id,
                    "walk_forward",
                    f"{result.model}_friendly_logloss",
                    result.friendly_logloss,
                    json.dumps({"n": result.friendly_n}, sort_keys=True),
                )
            conn.execute(
                """
                INSERT OR REPLACE INTO eval_metric
                  (run_id, as_of, model, market, n, brier, logloss, roi, clv)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    run_id,
                    as_of,
                    result.model,
                    result.market,
                    result.n,
                    result.brier,
                    result.logloss,
                ),
            )
        conn.commit()
    return results
