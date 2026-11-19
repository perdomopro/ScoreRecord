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
    # Compare the two latest runs of the same calibrator scope (league). Runs
    # of different leagues have unrelated coefficients and must not be compared.
    previous_run_id: int | None = None
    current_run_id: int | None = None
    comparisons = []
    for scope, run_ids in _latest_runs_by_scope(conn, limit=2).items():
        if len(run_ids) < 2:
            if current_run_id is None and run_ids:
                current_run_id = run_ids[0]
            continue
        scope_current, scope_previous = run_ids[0], run_ids[1]
        if current_run_id is None or scope_current > current_run_id or previous_run_id is None:
            current_run_id, previous_run_id = scope_current, scope_previous
        previous = _calibrator_artifacts(conn, scope_previous)
        current = _calibrator_artifacts(conn, scope_current)
        for market in sorted(set(previous) & set(current)):
            prev = previous[market]
            curr = current[market]
            a_change = _relative_change(curr["a"], prev["a"])
            b_change = _relative_change(curr["b"], prev["b"])
            comparisons.append(
                {
                    "market": market,
                    "scope": scope,
                    "previous_run_id": scope_previous,
                    "current_run_id": scope_current,
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


def _latest_runs_by_scope(
    conn: sqlite3.Connection,
    *,
    limit: int,
    scan_runs: int = 400,
) -> dict[str, list[int]]:
    rows = conn.execute(
        """
        SELECT ir.run_id, dq.details_json
        FROM ingest_run ir
        JOIN data_quality dq ON dq.run_id = ir.run_id
        WHERE ir.job_name = 'predict-fixtures'
          AND ir.status = 'succeeded'
          AND dq.check_name = 'calibrator_artifact'
          AND ir.run_id IN (
            SELECT run_id FROM ingest_run
            WHERE job_name = 'predict-fixtures' AND status = 'succeeded'
            ORDER BY run_id DESC
            LIMIT ?
          )
        ORDER BY ir.run_id DESC
        """,
        (scan_runs,),
    ).fetchall()
    by_scope: dict[str, list[int]] = {}
    for row in rows:
        scope = "unscoped"
        if row["details_json"]:
            details = json.loads(str(row["details_json"]))
            scope = str(details.get("calibrator_scope") or "unscoped")
        runs = by_scope.setdefault(scope, [])
        run_id = int(row["run_id"])
        if run_id not in runs and len(runs) < limit:
            runs.append(run_id)
    return by_scope


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
