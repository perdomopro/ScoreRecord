from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from scorerecord.db import schema_version
from scorerecord.league_config import GUI_LEAGUE_ORDER
from scorerecord.maintenance import latest_backup_age_hours

RESULT_GRACE_HOURS = 48
# Fixtures still unscored after this long were postponed or re-issued by the
# provider under another id; they stop counting as a freshness problem.
RESULT_WINDOW_DAYS = 21
HISTORY_LAG_DAYS = 3
BACKUP_MAX_AGE_HOURS = 48
SNAPSHOT_WINDOW_HOURS = 20
ODDS_WINDOW_HOURS = 24


@dataclass(frozen=True)
class HealthCheck:
    name: str
    status: str
    value: str
    details: str = ""

    def as_row(self) -> str:
        suffix = f" details={self.details}" if self.details else ""
        return f"{self.status} {self.name}: {self.value}{suffix}"


@dataclass(frozen=True)
class FailedRun:
    run_id: int
    job_name: str
    source: str | None
    started_utc: str
    finished_utc: str | None
    message: str | None

    def as_row(self) -> str:
        source = self.source or "na"
        finished = self.finished_utc or "na"
        message = (self.message or "").replace("\n", " ").strip() or "na"
        return (
            f"run_id={self.run_id} job={self.job_name} source={source} "
            f"started={self.started_utc} finished={finished} message={message}"
        )


@dataclass(frozen=True)
class FixtureOpsSummary:
    total: int
    finished: int
    missing_kickoff_utc: int
    future_resolved: int
    future_unresolved: int
    past_unfinished: int
    rows: list[sqlite3.Row]

    def as_row(self) -> str:
        return (
            f"fixtures={self.total} finished={self.finished} "
            f"missing_kickoff_utc={self.missing_kickoff_utc} "
            f"future_resolved={self.future_resolved} "
            f"future_unresolved={self.future_unresolved} "
            f"past_unfinished={self.past_unfinished}"
        )


def db_health_checks(conn: sqlite3.Connection) -> list[HealthCheck]:
    checks = [
        _integrity_check(conn),
        _foreign_key_check(conn),
        HealthCheck("schema_version", "OK", str(schema_version(conn))),
        _fixture_kickoff_check(conn),
        _prediction_artifact_check(conn),
        _prediction_forward_check(conn),
        _odds_closing_flag_check(conn),
        _snapshot_coverage_check(conn),
        _failed_run_check(conn),
        *pipeline_health_checks(conn),
    ]
    return checks


def pipeline_health_checks(conn: sqlite3.Connection) -> list[HealthCheck]:
    """Freshness checks for the daily forecast loop.

    These report WARNING, never FAIL: they describe a loop that is not closing,
    which the refresh job itself is supposed to repair.
    """
    return [
        _results_freshness_check(conn),
        _history_freshness_check(conn),
        _training_freshness_check(conn),
        _league_horizon_check(conn),
        _forecast_snapshot_check(conn),
        _club_odds_check(conn),
        _backup_check(conn),
    ]


def _club_placeholders() -> str:
    return ",".join("?" for _ in GUI_LEAGUE_ORDER)


def _results_freshness_check(conn: sqlite3.Connection) -> HealthCheck:
    rows = conn.execute(
        f"""
        SELECT league_code, COUNT(*) AS n
        FROM fixture
        WHERE league_code IN ({_club_placeholders()})
          AND kickoff_utc IS NOT NULL
          AND datetime(kickoff_utc) < datetime('now', ?)
          AND datetime(kickoff_utc) >= datetime('now', ?)
          AND home_score IS NULL
          AND status != 'cancelled'
        GROUP BY league_code
        ORDER BY league_code
        """,
        (*GUI_LEAGUE_ORDER, f"-{RESULT_GRACE_HOURS} hours", f"-{RESULT_WINDOW_DAYS} days"),
    ).fetchall()
    total = sum(int(row["n"]) for row in rows)
    details = " ".join(f"{row['league_code']}={row['n']}" for row in rows)
    return HealthCheck(
        "results_freshness",
        "OK" if total == 0 else "WARNING",
        f"played_without_score={total}",
        details,
    )


def _history_freshness_check(conn: sqlite3.Connection) -> HealthCheck:
    """Finished fixtures must reach match_history, which is what models train on."""
    lagging = []
    for league in GUI_LEAGUE_ORDER:
        row = conn.execute(
            """
            SELECT
              (SELECT MAX(substr(kickoff_utc, 1, 10)) FROM fixture
                WHERE league_code = ? AND home_score IS NOT NULL) AS last_result,
              (SELECT MAX(substr(date, 1, 10)) FROM match_history
                WHERE tournament = ?) AS last_history
            """,
            (league, league),
        ).fetchone()
        if not row["last_result"]:
            continue
        gap = conn.execute(
            "SELECT julianday(?) - julianday(COALESCE(?, '1970-01-01'))",
            (row["last_result"], row["last_history"]),
        ).fetchone()[0]
        if gap is not None and float(gap) > HISTORY_LAG_DAYS:
            lagging.append(f"{league}:history={row['last_history']},results={row['last_result']}")
    return HealthCheck(
        "history_freshness",
        "OK" if not lagging else "WARNING",
        f"lagging_leagues={len(lagging)}",
        " ".join(lagging),
    )


def _training_freshness_check(conn: sqlite3.Connection) -> HealthCheck:
    """The newest artifact of each league must cover its newest history."""
    artifacts: dict[str, str] = {}
    for row in conn.execute(
        """
        SELECT trained_until, details_json
        FROM model_artifact
        ORDER BY created_utc DESC, artifact_id DESC
        LIMIT 400
        """
    ).fetchall():
        try:
            details = json.loads(str(row["details_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        policy = details.get("training_policy")
        league = str(policy.get("league_code") or "") if isinstance(policy, dict) else ""
        if league and league not in artifacts:
            artifacts[league] = str(row["trained_until"])
    stale = []
    for league in GUI_LEAGUE_ORDER:
        if not _league_has_future_fixtures(conn, league):
            continue
        last_history = conn.execute(
            "SELECT MAX(substr(date, 1, 10)) FROM match_history WHERE tournament = ?",
            (league,),
        ).fetchone()[0]
        trained_until = artifacts.get(league)
        if not last_history or not trained_until:
            continue
        gap = conn.execute(
            "SELECT julianday(?) - julianday(?)", (last_history, trained_until)
        ).fetchone()[0]
        if gap is not None and float(gap) > HISTORY_LAG_DAYS:
            stale.append(f"{league}:trained_until={trained_until},history={last_history}")
    return HealthCheck(
        "training_freshness",
        "OK" if not stale else "WARNING",
        f"stale_leagues={len(stale)}",
        " ".join(stale),
    )


def _league_has_future_fixtures(conn: sqlite3.Connection, league: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM fixture
        WHERE league_code = ?
          AND kickoff_utc IS NOT NULL
          AND datetime(kickoff_utc) > datetime('now')
          AND status != 'cancelled'
        LIMIT 1
        """,
        (league,),
    ).fetchone()
    return row is not None


def _league_horizon_check(conn: sqlite3.Connection) -> HealthCheck:
    empty = [
        league for league in GUI_LEAGUE_ORDER if not _league_has_future_fixtures(conn, league)
    ]
    return HealthCheck(
        "league_horizon",
        "OK" if not empty else "WARNING",
        f"leagues_without_future_fixtures={len(empty)}",
        " ".join(empty),
    )


def _forecast_snapshot_check(conn: sqlite3.Connection) -> HealthCheck:
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n
        FROM fixture f
        WHERE f.league_code IN ({_club_placeholders()})
          AND f.kickoff_utc IS NOT NULL
          AND datetime(f.kickoff_utc) > datetime('now')
          AND datetime(f.kickoff_utc) <= datetime('now', ?)
          AND f.status != 'cancelled'
          AND EXISTS (SELECT 1 FROM prediction p WHERE p.fixture_id = f.fixture_id)
          AND NOT EXISTS (SELECT 1 FROM forecast_snapshot fs WHERE fs.fixture_id = f.fixture_id)
        """,
        (*GUI_LEAGUE_ORDER, f"+{SNAPSHOT_WINDOW_HOURS} hours"),
    ).fetchone()
    missing = int(row["n"] or 0)
    return HealthCheck(
        "forecast_snapshots",
        "OK" if missing == 0 else "WARNING",
        f"upcoming_without_snapshot={missing}",
        f"window_hours={SNAPSHOT_WINDOW_HOURS}",
    )


def _club_odds_check(conn: sqlite3.Connection) -> HealthCheck:
    row = conn.execute(
        f"""
        SELECT
          COUNT(*) AS upcoming,
          SUM(
            CASE WHEN EXISTS (
              SELECT 1 FROM odds_snapshots os WHERE os.fixture_id = f.fixture_id
            ) THEN 1 ELSE 0 END
          ) AS with_odds
        FROM fixture f
        WHERE f.league_code IN ({_club_placeholders()})
          AND f.home_id IS NOT NULL
          AND f.away_id IS NOT NULL
          AND f.kickoff_utc IS NOT NULL
          AND datetime(f.kickoff_utc) > datetime('now')
          AND datetime(f.kickoff_utc) <= datetime('now', ?)
          AND f.status != 'cancelled'
        """,
        (*GUI_LEAGUE_ORDER, f"+{ODDS_WINDOW_HOURS} hours"),
    ).fetchone()
    upcoming = int(row["upcoming"] or 0)
    with_odds = int(row["with_odds"] or 0)
    return HealthCheck(
        "club_odds_capture",
        "OK" if upcoming == 0 or with_odds > 0 else "WARNING",
        f"with_odds={with_odds}/{upcoming}",
        f"window_hours={ODDS_WINDOW_HOURS}",
    )


def _backup_check(conn: sqlite3.Connection) -> HealthCheck:
    db_file = next(
        (str(row[2]) for row in conn.execute("PRAGMA database_list") if row[1] == "main"),
        "",
    )
    if not db_file:
        return HealthCheck("backup_age", "OK", "in_memory_database")
    age = latest_backup_age_hours(Path(db_file))
    if age is None:
        return HealthCheck("backup_age", "WARNING", "no_backup_found")
    return HealthCheck(
        "backup_age",
        "OK" if age <= BACKUP_MAX_AGE_HOURS else "WARNING",
        f"hours={age:.1f}",
        f"max_hours={BACKUP_MAX_AGE_HOURS}",
    )


def _integrity_check(conn: sqlite3.Connection) -> HealthCheck:
    rows = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
    ok = rows == ["ok"]
    return HealthCheck("sqlite_integrity", "OK" if ok else "FAIL", ",".join(rows))


def _foreign_key_check(conn: sqlite3.Connection) -> HealthCheck:
    rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    return HealthCheck(
        "foreign_keys",
        "OK" if not rows else "FAIL",
        str(len(rows)),
        "violations",
    )


def _fixture_kickoff_check(conn: sqlite3.Connection) -> HealthCheck:
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS fixtures,
          SUM(CASE WHEN kickoff_utc IS NULL THEN 1 ELSE 0 END) AS missing,
          SUM(
            CASE
              WHEN home_id IS NOT NULL AND away_id IS NOT NULL AND kickoff_utc IS NULL
              THEN 1 ELSE 0
            END
          ) AS resolved_missing
        FROM fixture
        """
    ).fetchone()
    missing = int(row["missing"] or 0)
    resolved_missing = int(row["resolved_missing"] or 0)
    status = "OK" if resolved_missing == 0 else "WARNING"
    return HealthCheck(
        "fixture_kickoff_utc",
        status,
        f"missing={missing}/{int(row['fixtures'] or 0)}",
        f"resolved_missing={resolved_missing}",
    )


def _prediction_artifact_check(conn: sqlite3.Connection) -> HealthCheck:
    latest_run_id = _latest_predict_fixture_run(conn)
    if latest_run_id is None:
        return HealthCheck("prediction_artifact_id", "OK", "no_prediction_runs")
    latest = conn.execute(
        """
        SELECT
          COUNT(*) AS predictions,
          SUM(CASE WHEN artifact_id IS NULL THEN 1 ELSE 0 END) AS missing
        FROM prediction
        WHERE run_id = ?
        """,
        (latest_run_id,),
    ).fetchone()
    latest_predictions = int(latest["predictions"] or 0)
    latest_missing = int(latest["missing"] or 0)
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS predictions,
          SUM(CASE WHEN artifact_id IS NULL THEN 1 ELSE 0 END) AS missing
        FROM prediction
        """
    ).fetchone()
    predictions = int(row["predictions"] or 0)
    return HealthCheck(
        "prediction_artifact_id",
        "OK" if latest_missing == 0 else "WARNING",
        f"latest_run={latest_run_id} missing={latest_missing}/{latest_predictions}",
        f"legacy_missing={int(row['missing'] or 0)}/{predictions}",
    )


def _prediction_forward_check(conn: sqlite3.Connection) -> HealthCheck:
    latest_run_id = _latest_predict_fixture_run(conn)
    if latest_run_id is None:
        return HealthCheck("prediction_generated_before_kickoff", "OK", "no_prediction_runs")
    latest_row = conn.execute(
        """
        SELECT COUNT(*) AS late_predictions
        FROM prediction p
        JOIN fixture f ON f.fixture_id = p.fixture_id
        WHERE p.run_id = ?
          AND f.kickoff_utc IS NOT NULL
          AND p.generated_utc > f.kickoff_utc
        """,
        (latest_run_id,),
    ).fetchone()
    row = conn.execute(
        """
        SELECT COUNT(*) AS late_predictions
        FROM prediction p
        JOIN fixture f ON f.fixture_id = p.fixture_id
        WHERE f.kickoff_utc IS NOT NULL
          AND p.generated_utc > f.kickoff_utc
        """
    ).fetchone()
    late = int(latest_row["late_predictions"] or 0)
    legacy_late = int(row["late_predictions"] or 0)
    return HealthCheck(
        "prediction_generated_before_kickoff",
        "OK" if late == 0 else "WARNING",
        f"latest_run={latest_run_id} late={late}",
        f"legacy_late={legacy_late}",
    )


def _latest_predict_fixture_run(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        """
        SELECT run_id
        FROM ingest_run
        WHERE job_name = 'predict-fixtures'
          AND status = 'succeeded'
        ORDER BY run_id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["run_id"]) if row else None


def _odds_closing_flag_check(conn: sqlite3.Connection) -> HealthCheck:
    migration = conn.execute(
        """
        SELECT applied_utc
        FROM schema_migration
        WHERE version = 3
        """
    ).fetchone()
    applied_utc = migration["applied_utc"] if migration else None
    post_migration_filter = ""
    params: tuple[object, ...] = ()
    if applied_utc:
        post_migration_filter = "AND datetime(captured_utc) >= datetime(?)"
        params = (applied_utc,)
    row = conn.execute(
        """
        SELECT COUNT(*) AS flagged
        FROM odds
        WHERE is_closing = 1
        """
    ).fetchone()
    flagged = int(row["flagged"] or 0)
    post_row = conn.execute(
        f"""
        SELECT COUNT(*) AS flagged
        FROM odds
        WHERE is_closing = 1
          {post_migration_filter}
        """,
        params,
    ).fetchone()
    post_migration = int(post_row["flagged"] or 0)
    return HealthCheck(
        "raw_odds_closing_flags",
        "OK" if post_migration == 0 else "WARNING",
        f"post_migration={post_migration}",
        f"legacy_total={flagged}; derived closing should come from odds_snapshots",
    )


def _snapshot_coverage_check(conn: sqlite3.Connection) -> HealthCheck:
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS rows,
          COUNT(DISTINCT fixture_id) AS fixtures,
          COUNT(DISTINCT bookmaker) AS bookmakers
        FROM odds_snapshots
        """
    ).fetchone()
    rows = int(row["rows"] or 0)
    status = "OK" if rows > 0 else "WARNING"
    return HealthCheck(
        "odds_snapshots",
        status,
        f"rows={rows}",
        f"fixtures={int(row['fixtures'] or 0)} bookmakers={int(row['bookmakers'] or 0)}",
    )


def _failed_run_check(conn: sqlite3.Connection) -> HealthCheck:
    row = conn.execute(
        """
        SELECT COUNT(*) AS failed
        FROM ingest_run
        WHERE status = 'failed'
          AND started_utc >= datetime('now', '-7 days')
        """
    ).fetchone()
    failed = int(row["failed"] or 0)
    return HealthCheck("recent_failed_runs", "OK" if failed == 0 else "WARNING", str(failed))


def recent_failed_runs(
    conn: sqlite3.Connection,
    *,
    days: int = 7,
    limit: int = 10,
) -> list[FailedRun]:
    rows = conn.execute(
        """
        SELECT run_id, job_name, source, started_utc, finished_utc, message
        FROM ingest_run
        WHERE status = 'failed'
          AND started_utc >= datetime('now', ?)
        ORDER BY run_id DESC
        LIMIT ?
        """,
        (f"-{days} days", limit),
    ).fetchall()
    return [
        FailedRun(
            run_id=int(row["run_id"]),
            job_name=str(row["job_name"]),
            source=row["source"],
            started_utc=str(row["started_utc"]),
            finished_utc=row["finished_utc"],
            message=row["message"],
        )
        for row in rows
    ]


def fixture_ops_summary(conn: sqlite3.Connection, *, limit: int = 12) -> FixtureOpsSummary:
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN home_score IS NOT NULL AND away_score IS NOT NULL THEN 1 ELSE 0 END)
            AS finished,
          SUM(CASE WHEN kickoff_utc IS NULL THEN 1 ELSE 0 END) AS missing_kickoff_utc,
          SUM(
            CASE
              WHEN kickoff_utc IS NOT NULL
               AND datetime(kickoff_utc) > datetime('now')
               AND home_score IS NULL
               AND away_score IS NULL
               AND home_id IS NOT NULL
               AND away_id IS NOT NULL
              THEN 1 ELSE 0
            END
          ) AS future_resolved,
          SUM(
            CASE
              WHEN kickoff_utc IS NOT NULL
               AND datetime(kickoff_utc) > datetime('now')
               AND home_score IS NULL
               AND away_score IS NULL
               AND (home_id IS NULL OR away_id IS NULL)
              THEN 1 ELSE 0
            END
          ) AS future_unresolved,
          SUM(
            CASE
              WHEN kickoff_utc IS NOT NULL
               AND datetime(kickoff_utc) <= datetime('now')
               AND home_score IS NULL
               AND away_score IS NULL
              THEN 1 ELSE 0
            END
          ) AS past_unfinished
        FROM fixture
        """
    ).fetchone()
    rows = conn.execute(
        """
        SELECT
          fixture_id, kickoff_utc, kickoff_local, home_placeholder, away_placeholder,
          home_id, away_id, home_score, away_score
        FROM fixture
        WHERE home_score IS NULL
          AND away_score IS NULL
        ORDER BY
          CASE WHEN kickoff_utc IS NULL THEN 1 ELSE 0 END,
          kickoff_utc,
          kickoff_local,
          fixture_id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return FixtureOpsSummary(
        total=int(row["total"] or 0),
        finished=int(row["finished"] or 0),
        missing_kickoff_utc=int(row["missing_kickoff_utc"] or 0),
        future_resolved=int(row["future_resolved"] or 0),
        future_unresolved=int(row["future_unresolved"] or 0),
        past_unfinished=int(row["past_unfinished"] or 0),
        rows=rows,
    )
