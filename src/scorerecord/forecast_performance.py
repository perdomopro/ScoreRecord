"""Forecast-only performance reporting from settled prediction rows."""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scorerecord.runs import ingest_run, record_quality

EPS = 1e-6


@dataclass(frozen=True)
class ForecastPerformanceRow:
    group_type: str
    group_value: str
    decisions: int
    prediction_rows: int
    accuracy: float | None
    brier: float | None
    logloss: float | None

    def as_row(self) -> str:
        return (
            f"{self.group_type}={self.group_value} "
            f"decisions={self.decisions} prediction_rows={self.prediction_rows} "
            f"accuracy={_fmt(self.accuracy)} brier={_fmt(self.brier)} "
            f"logloss={_fmt(self.logloss)}"
        )


@dataclass(frozen=True)
class ForecastPerformanceReport:
    decisions: int
    prediction_rows: int
    fixture_count: int
    latest_settled_utc: str | None
    by_market: list[ForecastPerformanceRow]
    by_league: list[ForecastPerformanceRow]

    def as_rows(self) -> list[str]:
        rows = [
            f"decisions={self.decisions}",
            f"prediction_rows={self.prediction_rows}",
            f"fixture_count={self.fixture_count}",
            f"latest_settled_utc={self.latest_settled_utc or 'n/a'}",
        ]
        rows.extend(row.as_row() for row in self.by_market)
        rows.extend(row.as_row() for row in self.by_league)
        return rows


@dataclass(frozen=True)
class _Decision:
    fixture_id: str
    league_code: str
    market: str
    prediction_rows: int
    top_correct: int
    brier: float
    logloss: float


def forecast_performance_report(conn: sqlite3.Connection) -> ForecastPerformanceReport:
    rows = _prediction_result_rows(conn)
    decisions = _decisions_from_rows(rows)
    fixture_count = len({decision.fixture_id for decision in decisions})
    latest = max((str(row["settled_utc"]) for row in rows if row["settled_utc"]), default=None)
    return ForecastPerformanceReport(
        decisions=len(decisions),
        prediction_rows=len(rows),
        fixture_count=fixture_count,
        latest_settled_utc=latest,
        by_market=_group_report(decisions, "market"),
        by_league=_group_report(decisions, "league"),
    )


def record_forecast_performance_report(
    conn: sqlite3.Connection,
    *,
    cwd: Path | None = None,
) -> ForecastPerformanceReport:
    report = forecast_performance_report(conn)
    with ingest_run(
        conn,
        "forecast-performance-report",
        source="prediction_result",
        cwd=cwd,
    ) as run_id:
        details = {
            "decisions": report.decisions,
            "prediction_rows": report.prediction_rows,
            "fixture_count": report.fixture_count,
            "latest_settled_utc": report.latest_settled_utc,
        }
        record_quality(
            conn,
            run_id,
            "forecast_performance",
            "decisions",
            float(report.decisions),
            json.dumps(details, sort_keys=True),
        )
        for row in [*report.by_market, *report.by_league]:
            suffix = row.group_value.replace(" ", "_")
            prefix = f"{row.group_type}_{suffix}"
            for metric, value in {
                "accuracy": row.accuracy,
                "brier": row.brier,
                "logloss": row.logloss,
                "decisions": float(row.decisions),
            }.items():
                if value is None:
                    continue
                record_quality(
                    conn,
                    run_id,
                    "forecast_performance",
                    f"{prefix}_{metric}",
                    float(value),
                    row.as_row(),
                )
        conn.commit()
    return report


def _prediction_result_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
          pr.prediction_run_id,
          pr.fixture_id,
          pr.model_version,
          pr.market,
          pr.selection,
          pr.predicted_prob,
          pr.label,
          pr.brier,
          pr.logloss,
          pr.prediction_generated_utc,
          pr.settled_utc,
          COALESCE(f.league_code, 'UNKNOWN') AS league_code
        FROM prediction_result pr
        JOIN fixture f ON f.fixture_id = pr.fixture_id
        WHERE pr.market != 'exact_score'
        ORDER BY pr.prediction_run_id, pr.fixture_id, pr.market, pr.selection
        """
    ).fetchall()


def _decisions_from_rows(rows: list[sqlite3.Row]) -> list[_Decision]:
    grouped: dict[tuple[Any, ...], list[sqlite3.Row]] = {}
    for row in rows:
        key = (
            row["prediction_run_id"],
            row["fixture_id"],
            row["model_version"],
            row["market"],
            row["prediction_generated_utc"],
        )
        grouped.setdefault(key, []).append(row)

    decisions: list[_Decision] = []
    for group_rows in grouped.values():
        market = str(group_rows[0]["market"])
        top = max(group_rows, key=lambda row: float(row["predicted_prob"]))
        scored = _score_group(market, group_rows)
        if scored is None:
            continue
        brier, logloss = scored
        decisions.append(
            _Decision(
                fixture_id=str(group_rows[0]["fixture_id"]),
                league_code=str(group_rows[0]["league_code"] or "UNKNOWN"),
                market=market,
                prediction_rows=len(group_rows),
                top_correct=int(top["label"] or 0),
                brier=brier,
                logloss=logloss,
            )
        )
    return decisions


def _score_group(market: str, rows: list[sqlite3.Row]) -> tuple[float, float] | None:
    if market == "1X2":
        correct_rows = [row for row in rows if int(row["label"] or 0) == 1]
        if not correct_rows:
            return None
        brier = sum(float(row["brier"]) for row in rows)
        prob = float(correct_rows[0]["predicted_prob"])
        return brier, _binary_logloss(prob, 1)

    correct_rows = [row for row in rows if int(row["label"] or 0) == 1]
    if correct_rows:
        row = correct_rows[0]
        return float(row["brier"]), float(row["logloss"])
    if not rows:
        return None
    return (
        sum(float(row["brier"]) for row in rows) / len(rows),
        sum(float(row["logloss"]) for row in rows) / len(rows),
    )


def _group_report(
    decisions: list[_Decision],
    group_type: str,
) -> list[ForecastPerformanceRow]:
    if group_type not in {"market", "league"}:
        raise ValueError(f"Unsupported performance group: {group_type}")

    grouped: dict[str, list[_Decision]] = {}
    for decision in decisions:
        key = decision.market if group_type == "market" else decision.league_code
        grouped.setdefault(key, []).append(decision)

    report_rows = []
    for group_value, group_decisions in sorted(grouped.items()):
        n = len(group_decisions)
        report_rows.append(
            ForecastPerformanceRow(
                group_type=group_type,
                group_value=group_value,
                decisions=n,
                prediction_rows=sum(row.prediction_rows for row in group_decisions),
                accuracy=sum(row.top_correct for row in group_decisions) / n if n else None,
                brier=sum(row.brier for row in group_decisions) / n if n else None,
                logloss=sum(row.logloss for row in group_decisions) / n if n else None,
            )
        )
    return report_rows


def _binary_logloss(prob: float, label: int) -> float:
    p = min(max(prob, EPS), 1.0 - EPS)
    return -math.log(p if label else 1.0 - p)


def _fmt(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "n/a"
    return f"{value:.4f}"
