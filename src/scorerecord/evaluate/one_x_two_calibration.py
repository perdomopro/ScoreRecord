"""League-scoped 1X2 calibration reports.

This report is intentionally diagnostic. It does not promote 1X2 output to a
forecast principal and it does not produce value, staking, or recommendation
language.
"""

from __future__ import annotations

import csv
import dataclasses
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from scorerecord.evaluate.walk_forward import _FACTORIES, WalkForwardResult, walk_forward
from scorerecord.features.build import MatchFeature, load_match_features
from scorerecord.runs import ingest_run, record_quality


@dataclass(frozen=True)
class OneXTwoLeagueCalibration:
    league: str
    status: str
    historical_matches: int
    evaluated_matches: int
    dixon_coles_logloss: float | None
    marginal_logloss: float | None
    uniform_logloss: float | None
    dixon_coles_brier: float | None
    dixon_coles_ece: float | None
    close_logloss: float | None
    details_json: str

    def as_row(self) -> str:
        return (
            f"{self.league:<14} status={self.status:<24} "
            f"history={self.historical_matches:<5} eval={self.evaluated_matches:<5} "
            f"dc_ll={_fmt(self.dixon_coles_logloss)} "
            f"marg_ll={_fmt(self.marginal_logloss)} ece={_fmt(self.dixon_coles_ece)}"
        )


def run_one_x_two_calibration_report(
    conn: sqlite3.Connection,
    *,
    leagues: tuple[str, ...],
    start_year: int = 2021,
    min_train: int = 200,
    out_dir: Path | None = None,
    cwd: Path | None = None,
) -> list[OneXTwoLeagueCalibration]:
    all_matches = load_match_features(conn)
    as_of = datetime.now(UTC).isoformat(timespec="seconds")
    reports: list[OneXTwoLeagueCalibration] = []

    with ingest_run(conn, "one-x-two-calibration-report", source="match_history", cwd=cwd) as run_id:
        for league in leagues:
            league_matches = [
                match for match in all_matches if _same_league(match.tournament, league)
            ]
            report = _evaluate_league(
                league,
                league_matches,
                start_year=start_year,
                min_train=min_train,
            )
            reports.append(report)
            record_quality(
                conn,
                run_id,
                "one_x_two_calibration",
                f"{league}_status",
                float(report.evaluated_matches),
                json.dumps(dataclasses.asdict(report), sort_keys=True),
            )
            if report.dixon_coles_logloss is not None:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO eval_metric
                      (run_id, as_of, model, market, n, brier, logloss, roi, clv)
                    VALUES (?, ?, ?, '1X2', ?, ?, ?, NULL, NULL)
                    """,
                    (
                        run_id,
                        as_of,
                        f"dixon_coles:{league}",
                        report.evaluated_matches,
                        report.dixon_coles_brier,
                        report.dixon_coles_logloss,
                    ),
                )
        conn.commit()

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = [dataclasses.asdict(report) for report in reports]
        (out_dir / "one_x_two_calibration_report.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        _write_csv(out_dir / "one_x_two_calibration_report.csv", payload)
    return reports


def _evaluate_league(
    league: str,
    matches: list[MatchFeature],
    *,
    start_year: int,
    min_train: int,
) -> OneXTwoLeagueCalibration:
    if len(matches) < min_train * 2:
        return OneXTwoLeagueCalibration(
            league=league,
            status="INSUFFICIENT_DATA",
            historical_matches=len(matches),
            evaluated_matches=0,
            dixon_coles_logloss=None,
            marginal_logloss=None,
            uniform_logloss=None,
            dixon_coles_brier=None,
            dixon_coles_ece=None,
            close_logloss=None,
            details_json=json.dumps({"reason": "not enough rows for expanding-window report"}),
        )

    results: dict[str, WalkForwardResult] = {}
    errors: dict[str, str] = {}
    for model_key in ("baseline_uniform", "baseline_marginal", "dixon_coles"):
        try:
            results[model_key] = walk_forward(
                matches,
                _FACTORIES[model_key],
                start_year=start_year,
                min_train=min_train,
            )
        except ValueError as exc:
            errors[model_key] = str(exc)

    dc = results.get("dixon_coles")
    marginal = results.get("baseline_marginal")
    uniform = results.get("baseline_uniform")
    if dc is None:
        return OneXTwoLeagueCalibration(
            league=league,
            status="NO_EVALUATION",
            historical_matches=len(matches),
            evaluated_matches=0,
            dixon_coles_logloss=None,
            marginal_logloss=marginal.logloss if marginal else None,
            uniform_logloss=uniform.logloss if uniform else None,
            dixon_coles_brier=None,
            dixon_coles_ece=None,
            close_logloss=None,
            details_json=json.dumps({"errors": errors}, sort_keys=True),
        )

    status = _calibration_status(dc, marginal)
    return OneXTwoLeagueCalibration(
        league=league,
        status=status,
        historical_matches=len(matches),
        evaluated_matches=dc.n,
        dixon_coles_logloss=dc.logloss,
        marginal_logloss=marginal.logloss if marginal else None,
        uniform_logloss=uniform.logloss if uniform else None,
        dixon_coles_brier=dc.brier,
        dixon_coles_ece=dc.ece,
        close_logloss=dc.close_logloss,
        details_json=json.dumps(
            {
                "start_year": start_year,
                "min_train": min_train,
                "errors": errors,
                "dixon_coles": {
                    "close_n": dc.close_n,
                    "close_logloss": dc.close_logloss,
                    "class_calibration_bins": dc.class_calibration_bins,
                },
            },
            sort_keys=True,
        ),
    )


def _calibration_status(
    dc: WalkForwardResult,
    marginal: WalkForwardResult | None,
) -> str:
    beats_marginal = marginal is not None and dc.logloss < marginal.logloss
    if beats_marginal and dc.ece <= 0.05:
        return "CALIBRATION_OK"
    if beats_marginal and dc.ece <= 0.08:
        return "CALIBRATION_WARN"
    if beats_marginal:
        return "DISCRIMINATES_BUT_UNCALIBRATED"
    return "REVIEW_REQUIRED"


def _same_league(tournament: str | None, league: str) -> bool:
    return (tournament or "").casefold() == league.casefold()


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _fmt(value: float | None) -> str:
    return "na" if value is None else f"{value:.4f}"
