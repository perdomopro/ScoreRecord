import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from scorerecord.time_utils import parse_datetime_utc


@dataclass
class SharpSoftCoverageAuditReport:
    date_range: str
    markets_requested: list[str]
    leagues_requested: list[str]
    sharp_books_configured: list[str]
    tables_checked: list[str]
    historical_odds_state: str
    odds_snapshots_state: str
    overall_state: str
    bookmaker_level_available: bool
    bookmakers_found: list[str]
    sharp_candidates_found: list[str]
    soft_candidates_found: list[str]
    aggregate_candidates_found: list[str]
    source_coverage: dict[str, Any]
    market_league_coverage: dict[str, Any]
    bookmaker_market_coverage: dict[str, Any]
    horizon_coverage: dict[str, Any]
    timestamp_coverage: dict[str, Any]
    close_available: bool
    last_pre_kickoff_available: bool
    edge_detection_feasible: bool
    clv_feasible: bool
    blockers: list[str]
    recommended_next_phase: str


def normalize_bookmaker(name: str | None) -> str:
    if not name:
        return "aggregate"
    n = name.lower().strip()
    n = re.sub(r"[^\w\s]", "", n)
    if "pinnacle" in n or "ps3838" in n:
        return "pinnacle"
    if n in ("betfair exchange", "betfair ex"):
        return "betfair_exchange"
    if n in ("sbobet", "sbo"):
        return "sbobet"
    if "188bet" in n or "188 bet" in n:
        return "188bet"
    if n == "avg" or n == "max":
        return "aggregate"
    return n.replace(" ", "_")


def is_sharp(norm_name: str, sharp_books_config: list[str]) -> bool:
    norm_sharps = [normalize_bookmaker(b) for b in sharp_books_config]
    return norm_name in norm_sharps


def run_sharp_soft_audit(
    conn: sqlite3.Connection,
    date_from: str,
    date_to: str,
    markets: list[str],
    leagues: list[str],
    sharp_books: list[str],
) -> SharpSoftCoverageAuditReport:

    # Initialize trackers
    tables_checked = ["historical_odds", "odds_snapshots"]
    bookmakers_found = set()
    sharp_found = set()
    soft_found = set()
    agg_found = set()

    source_stats = {
        "historical_odds": {
            "fixture_count": 0,
            "snapshot_count": 0,
            "distinct_bookmakers": 0,
            "sharp_bookmakers_found": [],
            "soft_bookmakers_found": [],
            "aggregate_bookmakers_found": [],
            "fixtures_with_sharp": 0,
            "fixtures_with_soft": 0,
            "fixtures_with_sharp_and_soft_same_horizon": 0,
            "fixtures_with_last_pre_kickoff_close": 0,
            "fixtures_with_null_kickoff_utc": 0,
            "min_snapshot_at": None,
            "max_snapshot_at": None,
            "actual_history_days": 0,
        },
        "odds_snapshots": {
            "fixture_count": 0,
            "snapshot_count": 0,
            "distinct_bookmakers": 0,
            "sharp_bookmakers_found": [],
            "soft_bookmakers_found": [],
            "aggregate_bookmakers_found": [],
            "fixtures_with_sharp": 0,
            "fixtures_with_soft": 0,
            "fixtures_with_sharp_and_soft_same_horizon": 0,
            "fixtures_with_last_pre_kickoff_close": 0,
            "fixtures_with_null_kickoff_utc": 0,
            "min_snapshot_at": None,
            "max_snapshot_at": None,
            "actual_history_days": 0,
        },
    }

    # Query historical_odds
    cur = conn.cursor()
    # We join historical_match to filter by date and league
    cur.execute(
        """
        SELECT ho.source_event_id, ho.bookmaker, ho.market, hm.league_code, hm.match_date
        FROM historical_odds ho
        JOIN historical_match hm ON ho.source = hm.source AND ho.source_event_id = hm.source_event_id
        WHERE hm.match_date >= ? AND hm.match_date <= ?
    """,
        (date_from, date_to),
    )

    ho_fixtures_sharp = set()
    ho_fixtures_soft = set()

    for row in cur.fetchall():
        fix_id, bookie, mkt, lge, mdate = row
        if mkt not in markets or lge not in leagues:
            continue
        source_stats["historical_odds"]["snapshot_count"] += 1

        norm_b = normalize_bookmaker(bookie)
        bookmakers_found.add(norm_b)

        if norm_b == "aggregate":
            agg_found.add(norm_b)
        elif is_sharp(norm_b, sharp_books):
            sharp_found.add(norm_b)
            ho_fixtures_sharp.add(fix_id)
        else:
            soft_found.add(norm_b)
            ho_fixtures_soft.add(fix_id)

    source_stats["historical_odds"]["fixtures_with_sharp"] = len(ho_fixtures_sharp)
    source_stats["historical_odds"]["fixtures_with_soft"] = len(ho_fixtures_soft)
    source_stats["historical_odds"]["fixtures_with_sharp_and_soft_same_horizon"] = len(
        ho_fixtures_sharp.intersection(ho_fixtures_soft)
    )
    source_stats["historical_odds"]["distinct_bookmakers"] = (
        len(sharp_found) + len(soft_found) + len(agg_found)
    )

    # Query odds_snapshots
    cur.execute(
        """
        SELECT os.fixture_id, os.bookmaker, os.market, f.kickoff_utc, os.snapshot_at, hm.league_code
        FROM odds_snapshots os
        JOIN fixture f ON os.fixture_id = f.fixture_id
        LEFT JOIN historical_match hm ON f.fixture_id = hm.source_event_id AND hm.source = os.source
        WHERE (f.kickoff_utc >= ? AND f.kickoff_utc <= ? || 'T23:59:59Z') 
           OR (f.kickoff_utc IS NULL AND os.snapshot_at >= ? AND os.snapshot_at <= ? || 'T23:59:59Z')
    """,
        (date_from, date_to, date_from, date_to),
    )

    os_fixtures_sharp = set()
    os_fixtures_soft = set()
    os_fixtures_close = set()
    os_horizons = {}  # fixture_id -> {market: {horizon: {sharp: set(), soft: set()}}}
    market_league_coverage = {}

    null_kickoff = 0
    min_snap = "9999"
    max_snap = "0000"

    for row in cur.fetchall():
        fix_id, bookie, mkt, kickoff, snap, league_code = row
        if mkt not in markets:
            continue

        league = league_code or "UNKNOWN"
        if league_code and leagues and league_code not in leagues:
            continue

        source_stats["odds_snapshots"]["snapshot_count"] += 1
        if not kickoff:
            null_kickoff += 1
            # We still count the snapshot, but we can't compute horizons

        norm_b = normalize_bookmaker(bookie)
        bookmakers_found.add(norm_b)

        if snap:
            min_snap = min(min_snap, snap)
            max_snap = max(max_snap, snap)

        is_s = False
        is_so = False

        if norm_b == "aggregate":
            agg_found.add(norm_b)
        elif is_sharp(norm_b, sharp_books):
            sharp_found.add(norm_b)
            os_fixtures_sharp.add(fix_id)
            is_s = True
        else:
            soft_found.add(norm_b)
            os_fixtures_soft.add(fix_id)
            is_so = True

        # Time diff
        ko_dt = parse_datetime_utc(kickoff) if kickoff else None
        snap_dt = parse_datetime_utc(snap) if snap else None
        if ko_dt and snap_dt:
            diff = ko_dt - snap_dt
            mins = diff.total_seconds() / 60.0

            hz = None
            if 0 <= mins <= 60:
                hz = "LAST_PRE_KICKOFF"
                os_fixtures_close.add(fix_id)
            elif 60 < mins <= 360:
                hz = "T6H"
            elif 360 < mins <= 1440:
                hz = "T24H"

            if hz:
                if fix_id not in os_horizons:
                    os_horizons[fix_id] = {}
                if mkt not in os_horizons[fix_id]:
                    os_horizons[fix_id][mkt] = {}
                if hz not in os_horizons[fix_id][mkt]:
                    os_horizons[fix_id][mkt][hz] = {"sharp": set(), "soft": set()}

                if is_s:
                    os_horizons[fix_id][mkt][hz]["sharp"].add(norm_b)
                if is_so:
                    os_horizons[fix_id][mkt][hz]["soft"].add(norm_b)

                # Market League Coverage Tracker
                ml_key = f"{league}_{mkt}"
                if ml_key not in market_league_coverage:
                    market_league_coverage[ml_key] = {
                        "fixtures": set(),
                        "sharp_count": 0,
                        "soft_count": 0,
                    }
                market_league_coverage[ml_key]["fixtures"].add(fix_id)
                if is_s:
                    market_league_coverage[ml_key]["sharp_count"] += 1
                if is_so:
                    market_league_coverage[ml_key]["soft_count"] += 1

    # Convert sets to lengths for JSON
    ml_coverage_final = {}
    for ml, dat in market_league_coverage.items():
        ml_coverage_final[ml] = {
            "fixtures": len(dat["fixtures"]),
            "sharp_snapshots": dat["sharp_count"],
            "soft_snapshots": dat["soft_count"],
        }

    # Calculate fixtures_with_sharp_and_soft_same_horizon
    same_hz_count = 0
    for fix_id, mkts in os_horizons.items():
        has_both = False
        for mkt, hzs in mkts.items():
            for hz, books in hzs.items():
                if len(books["sharp"]) > 0 and len(books["soft"]) > 0:
                    has_both = True
                    break
            if has_both:
                break
        if has_both:
            same_hz_count += 1

    source_stats["odds_snapshots"]["fixtures_with_sharp"] = len(os_fixtures_sharp)
    source_stats["odds_snapshots"]["fixtures_with_soft"] = len(os_fixtures_soft)
    source_stats["odds_snapshots"]["fixtures_with_sharp_and_soft_same_horizon"] = same_hz_count
    source_stats["odds_snapshots"]["fixtures_with_last_pre_kickoff_close"] = len(os_fixtures_close)
    source_stats["odds_snapshots"]["fixtures_with_null_kickoff_utc"] = null_kickoff
    source_stats["odds_snapshots"]["min_snapshot_at"] = min_snap if min_snap != "9999" else None
    source_stats["odds_snapshots"]["max_snapshot_at"] = max_snap if max_snap != "0000" else None

    # State Logic
    def get_state(fixtures_with_both, has_close):
        if fixtures_with_both >= 200 and has_close:
            return "SHARP_SOFT_FEASIBLE"
        elif fixtures_with_both > 0 or has_close:
            return "PARTIAL_COVERAGE"
        else:
            return "NOT_FEASIBLE"

    ho_state = get_state(
        source_stats["historical_odds"]["fixtures_with_sharp_and_soft_same_horizon"], False
    )
    os_state = get_state(same_hz_count, len(os_fixtures_close) >= 200)

    edge_feasible = (
        same_hz_count > 0
        or source_stats["historical_odds"]["fixtures_with_sharp_and_soft_same_horizon"] > 0
    )
    clv_feasible = len(os_fixtures_close) > 0

    overall_state = "NOT_FEASIBLE"
    if ho_state == "SHARP_SOFT_FEASIBLE" or os_state == "SHARP_SOFT_FEASIBLE":
        overall_state = "SHARP_SOFT_FEASIBLE"
    elif ho_state == "PARTIAL_COVERAGE" or os_state == "PARTIAL_COVERAGE":
        overall_state = "PARTIAL_COVERAGE"

    blockers = []
    if overall_state == "NOT_FEASIBLE":
        blockers.append("No multi-book horizon data found with both sharp and soft books.")
    elif overall_state == "PARTIAL_COVERAGE":
        if not clv_feasible:
            blockers.append("Missing robust last_pre_kickoff close timing data.")
        if same_hz_count < 200:
            blockers.append(f"Volume too low ({same_hz_count} fixtures) for robust edge modeling.")

    # Fair price warning comment
    # Note: A true fair price should account for back/lay spread and commission on exchanges.
    # For this audit, we just detect coverage existence.

    return SharpSoftCoverageAuditReport(
        date_range=f"{date_from} to {date_to}",
        markets_requested=markets,
        leagues_requested=leagues,
        sharp_books_configured=sharp_books,
        tables_checked=tables_checked,
        historical_odds_state=ho_state,
        odds_snapshots_state=os_state,
        overall_state=overall_state,
        bookmaker_level_available=len(sharp_found) > 0 or len(soft_found) > 0,
        bookmakers_found=list(bookmakers_found),
        sharp_candidates_found=list(sharp_found),
        soft_candidates_found=list(soft_found),
        aggregate_candidates_found=list(agg_found),
        source_coverage=source_stats,
        market_league_coverage=ml_coverage_final,
        bookmaker_market_coverage={},
        horizon_coverage={},
        timestamp_coverage={},
        close_available=clv_feasible,
        last_pre_kickoff_available=clv_feasible,
        edge_detection_feasible=edge_feasible,
        clv_feasible=clv_feasible,
        blockers=blockers,
        recommended_next_phase="Odds capture improvement"
        if overall_state != "SHARP_SOFT_FEASIBLE"
        else "Sharp/Soft modeling",
    )
