"""Benchmark Goals V2 lambdas using strictly local Football-Data data."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import numpy as np

from scorerecord.features.shots_ratings import ProcessRatingTrackerEWMA
from scorerecord.ingest.football_data import resolve_league_code
from scorerecord.models.goals_v2_lambdas import (
    PoissonRegressor,
    compute_independent_probabilities,
    poisson_logloss,
)
from scorerecord.time_utils import parse_kickoff_utc


@dataclass(frozen=True)
class GoalsV2LambdaPrediction:
    match_id: str | None
    date: str
    league: str
    season: str
    home_team: str
    away_team: str
    home_goals: int
    away_goals: int
    model_variant: str
    lambda_home: float
    lambda_away: float
    lambda_total: float
    p_over_15: float
    p_over_25: float
    p_over_35: float
    p_btts_yes: float
    p_btts_no: float


@dataclass(frozen=True)
class GoalsV2LambdaBenchmarkReport:
    date_range: str
    leagues: str
    num_evaluated_matches: int
    raw_matches_loaded: int
    usable_matches_after_missing_stats_filter: int
    skipped_missing_stats: int
    skipped_before_min_train: int
    first_eval_date: str | None
    last_eval_date: str | None
    seasons_evaluated: list[str]
    leagues_evaluated: list[str]
    
    min_train: int
    half_life_days: float
    bootstrap_samples: int
    
    # Joint Home/Away LL
    goals_baseline_joint_home_away_ll: float
    process_only_joint_home_away_ll: float
    goals_plus_process_joint_home_away_ll: float
    
    # Total Goals LL (Pseudo NLL excluding factorial)
    goals_baseline_total_goals_pseudo_ll_excl_factorial: float
    process_only_total_goals_pseudo_ll_excl_factorial: float
    goals_plus_process_total_goals_pseudo_ll_excl_factorial: float
    
    # Deltas & CI
    delta_gpp_vs_goals_baseline: float
    bootstrap_ci_gpp_vs_goals_baseline: tuple[float, float]
    
    # Total Goal MAE / RMSE
    total_goal_mae: dict[str, float]
    total_goal_rmse: dict[str, float]
    
    # Calibration & Metrics
    lambda_total_calibration_buckets: dict[str, Any]
    by_league_metrics: dict[str, Any]
    by_season_metrics: dict[str, Any]
    
    benchmark_state: str


def _get_iso_week(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def run_goals_v2_lambda_benchmark(
    conn: sqlite3.Connection,
    from_date: date,
    to_date: date,
    leagues: tuple[str, ...],
    half_life_days: float = 365.0,
    min_train: int = 500,
    bootstrap_samples: int = 500,
    predictions_out: str | None = None,
) -> GoalsV2LambdaBenchmarkReport:
    """Run walk-forward benchmark for Goals V2 Lambdas."""
    
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
            id,
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
        
        home_ratings = tracker.get_ratings(home, match_dt)
        away_ratings = tracker.get_ratings(away, match_dt)
        
        dataset.append({
            "id": r["id"] if "id" in r.keys() else None,
            "league": r["league_code"],
            "season": r["season"],
            "match_dt": match_dt,
            "iso_week": _get_iso_week(match_dt),
            "home": home,
            "away": away,
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
            
        tracker.update(home, match_dt, home_g, away_g, hs, as_, hst, ast)
        tracker.update(away, match_dt, away_g, home_g, as_, hs, ast, hst)
        
    if len(dataset) <= min_train:
        return _empty_report_with_counts(from_date, to_date, leagues, min_train, half_life_days, bootstrap_samples, len(rows), len(dataset))
        
    dataset.sort(key=lambda x: x["match_dt"])
    
    weeks_data = defaultdict(list)
    for d in dataset:
        weeks_data[d["iso_week"]].append(d)
        
    sorted_weeks = sorted(weeks_data.keys())
    
    eval_results = []
    train_history = []
    
    # Models
    mod_gb_h = PoissonRegressor(4)
    mod_gb_a = PoissonRegressor(4)
    
    mod_po_h = PoissonRegressor(8)
    mod_po_a = PoissonRegressor(8)
    
    mod_gpp_h = PoissonRegressor(12)
    mod_gpp_a = PoissonRegressor(12)
    
    def _extract_X(data: list[dict[str, Any]], variant: str) -> np.ndarray:
        if variant == "gb":
            return np.array([
                [d["gb_h_gf"], d["gb_h_ga"], d["gb_a_gf"], d["gb_a_ga"]] 
                for d in data
            ])
        elif variant == "po":
            return np.array([
                [
                    d["sh_h_sf"], d["sh_h_sa"], d["sh_a_sf"], d["sh_a_sa"],
                    d["sot_h_stf"], d["sot_h_sta"], d["sot_a_stf"], d["sot_a_sta"]
                ]
                for d in data
            ])
        elif variant == "gpp":
            X_gb = _extract_X(data, "gb")
            X_po = _extract_X(data, "po")
            return np.hstack([X_gb, X_po])
        return np.array([])
        
    all_predictions_out = []

    for week in sorted_weeks:
        week_data = weeks_data[week]
        
        if len(train_history) >= min_train:
            y_home = np.array([d["home_goals"] for d in train_history])
            y_away = np.array([d["away_goals"] for d in train_history])
            
            mod_gb_h.fit(_extract_X(train_history, "gb"), y_home)
            mod_gb_a.fit(_extract_X(train_history, "gb"), y_away)
            
            mod_po_h.fit(_extract_X(train_history, "po"), y_home)
            mod_po_a.fit(_extract_X(train_history, "po"), y_away)
            
            mod_gpp_h.fit(_extract_X(train_history, "gpp"), y_home)
            mod_gpp_a.fit(_extract_X(train_history, "gpp"), y_away)
            
            y_home_eval = np.array([d["home_goals"] for d in week_data])
            y_away_eval = np.array([d["away_goals"] for d in week_data])
            y_total_eval = np.array([d["total_goals"] for d in week_data])
            
            lam_gb_h = mod_gb_h.predict(_extract_X(week_data, "gb"))
            lam_gb_a = mod_gb_a.predict(_extract_X(week_data, "gb"))
            
            lam_po_h = mod_po_h.predict(_extract_X(week_data, "po"))
            lam_po_a = mod_po_a.predict(_extract_X(week_data, "po"))
            
            lam_gpp_h = mod_gpp_h.predict(_extract_X(week_data, "gpp"))
            lam_gpp_a = mod_gpp_a.predict(_extract_X(week_data, "gpp"))
            
            # Save results
            for i, d in enumerate(week_data):
                # Calculate probs
                probs_gb = compute_independent_probabilities(lam_gb_h[i], lam_gb_a[i])
                probs_po = compute_independent_probabilities(lam_po_h[i], lam_po_a[i])
                probs_gpp = compute_independent_probabilities(lam_gpp_h[i], lam_gpp_a[i])
                
                eval_results.append({
                    "id": d["id"],
                    "match_dt": d["match_dt"],
                    "league": d["league"],
                    "season": d["season"],
                    "iso_week": week,
                    "y_home": y_home_eval[i],
                    "y_away": y_away_eval[i],
                    "y_total": y_total_eval[i],
                    
                    "lam_gb_h": lam_gb_h[i],
                    "lam_gb_a": lam_gb_a[i],
                    
                    "lam_po_h": lam_po_h[i],
                    "lam_po_a": lam_po_a[i],
                    
                    "lam_gpp_h": lam_gpp_h[i],
                    "lam_gpp_a": lam_gpp_a[i],
                })
                
                if predictions_out:
                    base_info = {
                        "match_id": d["id"],
                        "date": d["match_dt"].isoformat(),
                        "league": d["league"],
                        "season": d["season"],
                        "home_team": d["home"],
                        "away_team": d["away"],
                        "home_goals": int(d["home_goals"]),
                        "away_goals": int(d["away_goals"]),
                    }
                    all_predictions_out.append(
                        GoalsV2LambdaPrediction(
                            **base_info, model_variant="goals_baseline",
                            lambda_home=lam_gb_h[i], lambda_away=lam_gb_a[i], lambda_total=lam_gb_h[i]+lam_gb_a[i],
                            **probs_gb
                        )
                    )
                    all_predictions_out.append(
                        GoalsV2LambdaPrediction(
                            **base_info, model_variant="process_only",
                            lambda_home=lam_po_h[i], lambda_away=lam_po_a[i], lambda_total=lam_po_h[i]+lam_po_a[i],
                            **probs_po
                        )
                    )
                    all_predictions_out.append(
                        GoalsV2LambdaPrediction(
                            **base_info, model_variant="goals_plus_process",
                            lambda_home=lam_gpp_h[i], lambda_away=lam_gpp_a[i], lambda_total=lam_gpp_h[i]+lam_gpp_a[i],
                            **probs_gpp
                        )
                    )
                    
        train_history.extend(week_data)
        
    if predictions_out and all_predictions_out:
        with open(predictions_out, "w", encoding="utf-8") as f:
            for p in all_predictions_out:
                f.write(json.dumps(p.__dict__) + "\n")

    if not eval_results:
        return _empty_report_with_counts(from_date, to_date, leagues, min_train, half_life_days, bootstrap_samples, len(rows), len(dataset))
        
    y_home_all = np.array([r["y_home"] for r in eval_results])
    y_away_all = np.array([r["y_away"] for r in eval_results])
    y_total_all = np.array([r["y_total"] for r in eval_results])
    
    lam_gb_h_all = np.array([r["lam_gb_h"] for r in eval_results])
    lam_gb_a_all = np.array([r["lam_gb_a"] for r in eval_results])
    lam_gb_tot = lam_gb_h_all + lam_gb_a_all
    
    lam_po_h_all = np.array([r["lam_po_h"] for r in eval_results])
    lam_po_a_all = np.array([r["lam_po_a"] for r in eval_results])
    lam_po_tot = lam_po_h_all + lam_po_a_all
    
    lam_gpp_h_all = np.array([r["lam_gpp_h"] for r in eval_results])
    lam_gpp_a_all = np.array([r["lam_gpp_a"] for r in eval_results])
    lam_gpp_tot = lam_gpp_h_all + lam_gpp_a_all

    # Joint Home/Away LL
    gb_joint_ll = poisson_logloss(y_home_all, lam_gb_h_all) + poisson_logloss(y_away_all, lam_gb_a_all)
    po_joint_ll = poisson_logloss(y_home_all, lam_po_h_all) + poisson_logloss(y_away_all, lam_po_a_all)
    gpp_joint_ll = poisson_logloss(y_home_all, lam_gpp_h_all) + poisson_logloss(y_away_all, lam_gpp_a_all)
    
    # Total Goals LL
    gb_tot_ll = poisson_logloss(y_total_all, lam_gb_tot)
    po_tot_ll = poisson_logloss(y_total_all, lam_po_tot)
    gpp_tot_ll = poisson_logloss(y_total_all, lam_gpp_tot)
    
    delta_gpp = gpp_joint_ll - gb_joint_ll
    
    # Bootstrap CI for delta_gpp (joint LL)
    blocks = defaultdict(list)
    for r in eval_results:
        blocks[(r["league"], r["iso_week"])].append(r)
    block_keys = list(blocks.keys())
    n_blocks = len(block_keys)
    
    rng = np.random.default_rng(42)
    boot_deltas_gpp = []
    
    for _ in range(bootstrap_samples):
        sample_keys = rng.choice(block_keys, size=n_blocks, replace=True)
        sum_gb = 0.0
        sum_gpp = 0.0
        count = 0
        for k in sample_keys:
            for r in blocks[tuple(k)]:
                sum_gb += (r["lam_gb_h"] - r["y_home"] * np.log(max(r["lam_gb_h"], 1e-15)))
                sum_gb += (r["lam_gb_a"] - r["y_away"] * np.log(max(r["lam_gb_a"], 1e-15)))
                
                sum_gpp += (r["lam_gpp_h"] - r["y_home"] * np.log(max(r["lam_gpp_h"], 1e-15)))
                sum_gpp += (r["lam_gpp_a"] - r["y_away"] * np.log(max(r["lam_gpp_a"], 1e-15)))
                count += 1
        if count > 0:
            boot_deltas_gpp.append((sum_gpp - sum_gb) / count)
        
    ci_gpp = (
        float(np.percentile(boot_deltas_gpp, 2.5)), 
        float(np.percentile(boot_deltas_gpp, 97.5))
    )
    
    # Calibration buckets (expected total goals vs actual total goals)
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
        
    calib = {
        "goals_baseline": get_calib(lam_gb_tot, y_total_all),
        "process_only": get_calib(lam_po_tot, y_total_all),
        "goals_plus_process": get_calib(lam_gpp_tot, y_total_all)
    }
            
    # By League & Season metrics (using joint logloss for delta)
    by_league = {}
    by_season = {}
    for r in eval_results:
        lg = r["league"]
        sn = r["season"]
        if lg not in by_league:
            by_league[lg] = {"count": 0, "sum_gb_ll": 0.0, "sum_gpp_ll": 0.0}
        if sn not in by_season:
            by_season[sn] = {"count": 0, "sum_gb_ll": 0.0, "sum_gpp_ll": 0.0}
            
        gb = (r["lam_gb_h"] - r["y_home"] * np.log(max(r["lam_gb_h"], 1e-15))) + \
             (r["lam_gb_a"] - r["y_away"] * np.log(max(r["lam_gb_a"], 1e-15)))
        gpp = (r["lam_gpp_h"] - r["y_home"] * np.log(max(r["lam_gpp_h"], 1e-15))) + \
              (r["lam_gpp_a"] - r["y_away"] * np.log(max(r["lam_gpp_a"], 1e-15)))
              
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
    
    # State logic priority: goals_plus_process
    neg_leagues = sum(1 for v in by_league.values() if v["delta"] < 0)
    neg_seasons = sum(1 for v in by_season.values() if v["delta"] < 0)
    
    if delta_gpp < 0 and ci_gpp[1] < -0.005 and neg_leagues >= len(by_league) * 0.5 and neg_seasons >= len(by_season) * 0.5:
        state = "LAMBDA_PROMISING"
    elif delta_gpp > 0 and ci_gpp[0] > 0.0 and neg_leagues <= len(by_league) * 0.2 and neg_seasons <= len(by_season) * 0.2:
        state = "LAMBDA_REJECTED"
    else:
        state = "LAMBDA_INCONCLUSIVE"
        
    return GoalsV2LambdaBenchmarkReport(
        date_range=f"{from_date} to {to_date}",
        leagues=",".join(leagues),
        num_evaluated_matches=len(eval_results),
        raw_matches_loaded=len(rows),
        usable_matches_after_missing_stats_filter=len(dataset),
        skipped_missing_stats=len(rows) - len(dataset),
        skipped_before_min_train=len(dataset) - len(eval_results),
        first_eval_date=eval_results[0]["match_dt"].isoformat() if eval_results else None,
        last_eval_date=eval_results[-1]["match_dt"].isoformat() if eval_results else None,
        seasons_evaluated=list(by_season.keys()),
        leagues_evaluated=list(by_league.keys()),
        min_train=min_train,
        half_life_days=half_life_days,
        bootstrap_samples=bootstrap_samples,
        goals_baseline_joint_home_away_ll=gb_joint_ll,
        process_only_joint_home_away_ll=po_joint_ll,
        goals_plus_process_joint_home_away_ll=gpp_joint_ll,
        goals_baseline_total_goals_pseudo_ll_excl_factorial=gb_tot_ll,
        process_only_total_goals_pseudo_ll_excl_factorial=po_tot_ll,
        goals_plus_process_total_goals_pseudo_ll_excl_factorial=gpp_tot_ll,
        delta_gpp_vs_goals_baseline=delta_gpp,
        bootstrap_ci_gpp_vs_goals_baseline=ci_gpp,
        total_goal_mae={
            "goals_baseline": float(np.mean(np.abs(y_total_all - lam_gb_tot))),
            "process_only": float(np.mean(np.abs(y_total_all - lam_po_tot))),
            "goals_plus_process": float(np.mean(np.abs(y_total_all - lam_gpp_tot)))
        },
        total_goal_rmse={
            "goals_baseline": float(np.sqrt(np.mean((y_total_all - lam_gb_tot)**2))),
            "process_only": float(np.sqrt(np.mean((y_total_all - lam_po_tot)**2))),
            "goals_plus_process": float(np.sqrt(np.mean((y_total_all - lam_gpp_tot)**2)))
        },
        lambda_total_calibration_buckets=calib,
        by_league_metrics=by_league,
        by_season_metrics=by_season,
        benchmark_state=state,
    )


def _empty_report_with_counts(
    from_date: date,
    to_date: date,
    leagues: tuple[str, ...],
    min_train: int,
    half_life_days: float,
    bootstrap_samples: int,
    raw_matches_loaded: int = 0,
    usable_matches_after_missing_stats_filter: int = 0,
) -> GoalsV2LambdaBenchmarkReport:
    return GoalsV2LambdaBenchmarkReport(
        date_range=f"{from_date} to {to_date}",
        leagues=",".join(leagues),
        num_evaluated_matches=0,
        raw_matches_loaded=raw_matches_loaded,
        usable_matches_after_missing_stats_filter=usable_matches_after_missing_stats_filter,
        skipped_missing_stats=raw_matches_loaded - usable_matches_after_missing_stats_filter,
        skipped_before_min_train=usable_matches_after_missing_stats_filter,
        first_eval_date=None,
        last_eval_date=None,
        seasons_evaluated=[],
        leagues_evaluated=[],
        min_train=min_train,
        half_life_days=half_life_days,
        bootstrap_samples=bootstrap_samples,
        goals_baseline_joint_home_away_ll=0.0,
        process_only_joint_home_away_ll=0.0,
        goals_plus_process_joint_home_away_ll=0.0,
        goals_baseline_total_goals_pseudo_ll_excl_factorial=0.0,
        process_only_total_goals_pseudo_ll_excl_factorial=0.0,
        goals_plus_process_total_goals_pseudo_ll_excl_factorial=0.0,
        delta_gpp_vs_goals_baseline=0.0,
        bootstrap_ci_gpp_vs_goals_baseline=(0.0, 0.0),
        total_goal_mae={},
        total_goal_rmse={},
        lambda_total_calibration_buckets={},
        by_league_metrics={},
        by_season_metrics={},
        benchmark_state="LAMBDA_INCONCLUSIVE"
    )
