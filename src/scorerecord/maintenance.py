"""Storage maintenance: prune superseded predictions and back up the database."""

from __future__ import annotations

import gzip
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

BACKUP_PREFIX = "scorerecord_"
BACKUP_SUFFIX = ".db"
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


@dataclass(frozen=True)
class PruneSummary:
    rows_before: int
    rows_deleted: int
    dry_run: bool

    def as_row(self) -> str:
        return (
            f"rows_before={self.rows_before} rows_deleted={self.rows_deleted} "
            f"rows_after={self.rows_before - self.rows_deleted} dry_run={self.dry_run}"
        )


def prune_predictions(conn: sqlite3.Connection, *, dry_run: bool = False) -> PruneSummary:
    """Delete prediction rows superseded by a newer run for the same fixture.

    Kept per fixture: the latest run and every run a frozen forecast snapshot
    points at. Settled rows live in ``prediction_result`` and are unaffected.
    """
    rows_before = int(conn.execute("SELECT COUNT(*) FROM prediction").fetchone()[0])
    stale_filter = """
        FROM prediction
        WHERE run_id IS NOT NULL
          AND run_id < (
            SELECT MAX(p2.run_id) FROM prediction p2
            WHERE p2.fixture_id = prediction.fixture_id
          )
          AND NOT EXISTS (
            SELECT 1 FROM forecast_snapshot fs
            WHERE fs.fixture_id = prediction.fixture_id
              AND fs.prediction_run_id = prediction.run_id
          )
    """
    if dry_run:
        deleted = int(conn.execute(f"SELECT COUNT(*) {stale_filter}").fetchone()[0])
    else:
        deleted = conn.execute(f"DELETE {stale_filter}").rowcount
        # Score grids follow their prediction rows.
        conn.execute(
            """
            DELETE FROM prediction_score_grid
            WHERE NOT EXISTS (
              SELECT 1 FROM prediction p
              WHERE p.run_id = prediction_score_grid.run_id
                AND p.fixture_id = prediction_score_grid.fixture_id
            )
            """
        )
        conn.commit()
    return PruneSummary(rows_before=rows_before, rows_deleted=int(deleted), dry_run=dry_run)


def vacuum(conn: sqlite3.Connection) -> None:
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def backup_dir_for(db_path: Path) -> Path:
    return db_path.parent / "backups"


def backup_database(
    conn: sqlite3.Connection,
    db_path: Path,
    *,
    keep: int = 7,
    weekly: int = 4,
    compress: bool = True,
    now_utc: datetime | None = None,
) -> Path:
    """Write a consistent, gzip-compressed copy of the database and rotate old copies.

    Kept: the newest ``keep`` copies plus the newest copy of each of the last
    ``weekly`` ISO weeks. Restore with ``gunzip -k <file>.db.gz``.
    """
    target_dir = backup_dir_for(db_path)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now_utc or datetime.now(UTC)).strftime(STAMP_FORMAT)
    raw = target_dir / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"
    destination = sqlite3.connect(raw)
    try:
        conn.backup(destination)
    finally:
        destination.close()
    target = raw
    if compress:
        target = raw.with_name(raw.name + ".gz")
        with raw.open("rb") as source, gzip.open(target, "wb", compresslevel=6) as sink:
            shutil.copyfileobj(source, sink, length=1024 * 1024)
        raw.unlink()
    _rotate(target_dir, keep=keep, weekly=weekly)
    return target


def _rotate(target_dir: Path, *, keep: int, weekly: int) -> None:
    stamped = sorted(
        ((stamp, path) for path in target_dir.iterdir() if (stamp := _backup_stamp(path))),
        reverse=True,
    )
    keep_paths = {path for _, path in stamped[:keep]}
    weeks_seen: list[tuple[int, int]] = []
    for stamp, path in stamped:
        week = stamp.isocalendar()[:2]
        if week in weeks_seen:
            continue
        if len(weeks_seen) >= weekly:
            break
        weeks_seen.append(week)
        keep_paths.add(path)
    for _, path in stamped:
        if path not in keep_paths:
            path.unlink()


def latest_backup_age_hours(db_path: Path, *, now_utc: datetime | None = None) -> float | None:
    target_dir = backup_dir_for(db_path)
    if not target_dir.is_dir():
        return None
    newest = max(
        (
            path.stat().st_mtime
            for path in target_dir.iterdir()
            if path.name.endswith((BACKUP_SUFFIX, BACKUP_SUFFIX + ".gz"))
        ),
        default=None,
    )
    if newest is None:
        return None
    now = (now_utc or datetime.now(UTC)).timestamp()
    return max(now - newest, 0.0) / 3600.0


def _backup_stamp(path: Path) -> datetime | None:
    """Timestamp of a backup this module wrote; manual backups are left alone."""
    name = path.name
    if not name.startswith(BACKUP_PREFIX):
        return None
    for suffix in (BACKUP_SUFFIX + ".gz", BACKUP_SUFFIX):
        if name.endswith(suffix):
            stamp = name.removeprefix(BACKUP_PREFIX).removesuffix(suffix)
            try:
                return datetime.strptime(stamp, STAMP_FORMAT).replace(tzinfo=UTC)
            except ValueError:
                return None
    return None
