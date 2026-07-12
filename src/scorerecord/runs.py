from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def ingest_run(
    conn: sqlite3.Connection,
    job_name: str,
    *,
    source: str | None = None,
    cwd: Path | None = None,
) -> Iterator[int]:
    run_id = start_run(conn, job_name, source=source, cwd=cwd)
    try:
        yield run_id
    except Exception as exc:
        finish_run(conn, run_id, status="failed", message=str(exc))
        raise
    else:
        finish_run(conn, run_id, status="succeeded")


def start_run(
    conn: sqlite3.Connection,
    job_name: str,
    *,
    source: str | None = None,
    cwd: Path | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO ingest_run (job_name, status, source, code_git_sha)
        VALUES (?, 'running', ?, ?)
        """,
        (job_name, source, _git_sha(cwd)),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    message: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE ingest_run
        SET status = ?, finished_utc = CURRENT_TIMESTAMP, message = ?
        WHERE run_id = ?
        """,
        (status, message, run_id),
    )
    conn.commit()


def record_quality(
    conn: sqlite3.Connection,
    run_id: int | None,
    check_name: str,
    metric_name: str,
    metric_value: float | None,
    details_json: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO data_quality (run_id, check_name, metric_name, metric_value, details_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (run_id, check_name, metric_name, metric_value, details_json),
    )


def _git_sha(cwd: Path | None) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    sha = result.stdout.strip()
    return sha or None
