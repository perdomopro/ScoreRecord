import math
import sqlite3

from scorerecord.db import init_db
from scorerecord.evaluate.settlement import settle_fixtures


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _prediction_run(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        """
        INSERT INTO ingest_run (job_name, status, source)
        VALUES ('predict-fixtures', 'succeeded', 'fixture')
        """
    )
    return int(cursor.lastrowid)


def test_settlement_blocks_predictions_generated_after_kickoff() -> None:
    conn = _conn()
    run_id = _prediction_run(conn)
    conn.execute(
        """
        INSERT INTO fixture (
          fixture_id, kickoff_local, home_score, away_score
        )
        VALUES ('m1', '2026-06-11 13:00 UTC-6', 2, 1)
        """
    )
    conn.execute(
        """
        INSERT INTO prediction (
          run_id, fixture_id, model, market, selection, prob,
          fair_odds, generated_utc, model_version
        )
        VALUES (?, 'm1', 'dixon_coles', 'BTTS', 'yes', 0.60, 1.6667,
                '2026-06-12T00:00:00+00:00', 'test-v1')
        """,
        (run_id,),
    )

    summary = settle_fixtures(conn, dry_run=True)

    assert summary.finished_fixtures == 1
    assert summary.settled_predictions == 0
    assert summary.blocked_backfill_predictions == 1


def test_settlement_writes_multiclass_result_metrics() -> None:
    conn = _conn()
    run_id = _prediction_run(conn)
    conn.execute(
        """
        INSERT INTO fixture (
          fixture_id, kickoff_local, home_score, away_score
        )
        VALUES ('m2', '2026-06-11 13:00 UTC-6', 2, 1)
        """
    )
    for selection, prob in [("home", 0.60), ("draw", 0.25), ("away", 0.15)]:
        conn.execute(
            """
            INSERT INTO prediction (
              run_id, fixture_id, model, market, selection, prob,
              fair_odds, generated_utc, model_version
            )
            VALUES (?, 'm2', 'dixon_coles', '1X2', ?, ?, ?, 
                    '2026-06-11T18:00:00+00:00', 'test-v1')
            """,
            (run_id, selection, prob, 1.0 / prob),
        )

    summary = settle_fixtures(conn, as_of="2026-06-12T00:00:00+00:00")

    assert summary.settled_predictions == 3
    assert conn.execute("SELECT COUNT(*) FROM prediction_result").fetchone()[0] == 3
    metric = conn.execute(
        """
        SELECT n, brier, logloss
        FROM eval_metric
        WHERE model = 'test-v1' AND market = '1X2'
        """
    ).fetchone()
    assert metric["n"] == 1
    assert math.isclose(metric["brier"], (0.60 - 1.0) ** 2 + 0.25**2 + 0.15**2)
    assert math.isclose(metric["logloss"], -math.log(0.60))
