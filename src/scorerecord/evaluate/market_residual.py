"""Market-anchored residual evaluation engine."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

import numpy as np

from scorerecord.evaluate.backtest import (
    _walk_forward_ou25_benchmark_predictions,
)
from scorerecord.evaluate.scoring import (
    binary_brier,
    binary_logloss,
    club_week_block,
)


def _stable_sigmoid(x: np.ndarray) -> np.ndarray:
    """Compute sigmoid securely against overflow."""
    out = np.zeros_like(x, dtype=float)
    pos = x >= 0
    neg = ~pos
    out[pos] = 1.0 / (1.0 + np.exp(-x[pos]))
    exp_neg = np.exp(x[neg])
    out[neg] = exp_neg / (1.0 + exp_neg)
    return out


def _stable_logit(p: np.ndarray) -> np.ndarray:
    """Compute logit safely."""
    p_safe = np.clip(p, 1e-6, 1.0 - 1e-6)
    return np.log(p_safe / (1.0 - p_safe))


@dataclass(frozen=True)
class Ou25ResidualRow:
    match_id: str
    date: str
    league: str
    season: str
    y: int
    market_preclose_fair: float
    market_close_fair: float
    market_preclose_logit: float
    dixon_coles_raw_prob: float
    dixon_coles_calibrated_prob: float | None
    dc_raw_logit_minus_market_logit: float
    dc_calibrated_logit_minus_market_logit: float | None


class LogisticResidualModel:
    """Binary logistic regression that fits a residual signal over a fixed offset."""

    def __init__(self, l2: float = 1.0, max_iter: int = 100, tol: float = 1e-4) -> None:
        self.l2 = l2
        self.max_iter = max_iter
        self.tol = tol
        self.coef_: np.ndarray | None = None
        self.intercept_ = 0.0
        self.feature_mean_: np.ndarray | None = None
        self.feature_scale_: np.ndarray | None = None
        self.n_iter_ = 0
        self.converged_ = False

    def fit(self, offset_logit: np.ndarray, X: np.ndarray, y: np.ndarray) -> LogisticResidualModel:
        if X.shape[0] == 0:
            return self

        n_samples, n_features = X.shape
        self.feature_mean_ = np.mean(X, axis=0)
        self.feature_scale_ = np.std(X, axis=0)
        
        # Avoid division by zero for zero-variance features
        self.feature_scale_ = np.where(self.feature_scale_ < 1e-9, 1.0, self.feature_scale_)

        X_scaled = (X - self.feature_mean_) / self.feature_scale_
        X_mat = np.hstack([np.ones((n_samples, 1)), X_scaled])
        
        beta = np.zeros(n_features + 1)
        l2_matrix = np.eye(n_features + 1) * self.l2
        l2_matrix[0, 0] = 0.0  # Do not regularize intercept

        for i in range(self.max_iter):
            logits = offset_logit + X_mat @ beta
            p = _stable_sigmoid(logits)
            p = np.clip(p, 1e-6, 1.0 - 1e-6)

            grad = X_mat.T @ (p - y) + l2_matrix @ beta
            W = p * (1.0 - p)
            # W has shape (n_samples,). X_mat.T * W broadcasts W to each row of X_mat.T
            H = (X_mat.T * W) @ X_mat + l2_matrix

            try:
                step = np.linalg.solve(H, grad)
            except np.linalg.LinAlgError:
                # Fallback to pseudoinverse if singular
                step = np.linalg.pinv(H) @ grad

            beta -= step
            self.n_iter_ = i + 1

            if np.max(np.abs(step)) < self.tol:
                self.converged_ = True
                break

        self.intercept_ = float(beta[0])
        self.coef_ = beta[1:]
        return self

    def predict_proba(self, offset_logit: np.ndarray, X: np.ndarray) -> np.ndarray:
        if self.coef_ is None or self.feature_mean_ is None or self.feature_scale_ is None:
            raise RuntimeError("Model is not fitted")
        
        X_scaled = (X - self.feature_mean_) / self.feature_scale_
        logits = offset_logit + self.intercept_ + X_scaled @ self.coef_
        p = _stable_sigmoid(logits)
        return np.clip(p, 1e-6, 1.0 - 1e-6)


def build_ou25_residual_dataset(
    conn: sqlite3.Connection,
    *,
    from_date: date,
    to_date: date,
    leagues: tuple[str, ...],
    entry_odds: str,
    closing_odds: str,
    min_train: int,
) -> list[Ou25ResidualRow]:
    predictions = _walk_forward_ou25_benchmark_predictions(
        conn,
        from_date=from_date,
        to_date=to_date,
        leagues=leagues,
        min_train_size=min_train,
        entry_odds_type=entry_odds,
        closing_odds_type=closing_odds,
        block_size_days=7,
        samples=1,
    )

    dataset: list[Ou25ResidualRow] = []
    for p in predictions:
        # Require strictly valid odds
        if p.entry_over_odds <= 1.0 or p.entry_under_odds <= 1.0:
            continue
        if p.closing_over_odds is None or p.closing_over_odds <= 1.0:
            continue
        if p.closing_under_odds is None or p.closing_under_odds <= 1.0:
            continue
        if (
            p.market_preclose_prob <= 0.0
            or p.market_close_prob is None
            or p.market_close_prob <= 0.0
        ):
            continue

        # For binary y: label is 1 if total_goals > 2.5 else 0 (matches p.label)
        preclose_logit = float(_stable_logit(np.array([p.market_preclose_prob]))[0])
        raw_logit = float(_stable_logit(np.array([p.model_raw_prob]))[0])
        
        calibrated_logit_minus_market = None
        if p.model_calibrated_prob is not None:
            cal_logit = float(_stable_logit(np.array([p.model_calibrated_prob]))[0])
            calibrated_logit_minus_market = cal_logit - preclose_logit

        dataset.append(
            Ou25ResidualRow(
                match_id=p.match.source_event_id,
                date=p.match.match_date,
                league=p.match.league_code,
                season=p.match.season,
                y=p.label,
                market_preclose_fair=p.market_preclose_prob,
                market_close_fair=p.market_close_prob,
                market_preclose_logit=preclose_logit,
                dixon_coles_raw_prob=p.model_raw_prob,
                dixon_coles_calibrated_prob=p.model_calibrated_prob,
                dc_raw_logit_minus_market_logit=raw_logit - preclose_logit,
                dc_calibrated_logit_minus_market_logit=calibrated_logit_minus_market,
            )
        )
    return dataset


@dataclass(frozen=True)
class MarketResidualBenchmarkReport:
    market: str
    residual_benchmark_state: str
    num_matches: int
    num_selection_rows: int
    date_range: str
    leagues: str
    features_used: str
    min_train: int
    bootstrap_samples: int
    model_converged_count: int
    model_failed_count: int
    market_preclose_logloss: float
    market_close_logloss: float
    dixon_coles_calibrated_logloss: float | None
    market_offset_intercept_only_logloss: float
    market_residual_dc_logloss: float
    market_preclose_brier: float
    market_close_brier: float
    dixon_coles_calibrated_brier: float | None
    market_offset_intercept_only_brier: float
    market_residual_dc_brier: float
    delta_residual_vs_preclose: float
    delta_residual_vs_close: float
    delta_residual_vs_dixon_coles_calibrated: float | None
    delta_residual_vs_market_offset_intercept_only: float
    ci_delta_residual_vs_preclose: tuple[float, float]
    ci_delta_residual_vs_close: tuple[float, float]
    ci_delta_residual_vs_dixon_coles_calibrated: tuple[float, float] | None
    ci_delta_residual_vs_market_offset_intercept_only: tuple[float, float]
    model_params_summary: dict[str, float]


def run_market_residual_benchmark(
    conn: sqlite3.Connection,
    *,
    from_date: date,
    to_date: date,
    leagues: tuple[str, ...],
    entry_odds: str,
    closing_odds: str,
    bootstrap_block: str,
    bootstrap_samples: int,
    min_train: int,
    features: tuple[str, ...] = ("dc_raw_logit_minus_market_logit",),
    l2: float = 1.0,
    max_iter: int = 500,
) -> MarketResidualBenchmarkReport:
    if bootstrap_block != "week":
        raise ValueError("Phase 8.0 only supports bootstrap_block='week'.")

    dataset = build_ou25_residual_dataset(
        conn,
        from_date=from_date,
        to_date=to_date,
        leagues=leagues,
        entry_odds=entry_odds,
        closing_odds=closing_odds,
        min_train=min_train,
    )

    if len(dataset) < min_train:
        raise ValueError(f"Insufficient rows ({len(dataset)}) to meet min_train ({min_train})")

    # Sort strictly chronologically
    dataset.sort(key=lambda row: (row.date, row.match_id))

    # Group into evaluation blocks by ISO week to respect chronological walk-forward
    blocks_dict: dict[tuple[str, int, int], list[Ou25ResidualRow]] = defaultdict(list)
    for row in dataset:
        dt = date.fromisoformat(row.date[:10])
        year, week, _ = dt.isocalendar()
        blocks_dict[(row.league, year, week)].append(row)

    # Sort blocks chronologically
    sorted_blocks = sorted(blocks_dict.items(), key=lambda x: (x[0][1], x[0][2], x[0][0]))

    eval_results = []
    converged = 0
    failed = 0

    all_intercepts = []
    all_coefs = []

    # Chronological walk-forward
    current_block_idx = 0

    while current_block_idx < len(sorted_blocks):
        block_key, block_rows = sorted_blocks[current_block_idx]
        block_start_date = block_rows[0].date

        # Add all earlier data to train_buffer
        train_rows = [r for r in dataset if r.date < block_start_date]

        if len(train_rows) >= min_train:
            X_train = _extract_features(train_rows, features)
            y_train = np.array([r.y for r in train_rows], dtype=float)
            offset_train = np.array([r.market_preclose_logit for r in train_rows], dtype=float)

            # Fit full model
            model = LogisticResidualModel(l2=l2, max_iter=max_iter)
            model.fit(offset_train, X_train, y_train)

            # Fit intercept-only model
            model_intercept = LogisticResidualModel(l2=0.0, max_iter=max_iter)
            model_intercept.fit(offset_train, np.zeros((len(train_rows), 0)), y_train)

            if model.converged_:
                converged += 1
                all_intercepts.append(model.intercept_)
                all_coefs.append(model.coef_)
            else:
                failed += 1

            X_eval = _extract_features(block_rows, features)
            offset_eval = np.array([r.market_preclose_logit for r in block_rows], dtype=float)

            p_residual = model.predict_proba(offset_eval, X_eval)
            p_intercept = model_intercept.predict_proba(offset_eval, np.zeros((len(block_rows), 0)))

            for i, row in enumerate(block_rows):
                eval_results.append({
                    "row": row,
                    "p_residual": float(p_residual[i]),
                    "p_intercept": float(p_intercept[i]),
                })

        current_block_idx += 1

    if not eval_results:
        raise ValueError("No evaluation results produced. Check min_train or dataset size.")

    # Prepare for bootstrap
    metrics_by_block = defaultdict(list)
    for res in eval_results:
        row = res["row"]
        y = float(row.y)

        bkey = club_week_block(row.date, row.league)

        m_preclose = binary_logloss(row.market_preclose_fair, y)
        m_close = binary_logloss(row.market_close_fair, y)
        m_dc = (
            binary_logloss(row.dixon_coles_calibrated_prob, y)
            if row.dixon_coles_calibrated_prob
            else None
        )
        m_intercept = binary_logloss(res["p_intercept"], y)
        m_res = binary_logloss(res["p_residual"], y)

        b_preclose = binary_brier(row.market_preclose_fair, y)
        b_close = binary_brier(row.market_close_fair, y)
        b_dc = (
            binary_brier(row.dixon_coles_calibrated_prob, y)
            if row.dixon_coles_calibrated_prob
            else None
        )
        b_intercept = binary_brier(res["p_intercept"], y)
        b_res = binary_brier(res["p_residual"], y)

        metrics_by_block[bkey].append({
            "m_preclose": m_preclose,
            "m_close": m_close,
            "m_dc": m_dc,
            "m_intercept": m_intercept,
            "m_res": m_res,
            "b_preclose": b_preclose,
            "b_close": b_close,
            "b_dc": b_dc,
            "b_intercept": b_intercept,
            "b_res": b_res,
            "d_preclose": m_res - m_preclose,
            "d_close": m_res - m_close,
            "d_dc": (m_res - m_dc) if m_dc is not None else None,
            "d_intercept": m_res - m_intercept,
        })

    def _mean(key: str) -> float | None:
        vals = [item[key] for b in metrics_by_block.values() for item in b if item[key] is not None]
        return float(np.mean(vals)) if vals else None

    # Bootstrapping for CIs
    rng = np.random.default_rng(0)
    block_keys = list(metrics_by_block.keys())
    
    ci_preclose = []
    ci_close = []
    ci_dc = []
    ci_intercept = []

    for _ in range(bootstrap_samples):
        sample_keys = rng.choice(block_keys, size=len(block_keys), replace=True)
        s_d_preclose = []
        s_d_close = []
        s_d_dc = []
        s_d_intercept = []
        
        for k in sample_keys:
            for item in metrics_by_block[k]:
                s_d_preclose.append(item["d_preclose"])
                s_d_close.append(item["d_close"])
                if item["d_dc"] is not None:
                    s_d_dc.append(item["d_dc"])
                s_d_intercept.append(item["d_intercept"])
                
        ci_preclose.append(np.mean(s_d_preclose))
        ci_close.append(np.mean(s_d_close))
        if s_d_dc:
            ci_dc.append(np.mean(s_d_dc))
        ci_intercept.append(np.mean(s_d_intercept))

    def _ci(arr) -> tuple[float, float]:
        return (float(np.percentile(arr, 2.5)), float(np.percentile(arr, 97.5)))

    c_preclose = _ci(ci_preclose)
    c_close = _ci(ci_close)
    c_intercept = _ci(ci_intercept)
    c_dc = _ci(ci_dc) if ci_dc else None

    promising = (c_preclose[1] < 0.0) and (c_intercept[1] < 0.0)
    rejected = (c_preclose[0] > 0.0) or (c_intercept[0] > 0.0)
    
    if promising:
        state = "RESIDUAL_PROMISING"
    elif rejected:
        state = "RESIDUAL_REJECTED"
    else:
        state = "RESIDUAL_INCONCLUSIVE"

    params_summary = {}
    if all_intercepts:
        params_summary["intercept"] = float(np.mean(all_intercepts))
        for i, f in enumerate(features):
            params_summary[f"coef_{f}"] = float(np.mean([c[i] for c in all_coefs]))

    return MarketResidualBenchmarkReport(
        market="OU2.5",
        residual_benchmark_state=state,
        num_matches=len(eval_results),
        num_selection_rows=len(eval_results) * 2,
        date_range=f"{from_date.isoformat()} to {to_date.isoformat()}",
        leagues=",".join(leagues),
        features_used=",".join(features),
        min_train=min_train,
        bootstrap_samples=bootstrap_samples,
        model_converged_count=converged,
        model_failed_count=failed,
        market_preclose_logloss=_mean("m_preclose"),
        market_close_logloss=_mean("m_close"),
        dixon_coles_calibrated_logloss=_mean("m_dc"),
        market_offset_intercept_only_logloss=_mean("m_intercept"),
        market_residual_dc_logloss=_mean("m_res"),
        market_preclose_brier=_mean("b_preclose"),
        market_close_brier=_mean("b_close"),
        dixon_coles_calibrated_brier=_mean("b_dc"),
        market_offset_intercept_only_brier=_mean("b_intercept"),
        market_residual_dc_brier=_mean("b_res"),
        delta_residual_vs_preclose=_mean("d_preclose"),
        delta_residual_vs_close=_mean("d_close"),
        delta_residual_vs_dixon_coles_calibrated=_mean("d_dc"),
        delta_residual_vs_market_offset_intercept_only=_mean("d_intercept"),
        ci_delta_residual_vs_preclose=c_preclose,
        ci_delta_residual_vs_close=c_close,
        ci_delta_residual_vs_dixon_coles_calibrated=c_dc,
        ci_delta_residual_vs_market_offset_intercept_only=c_intercept,
        model_params_summary=params_summary,
    )


def _extract_features(rows: list[Ou25ResidualRow], features: tuple[str, ...]) -> np.ndarray:
    if not features:
        return np.zeros((len(rows), 0), dtype=float)
        
    allowed_features = {
        "dc_raw_logit_minus_market_logit",
    }
    
    for f in features:
        if f not in allowed_features:
            raise ValueError(
                f"Feature '{f}' is not in the allowlist. Allowed features are: {allowed_features}. "
                "elo_diff, dc_calibrated_logit_minus_market_logit, "
                "and market_close_* are strictly prohibited for now."
            )

    out = np.zeros((len(rows), len(features)), dtype=float)
    for i, row in enumerate(rows):
        for j, f in enumerate(features):
            out[i, j] = getattr(row, f)
    return out
