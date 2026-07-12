"""Goals V2 Lambda modeling layer."""

from __future__ import annotations

import math

import numpy as np


class PoissonRegressor:
    """Simple Poisson regression using Gradient Descent: log(lambda) = X @ w"""
    
    def __init__(
        self,
        n_features: int,
        learning_rate: float = 0.01,
        epochs: int = 1000,
        warm_start: bool = False,
    ):
        self.w = np.zeros(n_features + 1)
        self.learning_rate = learning_rate
        self.epochs = epochs
        self.warm_start = warm_start
        self.mean_x = np.zeros(n_features)
        self.std_x = np.ones(n_features)

    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        """Fit the model using train-only standardization."""
        if len(X) == 0:
            return
            
        # Normalize features for stable GD (Train-only standardization)
        self.mean_x = np.mean(X, axis=0)
        self.std_x = np.std(X, axis=0)
        self.std_x[self.std_x == 0] = 1.0
        
        X_norm = (X - self.mean_x) / self.std_x
        
        # Add intercept
        X_norm = np.hstack([np.ones((X_norm.shape[0], 1)), X_norm])
        
        # Reset weights if not warm starting, or if shapes differ
        if not self.warm_start or self.w.shape[0] != X_norm.shape[1]:
            self.w = np.zeros(X_norm.shape[1])
            # initialize intercept to log(mean(y))
            if np.mean(y) > 0:
                self.w[0] = np.log(np.mean(y))
        
        # Gradient descent
        m = len(y)
        for _ in range(self.epochs):
            eta = X_norm @ self.w
            # Stable numeric clipping
            eta = np.clip(eta, -10.0, 5.0)
            pred = np.exp(eta)
            grad = X_norm.T @ (pred - y) / m
            # Small L2 regularization
            grad[1:] += 0.001 * self.w[1:]
            self.w -= self.learning_rate * grad

    def predict(self, X: np.ndarray) -> np.ndarray:
        """Predict lambda values."""
        if len(X) == 0:
            return np.array([])
        X_norm = (X - self.mean_x) / self.std_x
        X_norm = np.hstack([np.ones((X_norm.shape[0], 1)), X_norm])
        eta = X_norm @ self.w
        # stable numeric clipping to prevent overflow
        eta = np.clip(eta, -10.0, 5.0)
        return np.exp(eta)


def poisson_pmf(lam: np.ndarray, k: int) -> np.ndarray:
    """Compute the Poisson PMF for a given lambda and k."""
    return (np.exp(-lam) * (lam ** k)) / float(math.factorial(k))


def compute_independent_probabilities(
    lambda_home: np.ndarray,
    lambda_away: np.ndarray
) -> dict[str, np.ndarray]:
    """Compute probabilities assuming independent Poisson distributions for home and away."""
    
    lambda_total = lambda_home + lambda_away
    
    # BTTS
    p_h_0 = poisson_pmf(lambda_home, 0)
    p_a_0 = poisson_pmf(lambda_away, 0)
    p_btts_no = p_h_0 + p_a_0 - (p_h_0 * p_a_0)
    p_btts_yes = 1.0 - p_btts_no
    
    # Over/Under
    p_tot_0 = poisson_pmf(lambda_total, 0)
    p_tot_1 = poisson_pmf(lambda_total, 1)
    p_tot_2 = poisson_pmf(lambda_total, 2)
    p_tot_3 = poisson_pmf(lambda_total, 3)
    
    p_over_15 = 1.0 - p_tot_0 - p_tot_1
    p_over_25 = p_over_15 - p_tot_2
    p_over_35 = p_over_25 - p_tot_3
    
    # Sanity clip to [0, 1] for floating point noise
    return {
        "p_btts_yes": np.clip(p_btts_yes, 0.0, 1.0),
        "p_btts_no": np.clip(p_btts_no, 0.0, 1.0),
        "p_over_15": np.clip(p_over_15, 0.0, 1.0),
        "p_over_25": np.clip(p_over_25, 0.0, 1.0),
        "p_over_35": np.clip(p_over_35, 0.0, 1.0),
    }

def poisson_logloss(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Compute mean Poisson negative log-likelihood (excluding log(y!) which is constant)."""
    eps = 1e-15
    y_pred = np.clip(y_pred, eps, None)
    nll = y_pred - y_true * np.log(y_pred)
    return float(np.mean(nll))
