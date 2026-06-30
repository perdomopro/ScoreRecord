"""Segmented calibration diagnostics for historical real-odds predictions."""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from scorerecord.evaluate.backtest import (
    RealOddsPrediction,
    _binary_logloss,
    _load_historical_odds_markets,
    _load_historical_odds_matches,
    _mean,
    _walk_forward_real_odds_predictions,
    decision_gate,
    odds_bucket,
    prob_bucket,
    sample_size_tier,
)
from scorerecord.ingest.football_data import resolve_league_code
from scorerecord.runs import ingest_run, record_quality


@dataclass(frozen=True)
class CalibrationDiagnosticsSummary:
    run_id: int
    output_dir: Path
    prediction_rows: int
    segment_rows: int
    overconfidence_rows: int
    overall_model_minus_market_logloss: float

    def as_row(self) -> str:
        return (
            f"run_id={self.run_id} output_dir={self.output_dir} "
            f"prediction_rows={self.prediction_rows} segment_rows={self.segment_rows} "
            f"overconfidence_rows={self.overconfidence_rows} "
            f"overall_model_minus_market_logloss={self.overall_model_minus_market_logloss:.6f}"
        )


def run_calibration_diagnostics(
    conn: sqlite3.Connection,
    *,
    from_date: date,
    to_date: date,
    leagues: tuple[str, ...],
    out_dir: Path,
    start_year: int = 2021,
    min_train: int = 500,
    min_segment_rows: int = 100,
    historical_odds_source: str = "football-data",
    entry_odds: str = "avg_open",
    closing_odds: str = "avg_close",
    cwd: Path | None = None,
) -> CalibrationDiagnosticsSummary:
    if not leagues:
        raise ValueError("At least one league is required")
    if historical_odds_source != "football-data":
        raise ValueError("Only historical_odds_source=football-data is supported")

    league_codes = tuple(resolve_league_code(league) for league in leagues)
    generated_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with ingest_run(
        conn,
        "calibration-diagnostics",
        source=historical_odds_source,
        cwd=cwd,
    ) as run_id:
        odds = _load_historical_odds_markets(
            conn,
            source=historical_odds_source,
            entry_odds_type=entry_odds,
            closing_odds_type=closing_odds,
        )
        predictions: list[RealOddsPrediction] = []
        league_prediction_counts: dict[str, int] = {}
        for league_code in league_codes:
            matches = _load_historical_odds_matches(
                conn,
                source=historical_odds_source,
                league_code=league_code,
                test_season=None,
                to_date=to_date,
            )
            league_predictions = _walk_forward_real_odds_predictions(
                matches,
                odds=odds,
                from_date=from_date,
                to_date=to_date,
                start_year=start_year,
                min_train=min_train,
                test_season=None,
            )
            predictions.extend(league_predictions)
            league_prediction_counts[league_code] = len(league_predictions)

        detail_rows = _diagnostic_detail_rows(predictions)
        by_league = _segment_rows(predictions, [("league", lambda row: row.match.league_code)])
        by_selection = _segment_rows(predictions, [("selection", lambda row: row.selection)])
        by_prob_bucket = _segment_rows(
            predictions,
            [("model_prob_bucket", lambda row: prob_bucket(row.model_prob))],
        )
        by_odds_bucket = _segment_rows(
            predictions,
            [("odds_bucket", lambda row: odds_bucket(row.entry_odds))],
        )
        model_vs_market = _segment_rows(
            predictions,
            [
                ("overall", lambda row: "all"),
                ("league", lambda row: row.match.league_code),
                ("selection", lambda row: row.selection),
                ("model_prob_bucket", lambda row: prob_bucket(row.model_prob)),
                ("market_prob_bucket", lambda row: prob_bucket(row.market_prob)),
                ("odds_bucket", lambda row: odds_bucket(row.entry_odds)),
                ("league_selection", lambda row: f"{row.match.league_code}:{row.selection}"),
                (
                    "selection_model_prob_bucket",
                    lambda row: f"{row.selection}:{prob_bucket(row.model_prob)}",
                ),
                (
                    "selection_odds_bucket",
                    lambda row: f"{row.selection}:{odds_bucket(row.entry_odds)}",
                ),
                (
                    "league_selection_prob_bucket",
                    lambda row: (
                        f"{row.match.league_code}:{row.selection}:"
                        f"{prob_bucket(row.model_prob)}"
                    ),
                ),
                (
                    "league_selection_odds_bucket",
                    lambda row: (
                        f"{row.match.league_code}:{row.selection}:"
                        f"{odds_bucket(row.entry_odds)}"
                    ),
                ),
            ],
        )
        overconfidence = _overconfidence_rows(
            model_vs_market,
            min_segment_rows=min_segment_rows,
        )
        overall = next(
            row
            for row in model_vs_market
            if row["group_type"] == "overall" and row["group"] == "all"
        )

        out_dir.mkdir(parents=True, exist_ok=True)
        _write_dict_rows(out_dir / f"calibration_diagnostics_rows_{run_id}.csv", detail_rows)
        _write_dict_rows(out_dir / f"calibration_by_league_{run_id}.csv", by_league)
        _write_dict_rows(out_dir / f"calibration_by_selection_{run_id}.csv", by_selection)
        _write_dict_rows(out_dir / f"calibration_by_prob_bucket_{run_id}.csv", by_prob_bucket)
        _write_dict_rows(out_dir / f"calibration_by_odds_bucket_{run_id}.csv", by_odds_bucket)
        _write_dict_rows(out_dir / f"model_vs_market_by_bucket_{run_id}.csv", model_vs_market)
        _write_dict_rows(out_dir / f"overconfidence_report_{run_id}.csv", overconfidence)

        metadata = {
            "run_id": run_id,
            "generated_utc": generated_utc,
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "leagues": list(leagues),
            "league_codes": list(league_codes),
            "league_prediction_counts": league_prediction_counts,
            "start_year": start_year,
            "min_train": min_train,
            "min_segment_rows": min_segment_rows,
            "historical_odds_source": historical_odds_source,
            "entry_odds_type": entry_odds,
            "closing_odds_type": closing_odds,
            "prediction_rows": len(predictions),
            "segment_rows": len(model_vs_market),
            "overconfidence_rows": len(overconfidence),
            "overall": overall,
            "interpretation": (
                "Positive model_minus_market_logloss means the fair market beat the model. "
                "Positive overconfidence means avg_model_prob exceeded the realized hit rate."
            ),
        }
        (out_dir / f"calibration_diagnostics_metadata_{run_id}.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        _record_diagnostics_quality(conn, run_id, metadata, model_vs_market, overconfidence)
        conn.commit()

    return CalibrationDiagnosticsSummary(
        run_id=run_id,
        output_dir=out_dir,
        prediction_rows=len(predictions),
        segment_rows=len(model_vs_market),
        overconfidence_rows=len(overconfidence),
        overall_model_minus_market_logloss=float(overall["model_minus_market_logloss"]),
    )


def _diagnostic_detail_rows(predictions: list[RealOddsPrediction]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for row in predictions:
        model_logloss = _binary_logloss(row.model_prob, row.won)
        market_logloss = _binary_logloss(row.market_prob, row.won)
        rows.append(
            {
                "match_date": row.match.match_date,
                "league_code": row.match.league_code,
                "season": row.match.season,
                "source_event_id": row.match.source_event_id,
                "home_team": row.match.home_team,
                "away_team": row.match.away_team,
                "selection": row.selection,
                "won": row.won,
                "model_prob": row.model_prob,
                "fair_market_prob": row.market_prob,
                "entry_odds": row.entry_odds,
                "model_prob_bucket": prob_bucket(row.model_prob),
                "market_prob_bucket": prob_bucket(row.market_prob),
                "odds_bucket": odds_bucket(row.entry_odds),
                "model_logloss": model_logloss,
                "market_logloss": market_logloss,
                "model_minus_market_logloss": model_logloss - market_logloss,
                "model_brier": (row.model_prob - row.won) ** 2,
                "market_brier": (row.market_prob - row.won) ** 2,
                "calibration_error": row.won - row.model_prob,
                "market_calibration_error": row.won - row.market_prob,
            }
        )
    return rows


def _segment_rows(
    predictions: list[RealOddsPrediction],
    groupers: Iterable[tuple[str, Callable[[RealOddsPrediction], str]]],
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for group_type, key_fn in groupers:
        grouped: dict[str, list[RealOddsPrediction]] = {}
        for row in predictions:
            grouped.setdefault(key_fn(row), []).append(row)
        for group, rows in sorted(grouped.items()):
            output.append(_segment_metric_row(group_type, group, rows))
    return output


def _segment_metric_row(
    group_type: str,
    group: str,
    rows: list[RealOddsPrediction],
) -> dict[str, object]:
    model_logloss = _mean([_binary_logloss(row.model_prob, row.won) for row in rows])
    market_logloss = _mean([_binary_logloss(row.market_prob, row.won) for row in rows])
    model_brier = _mean([(row.model_prob - row.won) ** 2 for row in rows])
    market_brier = _mean([(row.market_prob - row.won) ** 2 for row in rows])
    hit_rate = _mean([row.won for row in rows])
    avg_model_prob = _mean([row.model_prob for row in rows])
    avg_market_prob = _mean([row.market_prob for row in rows])
    return {
        "group_type": group_type,
        "group": group,
        "num_rows": len(rows),
        "sample_size_tier": sample_size_tier(len(rows)),
        "decision_gate": decision_gate(len(rows)),
        "hit_rate": hit_rate,
        "avg_model_prob": avg_model_prob,
        "avg_market_prob": avg_market_prob,
        "avg_entry_odds": _mean([row.entry_odds for row in rows]),
        "model_logloss": model_logloss,
        "market_logloss": market_logloss,
        "model_minus_market_logloss": model_logloss - market_logloss,
        "model_brier": model_brier,
        "market_brier": market_brier,
        "model_minus_market_brier": model_brier - market_brier,
        "calibration_error": hit_rate - avg_model_prob,
        "market_calibration_error": hit_rate - avg_market_prob,
        "overconfidence": avg_model_prob - hit_rate,
        "market_overconfidence": avg_market_prob - hit_rate,
        "abs_calibration_error": abs(hit_rate - avg_model_prob),
        "market_abs_calibration_error": abs(hit_rate - avg_market_prob),
        "model_ece": _expected_calibration_error(rows, lambda row: row.model_prob),
        "market_ece": _expected_calibration_error(rows, lambda row: row.market_prob),
        "model_better_logloss": int(model_logloss < market_logloss),
        "model_better_brier": int(model_brier < market_brier),
    }


def _overconfidence_rows(
    segment_rows: list[dict[str, object]],
    *,
    min_segment_rows: int,
) -> list[dict[str, object]]:
    supported = [row for row in segment_rows if int(row["num_rows"]) >= min_segment_rows]
    output: list[dict[str, object]] = []
    for row in supported:
        overconfidence = float(row["overconfidence"])
        model_gap = float(row["model_minus_market_logloss"])
        output.append(
            {
                **row,
                "direction": "overconfident" if overconfidence > 0 else "underconfident",
                "severity": _calibration_severity(abs(overconfidence), model_gap),
                "model_worse_than_market": int(model_gap > 0),
            }
        )
    return sorted(
        output,
        key=lambda row: (
            str(row["severity"]) != "critical",
            str(row["severity"]) != "warning",
            -abs(float(row["overconfidence"])),
            -float(row["model_minus_market_logloss"]),
        ),
    )


def _calibration_severity(abs_overconfidence: float, model_gap: float) -> str:
    if abs_overconfidence >= 0.08 and model_gap > 0:
        return "critical"
    if abs_overconfidence >= 0.05 or model_gap > 0.02:
        return "warning"
    return "monitor"


def _expected_calibration_error(
    rows: list[RealOddsPrediction],
    prob_getter: Callable[[RealOddsPrediction], float],
    *,
    n_bins: int = 10,
) -> float:
    if not rows:
        return 0.0
    total = len(rows)
    ece = 0.0
    for index in range(n_bins):
        lower = index / n_bins
        upper = (index + 1) / n_bins
        selected = [
            row
            for row in rows
            if prob_getter(row) >= lower
            and (prob_getter(row) < upper or index == n_bins - 1)
        ]
        if not selected:
            continue
        ece += (len(selected) / total) * abs(
            _mean([prob_getter(row) for row in selected]) - _mean([row.won for row in selected])
        )
    return ece


def _record_diagnostics_quality(
    conn: sqlite3.Connection,
    run_id: int,
    metadata: dict[str, object],
    segment_rows: list[dict[str, object]],
    overconfidence_rows: list[dict[str, object]],
) -> None:
    overall = metadata["overall"]
    critical_rows = [row for row in overconfidence_rows if row["severity"] == "critical"]
    model_better_rows = [
        row
        for row in segment_rows
        if int(row["num_rows"]) >= int(metadata["min_segment_rows"])
        and int(row["model_better_logloss"]) == 1
    ]
    details = {
        **metadata,
        "top_overconfidence_rows": overconfidence_rows[:20],
        "model_better_rows": model_better_rows[:20],
        "warning": (
            "Diagnostics identify segments; do not retune recommendation_score until "
            "calibration changes are validated out of sample."
        ),
    }
    for metric_name, value in [
        ("prediction_rows", metadata["prediction_rows"]),
        ("segment_rows", len(segment_rows)),
        ("overconfidence_rows", len(overconfidence_rows)),
        ("critical_overconfidence_rows", len(critical_rows)),
        ("supported_model_better_rows", len(model_better_rows)),
        ("overall_model_minus_market_logloss", overall["model_minus_market_logloss"]),
        ("overall_model_ece", overall["model_ece"]),
        ("overall_market_ece", overall["market_ece"]),
    ]:
        record_quality(
            conn,
            run_id,
            "calibration_diagnostics",
            metric_name,
            float(value),
            json.dumps(details, sort_keys=True),
        )


def _write_dict_rows(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
