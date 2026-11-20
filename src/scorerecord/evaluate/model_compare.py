"""Single gate for model changes (improvement plan, phase 4).

For each club league it builds out-of-sample predictions with a walk-forward
(refit every ``refit_days``, training only on earlier matches) and compares:

* ``base``: league frequencies over the 730 days before each match;
* ``current``: the configuration in production before tuning;
* ``tuned``: half-life, ridge and calibration chosen per league on a
  validation window;
* ``market``: de-vigged closing odds from Football-Data;
* ``blend``: logarithmic pooling of ``tuned`` and ``market`` with a weight
  chosen on the validation window.

Leagues listed in ``POOLS`` also try a pooled variant: the model is trained
on the league together with its second division, with a goal-level offset per
division, so promoted teams keep the rating they earned there. It competes
with the unpooled options on the same validation matches.

Parameters are selected on ``[validate_from, test_from)`` and judged on
``[test_from, until)``; the test window is never used to choose anything.
Differences are paired per match with 95% intervals from a bootstrap that
resamples whole weeks. A candidate is promoted only when its combined
log-loss improvement has an interval entirely below zero and no market gets
significantly worse.
"""

from __future__ import annotations

import bisect
import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np

from scorerecord.features.build import MatchFeature, load_match_features
from scorerecord.models.calibration import PlattCalibrator
from scorerecord.models.dixon_coles import DixonColes
from scorerecord.models.league_params import DEFAULT_CONFIG, ModelConfig
from scorerecord.models.markets import btts_probability, one_x_two_probabilities, over_probability
from scorerecord.models.result_calibration import ResultCalibrator

RESULT_KEYS = ("home", "draw", "away")
MARKETS = ("1X2", "OU2.5", "BTTS")
EPS = 1e-6
MIN_TRAIN = 200
MIN_CALIBRATION = 150
BASE_DAYS = 730
NEWCOMER_MATCHES = 19
# Competitions trained together with a league when the pooled variant is tried.
POOLS: dict[str, tuple[str, ...]] = {"EPL": ("CHAMPIONSHIP",)}


@dataclass(frozen=True)
class Windows:
    oof_from: date
    validate_from: date
    test_from: date
    until: date


@dataclass
class Forecast:
    match: MatchFeature
    block: date
    home: float
    draw: float
    away: float
    over25: float
    btts: float
    over15: float = 0.0
    over35: float = 0.0

    @property
    def day(self) -> date:
        return date.fromisoformat(self.match.match_date[:10])

    def result(self) -> dict[str, float]:
        return {"home": self.home, "draw": self.draw, "away": self.away}


# ----------------------------------------------------------------- data


def league_matches(
    conn: sqlite3.Connection, league: str, pool_with: tuple[str, ...] = ()
) -> list[MatchFeature]:
    wanted = {league, *pool_with}
    return [m for m in load_match_features(conn) if m.tournament in wanted]


def closing_odds(conn: sqlite3.Connection, league: str) -> dict[str, dict[str, float]]:
    """De-vigged closing probabilities per match from the Football-Data rows."""
    rows = conn.execute(
        """
        SELECT m.source_match_id, h.raw_json
        FROM match_history m
        JOIN historical_match h ON h.source_event_id = m.source_match_id
        WHERE m.tournament = ?
        """,
        (league,),
    ).fetchall()
    out: dict[str, dict[str, float]] = {}
    for source_match_id, raw_json in rows:
        try:
            raw = json.loads(raw_json or "{}")
        except json.JSONDecodeError:
            continue
        probs: dict[str, float] = {}
        for h, d, a in (
            ("AvgCH", "AvgCD", "AvgCA"),
            ("PSCH", "PSCD", "PSCA"),
            ("B365CH", "B365CD", "B365CA"),
        ):
            fair = _devig([raw.get(h), raw.get(d), raw.get(a)])
            if fair:
                probs.update(dict(zip(RESULT_KEYS, fair, strict=True)))
                break
        fair_ou = _devig([raw.get("AvgC>2.5"), raw.get("AvgC<2.5")])
        if fair_ou:
            probs["over25"] = fair_ou[0]
        if probs:
            out[str(source_match_id)] = probs
    return out


def _devig(prices: list[object]) -> list[float] | None:
    try:
        values = [float(p) for p in prices]
    except (TypeError, ValueError):
        return None
    if any(not math.isfinite(v) or v <= 1.0 for v in values):
        return None
    implied = [1.0 / v for v in values]
    total = sum(implied)
    return [v / total for v in implied]


# ---------------------------------------------------------- predictions


def xi_for(half_life_years: float) -> float:
    return math.log(2.0) / (half_life_years * 365.0)


def oof_forecasts(
    matches: list[MatchFeature],
    config: ModelConfig,
    *,
    start: date,
    until: date,
    refit_days: int = 28,
    reference: str | None = None,
) -> list[Forecast]:
    """Raw Dixon-Coles forecasts for matches in [start, until), refit per block.

    With ``reference`` set, ``matches`` may include other competitions: they
    are used for training (each with its own goal level) but only matches of
    ``reference`` are forecast.
    """
    ordered = sorted(matches, key=lambda m: m.match_date)
    dates = [m.match_date[:10] for m in ordered]
    out: list[Forecast] = []
    block = start
    while block < until:
        block_end = min(block + timedelta(days=refit_days), until)
        lo = bisect.bisect_left(dates, block.isoformat())
        hi = bisect.bisect_left(dates, block_end.isoformat())
        if hi > lo and lo >= MIN_TRAIN:
            model = DixonColes(
                xi=xi_for(config.half_life_years),
                l2=config.l2,
                solver="newton",
                reference_tournament=reference,
            ).fit(ordered[:lo])
            for match in ordered[lo:hi]:
                if reference is not None and match.tournament != reference:
                    continue
                grid = model.score_matrix(match)
                home, draw, away = one_x_two_probabilities(grid)
                out.append(
                    Forecast(
                        match=match,
                        block=block,
                        home=home,
                        draw=draw,
                        away=away,
                        over25=over_probability(grid, 2.5),
                        btts=btts_probability(grid),
                        over15=over_probability(grid, 1.5),
                        over35=over_probability(grid, 3.5),
                    )
                )
        block = block_end
    return out


def calibrate(forecasts: list[Forecast], config: ModelConfig) -> list[Forecast]:
    """Apply the calibrators of ``config``, fitted only on forecasts of earlier blocks."""
    by_block: dict[date, list[Forecast]] = defaultdict(list)
    for item in forecasts:
        by_block[item.block].append(item)
    history: list[Forecast] = []
    out: list[Forecast] = []
    for block in sorted(by_block):
        current = by_block[block]
        goal_hist = _window(history, block, config.goal_calibration_days)
        result_hist = _window(history, block, config.result_calibration_days)
        over_cal = _platt(goal_hist, "over25") if len(goal_hist) >= MIN_CALIBRATION else None
        btts_cal = _platt(goal_hist, "btts") if len(goal_hist) >= MIN_CALIBRATION else None
        result_cal = None
        if config.calibrate_result and len(result_hist) >= MIN_CALIBRATION:
            result_cal = ResultCalibrator().fit(
                [[f.home, f.draw, f.away] for f in result_hist],
                [_outcome(f.match) for f in result_hist],
            )
        for item in current:
            result = result_cal.transform(item.result()) if result_cal else item.result()
            out.append(
                replace(
                    item,
                    home=result["home"],
                    draw=result["draw"],
                    away=result["away"],
                    over25=over_cal.transform(item.over25) if over_cal else item.over25,
                    btts=btts_cal.transform(item.btts) if btts_cal else item.btts,
                )
            )
        history.extend(current)
    return out


def _window(history: list[Forecast], block: date, days: int | None) -> list[Forecast]:
    if days is None:
        return history
    floor = block - timedelta(days=days)
    return [f for f in history if f.day >= floor]


def _platt(history: list[Forecast], attr: str) -> PlattCalibrator:
    labels = [_label(f.match, attr) for f in history]
    return PlattCalibrator().fit([getattr(f, attr) for f in history], labels)


def _outcome(match: MatchFeature) -> int:
    if match.home_score > match.away_score:
        return 0
    return 1 if match.home_score == match.away_score else 2


def _label(match: MatchFeature, attr: str) -> int:
    if attr == "over25":
        return int(match.home_score + match.away_score > 2.5)
    return int(match.home_score > 0 and match.away_score > 0)


def base_forecasts(matches: list[MatchFeature], targets: list[MatchFeature]) -> list[Forecast]:
    """League frequencies over the 730 days before each target match."""
    ordered = sorted(matches, key=lambda m: m.match_date)
    dates = [m.match_date[:10] for m in ordered]
    prefix = [(0, 0, 0, 0, 0)]
    for m in ordered:
        last = prefix[-1]
        o = _outcome(m)
        prefix.append(
            (
                last[0] + (o == 0),
                last[1] + (o == 1),
                last[2] + (o == 2),
                last[3] + _label(m, "over25"),
                last[4] + _label(m, "btts"),
            )
        )
    out = []
    for match in targets:
        day = date.fromisoformat(match.match_date[:10])
        hi = bisect.bisect_left(dates, day.isoformat())
        lo = bisect.bisect_left(dates, (day - timedelta(days=BASE_DAYS)).isoformat())
        if hi - lo < 30:
            lo = 0
        n = hi - lo
        c = [prefix[hi][i] - prefix[lo][i] for i in range(5)]
        out.append(
            Forecast(
                match=match,
                block=day,
                home=(c[0] + 1) / (n + 3),
                draw=(c[1] + 1) / (n + 3),
                away=(c[2] + 1) / (n + 3),
                over25=(c[3] + 1) / (n + 2),
                btts=(c[4] + 1) / (n + 2),
            )
        )
    return out


def log_pool(model: dict[str, float], market: dict[str, float], weight: float) -> dict[str, float]:
    raw = {
        k: math.exp(
            (1 - weight) * math.log(max(model[k], EPS)) + weight * math.log(max(market[k], EPS))
        )
        for k in model
    }
    total = sum(raw.values())
    return {k: v / total for k, v in raw.items()}


# --------------------------------------------------------------- scoring


def losses(forecasts: list[Forecast]) -> dict[str, np.ndarray]:
    """Per-match log-loss for each market, aligned with ``forecasts``."""
    out = {market: np.zeros(len(forecasts)) for market in MARKETS}
    for i, f in enumerate(forecasts):
        o = _outcome(f.match)
        out["1X2"][i] = -math.log(max((f.home, f.draw, f.away)[o], EPS))
        out["OU2.5"][i] = _bll(f.over25, _label(f.match, "over25"))
        out["BTTS"][i] = _bll(f.btts, _label(f.match, "btts"))
    return out


def _bll(prob: float, label: int) -> float:
    p = min(max(prob, EPS), 1 - EPS)
    return -math.log(p if label else 1 - p)


def summary(forecasts: list[Forecast]) -> dict[str, dict[str, float]]:
    """Log-loss, Brier, accuracy and ECE per market, plus RPS for 1X2."""
    if not forecasts:
        return {}
    ll = losses(forecasts)
    result_pairs: list[tuple[float, int]] = []
    hits, rps, brier = [], [], []
    for f in forecasts:
        o = _outcome(f.match)
        probs = (f.home, f.draw, f.away)
        onehot = [int(k == o) for k in range(3)]
        brier.append(sum((p - y) ** 2 for p, y in zip(probs, onehot, strict=True)))
        rps.append(
            0.5
            * (
                (probs[0] - onehot[0]) ** 2
                + (probs[0] + probs[1] - onehot[0] - onehot[1]) ** 2
            )
        )
        hits.append(int(max(range(3), key=lambda k: probs[k]) == o))
        result_pairs.extend(zip(probs, onehot, strict=True))
    out = {
        "1X2": {
            "logloss": float(ll["1X2"].mean()),
            "brier": float(np.mean(brier)),
            "rps": float(np.mean(rps)),
            "accuracy": float(np.mean(hits)),
            "ece": _ece(result_pairs),
        }
    }
    for market, attr in (("OU2.5", "over25"), ("BTTS", "btts")):
        pairs = [(getattr(f, attr), _label(f.match, attr)) for f in forecasts]
        out[market] = {
            "logloss": float(ll[market].mean()),
            "brier": float(np.mean([(p - y) ** 2 for p, y in pairs])),
            "accuracy": float(np.mean([int((p >= 0.5) == bool(y)) for p, y in pairs])),
            "ece": _ece(pairs),
        }
    return out


def _ece(pairs: list[tuple[float, int]]) -> float:
    bins: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for p, y in pairs:
        bins[min(int(p * 10), 9)].append((p, y))
    total = len(pairs)
    return float(
        sum(
            len(cell) / total * abs(np.mean([p for p, _ in cell]) - np.mean([y for _, y in cell]))
            for cell in bins.values()
        )
    )


def paired_delta(
    candidate: np.ndarray,
    reference: np.ndarray,
    weeks: list[tuple[int, int]],
    *,
    samples: int = 1000,
    seed: int = 0,
) -> dict[str, float]:
    """Mean per-match difference (candidate - reference) with a weekly block bootstrap CI."""
    diff = np.asarray(candidate) - np.asarray(reference)
    groups: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, week in enumerate(weeks):
        groups[week].append(index)
    keys = list(groups)
    sums = np.array([diff[groups[k]].sum() for k in keys])
    counts = np.array([len(groups[k]) for k in keys])
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(keys), size=(samples, len(keys)))
    means = sums[draws].sum(axis=1) / counts[draws].sum(axis=1)
    low, high = np.percentile(means, [2.5, 97.5])
    return {
        "delta": float(diff.mean()),
        "ci_low": float(low),
        "ci_high": float(high),
        "n": int(len(diff)),
    }


# -------------------------------------------------------------- selection


@dataclass
class LeagueReport:
    league: str
    n_validate: int
    n_test: int
    chosen: ModelConfig
    grid: list[dict[str, object]]
    test: dict[str, dict[str, dict[str, float]]]
    deltas: dict[str, dict[str, dict[str, float]]]
    market: dict[str, object]
    newcomers: dict[str, object]
    decision: dict[str, object] = field(default_factory=dict)
    pooling: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "league": self.league,
            "n_validate": self.n_validate,
            "n_test": self.n_test,
            "chosen": self.chosen.as_dict(),
            "chosen_key": self.chosen.key,
            "grid": self.grid,
            "test": self.test,
            "deltas": self.deltas,
            "market": self.market,
            "newcomers": self.newcomers,
            "decision": self.decision,
            "pooling": self.pooling,
        }


def _in(forecasts: list[Forecast], start: date, end: date) -> list[Forecast]:
    return [f for f in forecasts if start <= f.day < end]


def _combined(ll: dict[str, np.ndarray]) -> np.ndarray:
    return ll["1X2"] + ll["OU2.5"] + ll["BTTS"]


def evaluate_league(
    conn: sqlite3.Connection,
    league: str,
    windows: Windows,
    *,
    half_lives: tuple[float, ...] = (1.0, 2.0, 3.0, 5.0),
    l2s: tuple[float, ...] = (0.3, 1.0, 3.0),
    refit_days: int = 28,
    bootstrap: int = 1000,
    matches: list[MatchFeature] | None = None,
    odds: dict[str, dict[str, float]] | None = None,
    pool_with: tuple[str, ...] = (),
    pool_matches: list[MatchFeature] | None = None,
) -> LeagueReport:
    if matches is None:
        loaded = league_matches(conn, league, pool_with)
        matches = [m for m in loaded if m.tournament == league]
        if pool_matches is None:
            pool_matches = [m for m in loaded if m.tournament != league]
    elif pool_with and pool_matches is None:
        pool_matches = [m for m in league_matches(conn, league, pool_with) if m.tournament != league]
    odds = odds if odds is not None else closing_odds(conn, league)
    span = {"start": windows.oof_from, "until": windows.until, "refit_days": refit_days}

    # 1. Model strength: half-life and ridge, scored on validation log-loss.
    raw_by_key: dict[tuple[float, float], list[Forecast]] = {}
    grid_rows: list[dict[str, object]] = []
    for hl in half_lives:
        for l2 in l2s:
            config = ModelConfig(half_life_years=hl, l2=l2)
            raw = oof_forecasts(matches, config, **span)
            raw_by_key[(hl, l2)] = raw
            val = _in(calibrate(raw, config), windows.validate_from, windows.test_from)
            ll = losses(val)
            grid_rows.append(
                {
                    "half_life_years": hl,
                    "l2": l2,
                    "n": len(val),
                    **{m: float(ll[m].mean()) if len(val) else None for m in MARKETS},
                    "combined": float(_combined(ll).mean()) if len(val) else None,
                }
            )
    scored = [row for row in grid_rows if row["combined"] is not None]
    if not scored:
        raise ValueError(f"{league}: no validation matches in the requested windows")
    best = min(scored, key=lambda row: row["combined"])
    hl, l2 = float(best["half_life_years"]), float(best["l2"])
    raw = raw_by_key[(hl, l2)]

    # 2. Calibration options for that model, also scored on validation.
    options = [
        ModelConfig(half_life_years=hl, l2=l2, goal_calibration_days=g, calibrate_result=c)
        for g in (None, 365)
        for c in (False, True)
    ]
    scored_options = []
    for option in options:
        val = _in(calibrate(raw, option), windows.validate_from, windows.test_from)
        scored_options.append((float(_combined(losses(val)).mean()), option.key, option))
    best_unpooled = min(scored_options, key=lambda item: (item[0], item[1]))

    # 2b. Pooled variant: same strength, trained with the second division too.
    pooled_raw: list[Forecast] = []
    best_pooled = None
    if pool_with and pool_matches:
        same = {f.match.source_match_id for f in raw}
        pooled_raw = [
            f
            for f in oof_forecasts(
                matches + pool_matches,
                replace(best_unpooled[2], pool_with=tuple(pool_with)),
                reference=league,
                **span,
            )
            if f.match.source_match_id in same
        ]
        pooled_scores = []
        for option in options:
            option = replace(option, pool_with=tuple(pool_with))
            val = _in(calibrate(pooled_raw, option), windows.validate_from, windows.test_from)
            pooled_scores.append((float(_combined(losses(val)).mean()), option.key, option))
        best_pooled = min(pooled_scores, key=lambda item: (item[0], item[1]))
        scored_options.extend(pooled_scores)
    chosen = min(scored_options, key=lambda item: (item[0], item[1]))[2]
    if chosen.pool_with:
        raw = pooled_raw

    # 3. Market weight, chosen on validation where closing odds exist.
    tuned_all = calibrate(raw, chosen)
    val_tuned = _in(tuned_all, windows.validate_from, windows.test_from)
    weight = _best_weight(val_tuned, odds)
    chosen = replace(
        chosen, market_weight=weight if weight is not None else DEFAULT_CONFIG.market_weight
    )

    # 4. Test window: every candidate on the same matches.
    current_raw = raw_by_key.get((DEFAULT_CONFIG.half_life_years, DEFAULT_CONFIG.l2))
    if current_raw is None:
        current_raw = oof_forecasts(matches, DEFAULT_CONFIG, **span)
    current = _in(calibrate(current_raw, DEFAULT_CONFIG), windows.test_from, windows.until)
    tuned = _in(tuned_all, windows.test_from, windows.until)
    common = {f.match.source_match_id for f in current} & {
        f.match.source_match_id for f in tuned
    }
    current = [f for f in current if f.match.source_match_id in common]
    tuned = [f for f in tuned if f.match.source_match_id in common]
    if not tuned:
        raise ValueError(f"{league}: no test matches in the requested windows")
    base = base_forecasts(matches, [f.match for f in tuned])
    weeks = [f.day.isocalendar()[:2] for f in tuned]

    ll = {"base": losses(base), "current": losses(current), "tuned": losses(tuned)}
    test = {"base": summary(base), "current": summary(current), "tuned": summary(tuned)}
    deltas: dict[str, dict[str, dict[str, float]]] = {}
    for name, (cand, ref) in {
        "tuned_vs_current": ("tuned", "current"),
        "current_vs_base": ("current", "base"),
        "tuned_vs_base": ("tuned", "base"),
    }.items():
        deltas[name] = {
            m: paired_delta(ll[cand][m], ll[ref][m], weeks, samples=bootstrap) for m in MARKETS
        }
        deltas[name]["combined"] = paired_delta(
            _combined(ll[cand]), _combined(ll[ref]), weeks, samples=bootstrap
        )

    report = LeagueReport(
        league=league,
        n_validate=len(val_tuned),
        n_test=len(tuned),
        chosen=chosen,
        grid=grid_rows,
        test=test,
        deltas=deltas,
        market=_market_section(tuned, current, odds, chosen.market_weight, bootstrap),
        newcomers=_newcomer_section(matches, tuned, current, base),
    )
    if best_pooled is not None:
        unpooled_raw = raw_by_key[(hl, l2)]
        report.pooling = _pooling_section(
            matches,
            pool_with,
            unpooled=_in(calibrate(unpooled_raw, best_unpooled[2]), windows.test_from, windows.until),
            pooled=_in(calibrate(pooled_raw, best_pooled[2]), windows.test_from, windows.until),
            validate={"unpooled": best_unpooled[0], "pooled": best_pooled[0]},
            bootstrap=bootstrap,
        )
    report.decision = decide(report)
    return report


def _pooling_section(
    matches: list[MatchFeature],
    pool_with: tuple[str, ...],
    *,
    unpooled: list[Forecast],
    pooled: list[Forecast],
    validate: dict[str, float],
    bootstrap: int,
) -> dict[str, object]:
    """Pooled against unpooled on the test window, overall and for newcomers."""
    by_id = {f.match.source_match_id: f for f in unpooled}
    pooled = [f for f in pooled if f.match.source_match_id in by_id]
    unpooled = [by_id[f.match.source_match_id] for f in pooled]
    out: dict[str, object] = {
        "pool_with": list(pool_with),
        "validate_combined": validate,
        "n_test": len(pooled),
    }
    if not pooled:
        return out
    weeks = [f.day.isocalendar()[:2] for f in pooled]
    lp, lu = losses(pooled), losses(unpooled)
    delta = {m: paired_delta(lp[m], lu[m], weeks, samples=bootstrap) for m in MARKETS}
    delta["combined"] = paired_delta(_combined(lp), _combined(lu), weeks, samples=bootstrap)
    out["pooled_vs_unpooled"] = delta
    newcomers = _newcomer_section(matches, pooled, unpooled, unpooled)
    if newcomers.get("n"):
        out["newcomers"] = {
            "n": newcomers["n"],
            "pooled": newcomers["tuned"],
            "unpooled": newcomers["current"],
        }
    return out


def _best_weight(forecasts: list[Forecast], odds: dict[str, dict[str, float]]) -> float | None:
    pairs = [
        (f, odds[f.match.source_match_id])
        for f in forecasts
        if all(k in odds.get(f.match.source_match_id, {}) for k in RESULT_KEYS)
    ]
    if len(pairs) < 100:
        return None
    best_w, best_ll = None, math.inf
    for step in range(21):
        w = step / 20
        total = 0.0
        for f, market in pairs:
            pooled = log_pool(f.result(), {k: market[k] for k in RESULT_KEYS}, w)
            total -= math.log(max(pooled[RESULT_KEYS[_outcome(f.match)]], EPS))
        if total < best_ll:
            best_w, best_ll = w, total
    return best_w


def _market_section(
    tuned: list[Forecast],
    current: list[Forecast],
    odds: dict[str, dict[str, float]],
    weight: float,
    bootstrap: int,
) -> dict[str, object]:
    by_id = {f.match.source_match_id: f for f in current}
    rows = [
        f for f in tuned if all(k in odds.get(f.match.source_match_id, {}) for k in RESULT_KEYS)
    ]
    if not rows:
        return {"n": 0}
    model_ll, current_ll, market_ll, blend_ll, weeks = [], [], [], [], []
    for f in rows:
        o = RESULT_KEYS[_outcome(f.match)]
        market = {k: odds[f.match.source_match_id][k] for k in RESULT_KEYS}
        blend = log_pool(f.result(), market, weight)
        model_ll.append(-math.log(max(f.result()[o], EPS)))
        current_ll.append(-math.log(max(by_id[f.match.source_match_id].result()[o], EPS)))
        market_ll.append(-math.log(max(market[o], EPS)))
        blend_ll.append(-math.log(max(blend[o], EPS)))
        weeks.append(f.day.isocalendar()[:2])
    m, c, k, b = (np.array(v) for v in (model_ll, current_ll, market_ll, blend_ll))
    section: dict[str, object] = {
        "n": len(rows),
        "weight": weight,
        "logloss_1x2": {
            "current": float(c.mean()),
            "tuned": float(m.mean()),
            "market": float(k.mean()),
            "blend": float(b.mean()),
        },
        "tuned_vs_market": paired_delta(m, k, weeks, samples=bootstrap),
        "blend_vs_market": paired_delta(b, k, weeks, samples=bootstrap),
    }
    ou_rows = [f for f in tuned if "over25" in odds.get(f.match.source_match_id, {})]
    if len(ou_rows) >= 50:
        labels = [_label(f.match, "over25") for f in ou_rows]
        mod = np.array([_bll(f.over25, y) for f, y in zip(ou_rows, labels, strict=True)])
        mkt = np.array(
            [
                _bll(odds[f.match.source_match_id]["over25"], y)
                for f, y in zip(ou_rows, labels, strict=True)
            ]
        )
        section["ou25"] = {
            "n": len(ou_rows),
            "tuned": float(mod.mean()),
            "market": float(mkt.mean()),
            "tuned_vs_market": paired_delta(
                mod, mkt, [f.day.isocalendar()[:2] for f in ou_rows], samples=bootstrap
            ),
        }
    return section


def _newcomer_section(
    matches: list[MatchFeature],
    tuned: list[Forecast],
    current: list[Forecast],
    base: list[Forecast],
) -> dict[str, object]:
    """Log-loss on test matches where a team had fewer than 19 prior league matches."""
    seen: dict[int, int] = defaultdict(int)
    prior: dict[str, int] = {}
    for m in sorted(matches, key=lambda m: m.match_date):
        prior[m.source_match_id] = min(seen[m.home_id], seen[m.away_id])
        seen[m.home_id] += 1
        seen[m.away_id] += 1
    pick = [
        i
        for i, f in enumerate(tuned)
        if prior.get(f.match.source_match_id, 0) < NEWCOMER_MATCHES
    ]
    if not pick:
        return {"n": 0}
    out: dict[str, object] = {"n": len(pick)}
    for name, rows in (("tuned", tuned), ("current", current), ("base", base)):
        ll = losses([rows[i] for i in pick])
        out[name] = {m: float(ll[m].mean()) for m in MARKETS}
    return out


def decide(report: LeagueReport) -> dict[str, object]:
    """Promotion rule: combined gain with CI below zero and no market significantly worse."""
    d = report.deltas["tuned_vs_current"]
    worse = [m for m in MARKETS if d[m]["ci_low"] > 0]
    promote = d["combined"]["ci_high"] < 0 and not worse
    reference = "tuned_vs_base" if promote else "current_vs_base"
    skill = {m: report.deltas[reference][m]["ci_high"] < 0 for m in MARKETS}
    market = report.market
    blend_ok = bool(market.get("n")) and market["blend_vs_market"]["ci_low"] <= 0
    return {
        "promote_tuned": promote,
        "significantly_worse_markets": worse,
        "beats_base": skill,
        "market_weight": report.chosen.market_weight if blend_ok else 1.0,
        "reason": (
            "tuned improves combined log-loss on the test window"
            if promote
            else "no significant improvement over the current model on the test window"
        ),
    }


# ---------------------------------------------------------------- report


def run(
    conn: sqlite3.Connection,
    leagues: tuple[str, ...],
    windows: Windows,
    pools: dict[str, tuple[str, ...]] | None = None,
    **kwargs: object,
) -> dict[str, object]:
    pools = POOLS if pools is None else pools
    reports = [
        evaluate_league(conn, league, windows, pool_with=pools.get(league, ()), **kwargs).as_dict()
        for league in leagues
    ]
    return {
        "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "windows": {k: v.isoformat() for k, v in windows.__dict__.items()},
        "leagues": reports,
    }


def production_configs(report: dict[str, object]) -> dict[str, ModelConfig]:
    """Configuration to publish per league, following each league's decision."""
    out = {}
    for row in report["leagues"]:
        decision = row["decision"]
        chosen = dict(row["chosen"])
        chosen["pool_with"] = tuple(chosen.get("pool_with") or ())
        base = ModelConfig(**chosen) if decision["promote_tuned"] else DEFAULT_CONFIG
        out[row["league"]] = replace(
            base,
            market_weight=float(decision["market_weight"]),
            ou_beats_base=bool(decision["beats_base"]["OU2.5"]),
            btts_beats_base=bool(decision["beats_base"]["BTTS"]),
        )
    return out


def markdown(report: dict[str, object]) -> str:
    w = report["windows"]
    lines = [
        "# Model comparison",
        "",
        f"Generated {report['generated_utc']}. Validation {w['validate_from']} to "
        f"{w['test_from']}, test {w['test_from']} to {w['until']}.",
        "",
        "| League | Test matches | Chosen | Combined tuned - current (95% CI) | Promote | Market weight |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["leagues"]:
        c = row["deltas"]["tuned_vs_current"]["combined"]
        lines.append(
            f"| {row['league']} | {row['n_test']} | {row['chosen_key']} | "
            f"{c['delta']:+.4f} [{c['ci_low']:+.4f}, {c['ci_high']:+.4f}] | "
            f"{'yes' if row['decision']['promote_tuned'] else 'no'} | "
            f"{row['decision']['market_weight']:.2f} |"
        )
    lines += [
        "",
        "| League | Market | Base | Current | Tuned | Market (closing) | Blend |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["leagues"]:
        t = row["test"]
        mk = row["market"].get("logloss_1x2", {})
        for market in MARKETS:
            if market == "1X2" and mk:
                mcol, bcol = f"{mk['market']:.4f}", f"{mk['blend']:.4f}"
            elif market == "OU2.5" and "ou25" in row["market"]:
                mcol, bcol = f"{row['market']['ou25']['market']:.4f}", "—"
            else:
                mcol, bcol = "—", "—"
            lines.append(
                f"| {row['league']} | {market} | {t['base'][market]['logloss']:.4f} | "
                f"{t['current'][market]['logloss']:.4f} | {t['tuned'][market]['logloss']:.4f} | "
                f"{mcol} | {bcol} |"
            )
    pooled_rows = [row for row in report["leagues"] if row.get("pooling", {}).get("n_test")]
    if pooled_rows:
        lines += [
            "",
            "| League | Pooled with | Validation unpooled / pooled | Test pooled - unpooled (95% CI) "
            "| Newcomers 1X2 unpooled / pooled (n) |",
            "| --- | --- | --- | --- | --- |",
        ]
        for row in pooled_rows:
            p = row["pooling"]
            v = p["validate_combined"]
            c = p["pooled_vs_unpooled"]["combined"]
            nc = p.get("newcomers")
            ncol = (
                f"{nc['unpooled']['1X2']:.4f} / {nc['pooled']['1X2']:.4f} ({nc['n']})" if nc else "—"
            )
            lines.append(
                f"| {row['league']} | {'+'.join(p['pool_with'])} | "
                f"{v['unpooled']:.4f} / {v['pooled']:.4f} | "
                f"{c['delta']:+.4f} [{c['ci_low']:+.4f}, {c['ci_high']:+.4f}] | {ncol} |"
            )
    lines += [
        "",
        "Log-loss, lower is better. Market and blend use only matches with closing odds.",
    ]
    return "\n".join(lines) + "\n"


def write_report(report: dict[str, object], outputs_dir: Path) -> tuple[Path, Path]:
    target = outputs_dir / "benchmarks"
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = target / f"model_compare_{stamp}.json"
    md_path = target / f"model_compare_{stamp}.md"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md_path.write_text(markdown(report), encoding="utf-8")
    return json_path, md_path
