"""Reference baselines for the 1X2 market, implemented with the stdlib only."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from scorerecord.features.build import MatchFeature

N_CLASSES = 3


def _softmax(scores: list[float]) -> list[float]:
    max_score = max(scores)
    exps = [math.exp(score - max_score) for score in scores]
    total = sum(exps)
    return [value / total for value in exps]


@dataclass
class _Scaler:
    means: list[float]
    stds: list[float]

    @classmethod
    def fit(cls, rows: list[list[float]]) -> _Scaler:
        n_features = len(rows[0])
        means = [0.0] * n_features
        for row in rows:
            for index, value in enumerate(row):
                means[index] += value
        means = [value / len(rows) for value in means]

        variances = [0.0] * n_features
        for row in rows:
            for index, value in enumerate(row):
                variances[index] += (value - means[index]) ** 2
        stds = [math.sqrt(value / len(rows)) for value in variances]
        stds = [value if value > 1e-9 else 1.0 for value in stds]
        return cls(means=means, stds=stds)

    def transform(self, row: list[float]) -> list[float]:
        return [(value - self.means[index]) / self.stds[index] for index, value in enumerate(row)]


class OneXTwoBaseline:
    """Multinomial logistic regression over standardized Elo diff and home field."""

    def __init__(
        self,
        *,
        lr: float = 0.3,
        l2: float = 1e-3,
        epochs: int = 500,
        seed: int = 0,
    ) -> None:
        self.lr = lr
        self.l2 = l2
        self.epochs = epochs
        self.seed = seed
        self._scaler: _Scaler | None = None
        self._weights: list[list[float]] | None = None
        self._bias: list[float] | None = None

    @property
    def name(self) -> str:
        return "baseline_logistic"

    def fit(self, matches: list[MatchFeature]) -> OneXTwoBaseline:
        if not matches:
            raise ValueError("Cannot fit on an empty training set")

        raw_features = [match.features() for match in matches]
        outcomes = [match.outcome for match in matches]
        self._scaler = _Scaler.fit(raw_features)
        features = [self._scaler.transform(row) for row in raw_features]
        n_features = len(features[0])

        rng = random.Random(self.seed)
        weights = [
            [rng.uniform(-0.01, 0.01) for _ in range(n_features)] for _ in range(N_CLASSES)
        ]
        bias = [0.0 for _ in range(N_CLASSES)]
        n_rows = len(features)

        for _ in range(self.epochs):
            grad_w = [[0.0] * n_features for _ in range(N_CLASSES)]
            grad_b = [0.0] * N_CLASSES
            for row, label in zip(features, outcomes):
                scores = [
                    bias[class_index]
                    + sum(weights[class_index][feature_index] * row[feature_index]
                          for feature_index in range(n_features))
                    for class_index in range(N_CLASSES)
                ]
                probs = _softmax(scores)
                for class_index in range(N_CLASSES):
                    error = probs[class_index] - (1.0 if class_index == label else 0.0)
                    grad_b[class_index] += error
                    for feature_index in range(n_features):
                        grad_w[class_index][feature_index] += error * row[feature_index]

            for class_index in range(N_CLASSES):
                bias[class_index] -= self.lr * (grad_b[class_index] / n_rows)
                for feature_index in range(n_features):
                    gradient = (
                        grad_w[class_index][feature_index] / n_rows
                        + self.l2 * weights[class_index][feature_index]
                    )
                    weights[class_index][feature_index] -= self.lr * gradient

        self._weights = weights
        self._bias = bias
        return self

    def predict_proba(self, matches: list[MatchFeature]) -> list[list[float]]:
        if self._weights is None or self._scaler is None or self._bias is None:
            raise RuntimeError("Model is not fitted")

        n_features = len(self._weights[0])
        predictions: list[list[float]] = []
        for match in matches:
            row = self._scaler.transform(match.features())
            scores = [
                self._bias[class_index]
                + sum(self._weights[class_index][feature_index] * row[feature_index]
                      for feature_index in range(n_features))
                for class_index in range(N_CLASSES)
            ]
            predictions.append(_softmax(scores))
        return predictions


class MarginalBaseline:
    """Predict the training-set 1X2 class frequencies for every match."""

    def __init__(self) -> None:
        self._probs: list[float] | None = None

    @property
    def name(self) -> str:
        return "baseline_marginal"

    def fit(self, matches: list[MatchFeature]) -> MarginalBaseline:
        counts = [0, 0, 0]
        for match in matches:
            counts[match.outcome] += 1
        total = sum(counts) or 1
        self._probs = [count / total for count in counts]
        return self

    def predict_proba(self, matches: list[MatchFeature]) -> list[list[float]]:
        if self._probs is None:
            raise RuntimeError("Model is not fitted")
        return [list(self._probs) for _ in matches]


class UniformBaseline:
    """Permanent sanity floor: predicts 1/3 for home, draw, and away."""

    @property
    def name(self) -> str:
        return "baseline_uniform"

    def fit(self, matches: list[MatchFeature]) -> UniformBaseline:
        if not matches:
            raise ValueError("Cannot fit on an empty training set")
        return self

    def predict_proba(self, matches: list[MatchFeature]) -> list[list[float]]:
        return [[1.0 / N_CLASSES] * N_CLASSES for _ in matches]
