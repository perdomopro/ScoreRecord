"""Phase 4: exact Dixon-Coles fit, 1X2 calibration, the model-comparison gate and
per-league settings."""

from __future__ import annotations

import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from scorerecord.evaluate import model_compare as mc
from scorerecord.features.build import MatchFeature
from scorerecord.models import league_params
from scorerecord.models.dixon_coles import DixonColes
from scorerecord.models.league_params import DEFAULT_CONFIG, ModelConfig
from scorerecord.models.result_calibration import ResultCalibrator


def _league(n_seasons: int = 4, teams: int = 16, seed: int = 7) -> list[MatchFeature]:
    """Synthetic double round-robin seasons with stable team strengths."""
    rng = np.random.default_rng(seed)
    attack = rng.normal(0, 0.3, teams)
    defense = rng.normal(0, 0.3, teams)
    out = []
    day = date(2021, 1, 4)
    for season in range(n_seasons):
        for round_ in range(2 * (teams - 1)):
            for i in range(0, teams, 2):
                h, a = (i + round_) % teams, (i + 1 + 2 * round_) % teams
                if h == a:
                    continue
                lh = math.exp(0.25 + 0.25 + attack[h] - defense[a])
                la = math.exp(0.25 + attack[a] - defense[h])
                out.append(
                    MatchFeature(
                        source_match_id=f"s{season}-r{round_}-{h}-{a}",
                        match_date=day.isoformat(),
                        home_id=h,
                        away_id=a,
                        home_score=int(rng.poisson(lh)),
                        away_score=int(rng.poisson(la)),
                        neutral=0,
                        tournament="TEST",
                        elo_home_pre=1500.0,
                        elo_away_pre=1500.0,
                    )
                )
            day += timedelta(days=12)
    return out


WINDOWS = mc.Windows(date(2022, 1, 1), date(2023, 1, 1), date(2024, 1, 1), date(2025, 1, 1))


def _evaluate(matches, odds, bootstrap=200):
    return mc.evaluate_league(
        None,  # type: ignore[arg-type]
        "TEST",
        WINDOWS,
        half_lives=(1.0, 3.0),
        l2s=(1.0,),
        bootstrap=bootstrap,
        matches=matches,
        odds=odds,
    )


def test_newton_and_adam_reach_the_same_optimum() -> None:
    matches = _league(n_seasons=2)
    adam = DixonColes(solver="adam").fit(matches)
    newton = DixonColes(solver="newton").fit(matches)
    assert newton.iterations < 30
    assert newton.penalized_loglik(matches) >= adam.penalized_loglik(matches) - 1e-3
    probe = matches[-1]
    assert np.allclose(
        adam.predict_proba([probe])[0], newton.predict_proba([probe])[0], atol=2e-3
    )


def test_result_calibrator_learns_draw_bias() -> None:
    rng = random.Random(3)
    probs, outcomes = [], []
    for _ in range(4000):
        home = rng.uniform(0.2, 0.7)
        draw = 0.26
        away = 1 - home - draw
        # Truth: draws happen more often than forecast.
        truth = np.array([home, draw * 1.25, away])
        truth /= truth.sum()
        probs.append([home, draw, away])
        outcomes.append(int(rng.choices([0, 1, 2], weights=truth)[0]))
    cal = ResultCalibrator().fit(probs, outcomes)
    assert cal.draw_bias > 0.1
    out = cal.transform({"home": 0.45, "draw": 0.26, "away": 0.29})
    assert math.isclose(sum(out.values()), 1.0)
    assert out["draw"] > 0.26
    identity = ResultCalibrator()
    assert math.isclose(identity.transform({"home": 0.5, "draw": 0.3, "away": 0.2})["home"], 0.5)


def test_gate_runs_walk_forward_and_reports_every_candidate() -> None:
    matches = _league(n_seasons=4)
    odds = {
        m.source_match_id: {"home": 0.45, "draw": 0.27, "away": 0.28}
        for m in matches
        if m.match_date >= "2023-01-01"
    }
    row = _evaluate(matches, odds).as_dict()
    assert row["n_test"] > 50 and row["n_validate"] > 50
    assert set(row["test"]) == {"base", "current", "tuned"}
    delta = row["deltas"]["tuned_vs_current"]["combined"]
    assert delta["ci_low"] <= delta["delta"] <= delta["ci_high"]
    # A constant "market" carries no information, so the fitted weight leans to the model.
    assert row["chosen"]["market_weight"] <= 0.5
    assert row["market"]["n"] > 0
    configs = mc.production_configs({"leagues": [row]})
    assert set(configs) == {"TEST"}
    if not row["decision"]["promote_tuned"]:
        assert configs["TEST"].half_life_years == DEFAULT_CONFIG.half_life_years


def test_test_window_is_never_used_for_selection() -> None:
    matches = _league(n_seasons=4)
    first = _evaluate(matches, {}, bootstrap=50)
    # Swap every score inside the test window: the chosen configuration must not move.
    scrambled = [
        m
        if m.match_date < "2024-01-01"
        else MatchFeature(**{**m.__dict__, "home_score": m.away_score, "away_score": m.home_score})
        for m in matches
    ]
    second = _evaluate(scrambled, {}, bootstrap=50)
    assert first.chosen == second.chosen


def test_paired_delta_bootstrap_brackets_a_known_difference() -> None:
    weeks = [(2025, w) for w in range(1, 41) for _ in range(5)]
    reference = np.full(len(weeks), 1.0)
    out = mc.paired_delta(reference - 0.05, reference, weeks, samples=300)
    assert math.isclose(out["delta"], -0.05)
    assert out["ci_high"] < 0


def test_league_params_roundtrip_and_default(tmp_path: Path) -> None:
    path = tmp_path / "league_params.json"
    assert league_params.league_config("EPL", path=path) == DEFAULT_CONFIG
    config = ModelConfig(
        half_life_years=2.0, l2=0.3, calibrate_result=True, market_weight=0.9, btts_beats_base=False
    )
    league_params.write_params(
        {"EPL": config},
        source="test",
        report="r.json",
        generated_utc="2026-11-20T00:00:00+00:00",
        path=path,
    )
    assert league_params.league_config("EPL", path=path) == config
    assert league_params.league_config("MLS", path=path) == DEFAULT_CONFIG
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["report"] == "r.json" and data["leagues"]["EPL"]["l2"] == 0.3


def test_dashboard_lean_is_not_highlighted_without_skill() -> None:
    from scorerecord.web import data

    lean = data._lean(0.40, 0.55, yes="Ambos anotan", no="No anotan ambos", skilled=False)
    assert not lean["highlight"] and "no ha superado" in lean["note"]
    assert data._lean(0.40, 0.55, yes="Ambos anotan", no="No anotan ambos")["highlight"]


def _two_divisions(
    n_seasons: int = 4, teams: int = 12, movers: int = 2, offset: float = 0.2, seed: int = 11
) -> list[MatchFeature]:
    """Two divisions on one strength scale; ``movers`` teams swap divisions each season.

    The lower division ("LOW") scores ``offset`` more goals (log scale) than the
    top one ("TOP") for the same ratings.
    """
    rng = np.random.default_rng(seed)
    n = 2 * teams
    attack = np.concatenate([rng.normal(0.3, 0.15, teams), rng.normal(-0.3, 0.15, teams)])
    defense = np.concatenate([rng.normal(0.3, 0.15, teams), rng.normal(-0.3, 0.15, teams)])
    top, low = list(range(teams)), list(range(teams, n))
    out = []
    day = date(2021, 1, 4)
    for season in range(n_seasons):
        for name, members, level in (("TOP", top, 0.0), ("LOW", low, offset)):
            pairs = [(h, a) for h in members for a in members if h != a]
            rng.shuffle(pairs)
            for k, (h, a) in enumerate(pairs):
                lh = math.exp(0.2 + level + 0.25 + attack[h] - defense[a])
                la = math.exp(0.2 + level + attack[a] - defense[h])
                out.append(
                    MatchFeature(
                        source_match_id=f"{name}-s{season}-{h}-{a}",
                        match_date=(day + timedelta(days=k // 6)).isoformat(),
                        home_id=int(h),
                        away_id=int(a),
                        home_score=int(rng.poisson(lh)),
                        away_score=int(rng.poisson(la)),
                        neutral=0,
                        tournament=name,
                        elo_home_pre=1500.0,
                        elo_away_pre=1500.0,
                    )
                )
        day += timedelta(days=365)
        down = list(rng.choice(top, movers, replace=False))
        up = list(rng.choice(low, movers, replace=False))
        top = [t for t in top if t not in down] + up
        low = [t for t in low if t not in up] + down
    return out


def test_division_offset_is_recovered_and_only_the_reference_is_forecast() -> None:
    matches = _two_divisions()
    pooled = DixonColes(xi=0.0, solver="newton", reference_tournament="TOP").fit(matches)
    assert set(pooled._offsets) == {"LOW"}
    assert abs(pooled._offsets["LOW"] - 0.2) < 0.1
    probe = next(m for m in matches if m.tournament == "TOP")
    as_low = MatchFeature(**{**probe.__dict__, "tournament": "LOW"})
    assert pooled.score_matrix(as_low)[0, 0] < pooled.score_matrix(probe)[0, 0]
    assert math.isfinite(pooled.penalized_loglik(matches))

    top_only = [m for m in matches if m.tournament == "TOP"]
    plain = DixonColes(xi=0.0, solver="newton").fit(top_only)
    same = DixonColes(xi=0.0, solver="newton", reference_tournament="TOP").fit(top_only)
    assert same._offsets == {}
    assert np.allclose(plain.score_matrix(probe), same.score_matrix(probe))

    config = ModelConfig(pool_with=("LOW",))
    forecasts = mc.oof_forecasts(
        matches, config, start=date(2023, 1, 1), until=date(2025, 1, 1), reference="TOP"
    )
    assert forecasts and all(f.match.tournament == "TOP" for f in forecasts)


def test_gate_reports_the_pooled_variant_and_publishes_it_as_a_tuple(tmp_path: Path) -> None:
    matches = _two_divisions()
    row = mc.evaluate_league(
        None,  # type: ignore[arg-type]
        "TOP",
        mc.Windows(date(2022, 1, 1), date(2023, 1, 1), date(2024, 1, 1), date(2025, 1, 1)),
        half_lives=(3.0,),
        l2s=(1.0,),
        bootstrap=50,
        matches=[m for m in matches if m.tournament == "TOP"],
        odds={},
        pool_with=("LOW",),
        pool_matches=[m for m in matches if m.tournament == "LOW"],
    ).as_dict()
    pooling = row["pooling"]
    assert pooling["pool_with"] == ["LOW"] and pooling["n_test"] > 0
    assert set(pooling["validate_combined"]) == {"unpooled", "pooled"}
    assert "combined" in pooling["pooled_vs_unpooled"]
    assert "Pooled with" in mc.markdown(
        {"generated_utc": "x", "windows": {k: "d" for k in ("validate_from", "test_from", "until")}, "leagues": [row]}
    )

    # A pooled choice survives the JSON round trip as a tuple.
    stored = json.loads(json.dumps(row))
    stored["chosen"]["pool_with"] = ["LOW"]
    stored["decision"]["promote_tuned"] = True
    config = mc.production_configs({"leagues": [stored]})["TOP"]
    assert config.pool_with == ("LOW",) and config.key.endswith("_poolLOW")
    path = tmp_path / "league_params.json"
    league_params.write_params(
        {"TOP": config}, source="t", report="r", generated_utc="2026-11-20T00:00:00+00:00", path=path
    )
    assert league_params.league_config("TOP", path=path) == config


def test_dashboard_explains_the_pooled_variant() -> None:
    from scorerecord.web import data

    details = {
        "league_name": "Premier League",
        "decision": {"promote_tuned": False},
        "chosen": {"pool_with": []},
        "pooling": {
            "pool_with": ["CHAMPIONSHIP"],
            "pooled_vs_unpooled": {
                "combined": {"delta": -0.0015, "ci_low": -0.0283, "ci_high": 0.0235}
            },
        },
    }
    note = data._pooling_note(details)
    assert note and "Championship" in note and "no se adopta" in note
    assert data._pooling_note({"league_name": "MLS"}) is None


def test_write_params_keeps_leagues_outside_the_run(tmp_path: Path) -> None:
    path = tmp_path / "league_params.json"
    epl = ModelConfig(half_life_years=2.0, l2=3.0)
    league_params.write_params(
        {"EPL": epl}, source="t", report="first.json", generated_utc="2026-11-20T00:00:00+00:00", path=path
    )
    serie_a = ModelConfig(half_life_years=1.0, ou_beats_base=False)
    league_params.write_params(
        {"SERIE_A": serie_a}, source="t", report="second.json", generated_utc="2026-11-21T00:00:00+00:00", path=path
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["leagues"]["EPL"]["report"] == "first.json"
    assert data["leagues"]["SERIE_A"]["report"] == "second.json"
    assert league_params.league_config("EPL", path=path) == epl
    assert league_params.league_config("SERIE_A", path=path) == serie_a
