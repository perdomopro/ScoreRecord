"""De-vig odds and compute model edge without generating recommendations."""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from scorerecord.models.manifest import MODEL_VERSION
from scorerecord.runs import ingest_run, record_quality


HIGH_EDGE_ALERT_THRESHOLD = 0.25


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
    prob_market_devig: float
    edge_ev: float
    edge_prob: float
    elo_diff: float | None
    close_match: int | None
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

    generated_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with ingest_run(conn, "compute-edge", source="odds+prediction", cwd=cwd) as run_id:
        inserted = 0
        for row in rows:
            cursor = conn.execute(
                """
                INSERT OR REPLACE INTO edge_analysis (
                  fixture_id, run_id, prediction_run_id, model_version, market,
                  selection, prob_model, odds_decimal, prob_market_devig,
                  edge_ev, edge_prob, elo_diff, close_match, bookmaker, captured_utc,
                  prediction_generated_utc, generated_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    row.prob_market_devig,
                    row.edge_ev,
                    row.edge_prob,
                    row.elo_diff,
                    row.close_match,
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
            rows.append(
                EdgeRow(
                    fixture_id=str(fixture_id),
                    prediction_run_id=pred["run_id"],
                    model_version=str(pred["model_version"]),
                    market=str(market),
                    selection=str(selection),
                    prob_model=float(pred["prob"]),
                    odds_decimal=odds_decimal,
                    prob_market_devig=market_prob,
                    edge_ev=edge_ev,
                    edge_prob=edge_prob,
                    elo_diff=elo_diff,
                    close_match=close_match,
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
        WITH latest AS (
          SELECT fixture_id, bookmaker, market, selection, MAX(captured_utc) AS captured_utc
          FROM odds
          WHERE is_closing = 0
          GROUP BY fixture_id, bookmaker, market, selection
        )
        SELECT o.*
        FROM odds o
        JOIN latest l
          ON l.fixture_id = o.fixture_id
         AND l.bookmaker = o.bookmaker
         AND l.market = o.market
         AND l.selection = o.selection
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
        WITH latest AS (
          SELECT fixture_id, market, selection, model_version, MAX(generated_utc) AS generated_utc
          FROM prediction
          WHERE model_version = ?
          GROUP BY fixture_id, market, selection, model_version
        )
        SELECT p.*
        FROM prediction p
        JOIN latest l
          ON l.fixture_id = p.fixture_id
         AND l.market = p.market
         AND l.selection = p.selection
         AND l.model_version = p.model_version
         AND l.generated_utc = p.generated_utc
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


def _normalize_implied(odds: list[float]) -> list[float]:
    implied = [1.0 / odd for odd in odds]
    overround = sum(implied)
    if overround <= 0 or not math.isfinite(overround):
        raise ValueError("Cannot de-vig odds with non-positive overround")
    return [prob / overround for prob in implied]


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
                "prob_market_devig": row.prob_market_devig,
                "edge_ev": row.edge_ev,
                "elo_diff": row.elo_diff,
                "close_match": row.close_match,
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
