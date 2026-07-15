import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scorerecord.features.elo import rebuild_elo
from scorerecord.health import db_health_checks
from scorerecord.ingest.fixtures_odds_api_io import ingest_fixtures_odds_api_io
from scorerecord.predict.fixtures import publish_fixture_predictions
from scorerecord.runs import ingest_run


def _health_status(conn: sqlite3.Connection) -> str:
    checks = db_health_checks(conn)
    return "OK" if all(c.status == "OK" for c in checks) else "DB_HEALTH_FAILED"

def run_epl_pilot_refresh(
    conn: sqlite3.Connection,
    api_key: str | None,
    base_url: str,
    league_code: str = "EPL",
    league_slug: str = "england-premier-league",
    competition: str = "English Premier League",
    season: str = "2026-2027",
    max_requests: int = 1,
    dry_run: bool = False,
    outputs_dir: Path | None = None,
) -> dict[str, Any]:
    
    result: dict[str, Any] = {
        "status": "OK",
        "dry_run": dry_run,
        "league": league_code,
        "season": season,
        "api_requests_used": 0,
        "ingest_status": "PENDING",
        "events_seen": 0,
        "events_valid": 0,
        "fixtures_inserted": 0,
        "fixtures_updated": 0,
        "unresolved_fixtures": 0,
        "duplicate_source_event_ids": 0,
        "build_elo_status": "PENDING",
        "predicted_fixtures": 0,
        "inserted_predictions": 0,
        "db_health_pre_status": "PENDING",
        "db_health_post_status": "PENDING",
        "next_action": "Review GUI EPL forecasts"
    }

    # 1. Pre-health check
    result["db_health_pre_status"] = _health_status(conn)
    if result["db_health_pre_status"] != "OK":
        result["status"] = "DB_HEALTH_FAILED"
        result["next_action"] = "Fix DB health issues"
        return result

    # 2. Ingest
    ingest_result = ingest_fixtures_odds_api_io(
        conn=conn,
        api_key=api_key,
        base_url=base_url,
        sport="football",
        league_code=league_code,
        league_slug=league_slug,
        competition=competition,
        season=season,
        raw_dir=None,
        max_requests=max_requests,
        dry_run=dry_run,
        verify_league_slug=False,
    )
    
    result["api_requests_used"] = ingest_result.get("api_requests_used", 0)
    result["events_seen"] = ingest_result.get("events_seen", 0)
    result["events_valid"] = ingest_result.get("events_valid", 0)
    result["fixtures_inserted"] = ingest_result.get("fixtures_inserted", 0)
    result["fixtures_updated"] = ingest_result.get("fixtures_updated", 0)
    result["unresolved_fixtures"] = ingest_result.get("unresolved_fixtures", 0)
    result["ingest_status"] = ingest_result.get("status", "UNKNOWN")

    if result["ingest_status"] != "OK":
        result["status"] = "INGEST_FAILED"
        result["next_action"] = "Check API key or rate limits"
        return result

    if result["unresolved_fixtures"] > 0:
        result["status"] = "UNRESOLVED_FIXTURES"
        result["next_action"] = "Run alias-gap-report and add SEED_ALIASES"
        return result

    # 5. Check duplicate source_event_ids
    dups = conn.execute(
        """
        SELECT source_event_id, COUNT(*) as c
        FROM fixture
        WHERE league_code = ?
        GROUP BY source_event_id
        HAVING c > 1
        """,
        (league_code,)
    ).fetchall()
    result["duplicate_source_event_ids"] = len(dups)
    if result["duplicate_source_event_ids"] > 0:
        result["status"] = "DUPLICATE_SOURCE_EVENT_IDS"
        result["next_action"] = "Fix duplicated source_event_ids in fixture table"
        return result

    if dry_run:
        result["status"] = "OK"
        result["build_elo_status"] = "SKIPPED_DRY_RUN"
        result["next_action"] = "Review dry-run output"
        result["would_publish_predictions"] = True
        return result

    # 6. Build Elo
    try:
        with ingest_run(conn, "build-elo", source="match_history", cwd=Path.cwd()):
            rebuild_elo(conn)
        result["build_elo_status"] = "OK"
    except Exception as e:
        result["status"] = "BUILD_ELO_FAILED"
        result["build_elo_status"] = f"ERROR: {e}"
        result["next_action"] = "Check build-elo logs"
        return result

    # 7. Publish predictions (EPL scoped)
    try:
        pred_summary = publish_fixture_predictions(
            conn,
            cwd=Path.cwd(),
            league=league_code
        )
        result["predicted_fixtures"] = pred_summary.predicted_fixtures
        result["inserted_predictions"] = pred_summary.inserted_predictions
    except Exception as e:
        result["status"] = "PREDICTION_FAILED"
        result["next_action"] = f"Check prediction logs: {e}"
        return result

    # 8. Post-health check
    result["db_health_post_status"] = _health_status(conn)
    if result["db_health_post_status"] != "OK":
        result["status"] = "DB_HEALTH_FAILED"
        result["next_action"] = "Fix DB health issues after prediction"
        return result

    # 9. Write artifact
    if outputs_dir:
        benchmarks_dir = outputs_dir / "benchmarks"
        benchmarks_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        artifact_path = benchmarks_dir / f"epl_pilot_refresh_{ts}.json"
        artifact_path.write_text(json.dumps(result, indent=2))

    return result
