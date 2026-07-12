"""Benchmark process ratings against goal ratings for predicting future goals."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import numpy as np

from scorerecord.features.shots_ratings import ProcessRatingTrackerEWMA
from scorerecord.ingest.football_data import resolve_league_code
from scorerecord.time_utils import parse_kickoff_utc


@dataclass(frozen=True)
class ShotsBenchmarkReport:
    date_range: str
    leagues: str
    num_matches: int
    min_train: int
    half_life_days: float
    goals_baseline_logloss: float
    shots_only_logloss: float
    sot_only_logloss: float
    combined_process_logloss: float
    goals_plus_process_logloss: float
    delta_combined_vs_goals_baseline: float
    delta_goals_plus_process_vs_goals_baseline: float
    bootstrap_ci_combined: tuple[float, float]
    bootstrap_ci_gpp: tuple[float, float]
    benchmark_state: str
    
    # Extra metrics
    home_goal_poisson_logloss: dict[str, float] = field(default_factory=dict)
    away_goal_poisson_logloss: dict[str, float] = field(default_factory=dict)
    total_goal_mae: dict[str, float] = field(default_factory=dict)
    total_goal_rmse: dict[str, float] = field(default_factory=dict)
    calibration_buckets: dict[str, Any] = field(default_factory=dict)
    by_league_metrics: dict[str, Any] = field(default_factory=dict)
    by_season_metrics: dict[str, Any] = field(default_factory=dict)


class PoissonRegressor:
    """Simple Poisson regression using Gradient Descent: log(lambda) = X @ w"""
    def __init__(self, n_features: int, learning_rate: float = 0.01, epochs: int = 1000):
        self.w = np.zeros(n_features + 1)
        self.learning_rate = learning_rate
        self.epochs = epochs
        self.mean_x = np.zeros(n_features)
        self.std_x = np.ones(n_features)

    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        if len(X) == 0:
            return
            
        # Normalize features for stable GD
        self.mean_x = np.mean(X, axis=0)
        self.std_x = np.std(X, axis=0)
        self.std_x[self.std_x == 0] = 1.0
        
        X_norm = (X - self.mean_x) / self.std_x
        
        # Add intercept
        X_norm = np.hstack([np.ones((X_norm.shape[0], 1)), X_norm])
        
        # Warm start if w matches shape
        if self.w.shape[0] != X_norm.shape[1]:
            self.w = np.zeros(X_norm.shape[1])
            # initialize intercept to log(mean(y))
            if np.mean(y) > 0:
                self.w[0] = np.log(np.mean(y))
        
        # Gradient descent
        m = len(y)
        for _ in range(self.epochs):
            eta = X_norm @ self.w
            eta = np.clip(eta, -10.0, 5.0)
            pred = np.exp(eta)
            grad = X_norm.T @ (pred - y) / m
            # Small L2 regularization
            grad[1:] += 0.001 * self.w[1:]
            self.w -= self.learning_rate * grad

    def predict(self, X: np.ndarray) -> np.ndarray:
        if len(X) == 0:
            return np.array([])
        X_norm = (X - self.mean_x) / self.std_x
        X_norm = np.hstack([np.ones((X_norm.shape[0], 1)), X_norm])
        eta = X_norm @ self.w
        eta = np.clip(eta, -10.0, 5.0)  # max exp(5) ~ 150 goals, to prevent overflow
        return np.exp(eta)


def poisson_logloss(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Compute mean Poisson negative log-likelihood (excluding log(y!) which is constant)."""
    # Poisson PMF: lambda^y * exp(-lambda) / y!
    # Log-likelihood: y * log(lambda) - lambda - log(y!)
    # Negative LL: lambda - y * log(lambda)
    # We clip lambda to avoid log(0)
    eps = 1e-15
    y_pred = np.clip(y_pred, eps, None)
    nll = y_pred - y_true * np.log(y_pred)
    return float(np.mean(nll))


def _get_iso_week(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def run_shots_process_benchmark(
    conn: sqlite3.Connection,
    from_date: date,
    to_date: date,
    leagues: tuple[str, ...],
    half_life_days: float = 365.0,
    min_train: int = 500,
) -> ShotsBenchmarkReport:
    """Run walk-forward prequential benchmark comparing goal vs process ratings."""
    
    # 1. Fetch matches chronologically
    league_codes = []
    for lg in leagues:
        try:
            league_codes.append(resolve_league_code(lg))
        except ValueError:
            pass

    placeholders = ",".join(["?"] * len(league_codes))
    
    conn.row_factory = sqlite3.Row
    query = f"""
        SELECT
            league_code,
            season,
            match_date,
            home_team,
            away_team,
            home_goals,
            away_goals,
            raw_json
        FROM historical_match
        WHERE league_code IN ({placeholders})
          AND match_date >= ?
          AND match_date <= ?
        ORDER BY match_date ASC
    """
    params = list(league_codes) + [from_date.isoformat(), to_date.isoformat()]
    rows = conn.execute(query, params).fetchall()

    tracker = ProcessRatingTrackerEWMA(half_life_days=half_life_days)
    
    dataset = []
    
    for r in rows:
        match_dt = parse_kickoff_utc(kickoff_utc=r["match_date"])
        if not match_dt:
            # Fallback to pure date if time missing
            match_dt = datetime.fromisoformat(r["match_date"]).replace(tzinfo=UTC)
            
        home = r["home_team"]
        away = r["away_team"]
        home_g = r["home_goals"]
        away_g = r["away_goals"]
        
        raw = json.loads(r["raw_json"]) if r["raw_json"] else {}
        
        try:
            hs = float(raw.get("HS", ""))
            as_ = float(raw.get("AS", ""))
            hst = float(raw.get("HST", ""))
            ast = float(raw.get("AST", ""))
        except (ValueError, TypeError):
            continue
        
        # 1. Get pre-match ratings (LEAKAGE SAFE)
        home_ratings = tracker.get_ratings(home, match_dt)
        away_ratings = tracker.get_ratings(away, match_dt)
        
        dataset.append({
            "league": r["league_code"],
            "season": r["season"],
            "match_dt": match_dt,
            "iso_week": _get_iso_week(match_dt),
            "home_goals": home_g,
            "away_goals": away_g,
            "total_goals": home_g + away_g,
            
            # Goal Baseline Features
            "gb_h_gf": home_ratings["goals_for"],
            "gb_h_ga": home_ratings["goals_against"],
            "gb_a_gf": away_ratings["goals_for"],
            "gb_a_ga": away_ratings["goals_against"],
            
            # Shots Features
            "sh_h_sf": home_ratings["shots_for"],
            "sh_h_sa": home_ratings["shots_against"],
            "sh_a_sf": away_ratings["shots_for"],
            "sh_a_sa": away_ratings["shots_against"],
            
            # SOT Features
            "sot_h_stf": home_ratings["sot_for"],
            "sot_h_sta": home_ratings["sot_against"],
            "sot_a_stf": away_ratings["sot_for"],
            "sot_a_sta": away_ratings["sot_against"],
        })
            
        # 2. Update post-match (LEAKAGE SAFE)
        tracker.update(home, match_dt, home_g, away_g, hs, as_, hst, ast)
        tracker.update(away, match_dt, away_g, home_g, as_, hs, ast, hst)
        
    if len(dataset) <= min_train:
        return _empty_report(from_date, to_date, leagues, min_train, half_life_days)
        
    # Group dataset by iso_week
    dataset.sort(key=lambda x: x["match_dt"])
    
    weeks_data = defaultdict(list)
    for d in dataset:
        weeks_data[d["iso_week"]].append(d)
        
    sorted_weeks = sorted(weeks_data.keys())
    
    # Store all evaluated results
    eval_results = []
    
    # Track historical data for training
    train_history: list[dict[str, Any]] = []
    
    # We maintain single instances of the regressors to benefit from warm-starts
    # Goals Baseline
    mod_gb_h = PoissonRegressor(4)
    mod_gb_a = PoissonRegressor(4)
    # Shots Only
    mod_sh_h = PoissonRegressor(4)
    mod_sh_a = PoissonRegressor(4)
    # SOT Only
    mod_sot_h = PoissonRegressor(4)
    mod_sot_a = PoissonRegressor(4)
    # Combined (Shots + SOT)
    mod_comb_h = PoissonRegressor(8)
    mod_comb_a = PoissonRegressor(8)
    # Goals + Process
    mod_gpp_h = PoissonRegressor(12)
    mod_gpp_a = PoissonRegressor(12)
    
    def _extract_X(data: list[dict[str, Any]], variant: str) -> np.ndarray:
        if variant == "gb":
            return np.array([
                [d["gb_h_gf"], d["gb_h_ga"], d["gb_a_gf"], d["gb_a_ga"]] 
                for d in data
            ])
        elif variant == "sh":
            return np.array([
                [d["sh_h_sf"], d["sh_h_sa"], d["sh_a_sf"], d["sh_a_sa"]] 
                for d in data
            ])
        elif variant == "sot":
            return np.array([
                [d["sot_h_stf"], d["sot_h_sta"], d["sot_a_stf"], d["sot_a_sta"]] 
                for d in data
            ])
        elif variant == "comb":
            X_sh = _extract_X(data, "sh")
            X_sot = _extract_X(data, "sot")
            return np.hstack([X_sh, X_sot])
        elif variant == "gpp":
            X_gb = _extract_X(data, "gb")
            X_comb = _extract_X(data, "comb")
            return np.hstack([X_gb, X_comb])
        return np.array([])
        
    for week in sorted_weeks:
        week_data = weeks_data[week]
        
        # Train on history if we have enough matches
        if len(train_history) >= min_train:
            y_home = np.array([d["home_goals"] for d in train_history])
            y_away = np.array([d["away_goals"] for d in train_history])
            
            # Fit all models
            mod_gb_h.fit(_extract_X(train_history, "gb"), y_home)
            mod_gb_a.fit(_extract_X(train_history, "gb"), y_away)
            
            mod_sh_h.fit(_extract_X(train_history, "sh"), y_home)
            mod_sh_a.fit(_extract_X(train_history, "sh"), y_away)
            
            mod_sot_h.fit(_extract_X(train_history, "sot"), y_home)
            mod_sot_a.fit(_extract_X(train_history, "sot"), y_away)
            
            mod_comb_h.fit(_extract_X(train_history, "comb"), y_home)
            mod_comb_a.fit(_extract_X(train_history, "comb"), y_away)
            
            mod_gpp_h.fit(_extract_X(train_history, "gpp"), y_home)
            mod_gpp_a.fit(_extract_X(train_history, "gpp"), y_away)
            
            # Predict for current week
            y_home_eval = np.array([d["home_goals"] for d in week_data])
            y_away_eval = np.array([d["away_goals"] for d in week_data])
            y_total_eval = np.array([d["total_goals"] for d in week_data])
            
            pred_gb_h = mod_gb_h.predict(_extract_X(week_data, "gb"))
            pred_gb_a = mod_gb_a.predict(_extract_X(week_data, "gb"))
            
            pred_sh_h = mod_sh_h.predict(_extract_X(week_data, "sh"))
            pred_sh_a = mod_sh_a.predict(_extract_X(week_data, "sh"))
            
            pred_sot_h = mod_sot_h.predict(_extract_X(week_data, "sot"))
            pred_sot_a = mod_sot_a.predict(_extract_X(week_data, "sot"))
            
            pred_comb_h = mod_comb_h.predict(_extract_X(week_data, "comb"))
            pred_comb_a = mod_comb_a.predict(_extract_X(week_data, "comb"))
            
            pred_gpp_h = mod_gpp_h.predict(_extract_X(week_data, "gpp"))
            pred_gpp_a = mod_gpp_a.predict(_extract_X(week_data, "gpp"))
            
            # Save results
            for i, d in enumerate(week_data):
                eval_results.append({
                    "league": d["league"],
                    "season": d["season"],
                    "iso_week": week,
                    "y_home": y_home_eval[i],
                    "y_away": y_away_eval[i],
                    "y_total": y_total_eval[i],
                    "pred_gb_h": pred_gb_h[i],
                    "pred_gb_a": pred_gb_a[i],
                    "pred_sh_h": pred_sh_h[i],
                    "pred_sh_a": pred_sh_a[i],
                    "pred_sot_h": pred_sot_h[i],
                    "pred_sot_a": pred_sot_a[i],
                    "pred_comb_h": pred_comb_h[i],
                    "pred_comb_a": pred_comb_a[i],
                    "pred_gpp_h": pred_gpp_h[i],
                    "pred_gpp_a": pred_gpp_a[i],
                })
        
        # Add current week to training history AFTER evaluating it
        train_history.extend(week_data)
        
    if not eval_results:
        return _empty_report(from_date, to_date, leagues, min_train, half_life_days)
        
    # Aggregate Metrics
    y_home_all = np.array([r["y_home"] for r in eval_results])
    y_away_all = np.array([r["y_away"] for r in eval_results])
    y_total_all = np.array([r["y_total"] for r in eval_results])
    
    pred_gb_h_all = np.array([r["pred_gb_h"] for r in eval_results])
    pred_gb_a_all = np.array([r["pred_gb_a"] for r in eval_results])
    pred_gb_total = pred_gb_h_all + pred_gb_a_all
    
    pred_sh_h_all = np.array([r["pred_sh_h"] for r in eval_results])
    pred_sh_a_all = np.array([r["pred_sh_a"] for r in eval_results])
    
    pred_sot_h_all = np.array([r["pred_sot_h"] for r in eval_results])
    pred_sot_a_all = np.array([r["pred_sot_a"] for r in eval_results])
    
    pred_comb_h_all = np.array([r["pred_comb_h"] for r in eval_results])
    pred_comb_a_all = np.array([r["pred_comb_a"] for r in eval_results])
    
    pred_gpp_h_all = np.array([r["pred_gpp_h"] for r in eval_results])
    pred_gpp_a_all = np.array([r["pred_gpp_a"] for r in eval_results])
    pred_gpp_total = pred_gpp_h_all + pred_gpp_a_all

    gb_ll = (poisson_logloss(y_home_all, pred_gb_h_all) + 
             poisson_logloss(y_away_all, pred_gb_a_all))
    sh_ll = (poisson_logloss(y_home_all, pred_sh_h_all) + 
             poisson_logloss(y_away_all, pred_sh_a_all))
    sot_ll = (poisson_logloss(y_home_all, pred_sot_h_all) + 
              poisson_logloss(y_away_all, pred_sot_a_all))
    comb_ll = (poisson_logloss(y_home_all, pred_comb_h_all) + 
               poisson_logloss(y_away_all, pred_comb_a_all))
    gpp_ll = (poisson_logloss(y_home_all, pred_gpp_h_all) + 
              poisson_logloss(y_away_all, pred_gpp_a_all))
    
    delta_comb = comb_ll - gb_ll
    delta_gpp = gpp_ll - gb_ll
    
    # Bootstrap CI
    blocks = defaultdict(list)
    for r in eval_results:
        blocks[(r["league"], r["iso_week"])].append(r)
    block_keys = list(blocks.keys())
    n_blocks = len(block_keys)
    
    rng = np.random.default_rng(42)
    boot_deltas_comb = []
    boot_deltas_gpp = []
    
    for _ in range(500):
        sample_keys = rng.choice(block_keys, size=n_blocks, replace=True)
        # Instead of allocating giant arrays, just sum the likelihoods for speed
        sum_gb = 0.0
        sum_comb = 0.0
        sum_gpp = 0.0
        count = 0
        for k in sample_keys:
            for r in blocks[tuple(k)]:
                sum_gb += (r["pred_gb_h"] - r["y_home"] * np.log(max(r["pred_gb_h"], 1e-15)))
                sum_gb += (r["pred_gb_a"] - r["y_away"] * np.log(max(r["pred_gb_a"], 1e-15)))
                
                sum_comb += (r["pred_comb_h"] - r["y_home"] * np.log(max(r["pred_comb_h"], 1e-15)))
                sum_comb += (r["pred_comb_a"] - r["y_away"] * np.log(max(r["pred_comb_a"], 1e-15)))
                
                sum_gpp += (r["pred_gpp_h"] - r["y_home"] * np.log(max(r["pred_gpp_h"], 1e-15)))
                sum_gpp += (r["pred_gpp_a"] - r["y_away"] * np.log(max(r["pred_gpp_a"], 1e-15)))
                count += 1
        if count > 0:
            boot_deltas_comb.append((sum_comb - sum_gb) / count)
            boot_deltas_gpp.append((sum_gpp - sum_gb) / count)
        
    ci_comb = (
        float(np.percentile(boot_deltas_comb, 2.5)), 
        float(np.percentile(boot_deltas_comb, 97.5))
    )
    ci_gpp = (
        float(np.percentile(boot_deltas_gpp, 2.5)), 
        float(np.percentile(boot_deltas_gpp, 97.5))
    )
    
    # Calibration buckets (expected total goals)
    percentiles = np.linspace(0, 100, 11)
    
    def get_calib(preds: np.ndarray, y: np.ndarray) -> dict[str, dict[str, float]]:
        bins = np.percentile(preds, percentiles)
        bins[0] = 0.0
        bins[-1] = 100.0
        calib_dict = {}
        for i in range(10):
            mask = (preds > bins[i]) & (preds <= bins[i+1])
            if np.any(mask):
                calib_dict[f"Decile {i+1}"] = {
                    "count": float(np.sum(mask)),
                    "expected": float(np.mean(preds[mask])),
                    "actual": float(np.mean(y[mask])),
                }
        return calib_dict
        
    calib_gb = get_calib(pred_gb_total, y_total_all)
    calib_gpp = get_calib(pred_gpp_total, y_total_all)
    
    calib = {
        "goals_baseline": calib_gb,
        "goals_plus_process": calib_gpp
    }
            
    # By League & Season metrics
    by_league = {}
    by_season = {}
    for r in eval_results:
        lg = r["league"]
        sn = r["season"]
        if lg not in by_league:
            by_league[lg] = {"count": 0, "sum_gb_ll": 0.0, "sum_gpp_ll": 0.0}
        if sn not in by_season:
            by_season[sn] = {"count": 0, "sum_gb_ll": 0.0, "sum_gpp_ll": 0.0}
            
        gb = (r["pred_gb_h"] - r["y_home"] * np.log(max(r["pred_gb_h"], 1e-15))) + \
             (r["pred_gb_a"] - r["y_away"] * np.log(max(r["pred_gb_a"], 1e-15)))
        gpp = (r["pred_gpp_h"] - r["y_home"] * np.log(max(r["pred_gpp_h"], 1e-15))) + \
              (r["pred_gpp_a"] - r["y_away"] * np.log(max(r["pred_gpp_a"], 1e-15)))
              
        by_league[lg]["count"] += 1
        by_league[lg]["sum_gb_ll"] += gb
        by_league[lg]["sum_gpp_ll"] += gpp
        
        by_season[sn]["count"] += 1
        by_season[sn]["sum_gb_ll"] += gb
        by_season[sn]["sum_gpp_ll"] += gpp
        
    for k, v in by_league.items():
        by_league[k] = {
            "count": v["count"],
            "gb_ll": v["sum_gb_ll"] / v["count"],
            "gpp_ll": v["sum_gpp_ll"] / v["count"],
            "delta": (v["sum_gpp_ll"] - v["sum_gb_ll"]) / v["count"],
        }
    for k, v in by_season.items():
        by_season[k] = {
            "count": v["count"],
            "gb_ll": v["sum_gb_ll"] / v["count"],
            "gpp_ll": v["sum_gpp_ll"] / v["count"],
            "delta": (v["sum_gpp_ll"] - v["sum_gb_ll"]) / v["count"],
        }
    
    # State logic
    best_ci = ci_gpp if delta_gpp < delta_comb else ci_comb
    best_delta = min(delta_gpp, delta_comb)
    
    neg_leagues = sum(1 for v in by_league.values() if v["delta"] < 0)
    neg_seasons = sum(1 for v in by_season.values() if v["delta"] < 0)
    
    cond1 = best_ci[1] < -0.005
    cond2 = neg_leagues >= len(by_league) * 0.5
    cond3 = neg_seasons >= len(by_season) * 0.5
    
    if cond1 and cond2 and cond3:
        state = "PROCESS_PROMISING"
    elif best_delta > 0.005 and best_ci[0] > 0.0 and neg_leagues <= len(by_league) * 0.2:
        state = "PROCESS_REJECTED"
    else:
        state = "PROCESS_INCONCLUSIVE"
        
    return ShotsBenchmarkReport(
        date_range=f"{from_date} to {to_date}",
        leagues=",".join(leagues),
        num_matches=len(eval_results),
        min_train=min_train,
        half_life_days=half_life_days,
        goals_baseline_logloss=gb_ll,
        shots_only_logloss=sh_ll,
        sot_only_logloss=sot_ll,
        combined_process_logloss=comb_ll,
        goals_plus_process_logloss=gpp_ll,
        delta_combined_vs_goals_baseline=delta_comb,
        delta_goals_plus_process_vs_goals_baseline=delta_gpp,
        bootstrap_ci_combined=ci_comb,
        bootstrap_ci_gpp=ci_gpp,
        benchmark_state=state,
        home_goal_poisson_logloss={
            "baseline": poisson_logloss(y_home_all, pred_gb_h_all),
            "gpp": poisson_logloss(y_home_all, pred_gpp_h_all)
        },
        away_goal_poisson_logloss={
            "baseline": poisson_logloss(y_away_all, pred_gb_a_all),
            "gpp": poisson_logloss(y_away_all, pred_gpp_a_all)
        },
        total_goal_mae={
            "baseline": float(np.mean(np.abs(y_total_all - pred_gb_total))),
            "gpp": float(np.mean(np.abs(y_total_all - pred_gpp_total)))
        },
        total_goal_rmse={
            "baseline": float(np.sqrt(np.mean((y_total_all - pred_gb_total)**2))),
            "gpp": float(np.sqrt(np.mean((y_total_all - pred_gpp_total)**2)))
        },
        calibration_buckets=calib,
        by_league_metrics=by_league,
        by_season_metrics=by_season,
    )


def _empty_report(
    from_date: date,
    to_date: date,
    leagues: tuple[str, ...],
    min_train: int,
    half_life_days: float
) -> ShotsBenchmarkReport:
    return ShotsBenchmarkReport(
        date_range=f"{from_date} to {to_date}",
        leagues=",".join(leagues),
        num_matches=0,
        min_train=min_train,
        half_life_days=half_life_days,
        goals_baseline_logloss=0.0,
        shots_only_logloss=0.0,
        sot_only_logloss=0.0,
        combined_process_logloss=0.0,
        goals_plus_process_logloss=0.0,
        delta_combined_vs_goals_baseline=0.0,
        delta_goals_plus_process_vs_goals_baseline=0.0,
        bootstrap_ci_combined=(0.0, 0.0),
        bootstrap_ci_gpp=(0.0, 0.0),
        benchmark_state="PROCESS_INCONCLUSIVE"
    )
