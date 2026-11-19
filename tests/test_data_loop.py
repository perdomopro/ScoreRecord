"""Tests for the pieces that close the daily forecast loop."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from scorerecord.db import connect, init_db
from scorerecord.evaluate.calibrator_drift import monitor_calibrator_drift
from scorerecord.forecast_snapshots import freeze_forecast_snapshot, settle_forecast_snapshots
from scorerecord.health import pipeline_health_checks
from scorerecord.ingest.bridge import drop_promoted_duplicates
from scorerecord.ingest.odds import _candidate_fixtures, _direct_event_id
from scorerecord.ingest.results import (
    final_score,
    ingest_results_odds_api_io,
    promote_finished_fixtures,
    sync_fixture_scores_from_history,
)
from scorerecord.league_config import league_config
from scorerecord.maintenance import backup_database, latest_backup_age_hours, prune_predictions
from scorerecord.predict.fixtures import publish_fixture_predictions

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    for team_id, name in ((1, "A"), (2, "B"), (3, "C")):
        conn.execute(
            "INSERT INTO team (team_id, canonical, canonical_key) VALUES (?, ?, ?)",
            (team_id, name, name.lower()),
        )
    return conn


def _fixture(
    conn: sqlite3.Connection,
    fixture_id: str,
    kickoff_utc: str,
    *,
    league: str = "EPL",
    event_id: str | None = None,
    home: int = 1,
    away: int = 2,
    scores: tuple[int, int] | None = None,
) -> None:
    home_score, away_score = scores if scores else (None, None)
    conn.execute(
        """
        INSERT INTO fixture (
          fixture_id, kickoff_utc, home_id, away_id, home_score, away_score,
          league_code, competition, source, source_event_id, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'Comp', 'odds_api_io', ?, 'scheduled')
        """,
        (fixture_id, kickoff_utc, home, away, home_score, away_score, league, event_id),
    )


def _history(conn: sqlite3.Connection, match_id: str, day: str, league: str = "EPL") -> None:
    conn.execute(
        """
        INSERT INTO match_history (
          source_match_id, date, home_id, away_id, home_score, away_score, tournament, neutral
        )
        VALUES (?, ?, 1, 2, 2, 1, ?, 0)
        """,
        (match_id, day, league),
    )
    conn.execute(
        """
        INSERT INTO team_rating_history (
          team_id, opponent_id, source_match_id, rating_system,
          rating_pre, rating_post, as_of, is_home, expected_result, actual_result
        )
        VALUES
          (1, 2, ?, 'elo', 1500, 1500, ?, 1, 0.5, 0.5),
          (2, 1, ?, 'elo', 1500, 1500, ?, 0, 0.5, 0.5)
        """,
        (match_id, day, match_id, day),
    )


def _prediction(
    conn: sqlite3.Connection,
    fixture_id: str,
    rows: list[tuple[str, str, float]],
    *,
    generated: str = "2026-11-12T12:00:00+00:00",
) -> int:
    run_id = int(
        conn.execute(
            "INSERT INTO ingest_run (job_name, status, source) "
            "VALUES ('predict-fixtures', 'succeeded', 'test')"
        ).lastrowid
    )
    for market, selection, prob in rows:
        conn.execute(
            """
            INSERT INTO prediction (
              run_id, fixture_id, model, market, selection, prob, fair_odds,
              generated_utc, model_version
            )
            VALUES (?, ?, 'dixon_coles', ?, ?, ?, ?, ?, 'test')
            """,
            (run_id, fixture_id, market, selection, prob, 1.0 / prob, generated),
        )
    return run_id


FULL_ROWS = [
    ("OU2.5", "over", 0.58),
    ("OU2.5", "under", 0.42),
    ("BTTS", "yes", 0.55),
    ("BTTS", "no", 0.45),
    ("1X2", "home", 0.50),
    ("1X2", "draw", 0.27),
    ("1X2", "away", 0.23),
]


# ---------------------------------------------------------------- results


def test_final_score_prefers_regulation_time() -> None:
    event = {"scores": {"home": 3, "away": 2, "periods": {"ft": {"home": 2, "away": 2}}}}
    assert final_score(event) == (2, 2)
    assert final_score({"scores": {"home": 1, "away": 0}}) == (1, 0)
    assert final_score({"scores": None}) is None


def test_ingest_results_scores_settled_and_marks_cancelled() -> None:
    conn = _conn()
    _fixture(conn, "f1", "2026-11-08T15:00:00Z", event_id="101")
    _fixture(conn, "f2", "2026-11-08T17:00:00Z", event_id="102", home=2, away=3)
    _fixture(conn, "f3", "2026-11-09T17:00:00Z", event_id="103", home=1, away=3)
    seen: list[dict[str, str]] = []

    def fake_fetch(url: str, params: dict[str, str]) -> list[dict[str, object]]:
        seen.append(params)
        return [
            {"id": 101, "status": "settled", "scores": {"home": 2, "away": 1}},
            {"id": 102, "status": "cancelled", "scores": {"home": 0, "away": 0}},
            {"id": 103, "status": "pending", "scores": {"home": 0, "away": 0}},
            {"id": 999, "status": "settled", "scores": {"home": 1, "away": 1}},
        ]

    result = ingest_results_odds_api_io(
        conn,
        api_key="k",
        base_url="https://example.test/v3",
        sport="football",
        league_code="EPL",
        league_slugs=("england-premier-league",),
        now_utc=NOW,
        fetch_json=fake_fetch,
    )

    assert result["status"] == "OK"
    assert result["fixtures_scored"] == 1
    assert result["fixtures_cancelled"] == 1
    assert result["unknown_events"] == 1
    assert seen[0]["status"] == "settled,cancelled"
    assert seen[0]["from"] == "2026-11-03T12:00:00Z"
    rows = {
        row["fixture_id"]: row
        for row in conn.execute("SELECT fixture_id, home_score, away_score, status FROM fixture")
    }
    assert (rows["f1"]["home_score"], rows["f1"]["away_score"]) == (2, 1)
    assert rows["f1"]["status"] == "finished"
    assert rows["f2"]["status"] == "cancelled"
    assert rows["f3"]["home_score"] is None and rows["f3"]["status"] == "scheduled"


def test_ingest_results_reports_api_error_without_writing() -> None:
    conn = _conn()
    _fixture(conn, "f1", "2026-11-08T15:00:00Z", event_id="101")

    def boom(url: str, params: dict[str, str]) -> object:
        raise RuntimeError("network down")

    result = ingest_results_odds_api_io(
        conn,
        api_key="k",
        base_url="https://example.test/v3",
        sport="football",
        league_code="EPL",
        league_slugs=("england-premier-league",),
        fetch_json=boom,
    )

    assert result["status"] == "API_ERROR"
    assert conn.execute("SELECT home_score FROM fixture").fetchone()[0] is None


def test_promote_finished_fixtures_is_idempotent_and_dedupes_other_sources() -> None:
    conn = _conn()
    _fixture(conn, "f1", "2026-11-08T15:00:00Z", scores=(2, 1))
    _fixture(conn, "f2", "2026-11-09T01:00:00Z", home=2, away=3, scores=(0, 0))
    _fixture(conn, "f3", "2026-11-20T15:00:00Z", home=1, away=3)
    # Football-Data already has f2 under its local date, one day earlier.
    conn.execute(
        """
        INSERT INTO match_history (
          source_match_id, date, home_id, away_id, home_score, away_score, tournament, neutral
        )
        VALUES ('football-data:EPL:x', '2026-11-08', 2, 3, 0, 0, 'EPL', 0)
        """
    )

    first = promote_finished_fixtures(conn, leagues=("EPL",))
    second = promote_finished_fixtures(conn, leagues=("EPL",))

    assert first == {"candidates": 2, "inserted": 1, "existing": 0, "duplicates": 1}
    assert second["inserted"] == 0 and second["existing"] == 1
    promoted = conn.execute(
        "SELECT date, home_score, away_score, tournament FROM match_history "
        "WHERE source_match_id = 'fixture:f1'"
    ).fetchone()
    assert tuple(promoted) == ("2026-11-08", 2, 1, "EPL")


def test_bridge_side_drops_promoted_duplicate() -> None:
    conn = _conn()
    _history(conn, "fixture:f1", "2026-11-08")

    removed = drop_promoted_duplicates(
        conn, tournament="EPL", home_id=1, away_id=2, match_date="2026-11-07"
    )

    assert removed == 1
    assert conn.execute("SELECT COUNT(*) FROM match_history").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM team_rating_history").fetchone()[0] == 0


# ------------------------------------------------------------- prediction


def test_club_training_cutoff_rolls_with_new_results() -> None:
    conn = _conn()
    for index in range(40):
        _history(conn, f"old-{index}", "2026-05-01")
    for index in range(5):
        _history(conn, f"new-{index}", "2026-11-10")
    _history(conn, "same-day", "2026-11-13")
    _fixture(conn, "soon", "2026-11-15T15:00:00Z")
    _fixture(conn, "far", "2027-02-01T15:00:00Z", home=1, away=3)

    summary = publish_fixture_predictions(conn, league="EPL", as_of_utc=NOW, horizon_days=21)

    assert summary.predicted_fixtures == 1
    assert summary.skipped_fixtures == 1
    artifact = conn.execute("SELECT trained_until, training_rows FROM model_artifact").fetchone()
    # 2026-06-11 was the frozen cutoff; matches up to yesterday must now count,
    # and a match dated today must not.
    assert artifact["training_rows"] == 45
    assert artifact["trained_until"] == "2026-11-11"
    assert {
        row[0] for row in conn.execute("SELECT DISTINCT fixture_id FROM prediction")
    } == {"soon"}


def test_artifact_is_reused_when_no_new_results() -> None:
    conn = _conn()
    for index in range(40):
        _history(conn, f"old-{index}", "2026-05-01")
    _fixture(conn, "soon", "2026-11-15T15:00:00Z")

    publish_fixture_predictions(conn, league="EPL", as_of_utc=NOW)
    publish_fixture_predictions(
        conn, league="EPL", as_of_utc=datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
    )

    assert conn.execute("SELECT COUNT(*) FROM model_artifact").fetchone()[0] == 1


# -------------------------------------------------------------- snapshots


def test_freeze_window_covers_every_league_once_and_includes_result() -> None:
    conn = _conn()
    _fixture(conn, "epl", "2026-11-13T20:00:00Z", league="EPL")
    _fixture(conn, "mx", "2026-11-14T02:00:00Z", league="LIGA_MX", home=2, away=3)
    _fixture(conn, "later", "2026-11-18T02:00:00Z", league="LIGA_MX", home=1, away=3)
    epl_run = _prediction(conn, "epl", FULL_ROWS)
    mx_run = _prediction(conn, "mx", FULL_ROWS)
    _prediction(conn, "later", FULL_ROWS)

    first = freeze_forecast_snapshot(conn, as_of_utc=NOW, window_hours=36, include_result=True)
    again = freeze_forecast_snapshot(conn, as_of_utc=NOW, window_hours=36, include_result=True)

    assert first.inserted == 6
    assert again.inserted == 0
    rows = conn.execute(
        "SELECT fixture_id, prediction_run_id, market, selection FROM forecast_snapshot "
        "ORDER BY fixture_id, market"
    ).fetchall()
    assert {row["fixture_id"] for row in rows} == {"epl", "mx"}
    assert {row["prediction_run_id"] for row in rows} == {epl_run, mx_run}
    assert ("1X2", "home") in {(row["market"], row["selection"]) for row in rows}


def test_refreeze_after_new_run_does_not_duplicate_fixture_market() -> None:
    conn = _conn()
    _fixture(conn, "epl", "2026-11-14T20:00:00Z")
    _prediction(conn, "epl", FULL_ROWS)
    freeze_forecast_snapshot(conn, as_of_utc=NOW, window_hours=36)
    _prediction(conn, "epl", FULL_ROWS, generated="2026-11-13T12:30:00+00:00")

    summary = freeze_forecast_snapshot(conn, as_of_utc=NOW, window_hours=36)

    assert summary.inserted == 0
    assert conn.execute("SELECT COUNT(*) FROM forecast_snapshot").fetchone()[0] == 2


def test_settle_snapshot_handles_result_market() -> None:
    conn = _conn()
    _fixture(conn, "epl", "2026-11-13T20:00:00Z")
    _prediction(conn, "epl", FULL_ROWS)
    freeze_forecast_snapshot(conn, as_of_utc=NOW, window_hours=36, include_result=True)
    conn.execute("UPDATE fixture SET home_score = 0, away_score = 0 WHERE fixture_id = 'epl'")

    summary = settle_forecast_snapshots(conn)

    assert summary.settled == 3
    settled = {
        row["market"]: (row["result_label"], row["correct"])
        for row in conn.execute("SELECT market, result_label, correct FROM forecast_snapshot")
    }
    assert settled == {"OU2.5": (0, 0), "BTTS": (0, 0), "1X2": (1, 0)}


# ------------------------------------------------------------------- odds


def test_odds_candidates_use_provider_event_id_and_horizon() -> None:
    conn = _conn()
    _fixture(conn, "past", "2020-01-01T00:00:00Z", event_id="1")
    _fixture(conn, "far", "2999-01-01T00:00:00Z", event_id="2", home=2, away=3)
    conn.execute(
        """
        INSERT INTO fixture (fixture_id, kickoff_utc, home_id, away_id, league_code, source,
                             source_event_id, status)
        VALUES ('near', datetime('now', '+3 hours'), 1, 3, 'EPL', 'odds_api_io', '3',
                'scheduled')
        """
    )

    limited = _candidate_fixtures(
        conn, fixture_id=None, closing_window_minutes=None, horizon_hours=48
    )
    unlimited = _candidate_fixtures(conn, fixture_id=None, closing_window_minutes=None)

    assert [row["fixture_id"] for row in limited] == ["near"]
    assert {row["fixture_id"] for row in unlimited} == {"near", "far"}
    assert _direct_event_id(limited[0]) == "3"


# ------------------------------------------------------------------ drift


def test_drift_compares_runs_of_the_same_league_only() -> None:
    conn = _conn()
    coefficients = {
        1: ("EPL", 0.60, 0.10),
        2: ("LIGA_MX", 1.90, -0.40),
        3: ("EPL", 0.61, 0.10),
        4: ("LIGA_MX", 1.91, -0.40),
    }
    for run_id, (scope, a, b) in coefficients.items():
        conn.execute(
            "INSERT INTO ingest_run (run_id, job_name, status, source) "
            "VALUES (?, 'predict-fixtures', 'succeeded', 'fixture')",
            (run_id,),
        )
        conn.execute(
            """
            INSERT INTO data_quality (run_id, check_name, metric_name, metric_value, details_json)
            VALUES (?, 'calibrator_artifact', 'BTTS_platt_coefficients', 100.0, ?)
            """,
            (run_id, json.dumps({"market": "BTTS", "a": a, "b": b, "calibrator_scope": scope})),
        )

    summary = monitor_calibrator_drift(conn, threshold=0.10)

    assert summary.markets_compared == 2
    assert summary.alerts == 0
    assert summary.max_relative_change is not None and summary.max_relative_change < 0.05


# ------------------------------------------------------------ maintenance


def test_prune_keeps_latest_run_and_frozen_run() -> None:
    conn = _conn()
    _fixture(conn, "epl", "2026-11-14T20:00:00Z")
    _prediction(conn, "epl", FULL_ROWS, generated="2026-11-10T12:00:00+00:00")
    frozen_run = _prediction(conn, "epl", FULL_ROWS, generated="2026-11-11T12:00:00+00:00")
    conn.execute(
        """
        INSERT INTO forecast_snapshot (
          fixture_id, prediction_run_id, as_of_utc, market, selection, prob, fair_odds,
          signal_code, model_version, market_validation_status
        )
        VALUES ('epl', ?, '2026-11-11T12:00:00+00:00', 'OU2.5', 'over', 0.58, 1.72,
                'LEAN', 'test', 'NOT_TESTED')
        """,
        (frozen_run,),
    )
    _prediction(conn, "epl", FULL_ROWS, generated="2026-11-12T12:00:00+00:00")
    latest_run = _prediction(conn, "epl", FULL_ROWS, generated="2026-11-13T12:00:00+00:00")

    preview = prune_predictions(conn, dry_run=True)
    summary = prune_predictions(conn)

    assert preview.rows_deleted == summary.rows_deleted == 14
    remaining = {row[0] for row in conn.execute("SELECT DISTINCT run_id FROM prediction")}
    assert remaining == {frozen_run, latest_run}


def test_backup_is_compressed_and_keeps_daily_plus_weekly(tmp_path: Path) -> None:
    import gzip as gz

    db_path = tmp_path / "data" / "scorerecord.db"
    conn = connect(db_path)
    init_db(conn)
    manual = tmp_path / "data" / "backups" / "scorerecord_pre_migration.db"
    manual.parent.mkdir(parents=True)
    manual.write_bytes(b"keep me")

    # 21 daily backups, 2026-11-01 .. 2026-11-21 (Sunday to Saturday).
    for day in range(1, 22):
        backup_database(
            conn, db_path, keep=3, weekly=2, now_utc=datetime(2026, 11, day, 6, 0, tzinfo=UTC)
        )

    names = sorted(path.name for path in manual.parent.iterdir())
    # Newest 3 daily copies, plus the newest copy of the previous ISO week
    # (Nov 9-15); the manual backup is never rotated.
    assert names == [
        "scorerecord_20261115T060000Z.db.gz",
        "scorerecord_20261119T060000Z.db.gz",
        "scorerecord_20261120T060000Z.db.gz",
        "scorerecord_20261121T060000Z.db.gz",
        "scorerecord_pre_migration.db",
    ]
    assert latest_backup_age_hours(db_path) is not None
    restored_path = tmp_path / "restored.db"
    restored_path.write_bytes(gz.decompress((manual.parent / names[-2]).read_bytes()))
    restored = sqlite3.connect(restored_path)
    assert restored.execute("SELECT COUNT(*) FROM team").fetchone()[0] == 0
    restored.close()
    conn.close()


# ----------------------------------------------------------------- health


def test_pipeline_health_flags_open_loop() -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO fixture (fixture_id, kickoff_utc, home_id, away_id, league_code, "
        "source, status) "
        "VALUES ('played', datetime('now', '-5 days'), 1, 2, 'EPL', 'odds_api_io', 'scheduled')"
    )
    _fixture(conn, "postponed", "2026-01-01T15:00:00Z", home=2, away=3)
    conn.execute(
        """
        INSERT INTO fixture (fixture_id, kickoff_utc, home_id, away_id, league_code, source,
                             status)
        VALUES ('soon', datetime('now', '+3 hours'), 1, 3, 'EPL', 'odds_api_io', 'scheduled')
        """
    )
    _prediction(conn, "soon", FULL_ROWS)

    checks = {check.name: check for check in pipeline_health_checks(conn)}

    assert checks["results_freshness"].status == "WARNING"
    assert "EPL=1" in checks["results_freshness"].details
    assert checks["forecast_snapshots"].status == "WARNING"
    assert checks["club_odds_capture"].status == "WARNING"
    assert checks["league_horizon"].status == "WARNING"
    assert "EPL" not in checks["league_horizon"].details.split()
    assert all(check.status in {"OK", "WARNING"} for check in checks.values())


def test_pipeline_health_is_quiet_when_loop_closes() -> None:
    conn = _conn()
    _fixture(conn, "played", "2026-01-01T15:00:00Z", scores=(1, 0))
    promote_finished_fixtures(conn, leagues=("EPL",))

    checks = {check.name: check for check in pipeline_health_checks(conn)}

    assert checks["results_freshness"].status == "OK"
    assert checks["history_freshness"].status == "OK"
    assert checks["forecast_snapshots"].status == "OK"
    assert checks["club_odds_capture"].status == "OK"


def test_league_config_exposes_every_provider_slug() -> None:
    assert league_config("LIGA_MX").odds_api_io_slugs == (
        "mexico-liga-mx-apertura",
        "mexico-liga-mx-clausura",
    )
    assert league_config("EPL").odds_api_io_slugs == ("england-premier-league",)


def test_sync_fixture_scores_uses_bridged_history_within_date_window() -> None:
    conn = _conn()
    # Kickoff 01:00 UTC on the 9th is the evening of the 8th local time.
    _fixture(conn, "night", "2026-11-09T01:00:00Z")
    _fixture(conn, "unplayed", "2026-11-20T01:00:00Z", home=2, away=3)
    _fixture(conn, "no_history", "2026-11-08T15:00:00Z", home=1, away=3)
    _history(conn, "football-data:EPL:2026-2027:2026-11-08:a:b", "2026-11-08")
    _history(conn, "football-data:EPL:2025-2026:2026-03-01:a:b", "2026-03-01")
    summary = sync_fixture_scores_from_history(conn, leagues=("EPL",), now_utc=NOW)

    assert summary == {"pending": 2, "scored": 1}
    rows = {
        row["fixture_id"]: (row["home_score"], row["away_score"], row["status"])
        for row in conn.execute("SELECT fixture_id, home_score, away_score, status FROM fixture")
    }
    assert rows["night"] == (2, 1, "finished")
    assert rows["unplayed"] == (None, None, "scheduled")
    assert rows["no_history"] == (None, None, "scheduled")
    # A fixture scored from Football-Data must not be promoted a second time.
    assert promote_finished_fixtures(conn, leagues=("EPL",))["duplicates"] == 1


def test_service_failures_are_read_from_the_alert_log(tmp_path: Path) -> None:
    from scorerecord.health import db_health_checks

    db_path = tmp_path / "scorerecord.db"
    conn = connect(db_path)
    init_db(conn)
    checks = {c.name: c for c in db_health_checks(conn, include_integrity=False)}
    assert checks["service_failures"].status == "OK"
    assert "sqlite_integrity" not in checks
    recent = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    (tmp_path / "alerts.log").write_text(
        "2020-01-01T00:00:00Z old.service failed\n"
        f"{recent} scorerecord-odds.service failed\n",
        encoding="utf-8",
    )
    checks = {c.name: c for c in db_health_checks(conn)}
    assert checks["service_failures"].status == "WARNING"
    assert checks["service_failures"].value == "1"
    assert "scorerecord-odds.service" in checks["service_failures"].details
    assert "sqlite_integrity" in checks
    conn.close()


def test_apply_release_removes_files_dropped_by_the_new_release(tmp_path: Path) -> None:
    import shutil
    import subprocess
    import tarfile

    import pytest

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    script = Path(__file__).resolve().parents[1] / "deploy" / "apply_release.sh"
    app = tmp_path / "app"
    (app / "data").mkdir(parents=True)
    (app / "data" / "scorerecord.db").write_text("db", encoding="utf-8")
    (app / ".env").write_text("SECRET=1", encoding="utf-8")

    def release(name: str, files: dict[str, str]) -> Path:
        src = tmp_path / name
        for rel, text in files.items():
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            (src / rel).write_text(text, encoding="utf-8")
        archive = tmp_path / f"{name}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for rel in files:
                tar.add(src / rel, arcname=rel)
        return archive

    def apply(archive: Path, *extra: str) -> None:
        subprocess.run(
            [bash, script.as_posix(), archive.as_posix(), app.as_posix(), *extra],
            check=True,
            capture_output=True,
        )

    # The first release lands on a tree that still has a file from an older deploy.
    (app / "src").mkdir()
    (app / "src" / "legacy.py").write_text("old", encoding="utf-8")
    seed = tmp_path / "seed-manifest"
    seed.write_text("src/legacy.py\n", encoding="utf-8")
    apply(release("r1", {"src/a.py": "a", "deploy/systemd/old.timer": "t"}), seed.as_posix())
    assert not (app / "src" / "legacy.py").exists()
    assert (app / "deploy" / "systemd" / "old.timer").exists()

    apply(release("r2", {"src/a.py": "a2"}))
    assert (app / "src" / "a.py").read_text(encoding="utf-8") == "a2"
    assert not (app / "deploy").exists()  # file removed, empty folders pruned
    assert (app / "data" / "scorerecord.db").exists() and (app / ".env").exists()
