"""Dixon-Coles bivariate-Poisson model for the 1X2 market."""

from __future__ import annotations

import math

import numpy as np

from scorerecord.features.build import MatchFeature
from scorerecord.models.markets import one_x_two_probabilities

_DEFAULT_HALF_LIFE_YEARS = 3.0
_DEFAULT_XI = math.log(2.0) / (_DEFAULT_HALF_LIFE_YEARS * 365.0)


class DixonColes:
    """Numpy-only Dixon-Coles model with Adam and ridge shrinkage."""

    def __init__(
        self,
        *,
        xi: float = _DEFAULT_XI,
        l2: float = 1.0,
        epochs: int = 600,
        lr: float = 0.05,
        max_goals: int = 10,
        rho_grid: int = 81,
        seed: int = 0,
    ) -> None:
        self.xi = xi
        self.l2 = l2
        self.epochs = epochs
        self.lr = lr
        self.max_goals = max_goals
        self.rho_grid = rho_grid
        self.seed = seed
        self._team_index: dict[int, int] = {}
        self._attack: np.ndarray | None = None
        self._defense: np.ndarray | None = None
        self._mu = 0.0
        self._gamma = 0.0
        self._rho = 0.0
        self._factorials = np.array(
            [float(math.factorial(goal)) for goal in range(self.max_goals + 1)]
        )

    @property
    def name(self) -> str:
        return "dixon_coles"

    def fit(self, matches: list[MatchFeature]) -> DixonColes:
        if not matches:
            raise ValueError("Cannot fit on an empty training set")

        teams = sorted({match.home_id for match in matches} | {match.away_id for match in matches})
        self._team_index = {team_id: index for index, team_id in enumerate(teams)}
        n_teams = len(teams)

        home = np.array([self._team_index[match.home_id] for match in matches])
        away = np.array([self._team_index[match.away_id] for match in matches])
        home_goals = np.array([match.home_score for match in matches], dtype=float)
        away_goals = np.array([match.away_score for match in matches], dtype=float)
        is_home = np.array([match.home_field for match in matches], dtype=float)

        ordinals = np.array([_date_ordinal(match.match_date) for match in matches], dtype=float)
        days_ago = ordinals.max() - ordinals
        weights = np.exp(-self.xi * days_ago)

        rng = np.random.default_rng(self.seed)
        attack = rng.normal(0.0, 0.01, n_teams)
        defense = rng.normal(0.0, 0.01, n_teams)
        mu = math.log(max(home_goals.mean() + away_goals.mean(), 1e-3) / 2.0)
        gamma = 0.1

        optimizer = _Adam(lr=self.lr)
        for _ in range(self.epochs):
            log_home_lambda = mu + gamma * is_home + attack[home] - defense[away]
            log_away_lambda = mu + attack[away] - defense[home]
            home_lambda = np.exp(log_home_lambda)
            away_lambda = np.exp(log_away_lambda)

            home_residual = weights * (home_goals - home_lambda)
            away_residual = weights * (away_goals - away_lambda)

            grad_mu = home_residual.sum() + away_residual.sum()
            grad_gamma = (is_home * home_residual).sum()

            grad_attack = np.zeros(n_teams)
            np.add.at(grad_attack, home, home_residual)
            np.add.at(grad_attack, away, away_residual)
            grad_attack -= self.l2 * attack

            grad_defense = np.zeros(n_teams)
            np.add.at(grad_defense, away, -home_residual)
            np.add.at(grad_defense, home, -away_residual)
            grad_defense -= self.l2 * defense

            mu, gamma, attack, defense = optimizer.step(
                [mu, gamma, attack, defense],
                [grad_mu, grad_gamma, grad_attack, grad_defense],
            )

        self._attack = attack
        self._defense = defense
        self._mu = float(mu)
        self._gamma = float(gamma)
        self._rho = self._fit_rho(home_goals, away_goals, home, away, is_home, weights)
        return self

    def _fit_rho(
        self,
        home_goals: np.ndarray,
        away_goals: np.ndarray,
        home: np.ndarray,
        away: np.ndarray,
        is_home: np.ndarray,
        weights: np.ndarray,
    ) -> float:
        if self._attack is None or self._defense is None:
            raise RuntimeError("Model is not fitted")

        home_lambda = np.exp(
            self._mu + self._gamma * is_home + self._attack[home] - self._defense[away]
        )
        away_lambda = np.exp(self._mu + self._attack[away] - self._defense[home])
        low_score = (home_goals <= 1) & (away_goals <= 1)
        if not low_score.any():
            return 0.0

        low_home_lambda = home_lambda[low_score]
        low_away_lambda = away_lambda[low_score]
        low_home_goals = home_goals[low_score]
        low_away_goals = away_goals[low_score]
        low_weights = weights[low_score]

        best_rho = 0.0
        best_log_likelihood = -np.inf
        for rho in np.linspace(-0.2, 0.2, self.rho_grid):
            tau = _tau(low_home_goals, low_away_goals, low_home_lambda, low_away_lambda, rho)
            if np.any(tau <= 1e-9):
                continue
            log_likelihood = float(np.sum(low_weights * np.log(tau)))
            if log_likelihood > best_log_likelihood:
                best_log_likelihood = log_likelihood
                best_rho = float(rho)
        return best_rho

    def predict_proba(self, matches: list[MatchFeature]) -> list[list[float]]:
        if self._attack is None or self._defense is None:
            raise RuntimeError("Model is not fitted")

        output: list[list[float]] = []
        for match in matches:
            output.append(one_x_two_probabilities(self.score_matrix(match)))
        return output

    def score_matrix(self, match: MatchFeature) -> np.ndarray:
        """Return normalized P(home_goals, away_goals) for one match."""
        if self._attack is None or self._defense is None:
            raise RuntimeError("Model is not fitted")

        goals = np.arange(self.max_goals + 1)
        home_attack, home_defense = self._team_params(match.home_id)
        away_attack, away_defense = self._team_params(match.away_id)
        home_lambda = math.exp(
            self._mu + self._gamma * match.home_field + home_attack - away_defense
        )
        away_lambda = math.exp(self._mu + away_attack - home_defense)

        home_pmf = np.exp(-home_lambda) * home_lambda**goals / self._factorials
        away_pmf = np.exp(-away_lambda) * away_lambda**goals / self._factorials
        score_grid = np.outer(home_pmf, away_pmf)

        score_grid[0, 0] *= 1.0 - home_lambda * away_lambda * self._rho
        score_grid[0, 1] *= 1.0 + home_lambda * self._rho
        score_grid[1, 0] *= 1.0 + away_lambda * self._rho
        score_grid[1, 1] *= 1.0 - self._rho
        score_grid = np.clip(score_grid, 0.0, None)

        total = score_grid.sum()
        if total <= 0:
            return np.full((self.max_goals + 1, self.max_goals + 1), 1.0 / (self.max_goals + 1) ** 2)
        return score_grid / total

    def _team_params(self, team_id: int) -> tuple[float, float]:
        index = self._team_index.get(team_id)
        if index is None:
            return 0.0, 0.0
        if self._attack is None or self._defense is None:
            raise RuntimeError("Model is not fitted")
        return float(self._attack[index]), float(self._defense[index])


def _tau(
    home_goals: np.ndarray,
    away_goals: np.ndarray,
    home_lambda: np.ndarray,
    away_lambda: np.ndarray,
    rho: float,
) -> np.ndarray:
    tau = np.ones_like(home_lambda)
    score_00 = (home_goals == 0) & (away_goals == 0)
    score_01 = (home_goals == 0) & (away_goals == 1)
    score_10 = (home_goals == 1) & (away_goals == 0)
    score_11 = (home_goals == 1) & (away_goals == 1)
    tau = np.where(score_00, 1.0 - home_lambda * away_lambda * rho, tau)
    tau = np.where(score_01, 1.0 + home_lambda * rho, tau)
    tau = np.where(score_10, 1.0 + away_lambda * rho, tau)
    tau = np.where(score_11, 1.0 - rho, tau)
    return tau


def _date_ordinal(iso_date: str) -> int:
    from datetime import date

    return date.fromisoformat(iso_date[:10]).toordinal()


class _Adam:
    """Minimal Adam optimizer that ascends objective gradients."""

    def __init__(
        self,
        *,
        lr: float,
        b1: float = 0.9,
        b2: float = 0.999,
        eps: float = 1e-8,
    ) -> None:
        self.lr = lr
        self.b1 = b1
        self.b2 = b2
        self.eps = eps
        self.t = 0
        self._m: list[np.ndarray] | None = None
        self._v: list[np.ndarray] | None = None

    def step(self, params: list[object], grads: list[object]) -> list[object]:
        if self._m is None:
            self._m = [np.zeros_like(np.asarray(param, dtype=float)) for param in params]
            self._v = [np.zeros_like(np.asarray(param, dtype=float)) for param in params]
        if self._v is None:
            raise RuntimeError("Adam state is invalid")

        self.t += 1
        output: list[object] = []
        for index, (param, grad) in enumerate(zip(params, grads)):
            param_array = np.asarray(param, dtype=float)
            grad_array = np.asarray(grad, dtype=float)
            self._m[index] = self.b1 * self._m[index] + (1.0 - self.b1) * grad_array
            self._v[index] = self.b2 * self._v[index] + (1.0 - self.b2) * (
                grad_array * grad_array
            )
            m_hat = self._m[index] / (1.0 - self.b1**self.t)
            v_hat = self._v[index] / (1.0 - self.b2**self.t)
            param_array = param_array + self.lr * m_hat / (np.sqrt(v_hat) + self.eps)
            output.append(param_array if param_array.ndim else float(param_array))
        return output
