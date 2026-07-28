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
        report_path = self.out_dir / "phase_13_4_2_final_report.md"
        report_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"Final report written to {report_path}")


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


def run_artifact_separation(ctx: AuditContext) -> None:
    rows = ctx.conn.execute(
        """
        SELECT artifact_id, model_version, params_json, calibration_json, details_json,
               created_utc
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
                "training_policy": policy,
            }

    for league in ctx.args.leagues:
        if league not in by_league:
            ctx.record("WARNING", "Artifact Separation", "Missing artifact", league)

    ids = [data["artifact_id"] for data in by_league.values()]
    if len(ids) != len(set(ids)):
        ctx.record("CRITICAL", "Artifact Separation", "Shared artifact id", str(by_league))

    ctx.write_evidence("artifact_audit.json", by_league)


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
    if not ctx.args.execute_refresh:
        ctx.record("INFO", "Daily Refresh", "Skipped live refresh", "read-only audit")
        return
    cmd = [sys.executable, "-m", "scorerecord.cli", "run-epl-pilot-refresh", "--json"]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path("src").resolve())
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    payload = {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    if result.returncode != 0:
        ctx.record("ERROR", "Daily Refresh", "Refresh failed", result.stderr.strip())
    ctx.write_evidence("daily_refresh_validation.json", payload)


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
        run_artifact_separation(ctx)
        run_prediction_provenance(ctx)
        run_calibrator_audit(ctx)
        run_prediction_integrity(ctx)
        run_db_health(ctx)
        run_readiness_report(ctx)
        run_gui_audit(ctx)
        run_daily_refresh(ctx)
        ctx.write_report()
        return 1 if any(f.severity in {"CRITICAL", "ERROR"} for f in ctx.findings) else 0
    finally:
        ctx.close()


if __name__ == "__main__":
    raise SystemExit(main())
