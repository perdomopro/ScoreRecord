"""Calibration of home / draw / away probabilities.

Dixon-Coles 1X2 probabilities come straight from the score matrix and are not
calibrated. This calibrator is a three-parameter conditional logit:

    p'_k  proportional to  exp(T * log p_k + b_k),   b_home = 0

``T`` sharpens (> 1) or flattens (< 1) the forecast; ``b_draw`` and ``b_away``
shift systematic draw or away bias (for example an overestimated home
advantage). It is fitted by Newton's method on out-of-sample predictions with a
small ridge toward the identity (T = 1, b = 0).
"""

from __future__ import annotations

import math

import numpy as np

KEYS = ("home", "draw", "away")
EPS = 1e-9


class ResultCalibrator:
    def __init__(self, *, ridge: float = 1.0, max_iter: int = 50) -> None:
        self.ridge = ridge
        self.max_iter = max_iter
        self.temperature = 1.0
        self.draw_bias = 0.0
        self.away_bias = 0.0
        self.n = 0

    @staticmethod
    def _features(probs: np.ndarray) -> np.ndarray:
        """Per outcome k: [log p_k, 1{k=draw}, 1{k=away}], shape (n, 3, 3)."""
        n = probs.shape[0]
        feats = np.zeros((n, 3, 3))
        feats[:, :, 0] = np.log(np.clip(probs, EPS, 1.0))
        feats[:, 1, 1] = 1.0
        feats[:, 2, 2] = 1.0
        return feats

    def fit(self, probs: list[list[float]], outcomes: list[int]) -> ResultCalibrator:
        if len(probs) != len(outcomes):
            raise ValueError("probs and outcomes must have the same length")
        if not probs:
            raise ValueError("Cannot fit a calibrator without data")
        p = np.asarray(probs, dtype=float)
        y = np.asarray(outcomes, dtype=int)
        feats = self._features(p)
        observed = feats[np.arange(len(y)), y]  # (n, 3)
        prior = np.array([1.0, 0.0, 0.0])
        theta = prior.copy()
        for _ in range(self.max_iter):
            scores = feats @ theta  # (n, 3)
            scores -= scores.max(axis=1, keepdims=True)
            q = np.exp(scores)
            q /= q.sum(axis=1, keepdims=True)
            expected = np.einsum("nk,nkj->nj", q, feats)
            grad = (observed - expected).sum(axis=0) - self.ridge * (theta - prior)
            centered = feats - expected[:, None, :]
            hess = np.einsum("nk,nki,nkj->ij", q, centered, centered) + self.ridge * np.eye(3)
            step = np.linalg.solve(hess, grad)
            theta = theta + step
            if float(np.max(np.abs(step))) < 1e-10:
                break
        self.temperature, self.draw_bias, self.away_bias = (float(v) for v in theta)
        self.n = len(y)
        return self

    def transform(self, probs: dict[str, float]) -> dict[str, float]:
        scores = [
            self.temperature * math.log(max(probs["home"], EPS)),
            self.temperature * math.log(max(probs["draw"], EPS)) + self.draw_bias,
            self.temperature * math.log(max(probs["away"], EPS)) + self.away_bias,
        ]
        top = max(scores)
        exps = [math.exp(s - top) for s in scores]
        total = sum(exps)
        return {key: value / total for key, value in zip(KEYS, exps, strict=True)}

    def as_dict(self) -> dict[str, float]:
        return {
            "temperature": self.temperature,
            "draw_bias": self.draw_bias,
            "away_bias": self.away_bias,
            "n": self.n,
        }
