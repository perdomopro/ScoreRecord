"""Monitor Platt calibrator coefficient drift between prediction runs."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from scorerecord.runs import ingest_run, record_quality


@dataclass(frozen=True)
class CalibratorDriftSummary:
    previous_run_id: int | None
    current_run_id: int | None
    markets_compared: int
    max_relative_change: float | None
    alerts: int

    def as_row(self) -> str:
        max_change = (
            "na" if self.max_relative_change is None else f"{self.max_relative_change:.4f}"
        )
        return (
            f"previous_run_id={self.previous_run_id or 'na'} "
            f"current_run_id={self.current_run_id or 'na'} "
            f"markets_compared={self.markets_compared} "
            f"max_relative_change={max_change} "
            f"alerts={self.alerts}"
        )


def monitor_calibrator_drift(
    conn: sqlite3.Connection,
    *,
    threshold: float = 0.10,
    cwd: Path | None = None,
) -> CalibratorDriftSummary:
    run_ids = _latest_prediction_runs_with_calibrators(conn, limit=2)
    previous_run_id = run_ids[1] if len(run_ids) > 1 else None
    current_run_id = run_ids[0] if run_ids else None

    comparisons = []
    if previous_run_id is not None and current_run_id is not None:
        previous = _calibrator_artifacts(conn, previous_run_id)
        current = _calibrator_artifacts(conn, current_run_id)
        for market in sorted(set(previous) & set(current)):
            prev = previous[market]
            curr = current[market]
            a_change = _relative_change(curr["a"], prev["a"])
            b_change = _relative_change(curr["b"], prev["b"])
            comparisons.append(
                {
                    "market": market,
                    "previous_run_id": previous_run_id,
                    "current_run_id": current_run_id,
                    "a_previous": prev["a"],
                    "a_current": curr["a"],
                    "b_previous": prev["b"],
                    "b_current": curr["b"],
                    "a_relative_change": a_change,
                    "b_relative_change": b_change,
                    "max_relative_change": max(a_change, b_change),
                }
            )

    max_change = max((row["max_relative_change"] for row in comparisons), default=None)
    alerts = sum(1 for row in comparisons if row["max_relative_change"] > threshold)
    summary = CalibratorDriftSummary(
        previous_run_id=previous_run_id,
        current_run_id=current_run_id,
        markets_compared=len(comparisons),
        max_relative_change=max_change,
        alerts=alerts,
    )

    with ingest_run(conn, "calibrator-drift", source="data_quality", cwd=cwd) as run_id:
        details = {
            "threshold": threshold,
            "previous_run_id": previous_run_id,
            "current_run_id": current_run_id,
            "comparisons": comparisons,
            "note": "Relative changes above threshold should trigger manual review before staking.",
        }
        for metric_name, value in [
            ("markets_compared", summary.markets_compared),
            ("max_relative_change", summary.max_relative_change or 0.0),
            ("alerts", summary.alerts),
        ]:
            record_quality(
                conn,
                run_id,
                "calibrator_drift",
                metric_name,
                float(value),
                json.dumps(details, sort_keys=True),
            )
        conn.commit()
    return summary


def _latest_prediction_runs_with_calibrators(
    conn: sqlite3.Connection,
    *,
    limit: int,
) -> list[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT ir.run_id
        FROM ingest_run ir
        JOIN data_quality dq ON dq.run_id = ir.run_id
        WHERE ir.job_name = 'predict-fixtures'
          AND ir.status = 'succeeded'
          AND dq.check_name = 'calibrator_artifact'
        ORDER BY ir.run_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [int(row["run_id"]) for row in rows]


def _calibrator_artifacts(conn: sqlite3.Connection, run_id: int) -> dict[str, dict[str, float]]:
    rows = conn.execute(
        """
        SELECT details_json
        FROM data_quality
        WHERE run_id = ?
          AND check_name = 'calibrator_artifact'
        """,
        (run_id,),
    ).fetchall()
    artifacts: dict[str, dict[str, float]] = {}
    for row in rows:
        if not row["details_json"]:
            continue
        details = json.loads(str(row["details_json"]))
        market = str(details.get("market") or "")
        if not market:
            continue
        artifacts[market] = {
            "a": float(details.get("a", 0.0)),
            "b": float(details.get("b", 0.0)),
        }
    return artifacts


def _relative_change(current: float, previous: float) -> float:
    denominator = max(abs(previous), 0.01)
    return abs(current - previous) / denominator
