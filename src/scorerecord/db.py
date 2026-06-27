from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from scorerecord.aliases import normalize_key


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    schema = resources.files("scorerecord.sql").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)
    _run_lightweight_migrations(conn)
    conn.commit()


def execute_many(
    conn: sqlite3.Connection,
    sql: str,
    rows: Iterable[Mapping[str, object] | Sequence[object]],
) -> int:
    cur = conn.executemany(sql, rows)
    conn.commit()
    return cur.rowcount


def table_count(conn: sqlite3.Connection, table_name: str) -> int:
    safe_tables = {
        "team",
        "team_alias",
        "player",
        "match_history",
        "goal_history",
        "fixture",
        "fixture_stats",
        "odds",
        "edge_analysis",
        "player_prop",
        "prediction",
        "prediction_result",
        "recommendation",
        "recommendation_result",
        "historical_match",
        "historical_odds",
        "eval_metric",
        "team_rating",
        "team_rating_history",
        "ingest_run",
        "data_quality",
    }
    if table_name not in safe_tables:
        raise ValueError(f"Unsupported table: {table_name}")
    return int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])


def _run_lightweight_migrations(conn: sqlite3.Connection) -> None:
    _add_column_if_missing(conn, "team", "canonical_key", "TEXT")
    _add_column_if_missing(conn, "team_alias", "alias_key", "TEXT")
    _add_column_if_missing(conn, "prediction", "run_id", "INTEGER REFERENCES ingest_run(run_id)")
    _add_column_if_missing(
        conn,
        "recommendation",
        "run_id",
        "INTEGER REFERENCES ingest_run(run_id)",
    )
    _add_column_if_missing(conn, "eval_metric", "run_id", "INTEGER REFERENCES ingest_run(run_id)")
    _add_column_if_missing(conn, "recommendation", "edge_prob", "REAL")
    _add_column_if_missing(conn, "recommendation", "capped_stake_units", "REAL")
    _add_column_if_missing(conn, "recommendation", "bookmaker", "TEXT")
    _add_column_if_missing(conn, "recommendation", "model_version", "TEXT NOT NULL DEFAULT 'dev'")
    _add_column_if_missing(conn, "recommendation", "odds_captured_utc", "TEXT")
    _add_column_if_missing(conn, "recommendation", "edge_run_id", "INTEGER REFERENCES ingest_run(run_id)")
    _add_column_if_missing(conn, "recommendation", "recommendation_score", "REAL")
    _add_column_if_missing(conn, "recommendation", "odds_quality_flags", "TEXT")
    _add_column_if_missing(conn, "recommendation", "high_edge_reason", "TEXT")
    _add_column_if_missing(conn, "recommendation", "paper", "INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing(conn, "edge_analysis", "elo_diff", "REAL")
    _add_column_if_missing(conn, "edge_analysis", "close_match", "INTEGER")
    _add_column_if_missing(conn, "edge_analysis", "raw_implied_prob", "REAL")
    _add_column_if_missing(conn, "edge_analysis", "market_overround", "REAL")
    _add_column_if_missing(conn, "edge_analysis", "odds_books", "INTEGER")
    _add_column_if_missing(conn, "edge_analysis", "median_odds", "REAL")
    _add_column_if_missing(conn, "edge_analysis", "odds_ratio_to_median", "REAL")
    _add_column_if_missing(conn, "edge_analysis", "odds_age_minutes", "REAL")
    _add_column_if_missing(conn, "edge_analysis", "odds_quality_flags", "TEXT")
    _add_column_if_missing(conn, "edge_analysis", "high_edge_reason", "TEXT")
    _add_column_if_missing(conn, "edge_analysis", "recommendation_score", "REAL")

    for row in conn.execute("SELECT team_id, canonical FROM team WHERE canonical_key IS NULL"):
        conn.execute(
            "UPDATE team SET canonical_key = ? WHERE team_id = ?",
            (normalize_key(row["canonical"]), row["team_id"]),
        )
    for row in conn.execute("SELECT alias, team_id FROM team_alias WHERE alias_key IS NULL"):
        conn.execute(
            "UPDATE team_alias SET alias_key = ? WHERE alias = ? AND team_id = ?",
            (normalize_key(row["alias"]), row["alias"], row["team_id"]),
        )

    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_team_canonical_key ON team(canonical_key)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_team_alias_key ON team_alias(alias_key)")


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    definition: str,
) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
