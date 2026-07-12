"""Cross-league policy-grid analysis for historical real-odds backtests."""

from __future__ import annotations

import csv
import glob
import json
import math
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from scorerecord.runs import ingest_run, record_quality


@dataclass(frozen=True)
class PolicyGridAnalysisSummary:
    run_id: int
    output_path: Path
    rows_written: int
    files_read: int
    best_decision: str

    def as_row(self) -> str:
        return (
            f"run_id={self.run_id} output_path={self.output_path} "
            f"rows_written={self.rows_written} files_read={self.files_read} "
            f"best_decision={self.best_decision}"
        )


def analyze_policy_grids(
    conn: sqlite3.Connection,
    *,
    inputs: Iterable[str],
    out_path: Path,
    cwd: Path | None = None,
) -> PolicyGridAnalysisSummary:
    files = _expand_inputs(inputs)
    if not files:
        raise ValueError("No policy grid CSV files matched the provided inputs")

    generated_utc = datetime.now(UTC).isoformat(timespec="seconds")
    with ingest_run(conn, "analyze-policy-grids", source="policy_grid_csv", cwd=cwd) as run_id:
        per_league = _latest_rows_by_config_and_league(files)
        rows = _aggregate_policy_rows(per_league.values())
        rows.sort(
            key=lambda row: (
                _decision_rank(str(row["decision"])),
                -float(row["weighted_mean_clv_real"]),
                -float(row["weighted_positive_clv_rate"]),
                -float(row["weighted_roi_capped_real"]),
                -int(row["total_bets"]),
            )
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _write_dict_rows(out_path, rows)
        metadata = {
            "run_id": run_id,
            "generated_utc": generated_utc,
            "input_files": [str(path) for path in files],
            "files_read": len(files),
            "unique_config_league_rows": len(per_league),
            "summary_rows": len(rows),
            "decision_policy": {
                "candidate": (
                    ">=3 leagues, >=1000 bets, weighted ROI >= 0, weighted CLV > 0, "
                    "weighted positive CLV > 50%, and positive CLV in >=60% of leagues"
                ),
                "review": (
                    ">=2 leagues, >=300 bets, not candidate, and CLV is not strongly "
                    "negative or at least one league has positive CLV"
                ),
                "reject": "Everything else; do not tune production thresholds from it.",
            },
        }
        _record_policy_analysis_quality(conn, run_id, metadata, rows)
        conn.commit()

    return PolicyGridAnalysisSummary(
        run_id=run_id,
        output_path=out_path,
        rows_written=len(rows),
        files_read=len(files),
        best_decision=str(rows[0]["decision"]) if rows else "none",
    )


def _latest_rows_by_config_and_league(
    files: list[Path],
) -> dict[tuple[tuple[object, ...], str], dict[str, object]]:
    latest: dict[tuple[tuple[object, ...], str], dict[str, object]] = {}
    for path in files:
        run_id = _run_id_from_path(path)
        metadata = _read_metadata(path, run_id)
        league = str(metadata.get("league_code") or f"unknown:{path.stem}")
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for raw in reader:
                if not raw:
                    continue
                row = _normalize_policy_row(raw, metadata, path, run_id, league)
                key = (_config_key(row), league)
                previous = latest.get(key)
                if previous is None or int(row["run_id"]) >= int(previous["run_id"]):
                    latest[key] = row
    return latest


def _aggregate_policy_rows(rows: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[tuple[object, ...], list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(_config_key(row), []).append(row)

    output: list[dict[str, object]] = []
    for key, group in grouped.items():
        (
            min_score,
            min_edge,
            max_picks_per_day,
            ranking_strategy,
            market_shrink_alpha,
        ) = key
        leagues = sorted({str(row["league_code"]) for row in group})
        total_bets = sum(int(row["num_bets"]) for row in group)
        total_clv_rows = sum(int(row["clv_rows"]) for row in group)
        weighted_roi = _weighted_mean(group, "roi_capped_real", "num_bets")
        weighted_clv = _weighted_mean(group, "mean_clv_real", "clv_rows")
        weighted_positive_clv = _weighted_mean(group, "positive_clv_rate", "clv_rows")
        profitable_leagues = sum(1 for row in group if float(row["roi_capped_real"]) > 0.0)
        positive_clv_leagues = sum(1 for row in group if float(row["mean_clv_real"]) > 0.0)
        positive_clv_rate_gt_50 = sum(
            1 for row in group if float(row["positive_clv_rate"]) > 0.50
        )
        best = max(
            group,
            key=lambda row: (
                float(row["mean_clv_real"]),
                float(row["positive_clv_rate"]),
                float(row["roi_capped_real"]),
            ),
        )
        worst = min(
            group,
            key=lambda row: (
                float(row["mean_clv_real"]),
                float(row["positive_clv_rate"]),
                float(row["roi_capped_real"]),
            ),
        )
        decision = _policy_decision(
            leagues_count=len(leagues),
            total_bets=total_bets,
            weighted_roi=weighted_roi,
            weighted_clv=weighted_clv,
            weighted_positive_clv=weighted_positive_clv,
            profitable_leagues=profitable_leagues,
            positive_clv_leagues=positive_clv_leagues,
        )
        output.append(
            {
                "min_score": min_score,
                "min_edge": min_edge,
                "max_picks_per_day": max_picks_per_day,
                "ranking_strategy": ranking_strategy,
                "market_shrink_alpha": market_shrink_alpha,
                "leagues_count": len(leagues),
                "leagues": ",".join(leagues),
                "total_bets": total_bets,
                "total_clv_rows": total_clv_rows,
                "weighted_roi_capped_real": weighted_roi,
                "weighted_mean_clv_real": weighted_clv,
                "weighted_positive_clv_rate": weighted_positive_clv,
                "profitable_leagues": profitable_leagues,
                "positive_clv_leagues": positive_clv_leagues,
                "positive_clv_rate_gt_50_leagues": positive_clv_rate_gt_50,
                "best_league": best["league_code"],
                "best_league_roi_capped_real": best["roi_capped_real"],
                "best_league_mean_clv_real": best["mean_clv_real"],
                "worst_league": worst["league_code"],
                "worst_league_roi_capped_real": worst["roi_capped_real"],
                "worst_league_mean_clv_real": worst["mean_clv_real"],
                "decision": decision,
            }
        )
    return output


def _normalize_policy_row(
    raw: dict[str, str],
    metadata: dict[str, object],
    path: Path,
    run_id: int,
    league: str,
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "source_file": str(path),
        "league_code": league,
        "from_date": str(metadata.get("from_date") or ""),
        "to_date": str(metadata.get("to_date") or ""),
        "min_score": _as_float(raw.get("min_score")),
        "min_edge": _as_float(raw.get("min_edge")),
        "max_picks_per_day": raw.get("max_picks_per_day") or "none",
        "ranking_strategy": raw.get("ranking_strategy") or "",
        "market_shrink_alpha": _as_float(raw.get("market_shrink_alpha"), default=1.0),
        "num_bets": _as_int(raw.get("num_bets")),
        "roi_capped_real": _as_float(raw.get("roi_capped_real")),
        "mean_clv_real": _as_float(raw.get("mean_clv_real")),
        "positive_clv_rate": _as_float(raw.get("positive_clv_rate")),
        "clv_rows": _as_int(raw.get("clv_rows")),
    }


def _config_key(row: dict[str, object]) -> tuple[object, ...]:
    return (
        round(float(row["min_score"]), 6),
        round(float(row["min_edge"]), 6),
        str(row["max_picks_per_day"]),
        str(row["ranking_strategy"]),
        round(float(row.get("market_shrink_alpha", 1.0)), 6),
    )


def _weighted_mean(rows: list[dict[str, object]], value_key: str, weight_key: str) -> float:
    weighted = [
        (float(row[value_key]), int(row[weight_key]))
        for row in rows
        if int(row[weight_key]) > 0
    ]
    total_weight = sum(weight for _, weight in weighted)
    if total_weight <= 0:
        return 0.0
    return sum(value * weight for value, weight in weighted) / total_weight


def _policy_decision(
    *,
    leagues_count: int,
    total_bets: int,
    weighted_roi: float,
    weighted_clv: float,
    weighted_positive_clv: float,
    profitable_leagues: int,
    positive_clv_leagues: int,
) -> str:
    if leagues_count >= 3:
        required_positive = math.ceil(leagues_count * 0.60)
        if (
            total_bets >= 1000
            and weighted_roi >= 0.0
            and weighted_clv > 0.0
            and weighted_positive_clv > 0.50
            and positive_clv_leagues >= required_positive
            and profitable_leagues >= math.ceil(leagues_count * 0.50)
        ):
            return "candidate"
    if (
        leagues_count >= 2
        and total_bets >= 300
        and weighted_positive_clv >= 0.45
        and (weighted_clv > -0.0025 or positive_clv_leagues > 0)
    ):
        return "review"
    return "reject"


def _record_policy_analysis_quality(
    conn: sqlite3.Connection,
    run_id: int,
    metadata: dict[str, object],
    rows: list[dict[str, object]],
) -> None:
    candidate_rows = [row for row in rows if row["decision"] == "candidate"]
    review_rows = [row for row in rows if row["decision"] == "review"]
    details = {
        **metadata,
        "top_rows": rows[:10],
        "warning": (
            "Candidate/review rows are screening signals only. Confirm with benchmark, "
            "calibration, and live CLV before changing production policy."
        ),
    }
    for metric_name, value in [
        ("files_read", metadata["files_read"]),
        ("summary_rows", len(rows)),
        ("candidate_rows", len(candidate_rows)),
        ("review_rows", len(review_rows)),
        (
            "best_weighted_mean_clv",
            max((float(row["weighted_mean_clv_real"]) for row in rows), default=0.0),
        ),
        (
            "best_weighted_roi_capped",
            max((float(row["weighted_roi_capped_real"]) for row in rows), default=0.0),
        ),
    ]:
        record_quality(
            conn,
            run_id,
            "policy_grid_analysis",
            metric_name,
            float(value),
            json.dumps(details, sort_keys=True),
        )


def _expand_inputs(inputs: Iterable[str]) -> list[Path]:
    output: list[Path] = []
    seen: set[Path] = set()
    for raw in inputs:
        for item in raw.split(","):
            pattern = item.strip()
            if not pattern:
                continue
            matches = [Path(match) for match in glob.glob(pattern)]
            if not matches:
                matches = [Path(pattern)]
            for match in matches:
                if not match.exists() or not match.is_file():
                    continue
                resolved = match.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    output.append(resolved)
    return sorted(output)


def _read_metadata(path: Path, run_id: int) -> dict[str, object]:
    metadata_path = path.with_name(f"policy_grid_metadata_{run_id}.json")
    if not metadata_path.exists():
        return {}
    return json.loads(metadata_path.read_text(encoding="utf-8"))


def _run_id_from_path(path: Path) -> int:
    match = re.search(r"policy_grid_real_odds_(\d+)\.csv$", path.name)
    return int(match.group(1)) if match else 0


def _decision_rank(decision: str) -> int:
    return {"candidate": 0, "review": 1, "reject": 2}.get(decision, 3)


def _as_float(value: object, *, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    return float(value)


def _as_int(value: object, *, default: int = 0) -> int:
    if value is None or value == "":
        return default
    return int(float(value))


def _write_dict_rows(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
