from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from scorerecord.runs import record_quality


@dataclass(frozen=True)
class FixtureResolutionReport:
    total: int
    full_resolved: int
    partial_or_unresolved: int
    unresolved_rows: list[dict[str, object]]

    @property
    def resolved_ratio(self) -> float:
        if self.total == 0:
            return 0.0
        return self.full_resolved / self.total


@dataclass(frozen=True)
class HistoricalAliasReport:
    historical_teams: int
    seed_backed_teams: int
    auto_created_teams: int
    duplicate_key_rows: list[dict[str, object]]

    @property
    def seed_backed_ratio(self) -> float:
        if self.historical_teams == 0:
            return 0.0
        return self.seed_backed_teams / self.historical_teams


def fixture_resolution_report(conn: sqlite3.Connection) -> FixtureResolutionReport:
    rows = conn.execute(
        """
        SELECT fixture_id, kickoff_local, stage, group_name,
               home_id, away_id, home_placeholder, away_placeholder
        FROM fixture
        ORDER BY kickoff_local, fixture_id
        """
    ).fetchall()
    unresolved = []
    for row in rows:
        if row["home_id"] is None or row["away_id"] is None:
            unresolved.append(dict(row))

    total = len(rows)
    full_resolved = total - len(unresolved)
    return FixtureResolutionReport(
        total=total,
        full_resolved=full_resolved,
        partial_or_unresolved=len(unresolved),
        unresolved_rows=unresolved,
    )


def record_fixture_resolution_quality(
    conn: sqlite3.Connection,
    run_id: int | None,
) -> FixtureResolutionReport:
    report = fixture_resolution_report(conn)
    details = {
        "unresolved": report.unresolved_rows,
        "note": "Knockout placeholders are expected before tournament bracket is known.",
    }
    record_quality(
        conn,
        run_id,
        "fixture_resolution",
        "resolved_ratio",
        report.resolved_ratio,
        json.dumps(details, sort_keys=True),
    )
    conn.commit()
    return report


def historical_alias_report(conn: sqlite3.Connection) -> HistoricalAliasReport:
    historical_team_ids = {
        int(row["team_id"])
        for row in conn.execute(
            """
            SELECT home_id AS team_id FROM match_history
            UNION
            SELECT away_id AS team_id FROM match_history
            """
        )
    }
    seed_backed = {
        int(row["team_id"])
        for row in conn.execute(
            """
            SELECT DISTINCT team_id
            FROM team_alias
            WHERE source = 'seed'
            """
        )
    }
    duplicate_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT canonical_key, COUNT(*) AS c, GROUP_CONCAT(canonical) AS canonical_names
            FROM team
            WHERE canonical_key IS NOT NULL
            GROUP BY canonical_key
            HAVING COUNT(*) > 1
            ORDER BY c DESC, canonical_key
            """
        )
    ]
    seed_count = len(historical_team_ids & seed_backed)
    return HistoricalAliasReport(
        historical_teams=len(historical_team_ids),
        seed_backed_teams=seed_count,
        auto_created_teams=len(historical_team_ids) - seed_count,
        duplicate_key_rows=duplicate_rows,
    )


def record_historical_alias_quality(
    conn: sqlite3.Connection,
    run_id: int | None,
) -> HistoricalAliasReport:
    report = historical_alias_report(conn)
    details = {
        "auto_created_teams": report.auto_created_teams,
        "duplicate_key_rows": report.duplicate_key_rows,
        "note": "Auto-created historical teams are expected for non-World-Cup nations; duplicates are the danger signal.",
    }
    record_quality(
        conn,
        run_id,
        "historical_aliases",
        "seed_backed_ratio",
        report.seed_backed_ratio,
        json.dumps(details, sort_keys=True),
    )
    record_quality(
        conn,
        run_id,
        "historical_aliases",
        "duplicate_canonical_keys",
        float(len(report.duplicate_key_rows)),
        json.dumps({"duplicates": report.duplicate_key_rows}, sort_keys=True),
    )
    conn.commit()
    return report
