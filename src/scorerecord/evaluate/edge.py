"""De-vig odds and compute model edge without generating recommendations."""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

from scorerecord.models.manifest import MODEL_VERSION
from scorerecord.runs import ingest_run, record_quality

HIGH_EDGE_ALERT_THRESHOLD = 0.25
ODDS_OUTLIER_RATIO = 1.20
STALE_ODDS_MINUTES = 180.0


@dataclass(frozen=True)
class EdgeSummary:
    rows_analyzed: int
    rows_inserted: int
    picks_gt_0: int
    picks_gt_3pct: int
    picks_gt_5pct: int
    max_edge_ev: float | None
    dry_run: bool

    def as_row(self) -> str:
        max_edge = "na" if self.max_edge_ev is None else f"{self.max_edge_ev:.4f}"
        return (
            f"rows_analyzed={self.rows_analyzed} "
            f"rows_inserted={self.rows_inserted} "
            f"picks_gt_0={self.picks_gt_0} "
            f"picks_gt_3pct={self.picks_gt_3pct} "
            f"picks_gt_5pct={self.picks_gt_5pct} "
            f"max_edge_ev={max_edge} "
            f"dry_run={self.dry_run}"
        )


@dataclass(frozen=True)
class EdgeRow:
    fixture_id: str
    prediction_run_id: int | None
    model_version: str
    market: str
    selection: str
    prob_model: float
    odds_decimal: float
    raw_implied_prob: float
    prob_market_devig: float
    market_overround: float
    edge_ev: float
    edge_prob: float
    elo_diff: float | None
    close_match: int | None
    odds_books: int
    median_odds: float
    odds_ratio_to_median: float
    odds_age_minutes: float | None
    odds_quality_flags: str
    high_edge_reason: str
    recommendation_score: float
    bookmaker: str
    captured_utc: str
    prediction_generated_utc: str | None


def devig_1x2(odds_home: float, odds_draw: float, odds_away: float) -> list[float]:
    return _normalize_implied([odds_home, odds_draw, odds_away])


def devig_binary(odds_a: float, odds_b: float) -> list[float]:
    return _normalize_implied([odds_a, odds_b])


def compute_edge(prob_model: float, prob_market_devig: float, odds_decimal: float) -> tuple[float, float]:
    return prob_model * odds_decimal - 1.0, prob_model - prob_market_devig


def compute_edges(
    conn: sqlite3.Connection,
    *,
    model_version: str = MODEL_VERSION,
    bookmaker: str | None = None,
    min_odds: float = 1.10,
    max_odds: float = 15.0,
    dry_run: bool = False,
    cwd: Path | None = None,
) -> EdgeSummary:
    rows = _edge_rows(
        conn,
        model_version=model_version,
        bookmaker=bookmaker,
        min_odds=min_odds,
        max_odds=max_odds,
    )
    summary = _summary(rows, inserted=0, dry_run=dry_run)
    if dry_run:
        return summary

    generated_utc = datetime.now(UTC).isoformat(timespec="seconds")
    with ingest_run(conn, "compute-edge", source="odds+prediction", cwd=cwd) as run_id:
        inserted = 0
        for row in rows:
            cursor = conn.execute(
                """
                INSERT OR REPLACE INTO edge_analysis (
                  fixture_id, run_id, prediction_run_id, model_version, market,
                  selection, prob_model, odds_decimal, raw_implied_prob,
                  prob_market_devig, market_overround, edge_ev, edge_prob,
                  elo_diff, close_match, odds_books, median_odds,
                  odds_ratio_to_median, odds_age_minutes, odds_quality_flags,
                  high_edge_reason, recommendation_score, bookmaker, captured_utc,
                  prediction_generated_utc, generated_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.fixture_id,
                    run_id,
                    row.prediction_run_id,
                    row.model_version,
                    row.market,
                    row.selection,
                    row.prob_model,
                    row.odds_decimal,
                    row.raw_implied_prob,
                    row.prob_market_devig,
                    row.market_overround,
                    row.edge_ev,
                    row.edge_prob,
                    row.elo_diff,
                    row.close_match,
                    row.odds_books,
                    row.median_odds,
                    row.odds_ratio_to_median,
                    row.odds_age_minutes,
                    row.odds_quality_flags,
                    row.high_edge_reason,
                    row.recommendation_score,
                    row.bookmaker,
                    row.captured_utc,
                    row.prediction_generated_utc,
                    generated_utc,
                ),
            )
            inserted += cursor.rowcount
        summary = _summary(rows, inserted=inserted, dry_run=False)
        _record_edge_quality(conn, run_id, rows, summary)
        conn.commit()
    return summary


def _edge_rows(
    conn: sqlite3.Connection,
    *,
    model_version: str,
    bookmaker: str | None,
    min_odds: float,
    max_odds: float,
) -> list[EdgeRow]:
    odds_rows = _latest_odds(conn, bookmaker=bookmaker)
    prediction_rows = _latest_predictions(conn, model_version=model_version)
    fixture_context = _fixture_context(conn)
    market_stats = _selection_market_stats(odds_rows)
    as_of = datetime.now(UTC)
    rows: list[EdgeRow] = []
    for key, selections in odds_rows.items():
        fixture_id, book, market = key
        prediction_key = (fixture_id, market)
        predictions = prediction_rows.get(prediction_key)
        if not predictions:
            continue
        devig_probs = _devig_market(selections)
        if not devig_probs:
            continue
        overround = _market_overround(selections)
        for selection, odds_row in selections.items():
            pred = predictions.get(selection)
            market_prob = devig_probs.get(selection)
            if pred is None or market_prob is None:
                continue
            odds_decimal = float(odds_row["decimal_odds"])
            if odds_decimal < min_odds or odds_decimal > max_odds:
                continue
            edge_ev, edge_prob = compute_edge(float(pred["prob"]), market_prob, odds_decimal)
            elo_diff, close_match = fixture_context.get(str(fixture_id), (None, None))
            raw_implied_prob = 1.0 / odds_decimal
            stats = market_stats.get(
                (str(fixture_id), str(market), str(selection)),
                {
                    "odds_books": 1,
                    "median_odds": odds_decimal,
                },
            )
            median_odds = float(stats["median_odds"])
            odds_ratio_to_median = odds_decimal / median_odds if median_odds > 0 else 1.0
            odds_age_minutes = _odds_age_minutes(str(odds_row["captured_utc"]), as_of)
            odds_quality_flags = _odds_quality_flags(
                odds_books=int(stats["odds_books"]),
                odds_ratio_to_median=odds_ratio_to_median,
                odds_age_minutes=odds_age_minutes,
            )
            high_edge_reason = _high_edge_reason(
                market=str(market),
                selection=str(selection),
                odds_decimal=odds_decimal,
                prob_model=float(pred["prob"]),
                edge_ev=edge_ev,
                edge_prob=edge_prob,
                odds_quality_flags=odds_quality_flags,
            )
            recommendation_score = _recommendation_score(
                edge_ev=edge_ev,
                odds_decimal=odds_decimal,
                market=str(market),
                selection=str(selection),
                odds_quality_flags=odds_quality_flags,
                close_match=close_match,
            )
            rows.append(
                EdgeRow(
                    fixture_id=str(fixture_id),
                    prediction_run_id=pred["run_id"],
                    model_version=str(pred["model_version"]),
                    market=str(market),
                    selection=str(selection),
                    prob_model=float(pred["prob"]),
                    odds_decimal=odds_decimal,
                    raw_implied_prob=raw_implied_prob,
                    prob_market_devig=market_prob,
                    market_overround=overround,
                    edge_ev=edge_ev,
                    edge_prob=edge_prob,
                    elo_diff=elo_diff,
                    close_match=close_match,
                    odds_books=int(stats["odds_books"]),
                    median_odds=median_odds,
                    odds_ratio_to_median=odds_ratio_to_median,
                    odds_age_minutes=odds_age_minutes,
                    odds_quality_flags=odds_quality_flags,
                    high_edge_reason=high_edge_reason,
                    recommendation_score=recommendation_score,
                    bookmaker=str(book),
                    captured_utc=str(odds_row["captured_utc"]),
                    prediction_generated_utc=pred["generated_utc"],
                )
            )
    rows.sort(key=lambda row: (row.fixture_id, row.market, row.selection, row.bookmaker))
    return rows


def _latest_odds(
    conn: sqlite3.Connection,
    *,
    bookmaker: str | None,
) -> dict[tuple[str, str, str], dict[str, sqlite3.Row]]:
    filter_sql = ""
    params: tuple[object, ...] = ()
    if bookmaker:
        filter_sql = "AND o.bookmaker = ?"
        params = (bookmaker,)
    rows = conn.execute(
        f"""
        WITH complete AS (
          SELECT
            o.fixture_id,
            o.bookmaker,
            o.market,
            o.captured_utc,
            COUNT(DISTINCT selection) AS selection_count,
            SUM(CASE WHEN selection = 'home' THEN 1 ELSE 0 END) AS has_home,
            SUM(CASE WHEN selection = 'draw' THEN 1 ELSE 0 END) AS has_draw,
            SUM(CASE WHEN selection = 'away' THEN 1 ELSE 0 END) AS has_away,
            SUM(CASE WHEN selection = 'over' THEN 1 ELSE 0 END) AS has_over,
            SUM(CASE WHEN selection = 'under' THEN 1 ELSE 0 END) AS has_under,
            SUM(CASE WHEN selection = 'yes' THEN 1 ELSE 0 END) AS has_yes,
            SUM(CASE WHEN selection = 'no' THEN 1 ELSE 0 END) AS has_no
          FROM odds o
          JOIN fixture f ON f.fixture_id = o.fixture_id
          WHERE o.is_closing = 0
            AND (
              f.kickoff_utc IS NULL
              OR datetime(f.kickoff_utc) > datetime('now')
            )
          GROUP BY o.fixture_id, o.bookmaker, o.market, o.captured_utc
          HAVING
            (
              market = '1X2'
              AND selection_count = 3
              AND has_home > 0
              AND has_draw > 0
              AND has_away > 0
            )
            OR (
              market LIKE 'OU%'
              AND selection_count = 2
              AND has_over > 0
              AND has_under > 0
            )
            OR (
              market = 'BTTS'
              AND selection_count = 2
              AND has_yes > 0
              AND has_no > 0
            )
        ),
        latest AS (
          SELECT fixture_id, bookmaker, market, MAX(captured_utc) AS captured_utc
          FROM complete
          GROUP BY fixture_id, bookmaker, market
        )
        SELECT o.*
        FROM odds o
        JOIN latest l
          ON l.fixture_id = o.fixture_id
         AND l.bookmaker = o.bookmaker
         AND l.market = o.market
         AND l.captured_utc = o.captured_utc
        WHERE o.is_closing = 0
          {filter_sql}
        ORDER BY o.fixture_id, o.bookmaker, o.market, o.selection
        """,
        params,
    ).fetchall()
    grouped: dict[tuple[str, str, str], dict[str, sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(
            (str(row["fixture_id"]), str(row["bookmaker"]), str(row["market"])),
            {},
        )[str(row["selection"])] = row
    return grouped


def _selection_market_stats(
    odds_rows: dict[tuple[str, str, str], dict[str, sqlite3.Row]],
) -> dict[tuple[str, str, str], dict[str, float | int]]:
    book_values: dict[tuple[str, str, str], dict[str, tuple[str, float]]] = {}
    for fixture_id, bookmaker, market in odds_rows:
        book_family = _bookmaker_family(bookmaker)
        for selection, row in odds_rows[(fixture_id, bookmaker, market)].items():
            key = (fixture_id, market, selection)
            captured_utc = str(row["captured_utc"])
            price = float(row["decimal_odds"])
            incumbent = book_values.setdefault(key, {}).get(book_family)
            if incumbent is None or captured_utc > incumbent[0]:
                book_values[key][book_family] = (captured_utc, price)
    return {
        key: {"odds_books": len(book_odds), "median_odds": float(median(odds))}
        for key, book_odds in book_values.items()
        for odds in ([price for _captured, price in book_odds.values()],)
        if odds
    }


def _bookmaker_family(bookmaker: str) -> str:
    return bookmaker.split("(", 1)[0].strip().casefold()


def _fixture_context(conn: sqlite3.Connection) -> dict[str, tuple[float | None, int | None]]:
    rows = conn.execute(
        """
        WITH latest_rating AS (
          SELECT team_id, MAX(as_of) AS as_of
          FROM team_rating
          WHERE rating_system = 'elo'
          GROUP BY team_id
        ),
        rating AS (
          SELECT tr.team_id, tr.rating
          FROM team_rating tr
          JOIN latest_rating lr
            ON lr.team_id = tr.team_id
           AND lr.as_of = tr.as_of
          WHERE tr.rating_system = 'elo'
        )
        SELECT
          f.fixture_id,
          hr.rating AS home_elo,
          ar.rating AS away_elo
        FROM fixture f
        LEFT JOIN rating hr ON hr.team_id = f.home_id
        LEFT JOIN rating ar ON ar.team_id = f.away_id
        """
    ).fetchall()
    context: dict[str, tuple[float | None, int | None]] = {}
    for row in rows:
        home_elo = row["home_elo"]
        away_elo = row["away_elo"]
        if home_elo is None or away_elo is None:
            context[str(row["fixture_id"])] = (None, None)
            continue
        elo_diff = float(home_elo) - float(away_elo)
        context[str(row["fixture_id"])] = (elo_diff, int(abs(elo_diff) < 75.0))
    return context


def _latest_predictions(
    conn: sqlite3.Connection,
    *,
    model_version: str,
) -> dict[tuple[str, str], dict[str, sqlite3.Row]]:
    rows = conn.execute(
        """
        WITH latest_run AS (
          SELECT run_id
          FROM ingest_run
          WHERE job_name = 'predict-fixtures'
            AND status = 'succeeded'
          ORDER BY run_id DESC
          LIMIT 1
        )
        SELECT p.*
        FROM prediction p
        JOIN latest_run lr ON lr.run_id = p.run_id
        JOIN fixture f ON f.fixture_id = p.fixture_id
        WHERE p.model_version = ?
          AND (
            f.kickoff_utc IS NULL
            OR datetime(f.kickoff_utc) > datetime('now')
          )
        ORDER BY p.fixture_id, p.market, p.selection
        """,
        (model_version,),
    ).fetchall()
    grouped: dict[tuple[str, str], dict[str, sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault((str(row["fixture_id"]), str(row["market"])), {})[
            str(row["selection"])
        ] = row
    return grouped


def _devig_market(selections: dict[str, sqlite3.Row]) -> dict[str, float] | None:
    keys = set(selections)
    if {"home", "draw", "away"}.issubset(keys):
        probs = devig_1x2(
            float(selections["home"]["decimal_odds"]),
            float(selections["draw"]["decimal_odds"]),
            float(selections["away"]["decimal_odds"]),
        )
        return dict(zip(("home", "draw", "away"), probs))
    if {"over", "under"}.issubset(keys):
        probs = devig_binary(
            float(selections["over"]["decimal_odds"]),
            float(selections["under"]["decimal_odds"]),
        )
        return dict(zip(("over", "under"), probs))
    if {"yes", "no"}.issubset(keys):
        probs = devig_binary(
            float(selections["yes"]["decimal_odds"]),
            float(selections["no"]["decimal_odds"]),
        )
        return dict(zip(("yes", "no"), probs))
    return None


def _market_overround(selections: dict[str, sqlite3.Row]) -> float:
    return sum(1.0 / float(row["decimal_odds"]) for row in selections.values())


def _normalize_implied(odds: list[float]) -> list[float]:
    implied = [1.0 / odd for odd in odds]
    overround = sum(implied)
    if overround <= 0 or not math.isfinite(overround):
        raise ValueError("Cannot de-vig odds with non-positive overround")
    return [prob / overround for prob in implied]


def _odds_age_minutes(captured_utc: str, as_of: datetime) -> float | None:
    captured = _parse_datetime(captured_utc)
    if captured is None:
        return None
    return max((as_of - captured).total_seconds() / 60.0, 0.0)


def _parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _odds_quality_flags(
    *,
    odds_books: int,
    odds_ratio_to_median: float,
    odds_age_minutes: float | None,
) -> str:
    flags = []
    if odds_age_minutes is not None and odds_age_minutes > STALE_ODDS_MINUTES:
        flags.append("stale_odds")
    if odds_books < 2:
        flags.append("low_market_depth")
    if odds_books >= 3 and odds_ratio_to_median > ODDS_OUTLIER_RATIO:
        flags.append("bookmaker_outlier")
    return ",".join(flags)


def _high_edge_reason(
    *,
    market: str,
    selection: str,
    odds_decimal: float,
    prob_model: float,
    edge_ev: float,
    edge_prob: float,
    odds_quality_flags: str,
) -> str:
    if edge_ev <= HIGH_EDGE_ALERT_THRESHOLD:
        return ""
    reasons = []
    if odds_decimal >= 5.0:
        reasons.append("long_odds_tail")
    if abs(edge_prob) >= 0.08:
        reasons.append("model_market_disagreement")
    if prob_model <= 0.20 or odds_decimal >= 5.0:
        reasons.append("calibration_tail_risk")
    if market == "1X2" and selection == "draw":
        reasons.append("draw_tail_risk")
    for flag in odds_quality_flags.split(","):
        if flag:
            reasons.append(flag)
    if not reasons:
        reasons.append("unknown")
    return ",".join(dict.fromkeys(reasons))


def _recommendation_score(
    *,
    edge_ev: float,
    odds_decimal: float,
    market: str,
    selection: str,
    odds_quality_flags: str,
    close_match: int | None,
) -> float:
    if edge_ev <= 0:
        return 0.0
    score = edge_ev
    if odds_decimal >= 8.0:
        score *= 0.45
    elif odds_decimal >= 5.0:
        score *= 0.60
    elif odds_decimal >= 3.0:
        score *= 0.85
    if market == "1X2" and selection == "draw":
        score *= 0.75
    if edge_ev > 0.20:
        score *= 0.70
    if odds_quality_flags:
        score *= 0.50
    if close_match == 1:
        score *= 1.05
    return score


def _summary(rows: list[EdgeRow], *, inserted: int, dry_run: bool) -> EdgeSummary:
    return EdgeSummary(
        rows_analyzed=len(rows),
        rows_inserted=inserted,
        picks_gt_0=sum(1 for row in rows if row.edge_ev > 0),
        picks_gt_3pct=sum(1 for row in rows if row.edge_ev > 0.03),
        picks_gt_5pct=sum(1 for row in rows if row.edge_ev > 0.05),
        max_edge_ev=max((row.edge_ev for row in rows), default=None),
        dry_run=dry_run,
    )


def _record_edge_quality(
    conn: sqlite3.Connection,
    run_id: int,
    rows: list[EdgeRow],
    summary: EdgeSummary,
) -> None:
    by_market: dict[str, list[float]] = {}
    close_edges = [row.edge_ev for row in rows if row.close_match == 1]
    high_edges = sorted(
        (row for row in rows if row.edge_ev > HIGH_EDGE_ALERT_THRESHOLD),
        key=lambda row: row.edge_ev,
        reverse=True,
    )
    for row in rows:
        by_market.setdefault(row.market, []).append(row.edge_ev)
    details = {
        "rows_analyzed": summary.rows_analyzed,
        "rows_inserted": summary.rows_inserted,
        "picks_gt_0": summary.picks_gt_0,
        "picks_gt_3pct": summary.picks_gt_3pct,
        "picks_gt_5pct": summary.picks_gt_5pct,
        "mean_edge_by_market": {
            market: sum(values) / len(values) for market, values in by_market.items()
        },
        "max_edge_by_market": {market: max(values) for market, values in by_market.items()},
        "close_match_rows": len(close_edges),
        "mean_close_match_edge": (
            sum(close_edges) / len(close_edges) if close_edges else None
        ),
        "max_close_match_edge": max(close_edges) if close_edges else None,
        "warning": "mean edge above 5% can indicate optimistic de-vig with few bookmakers",
    }
    for metric_name, value in [
        ("edge_picks_total", summary.rows_analyzed),
        ("edge_picks_gt_0", summary.picks_gt_0),
        ("edge_picks_gt_3pct", summary.picks_gt_3pct),
        ("edge_picks_gt_5pct", summary.picks_gt_5pct),
        ("edge_max_ev", summary.max_edge_ev or 0.0),
        ("edge_close_match_rows", len(close_edges)),
        ("edge_max_close_match_ev", max(close_edges) if close_edges else 0.0),
    ]:
        record_quality(
            conn,
            run_id,
            "edge_distribution",
            metric_name,
            float(value),
            json.dumps(details, sort_keys=True),
        )

    sanity_details = {
        "severity": "high" if high_edges else "ok",
        "threshold": HIGH_EDGE_ALERT_THRESHOLD,
        "high_edge_rows": len(high_edges),
        "max_edge_ev": summary.max_edge_ev,
        "top_edges": [
            {
                "fixture_id": row.fixture_id,
                "market": row.market,
                "selection": row.selection,
                "bookmaker": row.bookmaker,
                "prob_model": row.prob_model,
                "odds_decimal": row.odds_decimal,
                "raw_implied_prob": row.raw_implied_prob,
                "prob_market_devig": row.prob_market_devig,
                "market_overround": row.market_overround,
                "model_minus_fair_market_prob": row.edge_prob,
                "edge_ev": row.edge_ev,
                "elo_diff": row.elo_diff,
                "close_match": row.close_match,
                "odds_books": row.odds_books,
                "median_odds": row.median_odds,
                "odds_ratio_to_median": row.odds_ratio_to_median,
                "odds_age_minutes": row.odds_age_minutes,
                "odds_quality_flags": row.odds_quality_flags,
                "high_edge_reason": row.high_edge_reason,
                "recommendation_score": row.recommendation_score,
                "captured_utc": row.captured_utc,
            }
            for row in high_edges[:10]
        ],
        "action": "Review fixture matching, raw odds, and model disagreement before emitting recommendations.",
    }
    for metric_name, value in [
        ("high_edge_alert", 1.0 if high_edges else 0.0),
        ("high_edge_rows", float(len(high_edges))),
    ]:
        record_quality(
            conn,
            run_id,
            "edge_sanity",
            metric_name,
            float(value),
            json.dumps(sanity_details, sort_keys=True),
        )
    for market, values in by_market.items():
        record_quality(
            conn,
            run_id,
            "edge_distribution",
            f"mean_edge_{market}",
            float(sum(values) / len(values)),
            json.dumps(details, sort_keys=True),
        )
        record_quality(
            conn,
            run_id,
            "edge_distribution",
            f"max_edge_{market}",
            float(max(values)),
            json.dumps(details, sort_keys=True),
        )
