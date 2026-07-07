from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from itertools import groupby

from scorerecord.odds_coverage import STATUS_COVERAGE_OK, STATUS_INCOMPLETE_MARKET
from scorerecord.odds_horizons import (
    DEFAULT_DISPLAY_TZ,
    load_date_odds_horizon_coverage,
    load_fixture_odds_horizon_coverage,
)

GRADE_READY = "READY_FOR_ANALYSIS"
GRADE_PARTIAL = "PARTIAL_COVERAGE"
GRADE_WEAK = "WEAK_COVERAGE"
GRADE_NO_ODDS = "NO_ODDS"

_NEAR_KICKOFF_HORIZONS = {"T1H", "T15M"}


@dataclass(frozen=True)
class FixtureMarketCoverageSummary:
    fixture_id: str
    kickoff_utc: str | None
    home: str
    away: str
    market: str
    horizons_total: int
    horizons_covered: int
    horizons_incomplete: int
    horizons_missing: int
    complete_bookmaker_max: int
    has_last_pre_kickoff: bool
    has_t1h_or_t15m: bool
    coverage_grade: str
    blocking_reason: str | None

    def as_row(self) -> str:
        reason = self.blocking_reason or "none"
        return (
            "odds_coverage_summary: "
            f"fixture_id={self.fixture_id} "
            f"kickoff_utc={self.kickoff_utc or 'n/a'} "
            f"home={_compact_token(self.home)} "
            f"away={_compact_token(self.away)} "
            f"market={self.market} "
            f"grade={self.coverage_grade} "
            f"horizons_covered={self.horizons_covered}/{self.horizons_total} "
            f"incomplete={self.horizons_incomplete} "
            f"missing={self.horizons_missing} "
            f"max_complete_books={self.complete_bookmaker_max} "
            f"last_pre_kickoff={_yes_no(self.has_last_pre_kickoff)} "
            f"near_kickoff={_yes_no(self.has_t1h_or_t15m)} "
            f"blocking_reason={reason}"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "fixture_id": self.fixture_id,
            "kickoff_utc": self.kickoff_utc,
            "home": self.home,
            "away": self.away,
            "market": self.market,
            "horizons_total": self.horizons_total,
            "horizons_covered": self.horizons_covered,
            "horizons_incomplete": self.horizons_incomplete,
            "horizons_missing": self.horizons_missing,
            "complete_bookmaker_max": self.complete_bookmaker_max,
            "has_last_pre_kickoff": self.has_last_pre_kickoff,
            "has_t1h_or_t15m": self.has_t1h_or_t15m,
            "coverage_grade": self.coverage_grade,
            "blocking_reason": self.blocking_reason,
        }


@dataclass(frozen=True)
class CoverageSummaryReport:
    target_date: str | None
    fixtures: list[FixtureMarketCoverageSummary]
    total_fixture_markets: int
    ready_count: int
    partial_count: int
    weak_count: int
    no_odds_count: int

    def as_header(self) -> str:
        target = self.target_date or "n/a"
        return (
            f"date={target} "
            f"total_fixture_markets={self.total_fixture_markets} "
            f"ready={self.ready_count} "
            f"partial={self.partial_count} "
            f"weak={self.weak_count} "
            f"no_odds={self.no_odds_count}"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "target_date": self.target_date,
            "total_fixture_markets": self.total_fixture_markets,
            "ready_count": self.ready_count,
            "partial_count": self.partial_count,
            "weak_count": self.weak_count,
            "no_odds_count": self.no_odds_count,
            "fixtures": [fixture.as_dict() for fixture in self.fixtures],
        }


def build_odds_coverage_summary_for_date(
    conn: sqlite3.Connection,
    target_date: str,
    display_tz_name: str = DEFAULT_DISPLAY_TZ,
) -> CoverageSummaryReport:
    coverage = load_date_odds_horizon_coverage(
        conn,
        target_date,
        display_tz_name=display_tz_name,
    )
    return _build_report(conn, coverage, target_date=target_date)


def build_odds_coverage_summary_for_fixture(
    conn: sqlite3.Connection,
    fixture_id: str,
) -> CoverageSummaryReport:
    coverage = load_fixture_odds_horizon_coverage(conn, fixture_id)
    return _build_report(conn, coverage, target_date=None)


def _build_report(
    conn: sqlite3.Connection,
    coverage: list[object],
    *,
    target_date: str | None,
) -> CoverageSummaryReport:
    fixture_meta = _fixture_metadata(conn, [str(item.fixture_id) for item in coverage])
    rows: list[FixtureMarketCoverageSummary] = []
    coverage = sorted(coverage, key=lambda item: (str(item.fixture_id), str(item.market)))
    for (fixture_id, market), items_iter in groupby(
        coverage,
        key=lambda item: (str(item.fixture_id), str(item.market)),
    ):
        rows.append(
            _summarize_fixture_market(
                fixture_id=fixture_id,
                market=market,
                items=list(items_iter),
                meta=fixture_meta.get(fixture_id, _missing_fixture_meta(fixture_id)),
            )
        )
    rows.sort(key=lambda item: (item.kickoff_utc or "", item.fixture_id, item.market))
    return CoverageSummaryReport(
        target_date=target_date,
        fixtures=rows,
        total_fixture_markets=len(rows),
        ready_count=sum(1 for row in rows if row.coverage_grade == GRADE_READY),
        partial_count=sum(1 for row in rows if row.coverage_grade == GRADE_PARTIAL),
        weak_count=sum(1 for row in rows if row.coverage_grade == GRADE_WEAK),
        no_odds_count=sum(1 for row in rows if row.coverage_grade == GRADE_NO_ODDS),
    )


def _summarize_fixture_market(
    *,
    fixture_id: str,
    market: str,
    items: list[object],
    meta: dict[str, str | None],
) -> FixtureMarketCoverageSummary:
    horizons_total = len(items)
    horizons_covered = sum(1 for item in items if item.status == STATUS_COVERAGE_OK)
    horizons_incomplete = sum(1 for item in items if item.status == STATUS_INCOMPLETE_MARKET)
    horizons_missing = max(horizons_total - horizons_covered - horizons_incomplete, 0)
    complete_bookmaker_max = max(
        (int(item.complete_bookmaker_count) for item in items),
        default=0,
    )
    has_last_pre_kickoff = any(
        item.horizon == "LAST_PRE_KICKOFF" and item.status == STATUS_COVERAGE_OK
        for item in items
    )
    has_t1h_or_t15m = any(
        item.horizon in _NEAR_KICKOFF_HORIZONS and item.status == STATUS_COVERAGE_OK
        for item in items
    )
    coverage_grade, blocking_reason = _coverage_grade(
        horizons_covered=horizons_covered,
        horizons_incomplete=horizons_incomplete,
        complete_bookmaker_max=complete_bookmaker_max,
        has_last_pre_kickoff=has_last_pre_kickoff,
        has_t1h_or_t15m=has_t1h_or_t15m,
    )
    return FixtureMarketCoverageSummary(
        fixture_id=fixture_id,
        kickoff_utc=meta["kickoff_utc"],
        home=meta["home"] or "TBD",
        away=meta["away"] or "TBD",
        market=market,
        horizons_total=horizons_total,
        horizons_covered=horizons_covered,
        horizons_incomplete=horizons_incomplete,
        horizons_missing=horizons_missing,
        complete_bookmaker_max=complete_bookmaker_max,
        has_last_pre_kickoff=has_last_pre_kickoff,
        has_t1h_or_t15m=has_t1h_or_t15m,
        coverage_grade=coverage_grade,
        blocking_reason=blocking_reason,
    )


def _coverage_grade(
    *,
    horizons_covered: int,
    horizons_incomplete: int,
    complete_bookmaker_max: int,
    has_last_pre_kickoff: bool,
    has_t1h_or_t15m: bool,
) -> tuple[str, str | None]:
    if horizons_covered <= 0:
        if horizons_incomplete > 0:
            return GRADE_WEAK, "only_incomplete_horizons"
        return GRADE_NO_ODDS, "no_complete_horizons"
    if complete_bookmaker_max < 2:
        return GRADE_WEAK, "max_complete_bookmakers_below_2"
    if has_last_pre_kickoff and has_t1h_or_t15m:
        return GRADE_READY, None
    if not has_last_pre_kickoff:
        return GRADE_PARTIAL, "missing_last_pre_kickoff"
    return GRADE_PARTIAL, "missing_near_kickoff"


def _fixture_metadata(
    conn: sqlite3.Connection,
    fixture_ids: list[str],
) -> dict[str, dict[str, str | None]]:
    unique_ids = sorted(set(fixture_ids))
    if not unique_ids:
        return {}
    placeholders = ",".join("?" for _ in unique_ids)
    rows = conn.execute(
        f"""
        SELECT
          f.fixture_id,
          f.kickoff_utc,
          COALESCE(home.canonical, f.home_placeholder, 'TBD') AS home,
          COALESCE(away.canonical, f.away_placeholder, 'TBD') AS away
        FROM fixture f
        LEFT JOIN team home ON home.team_id = f.home_id
        LEFT JOIN team away ON away.team_id = f.away_id
        WHERE f.fixture_id IN ({placeholders})
        """,
        unique_ids,
    ).fetchall()
    return {
        str(row["fixture_id"]): {
            "kickoff_utc": row["kickoff_utc"],
            "home": row["home"],
            "away": row["away"],
        }
        for row in rows
    }


def _missing_fixture_meta(fixture_id: str) -> dict[str, str | None]:
    return {"kickoff_utc": None, "home": fixture_id, "away": "TBD"}


def _yes_no(flag: bool) -> str:
    return "yes" if flag else "no"


def _compact_token(raw: str) -> str:
    return raw.replace(" ", "_")
