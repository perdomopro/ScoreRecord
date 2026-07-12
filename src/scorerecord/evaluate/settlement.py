"""Settlement loop for published fixture predictions."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from scorerecord.evaluate.scoring import binary_brier, binary_logloss, multiclass_logloss
from scorerecord.runs import ingest_run, record_quality
from scorerecord.time_utils import parse_datetime_utc, parse_kickoff_utc

_SUPPORTED_MARKET_PREFIXES = ("OU",)
_SUPPORTED_MARKETS = {"1X2", "BTTS", "double_chance"}


@dataclass(frozen=True)
class SettlementSummary:
    finished_fixtures: int
    settled_predictions: int
    skipped_existing: int
    unsupported_predictions: int
    blocked_backfill_predictions: int
    missing_prediction_fixtures: int
    dry_run: bool

    def as_row(self) -> str:
        return (
            f"finished_fixtures={self.finished_fixtures} "
            f"settled_predictions={self.settled_predictions} "
            f"skipped_existing={self.skipped_existing} "
            f"unsupported_predictions={self.unsupported_predictions} "
            f"blocked_backfill_predictions={self.blocked_backfill_predictions} "
            f"missing_prediction_fixtures={self.missing_prediction_fixtures} "
            f"dry_run={self.dry_run}"
        )


def settle_fixtures(
    conn: sqlite3.Connection,
    *,
    as_of: str | None = None,
    only_unsettled: bool = True,
    allow_backfill: bool = False,
    dry_run: bool = False,
    cwd: Path | None = None,
) -> SettlementSummary:
    settled_utc = as_of or datetime.now(UTC).isoformat(timespec="seconds")
    finished_fixtures = _finished_fixture_count(conn)
    missing_prediction_fixtures = _missing_prediction_fixture_count(conn)
    rows = _settlement_candidates(conn, only_unsettled=only_unsettled)
    rows, blocked_backfill_predictions = _filter_forward_predictions(
        rows,
        allow_backfill=allow_backfill,
    )

    if dry_run:
        supported = [row for row in rows if _label_for_prediction(row) is not None]
        unsupported = len(rows) - len(supported)
        return SettlementSummary(
            finished_fixtures=finished_fixtures,
            settled_predictions=len(supported),
            skipped_existing=0,
            unsupported_predictions=unsupported,
            blocked_backfill_predictions=blocked_backfill_predictions,
            missing_prediction_fixtures=missing_prediction_fixtures,
            dry_run=True,
        )

    with ingest_run(conn, "settle-fixtures", source="prediction", cwd=cwd) as run_id:
        settled_predictions = 0
        skipped_existing = 0
        unsupported_predictions = 0
        for row in rows:
            label = _label_for_prediction(row)
            if label is None:
                unsupported_predictions += 1
                continue
            prob = float(row["prob"])
            brier = binary_brier(prob, label)
            logloss = binary_logloss(prob, label)
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO prediction_result (
                  settlement_run_id, prediction_run_id, fixture_id, model_version,
                  market, selection, predicted_prob, label, brier, logloss,
                  settled_utc, prediction_generated_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    row["run_id"],
                    row["fixture_id"],
                    row["model_version"],
                    row["market"],
                    row["selection"],
                    prob,
                    label,
                    brier,
                    logloss,
                    settled_utc,
                    row["generated_utc"],
                ),
            )
            if cursor.rowcount:
                settled_predictions += 1
            else:
                skipped_existing += 1

        _write_settlement_eval_metrics(conn, run_id, settled_utc)
        _record_settlement_quality(
            conn,
            run_id,
            SettlementSummary(
                finished_fixtures=finished_fixtures,
                settled_predictions=settled_predictions,
                skipped_existing=skipped_existing,
                unsupported_predictions=unsupported_predictions,
                blocked_backfill_predictions=blocked_backfill_predictions,
                missing_prediction_fixtures=missing_prediction_fixtures,
                dry_run=False,
            ),
        )
        conn.commit()

    return SettlementSummary(
        finished_fixtures=finished_fixtures,
        settled_predictions=settled_predictions,
        skipped_existing=skipped_existing,
        unsupported_predictions=unsupported_predictions,
        blocked_backfill_predictions=blocked_backfill_predictions,
        missing_prediction_fixtures=missing_prediction_fixtures,
        dry_run=False,
    )


def _settlement_candidates(
    conn: sqlite3.Connection,
    *,
    only_unsettled: bool,
) -> list[sqlite3.Row]:
    already_settled_filter = ""
    if only_unsettled:
        already_settled_filter = """
          AND NOT EXISTS (
            SELECT 1
            FROM prediction_result pr
            WHERE pr.prediction_run_id = p.run_id
              AND pr.fixture_id = p.fixture_id
              AND pr.model_version = p.model_version
              AND pr.market = p.market
              AND pr.selection = p.selection
              AND pr.prediction_generated_utc = p.generated_utc
          )
        """
    return conn.execute(
        f"""
        SELECT
          p.run_id, p.fixture_id, p.model, p.market, p.selection, p.prob,
          p.generated_utc, p.model_version,
          f.home_score, f.away_score, f.kickoff_local, f.kickoff_utc
        FROM prediction p
        JOIN fixture f ON f.fixture_id = p.fixture_id
        WHERE f.home_score IS NOT NULL
          AND f.away_score IS NOT NULL
          {already_settled_filter}
        ORDER BY p.fixture_id, p.run_id, p.market, p.selection
        """
    ).fetchall()


def _filter_forward_predictions(
    rows: list[sqlite3.Row],
    *,
    allow_backfill: bool,
) -> tuple[list[sqlite3.Row], int]:
    if allow_backfill:
        return rows, 0
    kept = []
    blocked = 0
    for row in rows:
        kickoff_utc = _row_kickoff_utc(row)
        generated_utc = _parse_generated_utc(str(row["generated_utc"]))
        if kickoff_utc is None or generated_utc is None or generated_utc > kickoff_utc:
            blocked += 1
            continue
        kept.append(row)
    return kept, blocked


def _label_for_prediction(row: sqlite3.Row) -> int | None:
    home_score = int(row["home_score"])
    away_score = int(row["away_score"])
    market = str(row["market"])
    selection = str(row["selection"])

    if market == "1X2":
        actual = "home" if home_score > away_score else "away" if away_score > home_score else "draw"
        return 1 if selection == actual else 0
    if market == "BTTS":
        actual = "yes" if home_score >= 1 and away_score >= 1 else "no"
        return 1 if selection == actual else 0
    if market.startswith(_SUPPORTED_MARKET_PREFIXES):
        try:
            line = float(market[2:])
        except ValueError:
            return None
        actual = "over" if (home_score + away_score) > line else "under"
        return 1 if selection == actual else 0
    if market == "double_chance":
        home_win = home_score > away_score
        draw = home_score == away_score
        away_win = away_score > home_score
        labels = {
            "1X": home_win or draw,
            "12": home_win or away_win,
            "X2": draw or away_win,
        }
        if selection not in labels:
            return None
        return 1 if labels[selection] else 0
    return None


def _write_settlement_eval_metrics(
    conn: sqlite3.Connection,
    settlement_run_id: int,
    as_of: str,
) -> None:
    rows = conn.execute(
        """
        SELECT *
        FROM prediction_result
        WHERE settlement_run_id = ?
        ORDER BY model_version, market, fixture_id
        """,
        (settlement_run_id,),
    ).fetchall()
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault((row["model_version"], row["market"]), []).append(row)

    for (model_version, market), market_rows in grouped.items():
        if market == "exact_score":
            continue
        brier, logloss, n = _aggregate_market_metrics(market, market_rows)
        conn.execute(
            """
            INSERT OR REPLACE INTO eval_metric
              (run_id, as_of, model, market, n, brier, logloss, roi, clv)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            (settlement_run_id, as_of, model_version, market, n, brier, logloss),
        )


def _aggregate_market_metrics(
    market: str,
    rows: list[sqlite3.Row],
) -> tuple[float, float, int]:
    if market == "1X2":
        by_fixture: dict[tuple[int, str, str], list[sqlite3.Row]] = {}
        for row in rows:
            by_fixture.setdefault(
                (row["prediction_run_id"], row["fixture_id"], row["prediction_generated_utc"]),
                [],
            ).append(row)
        briers = []
        loglosses = []
        for fixture_rows in by_fixture.values():
            briers.append(sum(float(row["brier"]) for row in fixture_rows))
            actual_rows = [row for row in fixture_rows if int(row["label"]) == 1]
            if actual_rows:
                loglosses.append(
                    multiclass_logloss(
                        _ordered_1x2_probabilities(fixture_rows),
                        _actual_1x2_label(fixture_rows),
                    )
                )
        return sum(briers) / len(briers), sum(loglosses) / len(loglosses), len(briers)

    brier = sum(float(row["brier"]) for row in rows) / len(rows)
    logloss = sum(float(row["logloss"]) for row in rows) / len(rows)
    return brier, logloss, len(rows)


def _record_settlement_quality(
    conn: sqlite3.Connection,
    run_id: int,
    summary: SettlementSummary,
) -> None:
    details = {
        "finished_fixtures": summary.finished_fixtures,
        "settled_predictions": summary.settled_predictions,
        "skipped_existing": summary.skipped_existing,
        "unsupported_predictions": summary.unsupported_predictions,
        "blocked_backfill_predictions": summary.blocked_backfill_predictions,
        "missing_prediction_fixtures": summary.missing_prediction_fixtures,
    }
    for metric_name, value in details.items():
        record_quality(
            conn,
            run_id,
            "settlement",
            metric_name,
            float(value),
            json.dumps(details, sort_keys=True),
        )


def _finished_fixture_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM fixture
            WHERE home_score IS NOT NULL
              AND away_score IS NOT NULL
            """
        ).fetchone()[0]
    )


def _missing_prediction_fixture_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM fixture f
            WHERE f.home_score IS NOT NULL
              AND f.away_score IS NOT NULL
              AND NOT EXISTS (
                SELECT 1
                FROM prediction p
                WHERE p.fixture_id = f.fixture_id
              )
            """
        ).fetchone()[0]
    )


def _ordered_1x2_probabilities(rows: list[sqlite3.Row]) -> list[float]:
    by_selection = {str(row["selection"]): float(row["predicted_prob"]) for row in rows}
    return [by_selection.get(selection, 0.0) for selection in ("home", "draw", "away")]


def _actual_1x2_label(rows: list[sqlite3.Row]) -> int:
    by_selection = {str(row["selection"]): int(row["label"]) for row in rows}
    for index, selection in enumerate(("home", "draw", "away")):
        if by_selection.get(selection) == 1:
            return index
    raise ValueError("Cannot score 1X2 fixture without a winning selection")


def _row_kickoff_utc(row: sqlite3.Row) -> datetime | None:
    return parse_kickoff_utc(kickoff_utc=row["kickoff_utc"], kickoff_local=row["kickoff_local"])


def _parse_generated_utc(value: str) -> datetime | None:
    return parse_datetime_utc(value)
