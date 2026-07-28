import json
import sqlite3

import pytest

from scorerecord.db import init_db
from scorerecord.predict.fixtures import publish_fixture_predictions


@pytest.fixture
def mem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    yield conn
    conn.close()


def _seed_teams(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO team (team_id, canonical, canonical_key) VALUES (1, 'A', 'a')")
    conn.execute("INSERT INTO team (team_id, canonical, canonical_key) VALUES (2, 'B', 'b')")
    conn.execute(
        """
        INSERT INTO team_rating (team_id, rating_system, rating, as_of)
        VALUES (1, 'elo', 1500, '2020-01-01')
        """
    )
    conn.execute(
        """
        INSERT INTO team_rating (team_id, rating_system, rating, as_of)
        VALUES (2, 'elo', 1500, '2020-01-01')
        """
    )


def _seed_history(
    conn: sqlite3.Connection,
    tournament: str,
    count: int,
    start_id: int = 1,
) -> None:
    for i in range(count):
        source_match_id = f"hist_{tournament}_{start_id + i}"
        conn.execute(
            """
            INSERT INTO match_history (
              source_match_id, date, home_id, away_id, home_score, away_score,
              tournament, neutral
            )
            VALUES (?, '2025-01-01', 1, 2, 1, 1, ?, 0)
            """,
            (source_match_id, tournament),
        )
        conn.execute(
            """
            INSERT INTO team_rating_history (
              team_id, opponent_id, source_match_id, rating_system,
              rating_pre, rating_post, as_of, is_home, expected_result, actual_result
            )
            VALUES
              (1, 2, ?, 'elo', 1500, 1500, '2025-01-01', 1, 0.5, 0.5),
              (2, 1, ?, 'elo', 1500, 1500, '2025-01-01', 0, 0.5, 0.5)
            """,
            (source_match_id, source_match_id),
        )


def _seed_fixture(conn: sqlite3.Connection, fixture_id: str, league_code: str) -> None:
    conn.execute(
        """
        INSERT INTO fixture (
          fixture_id, kickoff_local, kickoff_utc, home_id, away_id,
          league_code, competition, status
        )
        VALUES (?, '2026-08-15 15:00 UTC+1', '2026-08-15T14:00:00Z',
                1, 2, ?, 'Comp', 'scheduled')
        """,
        (fixture_id, league_code),
    )


def _latest_artifact_payload(conn: sqlite3.Connection) -> dict[str, object]:
    row = conn.execute(
        """
        SELECT details_json
        FROM model_artifact
        ORDER BY created_utc DESC, artifact_id DESC
        LIMIT 1
        """
    ).fetchone()
    assert row is not None
    return json.loads(row["details_json"])


def test_league_aware_artifacts_epl(mem_db):
    _seed_teams(mem_db)
    _seed_history(mem_db, "EPL", 50)
    _seed_history(mem_db, "FIFA World Cup", 30)
    _seed_fixture(mem_db, "f1", "EPL")

    summary = publish_fixture_predictions(mem_db, league="EPL")
    assert summary.predicted_fixtures == 1

    payload = _latest_artifact_payload(mem_db)
    assert payload["training_policy"]["league_code"] == "EPL"
    assert payload["training_policy"]["training_tournament"] == "EPL"
    assert payload["training_policy"]["training_scope"] == "club"
    assert payload["training_rows"] == 50

    calibration_json = payload["calibration"]
    for market in ["OU1.5", "OU2.5", "OU3.5", "BTTS"]:
        assert calibration_json["coefficients"][market]["status"] == (
            "CALIBRATOR_INSUFFICIENT_DATA"
        )
        assert calibration_json["coefficients"][market]["calibrator_scope"] == "EPL"


def test_league_aware_artifacts_world_cup(mem_db):
    _seed_teams(mem_db)
    _seed_history(mem_db, "EPL", 50)
    _seed_history(mem_db, "FIFA World Cup", 30)
    _seed_fixture(mem_db, "f1", "WORLD_CUP_2026")

    summary = publish_fixture_predictions(mem_db, league="WORLD_CUP_2026")
    assert summary.predicted_fixtures == 1

    payload = _latest_artifact_payload(mem_db)
    assert payload["training_policy"]["league_code"] == "WORLD_CUP_2026"
    assert payload["training_policy"]["training_scope"] == "international"
    assert payload["training_rows"] == 30


def test_default_prediction_scope_ignores_epl_fixtures(mem_db):
    _seed_teams(mem_db)
    _seed_history(mem_db, "EPL", 50)
    _seed_history(mem_db, "FIFA World Cup", 30)
    _seed_fixture(mem_db, "epl_fixture", "EPL")
    _seed_fixture(mem_db, "wc_fixture", "WORLD_CUP_2026")

    summary = publish_fixture_predictions(mem_db)

    assert summary.predicted_fixtures == 1
    predicted = mem_db.execute("SELECT DISTINCT fixture_id FROM prediction").fetchall()
    assert [row["fixture_id"] for row in predicted] == ["wc_fixture"]


def test_different_artifact_ids(mem_db):
    _seed_teams(mem_db)
    _seed_history(mem_db, "EPL", 50)
    _seed_history(mem_db, "FIFA World Cup", 30)
    _seed_fixture(mem_db, "f1", "EPL")
    _seed_fixture(mem_db, "f2", "WORLD_CUP_2026")
    publish_fixture_predictions(mem_db, league="EPL")
    publish_fixture_predictions(mem_db, league="WORLD_CUP_2026")

    rows = mem_db.execute("SELECT artifact_id FROM model_artifact").fetchall()
    assert len(rows) == 2
    assert rows[0]["artifact_id"] != rows[1]["artifact_id"]
