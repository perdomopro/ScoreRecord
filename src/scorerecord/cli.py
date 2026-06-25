from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

from scorerecord.aliases import seed_team_aliases
from scorerecord.db import connect, init_db, table_count
from scorerecord.evaluate.edge import compute_edges
from scorerecord.evaluate.goal_markets import run_goal_market_walk_forward
from scorerecord.evaluate.recommendations import compute_recommendation_clv, generate_recommendations
from scorerecord.evaluate.settlement import settle_fixtures
from scorerecord.evaluate.tune_decay import run_decay_sweep
from scorerecord.evaluate.walk_forward import run_walk_forward
from scorerecord.features.elo import rebuild_elo
from scorerecord.ingest.history import ingest_history
from scorerecord.ingest.odds import fetch_live_odds
from scorerecord.ingest.worldcup import ingest_worldcup_2026
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

    worldcup = subparsers.add_parser("ingest-worldcup", help="Ingest World Cup 2026 fixtures.")
    worldcup.add_argument("--refresh", action="store_true", help="Refresh network cache.")

    history = subparsers.add_parser("ingest-history", help="Ingest international results.")
    history.add_argument("--refresh", action="store_true", help="Refresh network cache.")
    history.add_argument("--start-date", default="2018-01-01", help="YYYY-MM-DD lower bound.")

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
    odds.add_argument("--markets", default="h2h,totals,btts")
    odds.add_argument("--bookmakers", help="Comma-separated Odds-API.io bookmaker names.")
    odds.add_argument("--fixture-id")
    odds.add_argument("--max-requests", type=int, default=90)
    odds.add_argument("--min-interval-seconds", type=float, default=36.0)
    odds.add_argument("--closing", action="store_true", help="Store odds as closing prices.")
    odds.add_argument("--dry-run", action="store_true")
    edge = subparsers.add_parser("compute-edge", help="De-vig odds and compute model edge.")
    edge.add_argument("--bookmaker")
    edge.add_argument("--min-odds", type=float, default=1.10)
    edge.add_argument("--max-odds", type=float, default=15.0)
    edge.add_argument("--dry-run", action="store_true")
    recommend = subparsers.add_parser("recommend", help="Generate paper recommendations.")
    recommend.add_argument("--edge-run-id", type=int)
    recommend.add_argument("--min-edge", type=float, default=0.05)
    recommend.add_argument("--min-prob", type=float, default=0.15)
    recommend.add_argument("--kelly-fraction", type=float, default=0.25)
    recommend.add_argument("--max-stake", type=float, default=1.0)
    recommend.add_argument("--dry-run", action="store_true")
    clv = subparsers.add_parser("compute-clv", help="Compute CLV for paper recommendations.")
    clv.add_argument("--dry-run", action="store_true")
    subparsers.add_parser("db-summary", help="Print useful database counts.")

    args = parser.parse_args(argv)
    settings = load_settings()
    if args.db_path:
        settings = replace(settings, db_path=Path(args.db_path))
    if args.raw_dir:
        settings = replace(settings, raw_dir=Path(args.raw_dir))

    with connect(settings.db_path) as conn:
        if args.command == "init-db":
            init_db(conn)
            seed_team_aliases(conn)
            print(f"Initialized {settings.db_path}")
            return 0

        init_db(conn)

        if args.command == "ingest-worldcup":
            with ingest_run(conn, "ingest-worldcup", source="openfootball", cwd=Path.cwd()) as run_id:
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
                    f"{row['fixture_id']} home={row['home_placeholder']} away={row['away_placeholder']}"
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
            summary = fetch_live_odds(
                conn,
                api_key=settings.odds_api_io_key,
                base_url=settings.odds_api_io_base_url,
                sport=args.sport,
                markets=args.markets,
                bookmakers=args.bookmakers or settings.odds_api_io_bookmakers,
                fixture_id=args.fixture_id,
                max_requests=args.max_requests,
                dry_run=args.dry_run,
                is_closing=args.closing,
                min_interval_seconds=args.min_interval_seconds,
                cwd=Path.cwd(),
            )
            print(summary.as_row())
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
                min_prob=args.min_prob,
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
                "prediction",
                "prediction_result",
                "team_rating",
                "team_rating_history",
                "recommendation",
                "recommendation_result",
                "eval_metric",
                "ingest_run",
                "data_quality",
            ]:
                print(f"{table}: {table_count(conn, table)}")
            return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
