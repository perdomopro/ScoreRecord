#!/usr/bin/env python3
"""Phase 13 production-readiness audit.

The audit is intentionally read-only by default. It checks that daily multi-league
forecast artifacts are scoped by league and that predictions do not mix model artifacts
across competitions.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

AUDIT_VERSION = "1.1.0"
DEFAULT_LEAGUES = ("EPL", "WORLD_CUP_2026")


@dataclass(frozen=True)
class Finding:
    severity: str
    step: str
    title: str
    details: str


class AuditContext:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.db_path = Path(os.environ.get("SCORERECORD_DB_PATH", "data/scorerecord.db"))
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.findings: list[Finding] = []
        self.started = time.time()
        self.out_dir = Path("outputs/benchmarks")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.evidence_files: list[str] = []
        self.timings: dict[str, float] = {}

    def close(self) -> None:
        self.conn.close()

    def record(self, severity: str, step: str, title: str, details: str) -> None:
        finding = Finding(severity=severity, step=step, title=title, details=details)
        self.findings.append(finding)
        print(f"[{severity}] {step}: {title} - {details}")
        if self.args.fail_fast and severity in {"CRITICAL", "ERROR"}:
            self.write_report()
            raise SystemExit(1)

    def write_evidence(self, name: str, payload: Any) -> None:
        path = self.out_dir / name
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        self.evidence_files.append(name)

    def write_report(self) -> None:
        grouped = {
            severity: [f for f in self.findings if f.severity == severity]
            for severity in ("CRITICAL", "ERROR", "WARNING", "INFO")
        }
        verdict = "FAILED" if grouped["CRITICAL"] or grouped["ERROR"] else "PASSED"
        lines = [
            "# Phase 13.4.2 - Final Production Validation & Integration Audit",
            "",
            f"- audit_version: `{AUDIT_VERSION}`",
            f"- verdict: `{verdict}`",
            f"- db_path: `{self.db_path}`",
            f"- generated_utc: `{datetime.now(UTC).isoformat(timespec='seconds')}`",
            f"- duration_seconds: `{time.time() - self.started:.2f}`",
            "",
        ]
        for severity, findings in grouped.items():
            lines.append(f"## {severity}")
            if not findings:
                lines.append("None.")
            for finding in findings:
                lines.append(
                    f"- **{finding.step}**: {finding.title} - {finding.details}"
                )
            lines.append("")
        lines.append("## Evidence")
        for name in self.evidence_files:
            lines.append(f"- `outputs/benchmarks/{name}`")
        lines.extend(["", "## Timings"])
        for name, seconds in self.timings.items():
            lines.append(f"- `{name}`: `{seconds:.2f}s`")
        report_path = self.out_dir / "phase_13_4_2_final_report.md"
        report_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"Final report written to {report_path}")


def _run_step(ctx: AuditContext, name: str, func: Any) -> None:
    started = time.time()
    try:
        func(ctx)
    finally:
        ctx.timings[name] = time.time() - started


def _json_obj(value: object) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _artifact_policy(row: sqlite3.Row) -> dict[str, Any]:
    details = _json_obj(row["details_json"])
    params = _json_obj(row["params_json"])
    policy = details.get("training_policy") or params.get("training_policy") or {}
    if not isinstance(policy, dict):
        return {}
    normalized = dict(policy)
    if "league_code" not in normalized:
        tournament = str(
            normalized.get("tournament") or normalized.get("training_tournament") or ""
        )
        if tournament == "FIFA World Cup 2026":
            normalized["league_code"] = "WORLD_CUP_2026"
        elif tournament:
            normalized["league_code"] = tournament
    return normalized


def _league_history_code(league: str) -> str:
    try:
        from scorerecord.ingest.football_data import FOOTBALL_DATA_LEAGUES
    except ImportError:
        return league
    return str(FOOTBALL_DATA_LEAGUES.get(league, league))


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone()
    return bool(row and int(row[0]) > 0)


def _latest_artifacts_by_league(ctx: AuditContext) -> dict[str, dict[str, Any]]:
    rows = ctx.conn.execute(
        """
        SELECT artifact_id, model_version, params_json, calibration_json, details_json,
               training_rows, trained_until, created_utc
        FROM model_artifact
        ORDER BY created_utc DESC, artifact_id DESC
        """
    ).fetchall()
    by_league: dict[str, dict[str, Any]] = {}
    for row in rows:
        policy = _artifact_policy(row)
        league_code = str(policy.get("league_code") or policy.get("training_tournament") or "")
        if league_code and league_code not in by_league:
            by_league[league_code] = {
                "artifact_id": row["artifact_id"],
                "model_version": row["model_version"],
                "created_utc": row["created_utc"],
                "training_rows": row["training_rows"],
                "trained_until": row["trained_until"],
                "training_policy": policy,
            }
    return by_league


def run_artifact_separation(ctx: AuditContext) -> None:
    by_league = _latest_artifacts_by_league(ctx)

    for league in ctx.args.leagues:
        if league not in by_league:
            ctx.record("WARNING", "Artifact Separation", "Missing artifact", league)

    ids = [data["artifact_id"] for data in by_league.values()]
    if len(ids) != len(set(ids)):
        ctx.record("CRITICAL", "Artifact Separation", "Shared artifact id", str(by_league))

    ctx.write_evidence("artifact_audit.json", by_league)


def run_training_scope_audit(ctx: AuditContext) -> None:
    artifacts = _latest_artifacts_by_league(ctx)
    evidence: dict[str, Any] = {}
    for league in ctx.args.leagues:
        if league == "WORLD_CUP_2026":
            rows = ctx.conn.execute(
                """
                SELECT MIN(date) AS min_date, MAX(date) AS max_date,
                       COUNT(*) AS match_count,
                       COUNT(DISTINCT home_id) + COUNT(DISTINCT away_id) AS rough_team_count
                FROM match_history
                WHERE tournament NOT IN ('EPL', 'LA_LIGA', 'SERIE_A', 'BUNDESLIGA',
                                         'LIGUE_1', 'CHAMPIONSHIP')
                """
            ).fetchone()
            mixed_count = 0
        else:
            rows = ctx.conn.execute(
                """
                SELECT MIN(date) AS min_date, MAX(date) AS max_date,
                       COUNT(*) AS match_count,
                       COUNT(DISTINCT home_id) + COUNT(DISTINCT away_id) AS rough_team_count
                FROM match_history
                WHERE tournament = ?
                """,
                (league,),
            ).fetchone()
            mixed_count = ctx.conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM match_history
                WHERE tournament != ?
                  AND source_match_id IN (
                    SELECT source_match_id FROM match_history WHERE tournament = ?
                  )
                """,
                (league, league),
            ).fetchone()["c"]
        artifact = artifacts.get(league)
        training_rows = int(artifact["training_rows"]) if artifact else None
        match_count = int(rows["match_count"] or 0)
        evidence[league] = {
            "min_date": rows["min_date"],
            "max_date": rows["max_date"],
            "match_count": match_count,
            "rough_team_count": int(rows["rough_team_count"] or 0),
            "artifact_training_rows": training_rows,
            "mixed_source_match_ids": int(mixed_count),
        }
        if artifact and training_rows is not None and training_rows > match_count:
            ctx.record(
                "ERROR",
                "Training Scope Audit",
                "Artifact rows exceed available scoped rows",
                f"{league}: artifact={training_rows} scoped={match_count}",
            )
        if mixed_count:
            ctx.record(
                "CRITICAL",
                "Training Scope Audit",
                "Mixed league source_match_id rows",
                f"{league}: {mixed_count}",
            )
    ctx.write_evidence("training_scope_audit.json", evidence)


def run_prediction_provenance(ctx: AuditContext) -> None:
    latest_runs = _latest_prediction_runs_by_league(ctx)
    if not latest_runs:
        ctx.record("WARNING", "Prediction Provenance", "No modern predictions", "no runs found")
        ctx.write_evidence("prediction_provenance.json", [])
        return
    placeholders = ",".join("?" for _ in latest_runs.values())
    rows = ctx.conn.execute(
        f"""
        SELECT p.fixture_id, p.run_id, p.artifact_id, f.league_code, ma.details_json,
               ma.params_json
        FROM prediction p
        JOIN fixture f ON f.fixture_id = p.fixture_id
        LEFT JOIN model_artifact ma ON ma.artifact_id = p.artifact_id
        WHERE p.artifact_id IS NOT NULL
          AND p.run_id IN ({placeholders})
        GROUP BY p.fixture_id, p.run_id, p.artifact_id, f.league_code
        """,
        tuple(latest_runs.values()),
    ).fetchall()
    evidence = []
    for row in rows:
        policy = _artifact_policy(row)
        fixture_league = str(row["league_code"] or "WORLD_CUP_2026")
        artifact_league = str(policy.get("league_code") or "")
        ok = bool(artifact_league) and fixture_league == artifact_league
        evidence.append(
            {
                "fixture_id": row["fixture_id"],
                "run_id": row["run_id"],
                "fixture_league": fixture_league,
                "artifact_id": row["artifact_id"],
                "artifact_league": artifact_league,
                "ok": ok,
            }
        )
        if not artifact_league:
            ctx.record(
                "WARNING",
                "Prediction Provenance",
                "Legacy artifact missing league policy",
                f"{row['fixture_id']} run_id={row['run_id']}",
            )
        elif not ok:
            ctx.record(
                "CRITICAL",
                "Prediction Provenance",
                "Fixture/artifact scope mismatch",
                f"{row['fixture_id']} fixture={fixture_league} artifact={artifact_league}",
            )
    ctx.write_evidence("prediction_provenance.json", evidence)


def _latest_prediction_runs_by_league(ctx: AuditContext) -> dict[str, int]:
    rows = ctx.conn.execute(
        """
        SELECT COALESCE(f.league_code, 'WORLD_CUP_2026') AS league_code,
               MAX(p.run_id) AS run_id
        FROM prediction p
        JOIN ingest_run ir ON ir.run_id = p.run_id
        JOIN fixture f ON f.fixture_id = p.fixture_id
        WHERE p.artifact_id IS NOT NULL
          AND ir.job_name = 'predict-fixtures'
          AND ir.status = 'succeeded'
        GROUP BY COALESCE(f.league_code, 'WORLD_CUP_2026')
        """
    ).fetchall()
    return {str(row["league_code"]): int(row["run_id"]) for row in rows}


def run_calibrator_audit(ctx: AuditContext) -> None:
    rows = ctx.conn.execute(
        "SELECT artifact_id, calibration_json, details_json, params_json FROM model_artifact"
    ).fetchall()
    evidence = []
    for row in rows:
        policy = _artifact_policy(row)
        expected_scope = policy.get("calibrator_scope") or policy.get("training_tournament")
        calibration = _json_obj(row["calibration_json"])
        for market, coeffs in calibration.get("coefficients", {}).items():
            if not isinstance(coeffs, dict):
                continue
            status = str(coeffs.get("status", "CALIBRATED"))
            scope = coeffs.get("calibrator_scope")
            evidence.append(
                {
                    "artifact_id": row["artifact_id"],
                    "market": market,
                    "status": status,
                    "scope": scope,
                    "expected_scope": expected_scope,
                }
            )
            if status not in {"CALIBRATED", "CALIBRATOR_INSUFFICIENT_DATA"}:
                ctx.record("ERROR", "Calibrator Audit", "Unexpected status", status)
            if scope and expected_scope and scope != expected_scope:
                ctx.record(
                    "ERROR",
                    "Calibrator Audit",
                    "Scope mismatch",
                    f"{row['artifact_id']} {market}: {scope} != {expected_scope}",
                )
    ctx.write_evidence("calibrator_audit.json", evidence)


def run_team_and_rating_audit(ctx: AuditContext) -> None:
    team_evidence: dict[str, Any] = {}
    missing_ratings: list[dict[str, Any]] = []
    for league in ctx.args.leagues:
        team_rows = ctx.conn.execute(
            """
            WITH scoped_teams AS (
              SELECT home_id AS team_id, 'history' AS source
              FROM match_history
              WHERE tournament = ?
              UNION ALL
              SELECT away_id AS team_id, 'history' AS source
              FROM match_history
              WHERE tournament = ?
              UNION ALL
              SELECT home_id AS team_id, 'fixture' AS source
              FROM fixture
              WHERE league_code = ? AND home_id IS NOT NULL
              UNION ALL
              SELECT away_id AS team_id, 'fixture' AS source
              FROM fixture
              WHERE league_code = ? AND away_id IS NOT NULL
            )
            SELECT t.team_id, t.canonical,
                   SUM(CASE WHEN st.source = 'history' THEN 1 ELSE 0 END) AS history_refs,
                   SUM(CASE WHEN st.source = 'fixture' THEN 1 ELSE 0 END) AS fixture_refs,
                   COUNT(DISTINCT ta.alias) AS alias_count,
                   tr.rating,
                   tr.as_of
            FROM scoped_teams st
            JOIN team t ON t.team_id = st.team_id
            LEFT JOIN team_alias ta ON ta.team_id = t.team_id
            LEFT JOIN team_rating tr
              ON tr.team_id = t.team_id AND tr.rating_system = 'elo'
            GROUP BY t.team_id, t.canonical, tr.rating, tr.as_of
            ORDER BY t.canonical
            """,
            (league, league, league, league),
        ).fetchall()
        rows = []
        for row in team_rows:
            history_refs = int(row["history_refs"] or 0)
            fixture_refs = int(row["fixture_refs"] or 0)
            if fixture_refs and history_refs:
                classification = "current_or_future_with_history"
            elif fixture_refs:
                classification = "current_or_future_no_history"
            elif history_refs:
                classification = "historical_only"
            else:
                classification = "unknown"
            item = {
                "team_id": row["team_id"],
                "canonical": row["canonical"],
                "history_refs": history_refs,
                "fixture_refs": fixture_refs,
                "alias_count": int(row["alias_count"] or 0),
                "rating": row["rating"],
                "rating_as_of": row["as_of"],
                "classification": classification,
            }
            rows.append(item)
            if row["rating"] is None:
                reason = "no Elo row for scoped team"
                if classification == "current_or_future_no_history":
                    reason = "future fixture team has no bridged historical matches"
                missing_ratings.append(
                    {
                        "league": league,
                        "club": row["canonical"],
                        "rating": None,
                        "matches_or_refs": history_refs + fixture_refs,
                        "reason": reason,
                        "suggested_fix": "bridge history, rebuild Elo, then republish predictions",
                    }
                )
        duplicate_alias_keys = ctx.conn.execute(
            """
            SELECT alias_key, COUNT(*) AS c
            FROM team_alias
            WHERE team_id IN (
              SELECT home_id FROM match_history WHERE tournament = ?
              UNION
              SELECT away_id FROM match_history WHERE tournament = ?
              UNION
              SELECT home_id FROM fixture WHERE league_code = ? AND home_id IS NOT NULL
              UNION
              SELECT away_id FROM fixture WHERE league_code = ? AND away_id IS NOT NULL
            )
              AND alias_key IS NOT NULL
            GROUP BY alias_key
            HAVING c > 1
            """,
            (league, league, league, league),
        ).fetchall()
        if duplicate_alias_keys:
            ctx.record(
                "ERROR",
                "Team Audit",
                "Duplicate alias keys",
                f"{league}: {len(duplicate_alias_keys)}",
            )
        team_evidence[league] = {
            "teams": rows,
            "team_count": len(rows),
            "missing_rating_count": sum(1 for row in rows if row["rating"] is None),
            "duplicate_alias_keys": [dict(row) for row in duplicate_alias_keys],
        }
    if missing_ratings:
        ctx.record(
            "WARNING",
            "Rating Coverage Audit",
            "Missing team ratings",
            str(len(missing_ratings)),
        )
    ctx.write_evidence("team_audit.json", team_evidence)
    ctx.write_evidence("rating_coverage.json", missing_ratings)


def run_bridge_audit(ctx: AuditContext) -> None:
    evidence: dict[str, Any] = {}
    for league in ctx.args.leagues:
        if league == "WORLD_CUP_2026":
            continue
        history_code = _league_history_code(league)
        orphans = ctx.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM match_history mh
            LEFT JOIN historical_match hm
              ON hm.source_event_id = mh.source_match_id
             AND hm.league_code = ?
            WHERE mh.tournament = ?
              AND hm.source_event_id IS NULL
            """,
            (history_code, league),
        ).fetchone()["c"]
        duplicate_ids = ctx.conn.execute(
            """
            SELECT source_match_id, COUNT(*) AS c
            FROM match_history
            WHERE tournament = ?
            GROUP BY source_match_id
            HAVING c > 1
            """,
            (league,),
        ).fetchall()
        invalid_dates = ctx.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM match_history
            WHERE tournament = ?
              AND (date IS NULL OR length(date) < 10)
            """,
            (league,),
        ).fetchone()["c"]
        evidence[league] = {
            "historical_match_league_code": history_code,
            "orphaned_match_history_rows": int(orphans),
            "duplicate_source_match_ids": [dict(row) for row in duplicate_ids],
            "invalid_date_rows": int(invalid_dates),
        }
        if orphans:
            ctx.record("ERROR", "Bridge Audit", "Orphaned bridged rows", f"{league}: {orphans}")
        if duplicate_ids:
            ctx.record(
                "CRITICAL",
                "Bridge Audit",
                "Duplicate source_match_id rows",
                f"{league}: {len(duplicate_ids)}",
            )
        if invalid_dates:
            ctx.record("ERROR", "Bridge Audit", "Invalid match dates", f"{league}: {invalid_dates}")
    ctx.write_evidence("bridge_audit.json", evidence)


def run_prediction_integrity(ctx: AuditContext) -> None:
    latest_runs = _latest_prediction_runs_by_league(ctx)
    if not latest_runs:
        ctx.write_evidence(
            "prediction_integrity.json",
            {"duplicates": [], "late_predictions": 0, "latest_runs": {}},
        )
        return
    placeholders = ",".join("?" for _ in latest_runs.values())
    duplicates = ctx.conn.execute(
        f"""
        SELECT fixture_id, market, selection, run_id, COUNT(*) AS c
        FROM prediction
        WHERE artifact_id IS NOT NULL
          AND run_id IN ({placeholders})
        GROUP BY fixture_id, market, selection, run_id
        HAVING c > 1
        """,
        tuple(latest_runs.values()),
    ).fetchall()
    late = ctx.conn.execute(
        f"""
        SELECT COUNT(*) AS c
        FROM prediction p
        JOIN fixture f ON f.fixture_id = p.fixture_id
        WHERE f.kickoff_utc IS NOT NULL
          AND p.artifact_id IS NOT NULL
          AND p.run_id IN ({placeholders})
          AND p.generated_utc > f.kickoff_utc
        """,
        tuple(latest_runs.values()),
    ).fetchone()["c"]
    evidence = {
        "duplicates": [dict(row) for row in duplicates],
        "late_predictions": late,
        "latest_runs": latest_runs,
    }
    if duplicates:
        ctx.record("CRITICAL", "Prediction Integrity", "Duplicate rows", str(len(duplicates)))
    if late:
        ctx.record("ERROR", "Prediction Integrity", "Late predictions", str(late))
    ctx.write_evidence("prediction_integrity.json", evidence)


def run_db_health(ctx: AuditContext) -> None:
    integrity = ctx.conn.execute("PRAGMA integrity_check").fetchone()[0]
    fk_rows = ctx.conn.execute("PRAGMA foreign_key_check").fetchall()
    evidence = {"integrity_check": integrity, "foreign_key_violations": len(fk_rows)}
    if integrity != "ok":
        ctx.record("CRITICAL", "DB Health", "Integrity check failed", str(integrity))
    if fk_rows:
        ctx.record("CRITICAL", "DB Health", "Foreign key violations", str(len(fk_rows)))
    ctx.write_evidence("db_health_final.json", evidence)


def run_readiness_report(ctx: AuditContext) -> None:
    cmd = [sys.executable, "-m", "scorerecord.cli", "multi-league-readiness-report"]
    cmd.extend(["--leagues", ",".join(ctx.args.leagues), "--json"])
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path("src").resolve())
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    payload: dict[str, Any]
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {"stdout": result.stdout, "stderr": result.stderr}
    if result.returncode != 0:
        ctx.record("ERROR", "Readiness Audit", "CLI failed", result.stderr.strip())
    for league_report in payload.get("league_reports", []):
        state = league_report.get("readiness_state")
        league = league_report.get("league")
        future_fixture_count = int(league_report.get("future_fixture_count") or 0)
        team_rating_count = int(league_report.get("team_rating_count") or 0)
        if state == "READY_FOR_PILOT" and future_fixture_count <= 0:
            ctx.record(
                "ERROR",
                "Readiness Audit",
                "READY_FOR_PILOT without future fixtures",
                str(league),
            )
        if state == "READY_FOR_PILOT" and team_rating_count <= 0:
            ctx.record(
                "ERROR",
                "Readiness Audit",
                "READY_FOR_PILOT without team ratings",
                str(league),
            )
    ctx.write_evidence("multi_league_readiness.json", payload)


def run_gui_audit(ctx: AuditContext) -> None:
    if ctx.args.skip_gui:
        ctx.record("INFO", "GUI Audit", "Skipped", "--skip-gui was set")
        return
    urls = [
        f"http://{ctx.args.host}:{ctx.args.port}/today",
        f"http://{ctx.args.host}:{ctx.args.port}/today?league=EPL",
        f"http://{ctx.args.host}:{ctx.args.port}/today?league=WORLD_CUP_2026",
    ]
    evidence = []
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                html = response.read().decode("utf-8", errors="replace")
            lower = html.lower()
            betting_terms = [term for term in ("kelly", "stake", "edge") if term in lower]
            ok = "forecast_only" in lower or "forecast-only" in lower
            evidence.append({"url": url, "ok": ok, "betting_terms": betting_terms})
            if not ok:
                ctx.record("ERROR", "GUI Audit", "Missing FORECAST_ONLY marker", url)
            if betting_terms:
                ctx.record("CRITICAL", "GUI Audit", "Betting terms found", str(betting_terms))
        except (OSError, urllib.error.URLError) as exc:
            ctx.record("WARNING", "GUI Audit", "Unreachable", f"{url}: {exc}")
    ctx.write_evidence("gui_validation.json", evidence)


def run_daily_refresh(ctx: AuditContext) -> None:
    latest_artifact_path = _latest_file(Path("outputs/benchmarks"), "epl_pilot_refresh_*.json")
    latest_artifact: dict[str, Any] | None = None
    if latest_artifact_path is not None:
        latest_artifact = _json_obj(latest_artifact_path.read_text(encoding="utf-8"))
    recent_runs = [
        dict(row)
        for row in ctx.conn.execute(
            """
            SELECT run_id, job_name, status, source, started_utc, finished_utc
            FROM ingest_run
            WHERE job_name IN ('build-elo', 'predict-fixtures', 'fetch-odds')
            ORDER BY run_id DESC
            LIMIT 12
            """
        ).fetchall()
    ]
    payload: dict[str, Any] = {
        "mode": "read_only",
        "latest_epl_refresh_artifact": str(latest_artifact_path) if latest_artifact_path else None,
        "latest_epl_refresh_result": latest_artifact,
        "recent_runs": recent_runs,
    }
    if not ctx.args.execute_refresh:
        ctx.record("INFO", "Daily Refresh", "Skipped live refresh", "read-only audit")
        ctx.write_evidence("daily_refresh_validation.json", payload)
        return
    cmd = [sys.executable, "-m", "scorerecord.cli", "run-epl-pilot-refresh", "--json"]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path("src").resolve())
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    payload.update(
        {
            "mode": "execute_refresh",
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    )
    if result.returncode != 0:
        ctx.record("ERROR", "Daily Refresh", "Refresh failed", result.stderr.strip())
    ctx.write_evidence("daily_refresh_validation.json", payload)


def _latest_file(directory: Path, pattern: str) -> Path | None:
    if not directory.exists():
        return None
    paths = sorted(directory.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return paths[0] if paths else None


def run_regression_and_performance(ctx: AuditContext) -> None:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    dirty_lines = []
    for line in result.stdout.splitlines():
        normalized = line.strip()
        if "outputs/benchmarks/" in normalized or "outputs\\benchmarks\\" in normalized:
            continue
        dirty_lines.append(normalized)
    payload = {
        "git_status_returncode": result.returncode,
        "dirty_files": dirty_lines,
        "timings": ctx.timings,
    }
    if result.returncode != 0:
        ctx.record("WARNING", "Regression Audit", "git status failed", result.stderr.strip())
    elif dirty_lines:
        ctx.record(
            "ERROR",
            "Regression Audit",
            "Working tree is dirty outside outputs/benchmarks",
            json.dumps(dirty_lines[:20]),
        )
    ctx.write_evidence("performance_and_regression.json", payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase 13.4.2 production audit")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--execute-refresh", action="store_true")
    parser.add_argument("--skip-gui", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--leagues", default="EPL,WORLD_CUP_2026")
    args = parser.parse_args()
    args.leagues = tuple(value.strip() for value in args.leagues.split(",") if value.strip())
    if not args.leagues:
        args.leagues = DEFAULT_LEAGUES
    return args


def main() -> int:
    ctx = AuditContext(parse_args())
    try:
        _run_step(ctx, "artifact_separation", run_artifact_separation)
        _run_step(ctx, "prediction_provenance", run_prediction_provenance)
        _run_step(ctx, "calibrator_audit", run_calibrator_audit)
        _run_step(ctx, "training_scope_audit", run_training_scope_audit)
        _run_step(ctx, "team_and_rating_audit", run_team_and_rating_audit)
        _run_step(ctx, "bridge_audit", run_bridge_audit)
        _run_step(ctx, "prediction_integrity", run_prediction_integrity)
        _run_step(ctx, "db_health", run_db_health)
        _run_step(ctx, "readiness_report", run_readiness_report)
        _run_step(ctx, "gui_audit", run_gui_audit)
        _run_step(ctx, "daily_refresh", run_daily_refresh)
        _run_step(ctx, "regression_and_performance", run_regression_and_performance)
        ctx.write_report()
        return 1 if any(f.severity in {"CRITICAL", "ERROR"} for f in ctx.findings) else 0
    finally:
        ctx.close()


if __name__ == "__main__":
    raise SystemExit(main())
