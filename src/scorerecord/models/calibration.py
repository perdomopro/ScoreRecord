"""Probability calibration helpers."""

from __future__ import annotations

import math

import numpy as np

_EPS = 1e-9


class PlattCalibrator:
    """One-dimensional Platt scaling over logit(probability)."""

    def __init__(
        self,
        *,
        lr: float = 0.05,
        l2: float = 1e-2,
        epochs: int = 600,
    ) -> None:
        self.lr = lr
        self.l2 = l2
        self.epochs = epochs
        self.a = 1.0
        self.b = 0.0

    def fit(self, probs: list[float], labels: list[int]) -> PlattCalibrator:
        if len(probs) != len(labels):
            raise ValueError("probs and labels must have the same length")
        if not probs:
            raise ValueError("Cannot fit calibrator with no data")

        x = np.array([_logit(prob) for prob in probs], dtype=float)
        y = np.array(labels, dtype=float)
        a = 1.0
        b = _logit(_smoothed_rate(labels))

        m_a = v_a = m_b = v_b = 0.0
        beta1 = 0.9
        beta2 = 0.999
        for step in range(1, self.epochs + 1):
            pred = _sigmoid(a * x + b)
            error = pred - y
            grad_a = float(np.mean(error * x) + self.l2 * (a - 1.0))
            grad_b = float(np.mean(error) + self.l2 * b)

            m_a = beta1 * m_a + (1.0 - beta1) * grad_a
            v_a = beta2 * v_a + (1.0 - beta2) * grad_a * grad_a
            m_b = beta1 * m_b + (1.0 - beta1) * grad_b
            v_b = beta2 * v_b + (1.0 - beta2) * grad_b * grad_b

            m_a_hat = m_a / (1.0 - beta1**step)
            v_a_hat = v_a / (1.0 - beta2**step)
            m_b_hat = m_b / (1.0 - beta1**step)
            v_b_hat = v_b / (1.0 - beta2**step)

            a -= self.lr * m_a_hat / (math.sqrt(v_a_hat) + 1e-8)
            b -= self.lr * m_b_hat / (math.sqrt(v_b_hat) + 1e-8)

        self.a = float(a)
        self.b = float(b)
        return self

    def transform(self, prob: float) -> float:
        return float(_sigmoid(self.a * _logit(prob) + self.b))


def _smoothed_rate(labels: list[int]) -> float:
    positives = sum(labels)
    return (positives + 0.5) / (len(labels) + 1.0)


def _logit(prob: float) -> float:
    p = min(max(prob, _EPS), 1.0 - _EPS)
    return math.log(p / (1.0 - p))


def _sigmoid(value: float | np.ndarray) -> float | np.ndarray:
    return 1.0 / (1.0 + np.exp(-value))
