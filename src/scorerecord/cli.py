from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

from scorerecord.aliases import seed_team_aliases
from scorerecord.db import (
    connect,
    init_db,
    migrate_db,
    schema_migration_rows,
    schema_version,
    table_count,
)
from scorerecord.evaluate.backtest import (
    run_calibration_report,
    run_historical_backtest,
    run_market_benchmark_report,
    run_real_odds_policy_grid,
)
from scorerecord.evaluate.calibration_diagnostics import run_calibration_diagnostics
from scorerecord.evaluate.calibrator_drift import monitor_calibrator_drift
from scorerecord.evaluate.edge import compute_edges
from scorerecord.evaluate.goal_markets import run_goal_market_walk_forward
from scorerecord.evaluate.policy_analysis import analyze_policy_grids
from scorerecord.evaluate.recommendations import (
    compute_recommendation_clv,
    generate_recommendations,
)
from scorerecord.evaluate.settlement import settle_fixtures
from scorerecord.evaluate.tune_decay import run_decay_sweep
from scorerecord.evaluate.walk_forward import run_walk_forward
from scorerecord.features.elo import rebuild_elo
from scorerecord.gui import serve_dashboard
from scorerecord.health import db_health_checks, fixture_ops_summary, recent_failed_runs
from scorerecord.ingest.football_data import (
    football_data_coverage_report,
    football_data_requested_seasons,
    ingest_football_data,
)
from scorerecord.ingest.history import ingest_history
from scorerecord.ingest.odds import fetch_live_odds, odds_snapshot_coverage_report
from scorerecord.ingest.worldcup import ingest_worldcup_2026, resolve_fixture_teams
from scorerecord.models.manifest import build_model_manifest
from scorerecord.predict.fixtures import publish_fixture_predictions
from scorerecord.quality import (
    fixture_resolution_report,
    historical_alias_report,
    record_fixture_resolution_quality,
    record_historical_alias_quality,
)
from scorerecord.runs import ingest_run
from scorerecord.settings import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scorerecord")
    parser.add_argument("--db-path", help="Override SQLite database path.")
    parser.add_argument("--raw-dir", help="Override raw cache directory.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Create/update database schema and seed aliases.")
    subparsers.add_parser("migrate-db", help="Apply idempotent database migrations.")
    subparsers.add_parser("db-version", help="Print applied database migration versions.")
    subparsers.add_parser("db-health", help="Run SQLite, schema, and pipeline health checks.")
    failures = subparsers.add_parser("db-failures", help="Print recent failed ingest runs.")
    failures.add_argument("--days", type=int, default=7)
    failures.add_argument("--limit", type=int, default=10)
    fixture_summary = subparsers.add_parser(
        "fixture-summary",
        help="Print operational fixture coverage for prediction and odds capture.",
    )
    fixture_summary.add_argument("--limit", type=int, default=12)
    resolve_fixture = subparsers.add_parser(
        "resolve-fixture-teams",
        help="Resolve a fixture placeholder to existing team aliases.",
    )
    resolve_fixture.add_argument("--fixture-id", required=True)
    resolve_fixture.add_argument("--home", required=True)
    resolve_fixture.add_argument("--away", required=True)

    worldcup = subparsers.add_parser("ingest-worldcup", help="Ingest World Cup 2026 fixtures.")
    worldcup.add_argument("--refresh", action="store_true", help="Refresh network cache.")

    history = subparsers.add_parser("ingest-history", help="Ingest international results.")
    history.add_argument("--refresh", action="store_true", help="Refresh network cache.")
    history.add_argument("--start-date", default="2018-01-01", help="YYYY-MM-DD lower bound.")
    football_data = subparsers.add_parser(
        "ingest-football-data",
        help="Ingest Football-Data historical results and supported odds markets.",
    )
    football_data.add_argument(
        "--league",
        required=True,
        help="League alias/code, e.g. EPL, E0, Liga MX, MLS, BRA, or ARG.",
    )
    football_data.add_argument("--season", help="Single season, e.g. 2023-2024.")
    football_data.add_argument("--from-season", help="Inclusive season range start.")
    football_data.add_argument("--to-season", help="Inclusive season range end.")
    football_data.add_argument("--refresh", action="store_true", help="Refresh network cache.")
    football_data.add_argument("--dry-run", action="store_true")
    historical_odds_report = subparsers.add_parser(
        "historical-odds-report",
        help="Print historical Football-Data match/odds coverage by season.",
    )
    historical_odds_report.add_argument("--league", help="League alias/code filter.")
    historical_odds_report.add_argument("--market", default="1X2", help="1X2 or OU2.5.")

    subparsers.add_parser("build-elo", help="Rebuild Elo ratings from match_history.")
    subparsers.add_parser("quality-report", help="Print data quality checks.")
    walk_forward = subparsers.add_parser("walk-forward", help="Backtest 1X2 with walk-forward.")
    walk_forward.add_argument("--start-year", type=int, default=2021)
    tune_decay = subparsers.add_parser("tune-decay", help="Sweep Dixon-Coles time decay.")
    tune_decay.add_argument("--start-year", type=int, default=2021)
    tune_decay.add_argument(
        "--half-lives",
        default="0.5,1.0,1.5,2.0,3.0",
        help="Comma-separated half-life candidates in years.",
    )
    tune_decay.add_argument("--l2", type=float, default=1.0)
    goal_markets = subparsers.add_parser(
        "goal-markets",
        help="Backtest Dixon-Coles-derived OU and BTTS markets.",
    )
    goal_markets.add_argument("--start-year", type=int, default=2021)
    goal_markets.add_argument(
        "--ou-lines",
        default="1.5,2.5,3.5",
        help="Comma-separated over/under lines.",
    )
    predict_fixtures = subparsers.add_parser(
        "predict-fixtures",
        help="Publish Dixon-Coles predictions for resolved tournament fixtures.",
    )
    predict_fixtures.add_argument(
        "--ou-lines",
        default="1.5,2.5,3.5",
        help="Comma-separated over/under lines.",
    )
    predict_fixtures.add_argument("--exact-top-n", type=int, default=5)
    subparsers.add_parser("model-manifest", help="Print the frozen model manifest.")
    settle = subparsers.add_parser("settle-fixtures", help="Settle finished fixtures.")
    settle.add_argument("--as-of", help="Settlement timestamp override.")
    settle.add_argument("--dry-run", action="store_true")
    settle.add_argument("--include-settled", action="store_true")
    settle.add_argument("--allow-backfill", action="store_true")
    odds = subparsers.add_parser("fetch-odds", help="Fetch raw odds from Odds-API.io.")
    odds.add_argument("--sport", default="soccer")
    odds.add_argument("--league", help="Odds-API.io league slug.")
    odds.add_argument("--markets", default="h2h,totals,btts")
    odds.add_argument("--bookmakers", help="Comma-separated Odds-API.io bookmaker names.")
    odds.add_argument("--fixture-id")
    odds.add_argument("--max-requests", type=int, default=90)
    odds.add_argument("--min-interval-seconds", type=float, default=36.0)
    odds.add_argument(
        "--closing",
        action="store_true",
        help=(
            "Deprecated compatibility flag: limit to the preplay window, but store raw odds; "
            "closing prices are derived later from odds_snapshots."
        ),
    )
    odds.add_argument(
        "--closing-window-minutes",
        type=int,
        default=60,
        help="Deprecated alias used with --closing for preplay-window raw capture.",
    )
    odds.add_argument(
        "--preplay-window-minutes",
        type=int,
        help="Fetch raw odds only for fixtures starting within this many minutes.",
    )
    odds.add_argument("--dry-run", action="store_true")
    snapshot_report = subparsers.add_parser(
        "odds-snapshot-report",
        help="Print live odds snapshot coverage by fixture.",
    )
    snapshot_report.add_argument("--limit", type=int, default=50)
    edge = subparsers.add_parser("compute-edge", help="De-vig odds and compute model edge.")
    edge.add_argument("--bookmaker")
    edge.add_argument("--min-odds", type=float, default=1.10)
    edge.add_argument("--max-odds", type=float, default=15.0)
    edge.add_argument("--dry-run", action="store_true")
    recommend = subparsers.add_parser("recommend", help="Generate paper recommendations.")
    recommend.add_argument("--edge-run-id", type=int)
    recommend.add_argument("--min-edge", type=float, default=0.05)
    recommend.add_argument("--max-edge", type=float, default=0.25)
    recommend.add_argument("--min-prob", type=float, default=0.15)
    recommend.add_argument("--min-score", type=float, default=0.05)
    recommend.add_argument("--max-odds-age-minutes", type=float, default=180.0)
    recommend.add_argument("--min-books", type=int, default=2)
    recommend.add_argument("--max-picks-per-fixture", type=int, default=1)
    recommend.add_argument("--max-picks-per-day", type=int)
    recommend.add_argument(
        "--allow-odds-quality-flags",
        action="store_true",
        help="Allow stale, low-depth, or outlier odds into paper recommendations.",
    )
    recommend.add_argument("--kelly-fraction", type=float, default=0.25)
    recommend.add_argument("--max-stake", type=float, default=1.0)
    recommend.add_argument("--dry-run", action="store_true")
    clv = subparsers.add_parser("compute-clv", help="Compute CLV for paper recommendations.")
    clv.add_argument("--dry-run", action="store_true")
    drift = subparsers.add_parser("calibrator-drift", help="Monitor Platt coefficient drift.")
    drift.add_argument("--threshold", type=float, default=0.10)
    backtest = subparsers.add_parser(
        "backtest",
        help="Historical walk-forward recommendation backtest with proxy odds.",
    )
    backtest.add_argument("--from", dest="from_date", required=True, help="YYYY-MM-DD")
    backtest.add_argument("--to", dest="to_date", required=True, help="YYYY-MM-DD")
    backtest.add_argument("--out-dir", default="outputs/backtests")
    backtest.add_argument("--start-year", type=int, default=2021)
    backtest.add_argument("--min-train", type=int, default=200)
    backtest.add_argument("--min-edge", type=float, default=0.05)
    backtest.add_argument("--max-edge", type=float, default=0.25)
    backtest.add_argument("--min-prob", type=float, default=0.15)
    backtest.add_argument("--min-score", type=float, default=0.0)
    backtest.add_argument("--max-picks-per-fixture", type=int, default=1)
    backtest.add_argument("--max-picks-per-day", type=int)
    backtest.add_argument("--odds-mode", choices=("proxy", "historical"), default="proxy")
    backtest.add_argument("--historical-odds-source", default="football-data")
    backtest.add_argument("--entry-odds", default="avg_open")
    backtest.add_argument("--closing-odds", default="avg_close")
    backtest.add_argument("--market", default="1X2")
    backtest.add_argument("--league", help="Historical odds league alias/code filter.")
    backtest.add_argument("--season", help="Historical odds season filter, e.g. 2023-2024.")
    policy_grid = subparsers.add_parser(
        "backtest-policy-grid",
        help="Compare historical real-odds selection policy gates without refitting per config.",
    )
    policy_grid.add_argument("--from", dest="from_date", required=True, help="YYYY-MM-DD")
    policy_grid.add_argument("--to", dest="to_date", required=True, help="YYYY-MM-DD")
    policy_grid.add_argument("--out-dir", default="outputs/backtests")
    policy_grid.add_argument("--start-year", type=int, default=2021)
    policy_grid.add_argument("--min-train", type=int, default=200)
    policy_grid.add_argument("--min-scores", default="0,0.05,0.075,0.10,0.125")
    policy_grid.add_argument("--min-edges", default="0.03,0.05,0.075")
    policy_grid.add_argument("--max-picks-per-days", default="none,10,5,3")
    policy_grid.add_argument("--market-shrink-alphas", default="1.0")
    policy_grid.add_argument("--max-edge", type=float, default=0.25)
    policy_grid.add_argument("--min-prob", type=float, default=0.15)
    policy_grid.add_argument("--max-picks-per-fixture", type=int, default=1)
    policy_grid.add_argument("--historical-odds-source", default="football-data")
    policy_grid.add_argument("--entry-odds", default="avg_open")
    policy_grid.add_argument("--closing-odds", default="avg_close")
    policy_grid.add_argument("--market", default="1X2")
    policy_grid.add_argument("--league", help="Historical odds league alias/code filter.")
    policy_grid.add_argument("--season", help="Historical odds season filter, e.g. 2023-2024.")
    benchmark = subparsers.add_parser(
        "market-benchmark-report",
        help=(
            "Compare walk-forward model probabilities against fair historical "
            "market probabilities."
        ),
    )
    benchmark.add_argument("--from", dest="from_date", required=True, help="YYYY-MM-DD")
    benchmark.add_argument("--to", dest="to_date", required=True, help="YYYY-MM-DD")
    benchmark.add_argument("--out-dir", default="outputs/backtests")
    benchmark.add_argument("--start-year", type=int, default=2021)
    benchmark.add_argument("--min-train", type=int, default=200)
    benchmark.add_argument("--historical-odds-source", default="football-data")
    benchmark.add_argument("--entry-odds", default="avg_open")
    benchmark.add_argument("--closing-odds", default="avg_close")
    benchmark.add_argument("--market", default="1X2")
    benchmark.add_argument("--league", help="Historical odds league alias/code filter.")
    benchmark.add_argument("--season", help="Historical odds season filter, e.g. 2023-2024.")
    diagnostics = subparsers.add_parser(
        "calibration-diagnostics",
        help="Write segmented calibration and model-vs-market diagnostics for historical odds.",
    )
    diagnostics.add_argument("--from", dest="from_date", required=True, help="YYYY-MM-DD")
    diagnostics.add_argument("--to", dest="to_date", required=True, help="YYYY-MM-DD")
    diagnostics.add_argument(
        "--leagues",
        required=True,
        help="Comma-separated league aliases/codes, e.g. EPL,LA_LIGA,SERIE_A.",
    )
    diagnostics.add_argument("--out-dir", default="outputs/diagnostics")
    diagnostics.add_argument("--start-year", type=int, default=2021)
    diagnostics.add_argument("--min-train", type=int, default=500)
    diagnostics.add_argument("--min-segment-rows", type=int, default=100)
    diagnostics.add_argument("--historical-odds-source", default="football-data")
    diagnostics.add_argument("--entry-odds", default="avg_open")
    diagnostics.add_argument("--closing-odds", default="avg_close")
    analyze_grids = subparsers.add_parser(
        "analyze-policy-grids",
        help="Aggregate policy-grid CSVs across leagues and flag candidate/review/reject configs.",
    )
    analyze_grids.add_argument("inputs", nargs="+", help="CSV files, globs, or comma lists.")
    analyze_grids.add_argument("--out", default="outputs/backtests/policy_grid_summary.csv")
    calibration = subparsers.add_parser(
        "calibration-report",
        help="Historical walk-forward calibration report by market and probability bucket.",
    )
    calibration.add_argument("--from", dest="from_date", required=True, help="YYYY-MM-DD")
    calibration.add_argument("--to", dest="to_date", required=True, help="YYYY-MM-DD")
    calibration.add_argument("--out-dir", default="outputs/backtests")
    calibration.add_argument("--start-year", type=int, default=2021)
    calibration.add_argument("--min-train", type=int, default=200)
    gui = subparsers.add_parser("gui", help="Serve the read-only paper/audit dashboard.")
    gui.add_argument("--host", default="127.0.0.1")
    gui.add_argument("--port", type=int, default=8765)
    gui.add_argument("--refresh-seconds", type=int, default=60)
    subparsers.add_parser("db-summary", help="Print useful database counts.")

    args = parser.parse_args(argv)
    settings = load_settings()
    if args.db_path:
        settings = replace(settings, db_path=Path(args.db_path))
    if args.raw_dir:
        settings = replace(settings, raw_dir=Path(args.raw_dir))

    if args.command == "gui":
        serve_dashboard(
            settings.db_path,
            host=args.host,
            port=args.port,
            refresh_seconds=args.refresh_seconds,
        )
        return 0

    with connect(settings.db_path) as conn:
        if args.command == "init-db":
            init_db(conn)
            seed_team_aliases(conn)
            print(f"Initialized {settings.db_path}")
            return 0

        if args.command == "migrate-db":
            version = migrate_db(conn)
            print(f"Applied database migrations through version {version}")
            return 0

        init_db(conn)

        if args.command == "db-version":
            print(f"schema_version: {schema_version(conn)}")
            for row in schema_migration_rows(conn):
                print(
                    f"migration: version={row['version']} "
                    f"name={row['name']} applied_utc={row['applied_utc']}"
                )
            return 0

        if args.command == "db-health":
            checks = db_health_checks(conn)
            for check in checks:
                print(check.as_row())
            return 0

        if args.command == "db-failures":
            rows = recent_failed_runs(conn, days=args.days, limit=args.limit)
            if not rows:
                print("No failed runs found.")
                return 0
            for row in rows:
                print(row.as_row())
            return 0

        if args.command == "fixture-summary":
            summary = fixture_ops_summary(conn, limit=args.limit)
            print(summary.as_row())
            for row in summary.rows:
                resolved = (
                    "resolved"
                    if row["home_id"] is not None and row["away_id"] is not None
                    else "unresolved"
                )
                score = (
                    "unscored"
                    if row["home_score"] is None or row["away_score"] is None
                    else f"{row['home_score']}-{row['away_score']}"
                )
                print(
                    "fixture: "
                    f"{row['fixture_id']} kickoff_utc={row['kickoff_utc'] or 'na'} "
                    f"home={row['home_placeholder'] or row['home_id'] or 'na'} "
                    f"away={row['away_placeholder'] or row['away_id'] or 'na'} "
                    f"{resolved} {score}"
                )
            return 0

        if args.command == "resolve-fixture-teams":
            resolve_fixture_teams(
                conn,
                fixture_id=args.fixture_id,
                home=args.home,
                away=args.away,
            )
            print(
                "resolved_fixture: "
                f"{args.fixture_id} home={args.home} away={args.away}"
            )
            return 0

        if args.command == "ingest-worldcup":
            with ingest_run(
                conn,
                "ingest-worldcup",
                source="openfootball",
                cwd=Path.cwd(),
            ) as run_id:
                count = ingest_worldcup_2026(conn, settings.raw_dir, refresh=args.refresh)
                report = record_fixture_resolution_quality(conn, run_id)
            print(f"Upserted {count} fixtures")
            print(
                "Fixture resolution: "
                f"{report.full_resolved}/{report.total} fully resolved "
                f"({report.resolved_ratio:.1%})"
            )
            return 0

        if args.command == "ingest-history":
            start_date = date.fromisoformat(args.start_date)
            with ingest_run(conn, "ingest-history", source="martj42", cwd=Path.cwd()) as run_id:
                match_count, goal_count = ingest_history(
                    conn,
                    settings.raw_dir,
                    start_date=start_date,
                    refresh=args.refresh,
                )
                record_historical_alias_quality(conn, run_id)
            print(f"Upserted {match_count} historical matches and {goal_count} goals")
            return 0

        if args.command == "ingest-football-data":
            seasons = football_data_requested_seasons(
                league=args.league,
                season=args.season,
                from_season=args.from_season,
                to_season=args.to_season,
            )
            totals = {
                "rows_seen": 0,
                "events_upserted": 0,
                "odds_upserted": 0,
                "missing_results": 0,
                "missing_open_odds": 0,
                "missing_closing_odds": 0,
                "invalid_odds": 0,
                "duplicate_events": 0,
            }
            for season in seasons:
                summary = ingest_football_data(
                    conn,
                    settings.raw_dir,
                    league=args.league,
                    season=season,
                    refresh=args.refresh,
                    dry_run=args.dry_run,
                    cwd=Path.cwd(),
                )
                print(f"{season}: {summary.as_row()}")
                for key in totals:
                    totals[key] += int(getattr(summary, key))
            if len(seasons) > 1:
                print(
                    "total: "
                    f"seasons={len(seasons)} rows_seen={totals['rows_seen']} "
                    f"events_upserted={totals['events_upserted']} "
                    f"odds_upserted={totals['odds_upserted']} "
                    f"missing_results={totals['missing_results']} "
                    f"missing_open_odds={totals['missing_open_odds']} "
                    f"missing_closing_odds={totals['missing_closing_odds']} "
                    f"invalid_odds={totals['invalid_odds']} "
                    f"duplicate_events={totals['duplicate_events']} "
                    f"dry_run={args.dry_run}"
                )
            return 0

        if args.command == "historical-odds-report":
            rows = football_data_coverage_report(conn, league=args.league, market=args.market)
            if not rows:
                print("No historical odds rows found.")
                return 0
            for row in rows:
                print(row.as_row())
            return 0

        if args.command == "build-elo":
            with ingest_run(conn, "build-elo", source="match_history", cwd=Path.cwd()):
                count = rebuild_elo(conn)
            print(f"Built Elo ratings for {count} teams")
            return 0

        if args.command == "quality-report":
            report = fixture_resolution_report(conn)
            aliases = historical_alias_report(conn)
            print(
                "fixture_resolution: "
                f"{report.full_resolved}/{report.total} fully resolved "
                f"({report.resolved_ratio:.1%})"
            )
            for row in report.unresolved_rows[:25]:
                print(
                    "unresolved_fixture: "
                    f"{row['fixture_id']} home={row['home_placeholder']} "
                    f"away={row['away_placeholder']}"
                )
            if len(report.unresolved_rows) > 25:
                print(f"... {len(report.unresolved_rows) - 25} more unresolved fixtures")
            print(
                "historical_aliases: "
                f"{aliases.seed_backed_teams}/{aliases.historical_teams} seed-backed "
                f"({aliases.seed_backed_ratio:.1%}); "
                f"{aliases.auto_created_teams} auto-created historical teams"
            )
            print(f"duplicate_canonical_keys: {len(aliases.duplicate_key_rows)}")
            for row in aliases.duplicate_key_rows[:25]:
                print(
                    "duplicate_key: "
                    f"{row['canonical_key']} count={row['c']} names={row['canonical_names']}"
                )
            return 0

        if args.command == "walk-forward":
            results = run_walk_forward(conn, start_year=args.start_year, cwd=Path.cwd())
            for result in results:
                print(result.as_row())
            return 0

        if args.command == "tune-decay":
            half_lives = tuple(float(value.strip()) for value in args.half_lives.split(","))
            trials, best = run_decay_sweep(
                conn,
                start_year=args.start_year,
                half_lives=half_lives,
                l2=args.l2,
                cwd=Path.cwd(),
            )
            for trial in trials:
                print(trial.as_row())
            close = "na" if best.close_logloss is None else f"{best.close_logloss:.4f}"
            print(f"Best half-life: {best.half_life_years}y (close_ll={close})")
            return 0

        if args.command == "goal-markets":
            ou_lines = tuple(float(value.strip()) for value in args.ou_lines.split(","))
            results = run_goal_market_walk_forward(
                conn,
                start_year=args.start_year,
                ou_lines=ou_lines,
                cwd=Path.cwd(),
            )
            for result in results:
                print(result.as_row())
            return 0

        if args.command == "predict-fixtures":
            ou_lines = tuple(float(value.strip()) for value in args.ou_lines.split(","))
            summary = publish_fixture_predictions(
                conn,
                ou_lines=ou_lines,
                exact_top_n=args.exact_top_n,
                cwd=Path.cwd(),
            )
            print(summary.as_row())
            return 0

        if args.command == "model-manifest":
            print(json.dumps(build_model_manifest(conn).as_dict(), indent=2, sort_keys=True))
            return 0

        if args.command == "settle-fixtures":
            summary = settle_fixtures(
                conn,
                as_of=args.as_of,
                only_unsettled=not args.include_settled,
                allow_backfill=args.allow_backfill,
                dry_run=args.dry_run,
                cwd=Path.cwd(),
            )
            print(summary.as_row())
            return 0

        if args.command == "fetch-odds":
            preplay_window_minutes = (
                args.preplay_window_minutes
                if args.preplay_window_minutes is not None
                else args.closing_window_minutes if args.closing else None
            )
            summary = fetch_live_odds(
                conn,
                api_key=settings.odds_api_io_key,
                base_url=settings.odds_api_io_base_url,
                sport=args.sport,
                league=args.league or settings.odds_api_io_league,
                markets=args.markets,
                bookmakers=args.bookmakers or settings.odds_api_io_bookmakers,
                fixture_id=args.fixture_id,
                max_requests=args.max_requests,
                dry_run=args.dry_run,
                is_closing=args.closing or args.preplay_window_minutes is not None,
                closing_window_minutes=preplay_window_minutes,
                min_interval_seconds=args.min_interval_seconds,
                cwd=Path.cwd(),
            )
            print(summary.as_row())
            return 0

        if args.command == "odds-snapshot-report":
            rows = odds_snapshot_coverage_report(conn, limit=args.limit)
            if not rows:
                print("No odds snapshots found.")
                return 0
            for row in rows:
                print(row.as_row())
            return 0

        if args.command == "compute-edge":
            summary = compute_edges(
                conn,
                bookmaker=args.bookmaker,
                min_odds=args.min_odds,
                max_odds=args.max_odds,
                dry_run=args.dry_run,
                cwd=Path.cwd(),
            )
            print(summary.as_row())
            return 0

        if args.command == "recommend":
            summary = generate_recommendations(
                conn,
                edge_run_id=args.edge_run_id,
                min_edge=args.min_edge,
                max_edge=args.max_edge,
                min_prob=args.min_prob,
                min_score=args.min_score,
                max_odds_age_minutes=args.max_odds_age_minutes,
                min_books=args.min_books,
                max_picks_per_fixture=args.max_picks_per_fixture,
                max_picks_per_day=args.max_picks_per_day,
                exclude_odds_quality_flags=not args.allow_odds_quality_flags,
                kelly_fraction=args.kelly_fraction,
                max_stake=args.max_stake,
                dry_run=args.dry_run,
                cwd=Path.cwd(),
            )
            print(summary.as_row())
            return 0

        if args.command == "compute-clv":
            summary = compute_recommendation_clv(
                conn,
                dry_run=args.dry_run,
                cwd=Path.cwd(),
            )
            print(summary.as_row())
            return 0

        if args.command == "calibrator-drift":
            summary = monitor_calibrator_drift(
                conn,
                threshold=args.threshold,
                cwd=Path.cwd(),
            )
            print(summary.as_row())
            return 0

        if args.command == "backtest":
            summary = run_historical_backtest(
                conn,
                from_date=date.fromisoformat(args.from_date),
                to_date=date.fromisoformat(args.to_date),
                out_dir=Path(args.out_dir),
                start_year=args.start_year,
                min_train=args.min_train,
                min_edge=args.min_edge,
                max_edge=args.max_edge,
                min_prob=args.min_prob,
                min_score=args.min_score,
                max_picks_per_fixture=args.max_picks_per_fixture,
                max_picks_per_day=args.max_picks_per_day,
                odds_mode=args.odds_mode,
                historical_odds_source=args.historical_odds_source,
                entry_odds=args.entry_odds,
                closing_odds=args.closing_odds,
                market=args.market,
                league=args.league,
                season=args.season,
                cwd=Path.cwd(),
            )
            print(summary.as_row())
            return 0

        if args.command == "backtest-policy-grid":
            summary = run_real_odds_policy_grid(
                conn,
                from_date=date.fromisoformat(args.from_date),
                to_date=date.fromisoformat(args.to_date),
                out_dir=Path(args.out_dir),
                start_year=args.start_year,
                min_train=args.min_train,
                min_scores=_parse_float_tuple(args.min_scores),
                min_edges=_parse_float_tuple(args.min_edges),
                max_picks_per_days=_parse_optional_int_tuple(args.max_picks_per_days),
                market_shrink_alphas=_parse_float_tuple(args.market_shrink_alphas),
                max_edge=args.max_edge,
                min_prob=args.min_prob,
                max_picks_per_fixture=args.max_picks_per_fixture,
                historical_odds_source=args.historical_odds_source,
                entry_odds=args.entry_odds,
                closing_odds=args.closing_odds,
                market=args.market,
                league=args.league,
                season=args.season,
                cwd=Path.cwd(),
            )
            print(summary.as_row())
            return 0

        if args.command == "market-benchmark-report":
            summary = run_market_benchmark_report(
                conn,
                from_date=date.fromisoformat(args.from_date),
                to_date=date.fromisoformat(args.to_date),
                out_dir=Path(args.out_dir),
                start_year=args.start_year,
                min_train=args.min_train,
                historical_odds_source=args.historical_odds_source,
                entry_odds=args.entry_odds,
                closing_odds=args.closing_odds,
                market=args.market,
                league=args.league,
                season=args.season,
                cwd=Path.cwd(),
            )
            print(summary.as_row())
            return 0

        if args.command == "calibration-diagnostics":
            summary = run_calibration_diagnostics(
                conn,
                from_date=date.fromisoformat(args.from_date),
                to_date=date.fromisoformat(args.to_date),
                leagues=_parse_str_tuple(args.leagues),
                out_dir=Path(args.out_dir),
                start_year=args.start_year,
                min_train=args.min_train,
                min_segment_rows=args.min_segment_rows,
                historical_odds_source=args.historical_odds_source,
                entry_odds=args.entry_odds,
                closing_odds=args.closing_odds,
                cwd=Path.cwd(),
            )
            print(summary.as_row())
            return 0

        if args.command == "analyze-policy-grids":
            summary = analyze_policy_grids(
                conn,
                inputs=args.inputs,
                out_path=Path(args.out),
                cwd=Path.cwd(),
            )
            print(summary.as_row())
            return 0

        if args.command == "calibration-report":
            summary = run_calibration_report(
                conn,
                from_date=date.fromisoformat(args.from_date),
                to_date=date.fromisoformat(args.to_date),
                out_dir=Path(args.out_dir),
                start_year=args.start_year,
                min_train=args.min_train,
                cwd=Path.cwd(),
            )
            print(summary.as_row())
            return 0

        if args.command == "db-summary":
            for table in [
                "team",
                "team_alias",
                "player",
                "match_history",
                "goal_history",
                "fixture",
                "odds",
                "edge_analysis",
                "model_artifact",
                "prediction",
                "prediction_result",
                "team_rating",
                "team_rating_history",
                "recommendation",
                "recommendation_result",
                "historical_match",
                "historical_odds",
                "odds_snapshots",
                "eval_metric",
                "ingest_run",
                "data_quality",
                "schema_migration",
            ]:
                print(f"{table}: {table_count(conn, table)}")
            return 0

    return 1


def _parse_float_tuple(value: str) -> tuple[float, ...]:
    return tuple(float(item.strip()) for item in value.split(",") if item.strip())


def _parse_str_tuple(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _parse_optional_int_tuple(value: str) -> tuple[int | None, ...]:
    output: list[int | None] = []
    for item in value.split(","):
        stripped = item.strip().casefold()
        if not stripped:
            continue
        output.append(None if stripped in {"none", "null", "off"} else int(stripped))
    return tuple(output)


if __name__ == "__main__":
    raise SystemExit(main())
