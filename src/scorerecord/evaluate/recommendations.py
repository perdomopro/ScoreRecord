"""Paper recommendation generation and CLV tracking."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from scorerecord.models.manifest import MODEL_VERSION
from scorerecord.runs import ingest_run, record_quality


@dataclass(frozen=True)
class RecommendationSummary:
    candidates_seen: int
    recommendations: int
    rows_inserted: int
    dry_run: bool

    def as_row(self) -> str:
        return (
            f"candidates_seen={self.candidates_seen} "
            f"recommendations={self.recommendations} "
            f"rows_inserted={self.rows_inserted} "
            f"dry_run={self.dry_run}"
        )


@dataclass(frozen=True)
class ClvSummary:
    recommendations_seen: int
    clv_rows: int
    mean_clv: float | None
    rows_inserted: int
    dry_run: bool

    def as_row(self) -> str:
        mean_clv = "na" if self.mean_clv is None else f"{self.mean_clv:.4f}"
        return (
            f"recommendations_seen={self.recommendations_seen} "
            f"clv_rows={self.clv_rows} "
            f"mean_clv={mean_clv} "
            f"rows_inserted={self.rows_inserted} "
            f"dry_run={self.dry_run}"
        )


@dataclass(frozen=True)
class RecommendationCandidate:
    edge_run_id: int
    fixture_id: str
    market: str
    selection: str
    model_version: str
    prob_model: float
    odds_decimal: float
    edge_ev: float
    edge_prob: float
    bookmaker: str
    odds_captured_utc: str


def kelly_stake(
    prob: float,
    odds_decimal: float,
    *,
    fraction: float = 0.25,
    max_stake: float = 1.0,
) -> float:
    b = odds_decimal - 1.0
    if b <= 0:
        return 0.0
    q = 1.0 - prob
    kelly_full = max((b * prob - q) / b, 0.0)
    return min(fraction * kelly_full, max_stake)


def generate_recommendations(
    conn: sqlite3.Connection,
    *,
    model_version: str = MODEL_VERSION,
    edge_run_id: int | None = None,
    min_edge: float = 0.05,
    max_edge: float = 0.25,
    min_prob: float = 0.15,
    kelly_fraction: float = 0.25,
    max_stake: float = 1.0,
    dry_run: bool = False,
    cwd: Path | None = None,
) -> RecommendationSummary:
    candidates = _recommendation_candidates(
        conn,
        model_version=model_version,
        edge_run_id=edge_run_id,
        min_edge=min_edge,
        max_edge=max_edge,
        min_prob=min_prob,
    )
    best = _best_candidates(candidates)
    if dry_run:
        return RecommendationSummary(
            candidates_seen=len(candidates),
            recommendations=len(best),
            rows_inserted=0,
            dry_run=True,
        )

    generated_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with ingest_run(conn, "recommend", source="edge_analysis", cwd=cwd) as run_id:
        inserted = 0
        for candidate in best:
            stake = kelly_stake(
                candidate.prob_model,
                candidate.odds_decimal,
                fraction=kelly_fraction,
                max_stake=max_stake,
            )
            cursor = conn.execute(
                """
                INSERT OR REPLACE INTO recommendation (
                  run_id, fixture_id, market, selection, model_prob, market_odds,
                  edge, edge_prob, kelly_fraction, stake_units, confidence,
                  bookmaker, model_version, odds_captured_utc, paper, generated_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    run_id,
                    candidate.fixture_id,
                    candidate.market,
                    candidate.selection,
                    candidate.prob_model,
                    candidate.odds_decimal,
                    candidate.edge_ev,
                    candidate.edge_prob,
                    kelly_fraction,
                    stake,
                    _confidence(candidate.edge_ev, candidate.prob_model),
                    candidate.bookmaker,
                    candidate.model_version,
                    candidate.odds_captured_utc,
                    generated_utc,
                ),
            )
            inserted += cursor.rowcount
        summary = RecommendationSummary(
            candidates_seen=len(candidates),
            recommendations=len(best),
            rows_inserted=inserted,
            dry_run=False,
        )
        _record_recommendation_quality(conn, run_id, best, summary)
        conn.commit()
    return summary


def compute_recommendation_clv(
    conn: sqlite3.Connection,
    *,
    model_version: str = MODEL_VERSION,
    dry_run: bool = False,
    cwd: Path | None = None,
) -> ClvSummary:
    rows = _clv_rows(conn, model_version=model_version)
    mean_clv = sum(row["clv"] for row in rows) / len(rows) if rows else None
    if dry_run:
        return ClvSummary(
            recommendations_seen=_recommendation_count(conn, model_version=model_version),
            clv_rows=len(rows),
            mean_clv=mean_clv,
            rows_inserted=0,
            dry_run=True,
        )

    computed_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with ingest_run(conn, "compute-clv", source="recommendation+closing_odds", cwd=cwd) as run_id:
        inserted = 0
        for row in rows:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO recommendation_result (
                  run_id, recommendation_run_id, fixture_id, market, selection,
                  bookmaker, model_version, odds_taken, closing_odds, clv,
                  recommendation_generated_utc, closing_captured_utc, computed_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    row["recommendation_run_id"],
                    row["fixture_id"],
                    row["market"],
                    row["selection"],
                    row["bookmaker"],
                    row["model_version"],
                    row["odds_taken"],
                    row["closing_odds"],
                    row["clv"],
                    row["recommendation_generated_utc"],
                    row["closing_captured_utc"],
                    computed_utc,
                ),
            )
            inserted += cursor.rowcount
        summary = ClvSummary(
            recommendations_seen=_recommendation_count(conn, model_version=model_version),
            clv_rows=len(rows),
            mean_clv=mean_clv,
            rows_inserted=inserted,
            dry_run=False,
        )
        _write_clv_eval_metrics(conn, run_id, computed_utc)
        _record_clv_quality(conn, run_id, summary)
        conn.commit()
    return summary


def compute_clv(odds_taken: float, closing_odds: float) -> float:
    return odds_taken / closing_odds - 1.0


def _recommendation_candidates(
    conn: sqlite3.Connection,
    *,
    model_version: str,
    edge_run_id: int | None,
    min_edge: float,
    max_edge: float,
    min_prob: float,
) -> list[RecommendationCandidate]:
    run_id = edge_run_id or _latest_edge_run(conn)
    if run_id is None:
        return []
    rows = conn.execute(
        """
        SELECT *
        FROM edge_analysis
        WHERE run_id = ?
          AND model_version = ?
          AND edge_ev >= ?
          AND edge_ev <= ?
          AND prob_model >= ?
        ORDER BY edge_ev DESC
        """,
        (run_id, model_version, min_edge, max_edge, min_prob),
    ).fetchall()
    return [
        RecommendationCandidate(
            edge_run_id=int(row["run_id"]),
            fixture_id=str(row["fixture_id"]),
            market=str(row["market"]),
            selection=str(row["selection"]),
            model_version=str(row["model_version"]),
            prob_model=float(row["prob_model"]),
            odds_decimal=float(row["odds_decimal"]),
            edge_ev=float(row["edge_ev"]),
            edge_prob=float(row["edge_prob"]),
            bookmaker=str(row["bookmaker"]),
            odds_captured_utc=str(row["captured_utc"]),
        )
        for row in rows
    ]


def _best_candidates(
    candidates: list[RecommendationCandidate],
) -> list[RecommendationCandidate]:
    best: dict[tuple[str, str, str], RecommendationCandidate] = {}
    for candidate in candidates:
        key = (candidate.fixture_id, candidate.market, candidate.selection)
        incumbent = best.get(key)
        if incumbent is None or candidate.edge_ev > incumbent.edge_ev:
            best[key] = candidate
    return sorted(best.values(), key=lambda row: row.edge_ev, reverse=True)


def _latest_edge_run(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        """
        SELECT run_id
        FROM ingest_run
        WHERE job_name = 'compute-edge'
          AND status = 'succeeded'
        ORDER BY run_id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["run_id"]) if row else None


def _confidence(edge_ev: float, prob: float) -> str:
    if edge_ev > 0.08 and prob > 0.25:
        return "high"
    if edge_ev > 0.05:
        return "medium"
    if edge_ev > 0.03:
        return "low"
    return "watch"


def _record_recommendation_quality(
    conn: sqlite3.Connection,
    run_id: int,
    candidates: list[RecommendationCandidate],
    summary: RecommendationSummary,
) -> None:
    details = {
        "candidates_seen": summary.candidates_seen,
        "recommendations": summary.recommendations,
        "rows_inserted": summary.rows_inserted,
        "min_edge_policy": "paper recommendations only",
        "max_edge_policy": "edges above the cap require manual review before recommendation",
        "max_edge_ev": max((candidate.edge_ev for candidate in candidates), default=None),
    }
    for metric_name, value in [
        ("recommendation_candidates", summary.candidates_seen),
        ("recommendations_inserted", summary.rows_inserted),
        ("paper_recommendations", summary.recommendations),
    ]:
        record_quality(
            conn,
            run_id,
            "paper_recommendation",
            metric_name,
            float(value),
            json.dumps(details, sort_keys=True),
        )


def _clv_rows(conn: sqlite3.Connection, *, model_version: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        WITH latest_closing AS (
          SELECT fixture_id, market, selection, bookmaker, MAX(captured_utc) AS captured_utc
          FROM odds
          WHERE is_closing = 1
          GROUP BY fixture_id, market, selection, bookmaker
        )
        SELECT
          r.run_id AS recommendation_run_id,
          r.fixture_id,
          r.market,
          r.selection,
          r.bookmaker,
          r.model_version,
          r.market_odds AS odds_taken,
          o.decimal_odds AS closing_odds,
          (r.market_odds / o.decimal_odds) - 1.0 AS clv,
          r.generated_utc AS recommendation_generated_utc,
          o.captured_utc AS closing_captured_utc
        FROM recommendation r
        JOIN latest_closing lc
          ON lc.fixture_id = r.fixture_id
         AND lc.market = r.market
         AND lc.selection = r.selection
         AND lc.bookmaker = r.bookmaker
        JOIN odds o
          ON o.fixture_id = lc.fixture_id
         AND o.market = lc.market
         AND o.selection = lc.selection
         AND o.bookmaker = lc.bookmaker
         AND o.captured_utc = lc.captured_utc
        WHERE r.paper = 1
          AND r.model_version = ?
          AND NOT EXISTS (
            SELECT 1
            FROM recommendation_result rr
            WHERE rr.recommendation_run_id = r.run_id
              AND rr.fixture_id = r.fixture_id
              AND rr.market = r.market
              AND rr.selection = r.selection
              AND rr.bookmaker = r.bookmaker
              AND rr.recommendation_generated_utc = r.generated_utc
          )
        ORDER BY r.fixture_id, r.market, r.selection
        """,
        (model_version,),
    ).fetchall()


def _recommendation_count(conn: sqlite3.Connection, *, model_version: str) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM recommendation
            WHERE paper = 1
              AND model_version = ?
            """,
            (model_version,),
        ).fetchone()[0]
    )


def _write_clv_eval_metrics(conn: sqlite3.Connection, run_id: int, as_of: str) -> None:
    rows = conn.execute(
        """
        SELECT market, AVG(clv) AS mean_clv, COUNT(*) AS n
        FROM recommendation_result
        WHERE run_id = ?
        GROUP BY market
        """,
        (run_id,),
    ).fetchall()
    for row in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO eval_metric
              (run_id, as_of, model, market, n, brier, logloss, roi, clv)
            VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
            """,
            (
                run_id,
                as_of,
                MODEL_VERSION,
                str(row["market"]),
                int(row["n"]),
                float(row["mean_clv"]),
            ),
        )


def _record_clv_quality(conn: sqlite3.Connection, run_id: int, summary: ClvSummary) -> None:
    details = {
        "recommendations_seen": summary.recommendations_seen,
        "clv_rows": summary.clv_rows,
        "mean_clv": summary.mean_clv,
        "gate": "mean_clv should be positive over 50+ settled forward picks before real staking",
    }
    for metric_name, value in [
        ("recommendations_seen", summary.recommendations_seen),
        ("clv_rows", summary.clv_rows),
        ("mean_clv", summary.mean_clv or 0.0),
    ]:
        record_quality(
            conn,
            run_id,
            "clv_tracking",
            metric_name,
            float(value),
            json.dumps(details, sort_keys=True),
        )
