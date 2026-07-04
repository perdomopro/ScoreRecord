import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from scorerecord.aliases import seed_team_aliases
from scorerecord.cli import main
from scorerecord.db import connect, init_db
from scorerecord.ingest import worldcup
from scorerecord.ingest.worldcup import (
    _fixture_id,
    _is_placeholder,
    ingest_worldcup_2026,
    resolve_fixture_teams,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    seed_team_aliases(conn)
    return conn


def test_worldcup_knockout_placeholders_are_not_teams() -> None:
    for name in ["W73", "L101", "1B", "2L", "3A/B/C/D/F", "3C/3D/3F/3G/3H"]:
        assert _is_placeholder(name)


def test_worldcup_real_team_names_are_not_placeholders() -> None:
    for name in ["Brazil", "Canada", "South Africa", "Cote d'Ivoire"]:
        assert not _is_placeholder(name)


def test_resolve_fixture_teams_uses_aliases() -> None:
    conn = _conn()
    conn.execute(
        """
        INSERT INTO fixture (fixture_id, kickoff_utc, home_placeholder, away_placeholder)
        VALUES ('knockout-1', '2026-07-04T20:00:00+00:00', 'W73', 'L101')
        """
    )

    resolve_fixture_teams(
        conn,
        fixture_id="knockout-1",
        home="MEX",
        away="United States of America",
    )

    row = conn.execute(
        """
        SELECT
          ht.canonical AS home,
          at.canonical AS away,
          f.home_placeholder,
          f.away_placeholder,
          f.status
        FROM fixture f
        JOIN team ht ON ht.team_id = f.home_id
        JOIN team at ON at.team_id = f.away_id
        WHERE f.fixture_id = 'knockout-1'
        """
    ).fetchone()
    assert row["home"] == "Mexico"
    assert row["away"] == "United States"
    assert row["home_placeholder"] is None
    assert row["away_placeholder"] is None
    assert row["status"] == "scheduled"


def test_resolve_fixture_teams_rejects_unknown_team() -> None:
    conn = _conn()
    conn.execute(
        """
        INSERT INTO fixture (fixture_id, kickoff_utc, home_placeholder, away_placeholder)
        VALUES ('knockout-2', '2026-07-04T20:00:00+00:00', 'W73', 'L101')
        """
    )

    try:
        resolve_fixture_teams(
            conn,
            fixture_id="knockout-2",
            home="Definitely Not A Team",
            away="Mexico",
        )
    except ValueError as exc:
        assert "Unknown team alias" in str(exc)
    else:
        raise AssertionError("Expected unknown team alias to be rejected")


def test_ingest_worldcup_does_not_overwrite_manual_resolution_with_placeholder() -> None:
    conn = _conn()
    payload = {
        "matches": [
            {
                "round": "Round of 32",
                "num": 73,
                "date": "2026-06-28",
                "time": "12:00 UTC-7",
                "team1": "W50",
                "team2": "W51",
                "ground": "Los Angeles",
            }
        ]
    }

    with _fake_worldcup_payload(payload):
        ingest_worldcup_2026(conn, Path("unused"))
    resolve_fixture_teams(
        conn,
        fixture_id="wc2026-match-073",
        home="Mexico",
        away="Canada",
    )

    with _fake_worldcup_payload(payload):
        ingest_worldcup_2026(conn, Path("unused"))

    row = conn.execute(
        """
        SELECT ht.canonical AS home, at.canonical AS away,
               f.home_placeholder, f.away_placeholder
        FROM fixture f
        JOIN team ht ON ht.team_id = f.home_id
        JOIN team at ON at.team_id = f.away_id
        WHERE f.fixture_id = 'wc2026-match-073'
        """
    ).fetchone()
    assert row["home"] == "Mexico"
    assert row["away"] == "Canada"
    assert row["home_placeholder"] is None
    assert row["away_placeholder"] is None


def test_fixture_id_stable_when_placeholder_becomes_real_team() -> None:
    conn = _conn()
    placeholder_payload = {
        "matches": [
            {
                "round": "Round of 32",
                "num": 74,
                "date": "2026-06-29",
                "time": "12:00 UTC-5",
                "team1": "W52",
                "team2": "W53",
                "ground": "Dallas",
            }
        ]
    }
    resolved_payload = {
        "matches": [
            {
                "round": "Round of 32",
                "num": 74,
                "date": "2026-06-29",
                "time": "12:00 UTC-5",
                "team1": "Mexico",
                "team2": "Canada",
                "ground": "Dallas",
            }
        ]
    }

    with _fake_worldcup_payload(placeholder_payload):
        ingest_worldcup_2026(conn, Path("unused"))
    with _fake_worldcup_payload(resolved_payload):
        ingest_worldcup_2026(conn, Path("unused"))

    rows = conn.execute(
        """
        SELECT f.fixture_id, ht.canonical AS home, at.canonical AS away
        FROM fixture f
        LEFT JOIN team ht ON ht.team_id = f.home_id
        LEFT JOIN team at ON at.team_id = f.away_id
        """
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["fixture_id"] == "wc2026-match-074"
    assert rows[0]["home"] == "Mexico"
    assert rows[0]["away"] == "Canada"


def test_fixture_id_uses_match_number_when_available() -> None:
    assert _fixture_id({"num": 101, "team1": "W97", "team2": "W98"}, 1) == "wc2026-match-101"


def test_resolve_fixture_teams_records_manual_run() -> None:
    with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / "scorerecord.db"
        raw_dir = Path(tmp) / "raw"
        assert main(["--db-path", str(db_path), "--raw-dir", str(raw_dir), "init-db"]) == 0
        with connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO fixture (
                  fixture_id, kickoff_utc, home_placeholder, away_placeholder
                )
                VALUES ('manual-1', '2026-07-04T20:00:00+00:00', 'W73', 'L101')
                """
            )
            conn.commit()

        assert (
            main(
                [
                    "--db-path",
                    str(db_path),
                    "--raw-dir",
                    str(raw_dir),
                    "resolve-fixture-teams",
                    "--fixture-id",
                    "manual-1",
                    "--home",
                    "Mexico",
                    "--away",
                    "Canada",
                ]
            )
            == 0
        )

        with connect(db_path) as conn:
            run = conn.execute(
                """
                SELECT run_id, status
                FROM ingest_run
                WHERE job_name = 'resolve-fixture-teams'
                """
            ).fetchone()
            quality = conn.execute(
                """
                SELECT details_json
                FROM data_quality
                WHERE run_id = ?
                  AND check_name = 'fixture_resolution'
                  AND metric_name = 'manual_resolve'
                """,
                (run["run_id"],),
            ).fetchone()
        assert run["status"] == "succeeded"
        assert json.loads(quality["details_json"]) == {
            "away": "Canada",
            "fixture_id": "manual-1",
            "home": "Mexico",
        }


class _fake_worldcup_payload:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.original = worldcup.fetch_text

    def __enter__(self) -> None:
        def fake_fetch_text(*args: object, **kwargs: object) -> str:
            del args, kwargs
            return json.dumps(self.payload)

        worldcup.fetch_text = fake_fetch_text

    def __exit__(self, *exc_info: object) -> None:
        del exc_info
        worldcup.fetch_text = self.original
