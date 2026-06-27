import json
import sqlite3

from scorerecord.db import init_db
from scorerecord.evaluate.calibrator_drift import monitor_calibrator_drift


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def test_monitor_calibrator_drift_records_alerts() -> None:
    conn = _conn()
    for run_id in (10, 11):
        conn.execute(
            """
            INSERT INTO ingest_run (run_id, job_name, status, source)
            VALUES (?, 'predict-fixtures', 'succeeded', 'fixture')
            """,
            (run_id,),
        )
    for run_id, a, b in [(10, 1.0, 0.10), (11, 1.05, 0.13)]:
        conn.execute(
            """
            INSERT INTO data_quality (
              run_id, check_name, metric_name, metric_value, details_json
            )
            VALUES (?, 'calibrator_artifact', 'BTTS_platt_coefficients', 100.0, ?)
            """,
            (run_id, json.dumps({"market": "BTTS", "a": a, "b": b})),
        )

    summary = monitor_calibrator_drift(conn, threshold=0.10)

    assert summary.previous_run_id == 10
    assert summary.current_run_id == 11
    assert summary.markets_compared == 1
    assert summary.alerts == 1
    assert (
        conn.execute(
            """
            SELECT COUNT(*)
            FROM data_quality
            WHERE check_name = 'calibrator_drift'
            """
        ).fetchone()[0]
        == 3
    )
