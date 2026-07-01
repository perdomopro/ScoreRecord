"""Shared scoring contract for historical and forward evaluation."""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date


EPS = 1e-6
ONE_X_TWO_LABELS = ("home", "draw", "away")


@dataclass(frozen=True)
class BootstrapInterval:
    mean: float
    lower: float
    upper: float
    confidence: float
    iterations: int
    seed: int
    n_blocks: int
    n_rows: int

    def as_dict(self) -> dict[str, float | int]:
        return {
            "mean": self.mean,
            "lower": self.lower,
            "upper": self.upper,
            "confidence": self.confidence,
            "iterations": self.iterations,
            "seed": self.seed,
            "n_blocks": self.n_blocks,
            "n_rows": self.n_rows,
        }


def multiclass_brier(probabilities: Sequence[float], label: int) -> float:
    """Brier score for one 1X2 match row."""
    normalized = normalize_probabilities(probabilities)
    _validate_multiclass(normalized, label)
    return sum(
        (float(probability) - (1.0 if class_index == label else 0.0)) ** 2
        for class_index, probability in enumerate(normalized)
    )


def multiclass_logloss(probabilities: Sequence[float], label: int) -> float:
    """Multiclass log-loss for one 1X2 match row."""
    normalized = normalize_probabilities(probabilities)
    _validate_multiclass(normalized, label)
    return -math.log(clamp_probability(float(normalized[label])))


def multiclass_expected_calibration_error(
    records: Sequence[tuple[Sequence[float], int]],
    *,
    n_bins: int = 10,
) -> float:
    """Average one-vs-rest ECE across home/draw/away."""
    if not records:
        return 0.0
    if n_bins < 1:
        raise ValueError("n_bins must be >= 1")
    eces: list[float] = []
    for class_index in range(len(ONE_X_TWO_LABELS)):
        bins = [{"n": 0.0, "prob_sum": 0.0, "hit_sum": 0.0} for _ in range(n_bins)]
        for probabilities, label in records:
            normalized = normalize_probabilities(probabilities)
            _validate_multiclass(normalized, label)
            probability = normalized[class_index]
            bin_index = min(n_bins - 1, int(probability * n_bins))
            bins[bin_index]["n"] += 1.0
            bins[bin_index]["prob_sum"] += probability
            bins[bin_index]["hit_sum"] += 1.0 if label == class_index else 0.0
        class_ece = 0.0
        for row in bins:
            n = row["n"]
            if n == 0:
                continue
            avg_probability = row["prob_sum"] / n
            frequency = row["hit_sum"] / n
            class_ece += (n / len(records)) * abs(avg_probability - frequency)
        eces.append(class_ece)
    return sum(eces) / len(eces)


def normalize_probabilities(probabilities: Sequence[float]) -> list[float]:
    """Return a probability vector normalized to sum to one."""
    if len(probabilities) != len(ONE_X_TWO_LABELS):
        raise ValueError("1X2 normalization requires exactly three probabilities")
    bounded = [_bounded_probability(float(probability)) for probability in probabilities]
    total = sum(bounded)
    if total <= 0.0 or not math.isfinite(total):
        raise ValueError("Probability vector has invalid mass")
    return [probability / total for probability in bounded]


def binary_brier(probability: float, label: int | bool) -> float:
    """Brier score for one binary market row."""
    target = _binary_label(label)
    return (_bounded_probability(probability) - target) ** 2


def binary_logloss(probability: float, label: int | bool) -> float:
    """Binary log-loss for one binary market row."""
    target = _binary_label(label)
    probability = clamp_probability(probability)
    return -(target * math.log(probability) + (1 - target) * math.log(1.0 - probability))


def clamp_probability(probability: float) -> float:
    if math.isnan(probability):
        raise ValueError("Probability cannot be NaN")
    return min(max(float(probability), EPS), 1.0 - EPS)


def _bounded_probability(probability: float) -> float:
    if math.isnan(probability):
        raise ValueError("Probability cannot be NaN")
    probability = float(probability)
    if probability < 0.0 or probability > 1.0:
        raise ValueError("Probability must be in [0, 1]")
    return probability


def block_bootstrap_mean(
    block_values: Mapping[str, Sequence[float]],
    *,
    iterations: int = 1000,
    confidence: float = 0.95,
    seed: int = 2026,
) -> BootstrapInterval:
    """Bootstrap a metric mean by resampling independent blocks."""
    if iterations < 1:
        raise ValueError("iterations must be >= 1")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be between 0 and 1")
    blocks = {key: [float(value) for value in values] for key, values in block_values.items()}
    blocks = {key: values for key, values in blocks.items() if values}
    if not blocks:
        raise ValueError("At least one non-empty block is required")

    keys = sorted(blocks)
    observed = _mean([value for key in keys for value in blocks[key]])
    rng = random.Random(seed)
    sampled_means: list[float] = []
    for _ in range(iterations):
        sampled: list[float] = []
        for _key in keys:
            sampled.extend(blocks[rng.choice(keys)])
        sampled_means.append(_mean(sampled))
    sampled_means.sort()
    alpha = 1.0 - confidence
    return BootstrapInterval(
        mean=observed,
        lower=_quantile(sampled_means, alpha / 2.0),
        upper=_quantile(sampled_means, 1.0 - alpha / 2.0),
        confidence=confidence,
        iterations=iterations,
        seed=seed,
        n_blocks=len(keys),
        n_rows=sum(len(values) for values in blocks.values()),
    )


def club_week_block(match_date: str, league: str) -> str:
    """Block key for club-league historical odds: league plus ISO week."""
    parsed = date.fromisoformat(match_date[:10])
    iso_year, iso_week, _iso_weekday = parsed.isocalendar()
    return f"{league}:{iso_year}-W{iso_week:02d}"


def worldcup_match_date_block(match_date: str) -> str:
    """Block key for World Cup fixtures, where match date is the natural batch."""
    return match_date[:10]


def _validate_multiclass(probabilities: Sequence[float], label: int) -> None:
    if len(probabilities) != len(ONE_X_TWO_LABELS):
        raise ValueError("1X2 scoring requires exactly three probabilities")
    if label < 0 or label >= len(ONE_X_TWO_LABELS):
        raise ValueError("Multiclass label is out of range")


def _binary_label(label: int | bool) -> int:
    if label not in (0, 1, False, True):
        raise ValueError("Binary label must be 0/1 or bool")
    return int(label)


def _mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("Cannot average an empty sequence")
    return sum(values) / len(values)


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    if not sorted_values:
        raise ValueError("Cannot compute a quantile with no values")
    if q <= 0:
        return float(sorted_values[0])
    if q >= 1:
        return float(sorted_values[-1])
    position = q * (len(sorted_values) - 1)
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return float(sorted_values[lower_index])
    lower = float(sorted_values[lower_index])
    upper = float(sorted_values[upper_index])
    fraction = position - lower_index
    return lower + (upper - lower) * fraction
