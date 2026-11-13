"""Storage maintenance: prune superseded predictions and back up the database."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

BACKUP_PREFIX = "scorerecord_"
BACKUP_SUFFIX = ".db"


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
    now_utc: datetime | None = None,
) -> Path:
    """Write a consistent copy of the database and rotate old copies."""
    target_dir = backup_dir_for(db_path)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now_utc or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    target = target_dir / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"
    destination = sqlite3.connect(target)
    try:
        conn.backup(destination)
    finally:
        destination.close()
    rotated = sorted(
        path
        for path in target_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}")
        if _is_rotating_backup(path)
    )
    for old in rotated[: max(len(rotated) - keep, 0)]:
        old.unlink()
    return target


def latest_backup_age_hours(db_path: Path, *, now_utc: datetime | None = None) -> float | None:
    target_dir = backup_dir_for(db_path)
    if not target_dir.is_dir():
        return None
    newest = max(
        (path.stat().st_mtime for path in target_dir.glob(f"*{BACKUP_SUFFIX}")),
        default=None,
    )
    if newest is None:
        return None
    now = (now_utc or datetime.now(UTC)).timestamp()
    return max(now - newest, 0.0) / 3600.0


def _is_rotating_backup(path: Path) -> bool:
    """Only rotate files this module named; manual backups are left alone."""
    stamp = path.name.removeprefix(BACKUP_PREFIX).removesuffix(BACKUP_SUFFIX)
    try:
        datetime.strptime(stamp, "%Y%m%dT%H%M%SZ")
    except ValueError:
        return False
    return True
