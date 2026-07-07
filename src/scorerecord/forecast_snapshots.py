"""Freeze and settle forecast-only GUI snapshots."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scorerecord.forecast_policy import FORECAST_MARKETS, select_primary_forecasts
from scorerecord.forecast_policy import signal_strength as forecast_signal_strength
from scorerecord.forecast_quality import forecast_quality_status_json
from scorerecord.runs import ingest_run
from scorerecord.time_utils import parse_kickoff_utc

DEFAULT_DISPLAY_TZ = "America/Mexico_City"
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


@dataclass(frozen=True)
class SnapshotMarket:
    fixture_id: str
    artifact_id: str | None
    market: str
    selection: str
    prob: float
    fair_odds: float
    model_version: str
    market_validation_status: str


def freeze_forecast_snapshot(
    conn: sqlite3.Connection,
    *,
    target_date: str | None = None,
    today: bool = False,
    as_of_utc: datetime | None = None,
    cwd: Path | None = None,
) -> ForecastSnapshotFreezeSummary:
    as_of = as_of_utc or datetime.now(UTC)
    selected_date = _target_date(target_date=target_date, today=today, as_of=as_of)
    prediction_run_id = _latest_prediction_run_id(conn)
    if prediction_run_id is None:
        return ForecastSnapshotFreezeSummary(selected_date, None, 0, 0)

    candidates = _snapshot_candidates(
        conn,
        prediction_run_id=prediction_run_id,
        target_date=selected_date,
        as_of=as_of,
    )
    inserted = 0
    as_of_text = as_of.isoformat(timespec="seconds")
    with ingest_run(conn, "freeze-forecast-snapshot", source="forecast_gui", cwd=cwd):
        for market in candidates:
            signal = forecast_signal_strength(market.prob)
            quality_status_json = forecast_quality_status_json(
                conn,
                prediction_run_id,
                market.market_validation_status,
            )
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO forecast_snapshot (
                  fixture_id, prediction_run_id, artifact_id, as_of_utc,
                  market, selection, prob, fair_odds, signal_code,
                  model_version, market_validation_status, quality_status_json,
                  source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'forecast_gui')
                """,
                (
                    market.fixture_id,
                    prediction_run_id,
                    market.artifact_id,
                    as_of_text,
                    market.market,
                    market.selection,
                    market.prob,
                    market.fair_odds,
                    signal.code,
                    market.model_version,
                    market.market_validation_status,
                    quality_status_json,
                ),
            )
            inserted += max(cursor.rowcount, 0)
    conn.commit()
    return ForecastSnapshotFreezeSummary(
        selected_date,
        prediction_run_id,
        len(candidates),
        inserted,
    )


def _target_date(*, target_date: str | None, today: bool, as_of: datetime) -> str:
    if target_date and not today:
        return target_date
    local = as_of.astimezone(ZoneInfo(DEFAULT_DISPLAY_TZ))
    return local.date().isoformat()


def _latest_prediction_run_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        """
        SELECT ir.run_id
        FROM ingest_run ir
        JOIN prediction p ON p.run_id = ir.run_id
        WHERE ir.job_name = 'predict-fixtures'
          AND ir.status = 'succeeded'
        GROUP BY ir.run_id
        ORDER BY ir.run_id DESC
        LIMIT 1
        """
    ).fetchone()
    if row:
        return int(row["run_id"])
    fallback = conn.execute(
        "SELECT MAX(run_id) AS run_id FROM prediction WHERE run_id IS NOT NULL"
    ).fetchone()
    return int(fallback["run_id"]) if fallback and fallback["run_id"] is not None else None


def _snapshot_candidates(
    conn: sqlite3.Connection,
    *,
    prediction_run_id: int,
    target_date: str,
    as_of: datetime,
) -> list[SnapshotMarket]:
    rows = _snapshot_prediction_rows(conn, prediction_run_id)
    grouped: dict[str, list[SnapshotMarket]] = {}
    fixture_dates: dict[str, str | None] = {}
    fixture_started: dict[str, bool] = {}
    for row in rows:
        kickoff = parse_kickoff_utc(
            kickoff_utc=row["kickoff_utc"],
            kickoff_local=row["kickoff_local"],
        )
        display_date = (
            kickoff.astimezone(ZoneInfo(DEFAULT_DISPLAY_TZ)).date().isoformat()
            if kickoff
            else None
        )
        fixture_id = str(row["fixture_id"])
        fixture_dates[fixture_id] = display_date
        fixture_started[fixture_id] = bool(kickoff and as_of >= kickoff)
        grouped.setdefault(fixture_id, []).append(
            SnapshotMarket(
                fixture_id=fixture_id,
                artifact_id=row["artifact_id"],
                market=str(row["market"]),
                selection=str(row["selection"]),
                prob=float(row["prob"]),
                fair_odds=float(row["fair_odds"]),
                model_version=str(row["model_version"]),
                market_validation_status=str(row["market_validation_status"] or "NOT_TESTED"),
            )
        )
    candidates: list[SnapshotMarket] = []
    for fixture_id, markets in grouped.items():
        if fixture_dates.get(fixture_id) != target_date:
            continue
        if fixture_started.get(fixture_id):
            continue
        candidates.extend(select_primary_forecasts(markets))
    return candidates


def _snapshot_prediction_rows(
    conn: sqlite3.Connection,
    prediction_run_id: int,
) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in FORECAST_MARKETS)
    return conn.execute(
        f"""
        SELECT f.fixture_id,
               f.kickoff_utc,
               f.kickoff_local,
               p.artifact_id,
               p.market,
               p.selection,
               p.prob,
               p.fair_odds,
               p.model_version,
               COALESCE(ma.market_validation_status, 'NOT_TESTED') AS market_validation_status
        FROM fixture f
        JOIN prediction p ON p.fixture_id = f.fixture_id
        LEFT JOIN model_artifact ma ON ma.artifact_id = p.artifact_id
        WHERE p.run_id = ?
          AND p.market IN ({placeholders})
          AND f.home_id IS NOT NULL
          AND f.away_id IS NOT NULL
          AND f.home_score IS NULL
          AND f.away_score IS NULL
        ORDER BY f.fixture_id, p.market, p.selection
        """,
        (prediction_run_id, *FORECAST_MARKETS),
    ).fetchall()


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
