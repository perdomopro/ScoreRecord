import sqlite3

from scorerecord.db import init_db
from scorerecord.models.manifest import build_model_manifest


def test_manifest_declares_run_ids_are_database_local() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)

    manifest = build_model_manifest(conn).as_dict()

    assert manifest["run_id_scope"] == "sqlite_database_local"
    assert manifest["predictive_validation_run_id"] is None
    assert manifest["market_validation_status"] == "NOT_TESTED"
    assert manifest["trained_until"] == "2026-06-11"
    assert "model_artifact_id" in manifest
