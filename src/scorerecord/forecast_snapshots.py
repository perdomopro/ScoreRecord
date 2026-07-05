"""Freeze and settle forecast-only GUI snapshots."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from scorerecord.gui import _signal_strength, load_forecast_data
from scorerecord.runs import ingest_run

EPS = 1e-6


@dataclass(frozen=True)
class ForecastSnapshotFreezeSummary:
    target_date: str
    prediction_run_id: int | None
    candidates: int
    inserted: int

    def as_row(self) -> str:
        return (
            f"target_date={self.target_date} "
            f"prediction_run_id={self.prediction_run_id} "
            f"candidates={self.candidates} inserted={self.inserted}"
        )


@dataclass(frozen=True)
class ForecastSnapshotSettlementSummary:
    eligible: int
    settled: int

    def as_row(self) -> str:
        return f"eligible={self.eligible} settled={self.settled}"


@dataclass(frozen=True)
class ForecastReportRow:
    group_type: str
    group_value: str
    n: int
    settled: int
    accuracy: float | None
    brier: float | None
    logloss: float | None

    def as_row(self) -> str:
        accuracy = _fmt(self.accuracy)
        brier = _fmt(self.brier)
        logloss = _fmt(self.logloss)
        return (
            f"{self.group_type}={self.group_value} n={self.n} settled={self.settled} "
            f"accuracy={accuracy} brier={brier} logloss={logloss}"
        )


@dataclass(frozen=True)
class ForecastSnapshotReport:
    total_snapshots: int
    settled_snapshots: int
    pending_snapshots: int
    by_market: list[ForecastReportRow]
    by_signal_code: list[ForecastReportRow]

    def as_rows(self) -> list[str]:
        rows = [
            f"total_snapshots={self.total_snapshots}",
            f"settled_snapshots={self.settled_snapshots}",
            f"pending_snapshots={self.pending_snapshots}",
        ]
        rows.extend(row.as_row() for row in self.by_market)
        rows.extend(row.as_row() for row in self.by_signal_code)
        return rows


def freeze_forecast_snapshot(
    conn: sqlite3.Connection,
    *,
    target_date: str | None = None,
    today: bool = False,
    as_of_utc: datetime | None = None,
    cwd: Path | None = None,
) -> ForecastSnapshotFreezeSummary:
    as_of = as_of_utc or datetime.now(UTC)
    selected_date = None if today else target_date or as_of.date().isoformat()
    data = load_forecast_data(conn, target_date=selected_date, now_utc=as_of)
    if data.latest_run_id is None:
        return ForecastSnapshotFreezeSummary(data.target_date, None, 0, 0)

    candidates = [
        (fixture, market)
        for fixture in data.fixtures
        if fixture.is_resolved and not fixture.is_started
        for market in fixture.primary_markets
        if market.market in {"OU2.5", "BTTS"}
    ]
    inserted = 0
    as_of_text = as_of.isoformat(timespec="seconds")
    with ingest_run(conn, "freeze-forecast-snapshot", source="forecast_gui", cwd=cwd):
        for fixture, market in candidates:
            signal = _signal_strength(market.prob)
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO forecast_snapshot (
                  fixture_id, prediction_run_id, artifact_id, as_of_utc,
                  market, selection, prob, fair_odds, signal_code,
                  model_version, market_validation_status, source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'forecast_gui')
                """,
                (
                    fixture.fixture_id,
                    data.latest_run_id,
                    market.artifact_id,
                    as_of_text,
                    market.market,
                    market.selection,
                    market.prob,
                    market.fair_odds,
                    signal.code,
                    market.model_version,
                    market.market_validation_status,
                ),
            )
            inserted += max(cursor.rowcount, 0)
    conn.commit()
    return ForecastSnapshotFreezeSummary(
        data.target_date,
        data.latest_run_id,
        len(candidates),
        inserted,
    )


def settle_forecast_snapshots(
    conn: sqlite3.Connection,
    *,
    as_of_utc: datetime | None = None,
    cwd: Path | None = None,
) -> ForecastSnapshotSettlementSummary:
    rows = conn.execute(
        """
        SELECT fs.snapshot_id, fs.market, fs.selection, f.home_score, f.away_score
        FROM forecast_snapshot fs
        JOIN fixture f ON f.fixture_id = fs.fixture_id
        WHERE fs.settled = 0
          AND f.home_score IS NOT NULL
          AND f.away_score IS NOT NULL
        ORDER BY fs.snapshot_id
        """
    ).fetchall()
    settled_utc = (as_of_utc or datetime.now(UTC)).isoformat(timespec="seconds")
    settled = 0
    with ingest_run(conn, "settle-forecast-snapshots", source="forecast_snapshot", cwd=cwd):
        for row in rows:
            label = _result_label(
                str(row["market"]),
                int(row["home_score"]),
                int(row["away_score"]),
            )
            correct = _is_correct(str(row["market"]), str(row["selection"]), label)
            if correct is None:
                continue
            conn.execute(
                """
                UPDATE forecast_snapshot
                SET settled = 1,
                    result_label = ?,
                    correct = ?,
                    settled_utc = ?
                WHERE snapshot_id = ?
                """,
                (label, int(correct), settled_utc, row["snapshot_id"]),
            )
            settled += 1
    conn.commit()
    return ForecastSnapshotSettlementSummary(len(rows), settled)


def forecast_snapshot_report(conn: sqlite3.Connection) -> ForecastSnapshotReport:
    totals = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN settled = 1 THEN 1 ELSE 0 END) AS settled
        FROM forecast_snapshot
        """
    ).fetchone()
    total = int(totals["total"] or 0)
    settled = int(totals["settled"] or 0)
    return ForecastSnapshotReport(
        total_snapshots=total,
        settled_snapshots=settled,
        pending_snapshots=total - settled,
        by_market=_report_group(conn, "market"),
        by_signal_code=_report_group(conn, "signal_code"),
    )


def _report_group(conn: sqlite3.Connection, column: str) -> list[ForecastReportRow]:
    if column not in {"market", "signal_code"}:
        raise ValueError(f"Unsupported report group: {column}")
    rows = conn.execute(
        f"""
        SELECT {column} AS group_value, settled, correct, prob
        FROM forecast_snapshot
        ORDER BY {column}
        """,
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["group_value"]), []).append(row)
    report_rows: list[ForecastReportRow] = []
    for group_value, group_rows in grouped.items():
        settled_rows = [row for row in group_rows if int(row["settled"] or 0) == 1]
        accuracy = None
        brier = None
        logloss = None
        if settled_rows:
            correct_values = [int(row["correct"] or 0) for row in settled_rows]
            probs = [float(row["prob"]) for row in settled_rows]
            accuracy = sum(correct_values) / len(correct_values)
            brier = (
                sum(
                    (prob - correct) ** 2
                    for prob, correct in zip(probs, correct_values)
                )
                / len(probs)
            )
            logloss = sum(
                _binary_logloss(prob, correct)
                for prob, correct in zip(probs, correct_values)
            ) / len(probs)
        report_rows.append(
            ForecastReportRow(
                group_type=column,
                group_value=group_value,
                n=len(group_rows),
                settled=len(settled_rows),
                accuracy=accuracy,
                brier=brier,
                logloss=logloss,
            )
        )
    return report_rows


def _result_label(market: str, home_score: int, away_score: int) -> int:
    if market == "OU2.5":
        return int(home_score + away_score > 2.5)
    if market == "BTTS":
        return int(home_score > 0 and away_score > 0)
    raise ValueError(f"Unsupported forecast snapshot market: {market}")


def _is_correct(market: str, selection: str, label: int) -> bool | None:
    if market == "OU2.5":
        if selection == "over":
            return label == 1
        if selection == "under":
            return label == 0
    if market == "BTTS":
        if selection == "yes":
            return label == 1
        if selection == "no":
            return label == 0
    return None


def _binary_logloss(prob: float, correct: int) -> float:
    p = min(max(prob, EPS), 1.0 - EPS)
    return -math.log(p if correct else 1.0 - p)


def _fmt(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "n/a"
    return f"{value:.4f}"
