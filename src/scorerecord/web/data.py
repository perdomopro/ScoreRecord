"""Read-only data access for the web dashboard.

Everything here reads precomputed rows (fixtures, predictions, score grids,
odds snapshots, match history). No model is fitted at request time.
"""

from __future__ import annotations

import bisect
import json
import math
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from scorerecord.forecast_policy import result_signal_strength
from scorerecord.league_config import GUI_LEAGUE_ORDER, league_label
from scorerecord.models.league_params import league_config as model_config
from scorerecord.time_utils import parse_datetime_utc, parse_kickoff_utc

DEFAULT_DISPLAY_TZ = "America/Mexico_City"
# Weight of the market in the model+market blend (logarithmic pooling).
# Provisional: the club OU2.5 benchmark found the market ahead of the model;
# the weight is to be fitted in the model-comparison phase.
MARKET_WEIGHT = 0.7  # fallback; each league's weight comes from models/league_params.json
BASE_RATE_DAYS = 730
RESULT_KEYS = ("home", "draw", "away")
EPS = 1e-6


def display_tz() -> ZoneInfo:
    return ZoneInfo(os.getenv("SCORERECORD_DISPLAY_TZ") or DEFAULT_DISPLAY_TZ)


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the database without write access; the dashboard never writes."""
    resolved = Path(db_path).resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"database not found: {resolved}")
    conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


# --------------------------------------------------------------- probabilities


def devig(odds: dict[str, float]) -> dict[str, float] | None:
    """Fair probabilities from one bookmaker's complete price set."""
    if not odds or any(price <= 1.0 for price in odds.values()):
        return None
    implied = {key: 1.0 / price for key, price in odds.items()}
    total = sum(implied.values())
    return {key: value / total for key, value in implied.items()}


def log_pool(model: dict[str, float], market: dict[str, float], weight: float) -> dict[str, float]:
    """Weighted geometric mean of two distributions over the same outcomes."""
    raw = {
        key: math.exp(
            (1.0 - weight) * math.log(max(model[key], EPS))
            + weight * math.log(max(market[key], EPS))
        )
        for key in model
    }
    total = sum(raw.values())
    return {key: value / total for key, value in raw.items()}


def binary_logloss(prob: float, outcome: int) -> float:
    p = min(max(prob, EPS), 1.0 - EPS)
    return -math.log(p if outcome else 1.0 - p)


# ------------------------------------------------------------------ base rates


class BaseRates:
    """League outcome frequencies over the window before a reference date.

    The honest baseline: a forecast that cannot beat "how often this happens
    in this league" carries no information.
    """

    def __init__(self, conn: sqlite3.Connection, leagues: tuple[str, ...]) -> None:
        self._dates: dict[str, list[str]] = {}
        self._prefix: dict[str, list[tuple[int, ...]]] = {}
        if not leagues:
            return
        placeholders = ",".join("?" for _ in leagues)
        rows = conn.execute(
            f"""
            SELECT tournament, substr(date, 1, 10) AS day, home_score, away_score
            FROM match_history
            WHERE tournament IN ({placeholders})
            ORDER BY tournament, day
            """,
            leagues,
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            grouped[str(row["tournament"])].append(row)
        for league, league_rows in grouped.items():
            dates: list[str] = []
            prefix: list[tuple[int, ...]] = [(0, 0, 0, 0, 0)]
            for row in league_rows:
                h, a = int(row["home_score"]), int(row["away_score"])
                last = prefix[-1]
                prefix.append(
                    (
                        last[0] + (h > a),
                        last[1] + (h == a),
                        last[2] + (h < a),
                        last[3] + (h + a > 2.5),
                        last[4] + (h > 0 and a > 0),
                    )
                )
                dates.append(str(row["day"]))
            self._dates[league] = dates
            self._prefix[league] = prefix

    def at(self, league: str, ref: date) -> dict[str, float] | None:
        dates = self._dates.get(league)
        if not dates:
            return None
        lo = bisect.bisect_left(dates, (ref - timedelta(days=BASE_RATE_DAYS)).isoformat())
        hi = bisect.bisect_left(dates, ref.isoformat())
        n = hi - lo
        if n < 30:
            return None
        prefix = self._prefix[league]
        counts = [prefix[hi][i] - prefix[lo][i] for i in range(5)]
        # Light smoothing keeps a rare outcome from becoming 0 or 1.
        return {
            "home": (counts[0] + 1) / (n + 3),
            "draw": (counts[1] + 1) / (n + 3),
            "away": (counts[2] + 1) / (n + 3),
            "over25": (counts[3] + 1) / (n + 2),
            "btts": (counts[4] + 1) / (n + 2),
            "n": float(n),
        }


# ----------------------------------------------------------------- match view


@dataclass(frozen=True)
class Verdict:
    market: str
    label: str
    correct: bool


@dataclass(frozen=True)
class FormEntry:
    result: str  # "G", "E", "P"
    day: str
    opponent: str
    score: str
    home: bool


@dataclass
class MatchView:
    fixture_id: str
    league_code: str
    kickoff_utc: datetime
    kickoff_local: datetime
    home_id: int
    away_id: int
    home: str
    away: str
    home_score: int | None
    away_score: int | None
    status: str
    model: dict[str, float] | None = None
    market: dict[str, float] | None = None
    market_meta: dict[str, str] | None = None
    base: dict[str, float] | None = None
    xg: tuple[float, float] | None = None
    grid: list[list[float]] | None = None
    run_id: int | None = None
    generated_utc: str | None = None
    artifact_id: str | None = None
    model_version: str | None = None
    snapshots: list[dict[str, object]] = field(default_factory=list)

    @property
    def league_name(self) -> str:
        return league_label(self.league_code)

    @property
    def finished(self) -> bool:
        return self.home_score is not None and self.away_score is not None

    @property
    def outcome(self) -> str | None:
        if not self.finished:
            return None
        if self.home_score > self.away_score:
            return "home"
        return "draw" if self.home_score == self.away_score else "away"

    @property
    def market_weight(self) -> float:
        return model_config(self.league_code).market_weight

    @property
    def uses_market(self) -> bool:
        return bool(self.market and all(key in self.market for key in RESULT_KEYS))

    @property
    def result_probs(self) -> dict[str, float] | None:
        """Headline 1X2 probabilities: model and market blended when odds exist."""
        if self.model is None:
            return None
        model = {key: self.model[key] for key in RESULT_KEYS}
        if self.uses_market:
            return log_pool(
                model, {key: self.market[key] for key in RESULT_KEYS}, self.market_weight
            )
        return model

    @property
    def btts_prob(self) -> float | None:
        if self.model is None:
            return None
        model = self.model["btts"]
        if self.market and "btts" in self.market:
            pooled = log_pool(
                {"yes": model, "no": 1 - model},
                {"yes": self.market["btts"], "no": 1 - self.market["btts"]},
                self.market_weight,
            )
            return pooled["yes"]
        return model

    def outcome_label(self, key: str) -> str:
        return {"home": f"Gana {self.home}", "draw": "Empate", "away": f"Gana {self.away}"}[key]

    @property
    def pick(self) -> dict[str, object] | None:
        probs = self.result_probs
        if probs is None:
            return None
        key = max(RESULT_KEYS, key=lambda k: probs[k])
        strength = result_signal_strength(probs[key])
        return {
            "key": key,
            "label": self.outcome_label(key),
            "prob": probs[key],
            "strength": strength.label,
            "strength_code": strength.code,
        }

    @property
    def goals_lean(self) -> dict[str, object] | None:
        if self.model is None:
            return None
        return _lean(
            self.model["over25"],
            self.base["over25"] if self.base else None,
            yes="Más de 2.5 goles",
            no="Menos de 2.5 goles",
            skilled=model_config(self.league_code).ou_beats_base,
        )

    @property
    def btts_lean(self) -> dict[str, object] | None:
        prob = self.btts_prob
        if prob is None:
            return None
        return _lean(
            prob,
            self.base["btts"] if self.base else None,
            yes="Ambos anotan",
            no="No anotan ambos",
            skilled=model_config(self.league_code).btts_beats_base,
        )

    @property
    def top_scores(self) -> list[tuple[str, float]]:
        if not self.grid:
            return []
        cells = [
            (f"{h}-{a}", prob) for h, row in enumerate(self.grid) for a, prob in enumerate(row)
        ]
        return sorted(cells, key=lambda cell: cell[1], reverse=True)[:5]

    @property
    def verdicts(self) -> list[Verdict]:
        if not self.finished or self.model is None:
            return []
        total = self.home_score + self.away_score
        both = self.home_score > 0 and self.away_score > 0
        out = []
        pick = self.pick
        if pick:
            out.append(Verdict("1X2", str(pick["label"]), pick["key"] == self.outcome))
        over = self.model["over25"] >= 0.5
        out.append(
            Verdict("OU2.5", "Más de 2.5" if over else "Menos de 2.5", over == (total > 2.5))
        )
        btts = self.btts_prob
        if btts is not None:
            yes = btts >= 0.5
            out.append(Verdict("BTTS", "Ambos anotan" if yes else "No ambos", yes == both))
        return out

    def as_json(self) -> dict[str, object]:
        return {
            "fixture_id": self.fixture_id,
            "league": self.league_code,
            "kickoff_utc": self.kickoff_utc.isoformat(),
            "home": self.home,
            "away": self.away,
            "status": self.status,
            "score": [self.home_score, self.away_score] if self.finished else None,
            "model": self.model,
            "market": self.market,
            "market_source": self.market_meta,
            "blend_1x2": self.result_probs,
            "blend_btts": self.btts_prob,
            "market_weight": self.market_weight if self.uses_market else 0.0,
            "base_rates": self.base,
            "expected_goals": list(self.xg) if self.xg else None,
            "top_scores": self.top_scores,
            "pick": self.pick,
            "verdicts": [v.__dict__ for v in self.verdicts],
            "prediction_run_id": self.run_id,
            "generated_utc": self.generated_utc,
            "model_version": self.model_version,
        }


def _lean(
    prob: float, base: float | None, *, yes: str, no: str, skilled: bool = True
) -> dict[str, object]:
    """Describe a binary forecast by its likelier side, against the league rate.

    ``prob`` and ``base`` are always the probability of the ``yes`` event; the
    displayed probability is the one of the side named.
    """
    is_yes = prob >= 0.5
    diff = None if base is None else prob - base
    if diff is None:
        note = "sin promedio de liga"
    elif not skilled:
        note = "en esta liga el modelo no ha superado al promedio en este mercado"
    elif abs(diff) < 0.04:
        note = "en línea con la liga"
    elif abs(diff) < 0.08:
        note = "algo más que en la liga" if diff > 0 else "algo menos que en la liga"
    else:
        note = "mucho más que en la liga" if diff > 0 else "mucho menos que en la liga"
    return {
        "side": yes if is_yes else no,
        "side_prob": prob if is_yes else 1.0 - prob,
        "yes": yes,
        "prob": prob,
        "base": base,
        "diff": diff,
        "note": note,
        "highlight": skilled and diff is not None and abs(diff) >= 0.04,
        "skilled": skilled,
    }


# ------------------------------------------------------------------- loading


def league_scope(league: str | None) -> tuple[str, ...]:
    if league and league in GUI_LEAGUE_ORDER:
        return (league,)
    return tuple(GUI_LEAGUE_ORDER)


def load_matches(
    conn: sqlite3.Connection,
    *,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
    league: str | None = None,
    fixture_id: str | None = None,
    finished: bool | None = None,
    now_utc: datetime | None = None,
    base_rates: BaseRates | None = None,
) -> list[MatchView]:
    """Fixtures with their latest pre-kickoff forecast, market and base rates."""
    scope = league_scope(league)
    now = now_utc or datetime.now(UTC)
    tz = display_tz()
    clauses = ["f.home_id IS NOT NULL", "f.away_id IS NOT NULL", "f.status != 'cancelled'"]
    params: list[object] = []
    if fixture_id is not None:
        clauses.append("f.fixture_id = ?")
        params.append(fixture_id)
    else:
        clauses.append(f"f.league_code IN ({','.join('?' for _ in scope)})")
        params.extend(scope)
    if finished is True:
        clauses.append("f.home_score IS NOT NULL AND f.away_score IS NOT NULL")
    elif finished is False:
        clauses.append("f.home_score IS NULL")
    rows = conn.execute(
        f"""
        SELECT f.fixture_id, f.kickoff_utc, f.kickoff_local, f.league_code,
               f.home_id, f.away_id, f.home_score, f.away_score,
               ht.canonical AS home, at.canonical AS away
        FROM fixture f
        JOIN team ht ON ht.team_id = f.home_id
        JOIN team at ON at.team_id = f.away_id
        WHERE {" AND ".join(clauses)}
        """,
        params,
    ).fetchall()

    matches: list[MatchView] = []
    for row in rows:
        kickoff = parse_kickoff_utc(
            kickoff_utc=row["kickoff_utc"], kickoff_local=row["kickoff_local"]
        )
        if kickoff is None:
            continue
        if start_utc is not None and kickoff < start_utc:
            continue
        if end_utc is not None and kickoff >= end_utc:
            continue
        if row["home_score"] is not None and row["away_score"] is not None:
            status = "finished"
        elif kickoff <= now:
            status = "live" if now - kickoff < timedelta(hours=2, minutes=30) else "pending_result"
        else:
            status = "upcoming"
        matches.append(
            MatchView(
                fixture_id=str(row["fixture_id"]),
                league_code=str(row["league_code"]),
                kickoff_utc=kickoff,
                kickoff_local=kickoff.astimezone(tz),
                home_id=int(row["home_id"]),
                away_id=int(row["away_id"]),
                home=str(row["home"]),
                away=str(row["away"]),
                home_score=row["home_score"],
                away_score=row["away_score"],
                status=status,
            )
        )
    matches.sort(key=lambda m: (m.kickoff_utc, m.league_code, m.home))
    if not matches:
        return matches
    _attach_predictions(conn, matches)
    _attach_market(conn, matches)
    _attach_snapshots(conn, matches)
    rates = base_rates or BaseRates(conn, tuple(sorted({m.league_code for m in matches})))
    for match in matches:
        match.base = rates.at(match.league_code, match.kickoff_utc.date())
    return matches


def _chunks(items: list[str], size: int = 400):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _attach_predictions(conn: sqlite3.Connection, matches: list[MatchView]) -> None:
    by_id = {m.fixture_id: m for m in matches}
    runs: dict[tuple[str, int], list[sqlite3.Row]] = defaultdict(list)
    for chunk in _chunks(list(by_id)):
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"""
            SELECT run_id, fixture_id, market, selection, prob, generated_utc,
                   artifact_id, model_version
            FROM prediction
            WHERE fixture_id IN ({placeholders})
              AND market IN ('1X2', 'OU1.5', 'OU2.5', 'OU3.5', 'BTTS')
            """,
            chunk,
        ):
            runs[(str(row["fixture_id"]), int(row["run_id"] or 0))].append(row)
    best: dict[str, int] = {}
    for (fixture_id, run_id), run_rows in runs.items():
        generated = parse_datetime_utc(run_rows[0]["generated_utc"])
        # Only forecasts published before kickoff count.
        if generated is None or generated >= by_id[fixture_id].kickoff_utc:
            continue
        if run_id > best.get(fixture_id, -1):
            best[fixture_id] = run_id
    needed = [("1X2", k) for k in RESULT_KEYS] + [("OU2.5", "over"), ("BTTS", "yes")]
    for fixture_id, run_id in list(best.items()):
        values = {
            (str(row["market"]), str(row["selection"])): float(row["prob"])
            for row in runs[(fixture_id, run_id)]
        }
        if any(key not in values for key in needed):
            del best[fixture_id]
            continue
        match = by_id[fixture_id]
        match.model = {
            "home": values[("1X2", "home")],
            "draw": values[("1X2", "draw")],
            "away": values[("1X2", "away")],
            "over25": values[("OU2.5", "over")],
            "btts": values[("BTTS", "yes")],
        }
        for line, key in (("OU1.5", "over15"), ("OU3.5", "over35")):
            if (line, "over") in values:
                match.model[key] = values[(line, "over")]
        first = runs[(fixture_id, run_id)][0]
        match.run_id = run_id
        match.generated_utc = str(first["generated_utc"])
        match.artifact_id = first["artifact_id"]
        match.model_version = first["model_version"]
    if not best or not table_exists(conn, "prediction_score_grid"):
        return
    pairs = list(best.items())
    for index in range(0, len(pairs), 300):
        chunk = pairs[index : index + 300]
        clause = " OR ".join("(fixture_id = ? AND run_id = ?)" for _ in chunk)
        flat = [value for pair in chunk for value in pair]
        for row in conn.execute(
            "SELECT fixture_id, xg_home, xg_away, grid_json "
            f"FROM prediction_score_grid WHERE {clause}",
            flat,
        ):
            match = by_id[str(row["fixture_id"])]
            match.xg = (float(row["xg_home"]), float(row["xg_away"]))
            match.grid = json.loads(str(row["grid_json"]))


def _attach_market(conn: sqlite3.Connection, matches: list[MatchView]) -> None:
    by_id = {m.fixture_id: m for m in matches}
    sets: dict[tuple[str, str, str, str], dict[str, float]] = defaultdict(dict)
    for chunk in _chunks(list(by_id)):
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"""
            SELECT fixture_id, bookmaker, market, selection, decimal_odds, snapshot_at
            FROM odds_snapshots
            WHERE fixture_id IN ({placeholders})
              AND market IN ('1X2', 'BTTS')
            """,
            chunk,
        ):
            key = (
                str(row["fixture_id"]),
                str(row["market"]),
                str(row["bookmaker"]),
                str(row["snapshot_at"]),
            )
            sets[key][str(row["selection"])] = float(row["decimal_odds"])
    latest: dict[tuple[str, str], tuple[datetime, str, dict[str, float]]] = {}
    for (fixture_id, market, bookmaker, snapshot_at), prices in sets.items():
        taken = parse_datetime_utc(snapshot_at)
        if taken is None or taken >= by_id[fixture_id].kickoff_utc:
            continue
        required = RESULT_KEYS if market == "1X2" else ("yes", "no")
        if any(sel not in prices for sel in required):
            continue
        current = latest.get((fixture_id, market))
        if current is None or taken > current[0]:
            latest[(fixture_id, market)] = (taken, bookmaker, {s: prices[s] for s in required})
    for (fixture_id, market), (taken, bookmaker, prices) in latest.items():
        fair = devig(prices)
        if fair is None:
            continue
        match = by_id[fixture_id]
        match.market = match.market or {}
        if market == "1X2":
            match.market.update(fair)
        else:
            match.market["btts"] = fair["yes"]
        match.market_meta = {
            "bookmaker": bookmaker,
            "snapshot_utc": taken.isoformat(timespec="minutes"),
        }


def _attach_snapshots(conn: sqlite3.Connection, matches: list[MatchView]) -> None:
    by_id = {m.fixture_id: m for m in matches}
    for chunk in _chunks(list(by_id)):
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"""
            SELECT fixture_id, market, selection, prob, as_of_utc, settled, correct
            FROM forecast_snapshot
            WHERE fixture_id IN ({placeholders})
            ORDER BY snapshot_id
            """,
            chunk,
        ):
            by_id[str(row["fixture_id"])].snapshots.append(dict(row))


# ------------------------------------------------------------ team context


def team_form(
    conn: sqlite3.Connection, team_id: int, league: str, before: date, limit: int = 5
) -> list[FormEntry]:
    rows = conn.execute(
        """
        SELECT substr(m.date, 1, 10) AS day, m.home_id, m.home_score, m.away_score,
               ht.canonical AS home, at.canonical AS away
        FROM match_history m
        JOIN team ht ON ht.team_id = m.home_id
        JOIN team at ON at.team_id = m.away_id
        WHERE m.tournament = ? AND (m.home_id = ? OR m.away_id = ?) AND m.date < ?
        ORDER BY m.date DESC
        LIMIT ?
        """,
        (league, team_id, team_id, before.isoformat(), limit),
    ).fetchall()
    out = []
    for row in rows:
        is_home = int(row["home_id"]) == team_id
        own, other = (
            (row["home_score"], row["away_score"])
            if is_home
            else (row["away_score"], row["home_score"])
        )
        out.append(
            FormEntry(
                result="G" if own > other else "E" if own == other else "P",
                day=str(row["day"]),
                opponent=str(row["away"] if is_home else row["home"]),
                score=f"{row['home_score']}-{row['away_score']}",
                home=is_home,
            )
        )
    return out


def head_to_head(
    conn: sqlite3.Connection, home_id: int, away_id: int, before: date, limit: int = 5
) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT substr(m.date, 1, 10) AS day, ht.canonical AS home, at.canonical AS away,
               m.home_score, m.away_score
        FROM match_history m
        JOIN team ht ON ht.team_id = m.home_id
        JOIN team at ON at.team_id = m.away_id
        WHERE ((m.home_id = ? AND m.away_id = ?) OR (m.home_id = ? AND m.away_id = ?))
          AND m.date < ?
        ORDER BY m.date DESC
        LIMIT ?
        """,
        (home_id, away_id, away_id, home_id, before.isoformat(), limit),
    ).fetchall()
    return [dict(row) for row in rows]


def elo_history(conn: sqlite3.Connection, team_id: int, limit: int = 20) -> list[float]:
    rows = conn.execute(
        """
        SELECT rating_post
        FROM team_rating_history
        WHERE team_id = ? AND rating_system = 'elo'
        ORDER BY as_of DESC, source_match_id DESC
        LIMIT ?
        """,
        (team_id, limit),
    ).fetchall()
    return [float(row[0]) for row in reversed(rows)]


def model_provenance(conn: sqlite3.Connection, artifact_id: str | None) -> dict[str, object] | None:
    if not artifact_id:
        return None
    row = conn.execute(
        """
        SELECT model_version, trained_until, training_rows, code_git_sha, created_utc
        FROM model_artifact WHERE artifact_id = ?
        """,
        (artifact_id,),
    ).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------- date index


def match_days(
    conn: sqlite3.Connection, *, league: str | None, start_utc: datetime, end_utc: datetime
) -> list[tuple[date, int]]:
    """Local dates that have fixtures between two instants, with their counts."""
    scope = league_scope(league)
    rows = conn.execute(
        f"""
        SELECT kickoff_utc, kickoff_local FROM fixture
        WHERE league_code IN ({",".join("?" for _ in scope)})
          AND home_id IS NOT NULL AND away_id IS NOT NULL AND status != 'cancelled'
        """,
        scope,
    ).fetchall()
    tz = display_tz()
    counts: dict[date, int] = defaultdict(int)
    for row in rows:
        kickoff = parse_kickoff_utc(
            kickoff_utc=row["kickoff_utc"], kickoff_local=row["kickoff_local"]
        )
        if kickoff is not None and start_utc <= kickoff < end_utc:
            counts[kickoff.astimezone(tz).date()] += 1
    return sorted(counts.items())


def local_day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=display_tz())
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


# -------------------------------------------------------------- performance


@dataclass
class MarketScore:
    market: str
    n: int = 0
    hits: int = 0
    model_ll: float = 0.0
    base_ll: float = 0.0
    n_market: int = 0
    model_ll_on_market: float = 0.0
    market_ll: float = 0.0
    blend_ll: float = 0.0

    @property
    def accuracy(self) -> float | None:
        return self.hits / self.n if self.n else None

    @property
    def model_logloss(self) -> float | None:
        return self.model_ll / self.n if self.n else None

    @property
    def base_logloss(self) -> float | None:
        return self.base_ll / self.n if self.n else None

    @property
    def skill(self) -> float | None:
        """Log-loss gained over the league base rate per match (positive is better)."""
        return (self.base_ll - self.model_ll) / self.n if self.n else None

    @property
    def market_logloss(self) -> float | None:
        return self.market_ll / self.n_market if self.n_market else None

    @property
    def model_logloss_on_market(self) -> float | None:
        return self.model_ll_on_market / self.n_market if self.n_market else None

    @property
    def blend_logloss(self) -> float | None:
        return self.blend_ll / self.n_market if self.n_market else None

    def as_json(self) -> dict[str, object]:
        return {
            "n": self.n,
            "accuracy": self.accuracy,
            "model_logloss": self.model_logloss,
            "base_logloss": self.base_logloss,
            "skill_vs_base": self.skill,
            "n_with_market": self.n_market,
            "market_logloss": self.market_logloss,
            "model_logloss_same_matches": self.model_logloss_on_market,
            "blend_logloss": self.blend_logloss,
        }


PERFORMANCE_MARKETS = ("1X2", "OU2.5", "BTTS")


@dataclass
class PerformanceData:
    matches: int
    first_day: date | None
    last_day: date | None
    by_market: dict[str, MarketScore]
    by_league: dict[str, dict[str, MarketScore]]
    calibration: dict[str, list[dict[str, float]]]
    monthly: list[dict[str, object]]

    def as_json(self) -> dict[str, object]:
        return {
            "matches": self.matches,
            "from": self.first_day.isoformat() if self.first_day else None,
            "to": self.last_day.isoformat() if self.last_day else None,
            "market_weight_by_league": {
                code: model_config(code).market_weight for code in GUI_LEAGUE_ORDER
            },
            "by_market": {k: v.as_json() for k, v in self.by_market.items()},
            "by_league": {
                league: {k: v.as_json() for k, v in markets.items()}
                for league, markets in self.by_league.items()
            },
            "calibration": self.calibration,
            "monthly": self.monthly,
        }


def _fresh_scores() -> dict[str, MarketScore]:
    return {market: MarketScore(market) for market in PERFORMANCE_MARKETS}


def performance(matches: list[MatchView]) -> PerformanceData:
    scored = [m for m in matches if m.finished and m.model is not None and m.base is not None]
    by_market = _fresh_scores()
    by_league: dict[str, dict[str, MarketScore]] = defaultdict(_fresh_scores)
    monthly: dict[str, dict[str, MarketScore]] = defaultdict(_fresh_scores)
    bins = {market: [[0.0, 0.0, 0] for _ in range(10)] for market in PERFORMANCE_MARKETS}
    for match in scored:
        model, base, outcome = match.model, match.base, match.outcome
        both = int(match.home_score > 0 and match.away_score > 0)
        over = int(match.home_score + match.away_score > 2.5)
        entries = {
            "1X2": (
                -math.log(max(model[outcome], EPS)),
                -math.log(max(base[outcome], EPS)),
                max(RESULT_KEYS, key=lambda k: model[k]) == outcome,
            ),
            "OU2.5": (
                binary_logloss(model["over25"], over),
                binary_logloss(base["over25"], over),
                (model["over25"] >= 0.5) == bool(over),
            ),
            "BTTS": (
                binary_logloss(model["btts"], both),
                binary_logloss(base["btts"], both),
                (model["btts"] >= 0.5) == bool(both),
            ),
        }
        with_market: dict[str, tuple[float, float]] = {}
        if match.uses_market:
            blend = match.result_probs
            with_market["1X2"] = (
                -math.log(max(match.market[outcome], EPS)),
                -math.log(max(blend[outcome], EPS)),
            )
        if match.market and "btts" in match.market:
            with_market["BTTS"] = (
                binary_logloss(match.market["btts"], both),
                binary_logloss(match.btts_prob, both),
            )
        month = match.kickoff_local.strftime("%Y-%m")
        for market, (m_ll, b_ll, hit) in entries.items():
            for bucket in (
                by_market[market],
                by_league[match.league_code][market],
                monthly[month][market],
            ):
                bucket.n += 1
                bucket.hits += int(hit)
                bucket.model_ll += m_ll
                bucket.base_ll += b_ll
                if market in with_market:
                    bucket.n_market += 1
                    bucket.model_ll_on_market += m_ll
                    bucket.market_ll += with_market[market][0]
                    bucket.blend_ll += with_market[market][1]
        for key in RESULT_KEYS:
            _bin(bins["1X2"], model[key], int(key == outcome))
        _bin(bins["OU2.5"], model["over25"], over)
        _bin(bins["BTTS"], model["btts"], both)

    calibration = {
        market: [{"mean_prob": s / c, "observed": h / c, "n": c} for s, h, c in cells if c > 0]
        for market, cells in bins.items()
    }
    days = [m.kickoff_local.date() for m in scored]
    monthly_rows = [
        {
            "month": month,
            **{
                market: {"n": s.n, "skill": s.skill, "model_logloss": s.model_logloss}
                for market, s in markets.items()
            },
        }
        for month, markets in sorted(monthly.items())
    ]
    return PerformanceData(
        matches=len(scored),
        first_day=min(days) if days else None,
        last_day=max(days) if days else None,
        by_market=by_market,
        by_league=dict(by_league),
        calibration=calibration,
        monthly=monthly_rows,
    )


def _bin(cells: list[list[float]], prob: float, happened: int) -> None:
    index = min(int(prob * 10), 9)
    cells[index][0] += prob
    cells[index][1] += happened
    cells[index][2] += 1


# ------------------------------------------------------------ model gate


def latest_model_compare(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Per-league rows of the most recent ``model-compare`` run, if any."""
    row = conn.execute(
        """
        SELECT MAX(run_id) FROM data_quality WHERE check_name = 'model_compare'
        """
    ).fetchone()
    if not row or row[0] is None:
        return []
    out = []
    for record in conn.execute(
        """
        SELECT metric_name, details_json FROM data_quality
        WHERE check_name = 'model_compare' AND run_id = ?
        ORDER BY metric_name
        """,
        (row[0],),
    ):
        try:
            details = json.loads(str(record["details_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        details["league"] = str(record["metric_name"])
        details["league_name"] = league_label(details["league"])
        out.append(details)
    order = {code: index for index, code in enumerate(GUI_LEAGUE_ORDER)}
    return sorted(out, key=lambda item: order.get(item["league"], 99))
