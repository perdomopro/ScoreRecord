"""Forecast-only data-quality helpers."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass

from scorerecord.forecast_policy import FORECAST_MARKETS


@dataclass(frozen=True)
class ForecastQualityItem:
    label: str
    status: str
    detail: str


def forecast_quality_items(
    conn: sqlite3.Connection,
    prediction_run_id: int | None,
) -> list[ForecastQualityItem]:
    return [
        _calibration_quality(conn, prediction_run_id),
        _ou_monotonicity_quality(conn, prediction_run_id),
        _elo_coverage_quality(conn, prediction_run_id),
    ]


def forecast_quality_status_json(
    conn: sqlite3.Connection,
    prediction_run_id: int | None,
    market_validation_status: str,
) -> str:
    items = forecast_quality_items(conn, prediction_run_id)
    payload = {
        _quality_key(item.label): item.status
        for item in items
    }
    payload["market_validation"] = market_validation_status or "NOT_TESTED"
    payload["details"] = [asdict(item) for item in items]
    return json.dumps(payload, sort_keys=True)


def _calibration_quality(
    conn: sqlite3.Connection,
    prediction_run_id: int | None,
) -> ForecastQualityItem:
    rows = [
        _latest_quality_metric(
            conn,
            prediction_run_id,
            "calibrator_sanity",
            f"{market}_mean_calibration_improved",
        )
        for market in FORECAST_MARKETS
    ]
    ok_count = sum(1 for row in rows if row is not None and float(row["metric_value"] or 0) >= 1)
    status = "OK" if ok_count == len(FORECAST_MARKETS) else "WARN"
    return ForecastQualityItem("Calibration", status, f"{ok_count}/{len(FORECAST_MARKETS)} checks")


def _ou_monotonicity_quality(
    conn: sqlite3.Connection,
    prediction_run_id: int | None,
) -> ForecastQualityItem:
    row = _latest_quality_metric(
        conn,
        prediction_run_id,
        "prediction_sanity",
        "ou_monotonicity_violations",
    )
    violations = int(float(row["metric_value"] or 0)) if row is not None else -1
    status = "OK" if violations == 0 else "WARN"
    detail = "missing" if violations < 0 else f"{violations} violations"
    return ForecastQualityItem("OU monotonicity", status, detail)


def _elo_coverage_quality(
    conn: sqlite3.Connection,
    prediction_run_id: int | None,
) -> ForecastQualityItem:
    row = _latest_quality_metric(
        conn,
        prediction_run_id,
        "fixture_elo",
        "missing_team_rating_slots",
    )
    missing = int(float(row["metric_value"] or 0)) if row is not None else -1
    status = "OK" if missing == 0 else "WARN"
    detail = "missing" if missing < 0 else f"{missing} missing rating slots"
    return ForecastQualityItem("Elo coverage", status, detail)


def _latest_quality_metric(
    conn: sqlite3.Connection,
    prediction_run_id: int | None,
    check_name: str,
    metric_name: str,
) -> sqlite3.Row | None:
    if prediction_run_id is None:
        return None
    return conn.execute(
        """
        SELECT run_id, metric_value, details_json
        FROM data_quality
        WHERE run_id = ?
          AND check_name = ?
          AND metric_name = ?
        ORDER BY quality_id DESC
        LIMIT 1
        """,
        (prediction_run_id, check_name, metric_name),
    ).fetchone()


def _quality_key(label: str) -> str:
    return label.lower().replace(" ", "_")
