import sqlite3

from scorerecord.db import init_db, schema_version
from scorerecord.health import db_health_checks


def test_schema_version_is_recorded_by_init_db() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    init_db(conn)

    assert schema_version(conn) >= 6


def test_db_health_reports_ok_for_empty_initialized_database() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)

    checks = {check.name: check for check in db_health_checks(conn)}

    assert checks["sqlite_integrity"].status == "OK"
    assert checks["foreign_keys"].status == "OK"
    assert checks["prediction_artifact_id"].value == "no_prediction_runs"
