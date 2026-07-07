import json
import math
import sqlite3
from datetime import UTC, datetime

from scorerecord.db import init_db
from scorerecord.forecast_snapshots import (
    forecast_snapshot_report,
    freeze_forecast_snapshot,
    settle_forecast_snapshots,
)
from scorerecord.gui import load_forecast_data, render_match_forecast


MODEL_VERSION = "test-forecast-v1"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _run(conn: sqlite3.Connection, job_name: str = "predict-fixtures") -> int:
    cursor = conn.execute(
        "INSERT INTO ingest_run (job_name, status, source) VALUES (?, 'succeeded', 'test')",
        (job_name,),
    )
    return int(cursor.lastrowid)


def _insert_fixture(
    conn: sqlite3.Connection,
    *,
    fixture_id: str = "m1",
    kickoff_utc: str = "2026-07-04T20:00:00+00:00",
    scores: tuple[int, int] | None = None,
) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO team (team_id, canonical, canonical_key) VALUES (1, 'A', 'a')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO team (team_id, canonical, canonical_key) VALUES (2, 'B', 'b')"
    )
    home_score, away_score = scores if scores is not None else (None, None)
    conn.execute(
        """
        INSERT INTO fixture (
          fixture_id, kickoff_local, kickoff_utc, home_id, away_id, home_score, away_score
        )
        VALUES (?, '2026-07-04 14:00 UTC-6', ?, 1, 2, ?, ?)
        """,
        (fixture_id, kickoff_utc, home_score, away_score),
    )
    run_id = _run(conn)
    conn.execute(
        """
        INSERT INTO model_artifact (
          artifact_id, model_version, market_validation_status, trained_until,
          training_rows, params_json
        )
        VALUES ('artifact-1', ?, 'NOT_TESTED', '2026-06-11', 100, '{}')
        """,
        (MODEL_VERSION,),
    )
    rows = [
        ("OU1.5", "over", 0.76),
        ("OU1.5", "under", 0.24),
        ("OU2.5", "over", 0.59),
        ("OU2.5", "under", 0.41),
        ("OU3.5", "over", 0.32),
        ("OU3.5", "under", 0.68),
        ("BTTS", "yes", 0.501),
        ("BTTS", "no", 0.499),
        ("1X2", "home", 0.99),
    ]
    for market, selection, prob in rows:
        conn.execute(
            """
            INSERT INTO prediction (
              run_id, fixture_id, artifact_id, model, market, selection,
              prob, fair_odds, generated_utc, model_version
            )
            VALUES (?, ?, 'artifact-1', 'dixon_coles', ?, ?, ?, ?,
                    '2026-07-04T12:00:00+00:00', ?)
            """,
            (run_id, fixture_id, market, selection, prob, 1.0 / prob, MODEL_VERSION),
        )
    return run_id


def test_freeze_creates_ou25_and_btts_snapshots_without_1x2() -> None:
    conn = _conn()
    run_id = _insert_fixture(conn)

    summary = freeze_forecast_snapshot(
        conn,
        target_date="2026-07-04",
        as_of_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )

    assert summary.prediction_run_id == run_id
    assert summary.candidates == 2
    assert summary.inserted == 2
    rows = conn.execute(
        """
        SELECT market, selection, signal_code, quality_status_json
        FROM forecast_snapshot
        ORDER BY market
        """
    ).fetchall()
    assert [(row["market"], row["selection"]) for row in rows] == [
        ("BTTS", "yes"),
        ("OU2.5", "over"),
    ]
    assert {row["market"] for row in rows} == {"BTTS", "OU2.5"}
    assert {row["signal_code"] for row in rows} == {"NO_SIGNAL", "MODERATE"}
    quality = json.loads(rows[0]["quality_status_json"])
    assert quality["market_validation"] == "NOT_TESTED"
    assert quality["calibration"] == "WARN"


def test_freeze_ignores_started_fixtures_and_is_idempotent() -> None:
    conn = _conn()
    _insert_fixture(conn)

    started = freeze_forecast_snapshot(
        conn,
        target_date="2026-07-04",
        as_of_utc=datetime(2026, 7, 4, 21, 0, tzinfo=UTC),
    )
    assert started.candidates == 0
    assert started.inserted == 0

    first = freeze_forecast_snapshot(
        conn,
        target_date="2026-07-04",
        as_of_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    second = freeze_forecast_snapshot(
        conn,
        target_date="2026-07-04",
        as_of_utc=datetime(2026, 7, 4, 12, 5, tzinfo=UTC),
    )
    assert first.inserted == 2
    assert second.candidates == 2
    assert second.inserted == 0
    assert conn.execute("SELECT COUNT(*) FROM forecast_snapshot").fetchone()[0] == 2


def test_settlement_handles_ou25_over_and_btts_yes() -> None:
    conn = _conn()
    _insert_fixture(conn)
    freeze_forecast_snapshot(
        conn,
        target_date="2026-07-04",
        as_of_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    conn.execute(
        "UPDATE fixture SET home_score = 2, away_score = 1 WHERE fixture_id = 'm1'"
    )

    summary = settle_forecast_snapshots(
        conn,
        as_of_utc=datetime(2026, 7, 5, 0, 0, tzinfo=UTC),
    )

    assert summary.eligible == 2
    assert summary.settled == 2
    rows = conn.execute(
        "SELECT market, result_label, correct FROM forecast_snapshot ORDER BY market"
    ).fetchall()
    assert [(row["market"], row["result_label"], row["correct"]) for row in rows] == [
        ("BTTS", 1, 1),
        ("OU2.5", 1, 1),
    ]


def test_settlement_handles_ou25_under_and_btts_no() -> None:
    conn = _conn()
    _insert_fixture(conn)
    conn.execute(
        """
        UPDATE prediction
        SET prob = CASE selection WHEN 'over' THEN 0.41 ELSE 0.59 END,
            fair_odds = CASE selection WHEN 'over' THEN 1.0 / 0.41 ELSE 1.0 / 0.59 END
        WHERE fixture_id = 'm1' AND market = 'OU2.5'
        """
    )
    conn.execute(
        """
        UPDATE prediction
        SET prob = CASE selection WHEN 'yes' THEN 0.40 ELSE 0.60 END,
            fair_odds = CASE selection WHEN 'yes' THEN 1.0 / 0.40 ELSE 1.0 / 0.60 END
        WHERE fixture_id = 'm1' AND market = 'BTTS'
        """
    )
    freeze_forecast_snapshot(
        conn,
        target_date="2026-07-04",
        as_of_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    conn.execute(
        "UPDATE fixture SET home_score = 1, away_score = 0 WHERE fixture_id = 'm1'"
    )

    settle_forecast_snapshots(conn, as_of_utc=datetime(2026, 7, 5, 0, 0, tzinfo=UTC))

    rows = conn.execute(
        "SELECT market, selection, result_label, correct FROM forecast_snapshot ORDER BY market"
    ).fetchall()
    observed = [
        (row["market"], row["selection"], row["result_label"], row["correct"])
        for row in rows
    ]
    assert observed == [
        ("BTTS", "no", 0, 1),
        ("OU2.5", "under", 0, 1),
    ]


def test_forecast_report_groups_by_market_and_signal_code() -> None:
    conn = _conn()
    _insert_fixture(conn)
    freeze_forecast_snapshot(
        conn,
        target_date="2026-07-04",
        as_of_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    conn.execute(
        "UPDATE fixture SET home_score = 2, away_score = 1 WHERE fixture_id = 'm1'"
    )
    settle_forecast_snapshots(conn, as_of_utc=datetime(2026, 7, 5, 0, 0, tzinfo=UTC))

    report = forecast_snapshot_report(conn)

    assert report.total_snapshots == 2
    assert report.settled_snapshots == 2
    assert report.pending_snapshots == 0
    market_rows = {row.group_value: row for row in report.by_market}
    assert set(market_rows) == {"BTTS", "OU2.5"}
    assert market_rows["OU2.5"].accuracy == 1.0
    assert math.isclose(market_rows["OU2.5"].brier or 0.0, (0.59 - 1.0) ** 2)
    signal_rows = {row.group_value: row for row in report.by_signal_code}
    assert set(signal_rows) == {"MODERATE", "NO_SIGNAL"}
    assert any(row.startswith("market=OU2.5") for row in report.as_rows())
    assert any(row.startswith("signal_code=MODERATE") for row in report.as_rows())


def test_match_view_renders_frozen_and_settled_forecast() -> None:
    conn = _conn()
    _insert_fixture(conn)
    freeze_forecast_snapshot(
        conn,
        target_date="2026-07-04",
        as_of_utc=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    conn.execute(
        "UPDATE fixture SET home_score = 2, away_score = 1 WHERE fixture_id = 'm1'"
    )
    settle_forecast_snapshots(conn, as_of_utc=datetime(2026, 7, 5, 0, 0, tzinfo=UTC))

    data = load_forecast_data(
        conn,
        fixture_id="m1",
        now_utc=datetime(2026, 7, 5, 1, 0, tzinfo=UTC),
    )
    html = render_match_forecast(data, "m1")

    assert "Forecast congelado" in html
    assert "OU2.5" in html
    assert "MODERATE" in html
    assert "Correcto" in html
