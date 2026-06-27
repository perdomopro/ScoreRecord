"""Generate Dixon-Coles multi-market predictions for real tournament fixtures."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from scorerecord.features.build import MatchFeature, load_match_features
from scorerecord.evaluate.goal_markets import _walk_forward_predictions, build_goal_markets
from scorerecord.models.calibration import PlattCalibrator
from scorerecord.models.dixon_coles import DixonColes
from scorerecord.models.manifest import MODEL_VERSION, build_model_manifest
from scorerecord.models.markets import (
    btts_probability,
    double_chance_probabilities,
    one_x_two_probabilities,
    over_probability,
    top_exact_scores,
    under_probability,
)
from scorerecord.runs import ingest_run, record_quality


DEFAULT_OU_LINES = (1.5, 2.5, 3.5)


@dataclass(frozen=True)
class FixturePublishSummary:
    predicted_fixtures: int
    skipped_fixtures: int
    inserted_predictions: int

    def as_row(self) -> str:
        return (
            f"predicted_fixtures={self.predicted_fixtures} "
            f"skipped_fixtures={self.skipped_fixtures} "
            f"inserted_predictions={self.inserted_predictions}"
        )


@dataclass(frozen=True)
class CalibratorArtifact:
    market: str
    calibrator: PlattCalibrator
    n: int
    raw_mean: float
    calibrated_mean: float
    observed_rate: float

    @property
    def delta_mean(self) -> float:
        return self.calibrated_mean - self.raw_mean

    @property
    def raw_error(self) -> float:
        return abs(self.raw_mean - self.observed_rate)

    @property
    def calibrated_error(self) -> float:
        return abs(self.calibrated_mean - self.observed_rate)


def publish_fixture_predictions(
    conn: sqlite3.Connection,
    *,
    ou_lines: tuple[float, ...] = DEFAULT_OU_LINES,
    exact_top_n: int = 5,
    cwd: Path | None = None,
) -> FixturePublishSummary:
    training_matches = load_match_features(conn)
    model = DixonColes().fit(training_matches)
    calibrators = _fit_goal_market_calibrators(training_matches, ou_lines=ou_lines)
    generated_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with ingest_run(conn, "predict-fixtures", source="fixture", cwd=cwd) as run_id:
        _record_model_manifest(conn, run_id)
        for artifact in calibrators.values():
            _record_calibrator_artifact(conn, run_id, artifact)
        fixtures = conn.execute(
            """
            SELECT fixture_id, kickoff_local, home_id, away_id, home_placeholder, away_placeholder
            FROM fixture
            ORDER BY kickoff_local, fixture_id
            """
        ).fetchall()
        predicted_fixtures = 0
        skipped_fixtures = 0
        inserted_predictions = 0
        for fixture in fixtures:
            if fixture["home_id"] is None or fixture["away_id"] is None:
                skipped_fixtures += 1
                continue
            predicted_fixtures += 1
            match = _fixture_as_match_feature(conn, fixture)
            score_grid = model.score_matrix(match)
            rows = _prediction_rows(
                fixture_id=fixture["fixture_id"],
                score_grid=score_grid,
                ou_lines=ou_lines,
                exact_top_n=exact_top_n,
                calibrators=calibrators,
            )
            for market, selection, prob in rows:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO prediction (
                      run_id, fixture_id, model, market, selection, prob,
                      fair_odds, generated_utc, model_version
                    )
                    VALUES (?, ?, 'dixon_coles', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        fixture["fixture_id"],
                        market,
                        selection,
                        prob,
                        _fair_odds(prob),
                        generated_utc,
                        MODEL_VERSION,
                    ),
                )
                inserted_predictions += 1
        summary = FixturePublishSummary(
            predicted_fixtures=predicted_fixtures,
            skipped_fixtures=skipped_fixtures,
            inserted_predictions=inserted_predictions,
        )
        record_quality(
            conn,
            run_id,
            "fixture_predictions",
            "predicted_fixtures",
            float(predicted_fixtures),
            json.dumps(
                {
                    "skipped_fixtures": skipped_fixtures,
                    "inserted_predictions": inserted_predictions,
                    "model_version": MODEL_VERSION,
                },
                sort_keys=True,
            ),
        )
        _record_fixture_elo_quality(conn, run_id, fixtures)
        conn.commit()
    return summary


def _fixture_as_match_feature(conn: sqlite3.Connection, fixture: sqlite3.Row) -> MatchFeature:
    kickoff = fixture["kickoff_local"] or "2026-01-01"
    home_id = int(fixture["home_id"])
    away_id = int(fixture["away_id"])
    elo_home = _latest_elo(conn, home_id)
    elo_away = _latest_elo(conn, away_id)
    return MatchFeature(
        source_match_id=fixture["fixture_id"],
        match_date=str(kickoff)[:10],
        home_id=home_id,
        away_id=away_id,
        home_score=0,
        away_score=0,
        neutral=1,
        tournament="FIFA World Cup",
        elo_home_pre=elo_home if elo_home is not None else 1500.0,
        elo_away_pre=elo_away if elo_away is not None else 1500.0,
    )


def _latest_elo(conn: sqlite3.Connection, team_id: int) -> float | None:
    row = conn.execute(
        """
        SELECT rating
        FROM team_rating
        WHERE team_id = ?
          AND rating_system = 'elo'
        ORDER BY as_of DESC
        LIMIT 1
        """,
        (team_id,),
    ).fetchone()
    return float(row["rating"]) if row else None


def _prediction_rows(
    *,
    fixture_id: str,
    score_grid: object,
    ou_lines: tuple[float, ...],
    exact_top_n: int,
    calibrators: dict[str, CalibratorArtifact],
) -> list[tuple[str, str, float]]:
    del fixture_id
    rows: list[tuple[str, str, float]] = []
    for selection, prob in zip(("home", "draw", "away"), one_x_two_probabilities(score_grid)):
        rows.append(("1X2", selection, prob))
    for line in ou_lines:
        market = f"OU{line}"
        over = _calibrate(market, over_probability(score_grid, line), calibrators)
        rows.append((market, "over", over))
        rows.append((market, "under", 1.0 - over))
    btts = _calibrate("BTTS", btts_probability(score_grid), calibrators)
    rows.append(("BTTS", "yes", btts))
    rows.append(("BTTS", "no", 1.0 - btts))
    for selection, prob in double_chance_probabilities(score_grid).items():
        rows.append(("double_chance", selection, prob))
    for score in top_exact_scores(score_grid, top_n=exact_top_n):
        rows.append(("exact_score", score.selection, score.prob))
    return rows


def _fair_odds(prob: float) -> float:
    return 1.0 / max(prob, 1e-9)


def _fit_goal_market_calibrators(
    training_matches: list[MatchFeature],
    *,
    ou_lines: tuple[float, ...],
) -> dict[str, CalibratorArtifact]:
    markets = build_goal_markets(ou_lines=ou_lines)
    predictions = _walk_forward_predictions(
        training_matches,
        markets=markets,
        start_year=2021,
        min_train=200,
    )
    calibrators: dict[str, CalibratorArtifact] = {}
    for market in markets:
        rows = predictions.get(("dixon_coles", market.market), [])
        if not rows:
            continue
        raw_probs = [row.prob for row in rows]
        labels = [row.label for row in rows]
        calibrator = PlattCalibrator().fit(raw_probs, labels)
        calibrated_probs = [calibrator.transform(prob) for prob in raw_probs]
        calibrators[market.market] = CalibratorArtifact(
            market=market.market,
            calibrator=calibrator,
            n=len(rows),
            raw_mean=sum(raw_probs) / len(raw_probs),
            calibrated_mean=sum(calibrated_probs) / len(calibrated_probs),
            observed_rate=sum(labels) / len(labels),
        )
    return calibrators


def _calibrate(
    market: str,
    prob: float,
    calibrators: dict[str, CalibratorArtifact],
) -> float:
    artifact = calibrators.get(market)
    if artifact is None:
        return prob
    return artifact.calibrator.transform(prob)


def _record_model_manifest(conn: sqlite3.Connection, run_id: int) -> None:
    manifest = build_model_manifest(conn, prediction_run_id=run_id).as_dict()
    record_quality(
        conn,
        run_id,
        "model_manifest",
        MODEL_VERSION,
        None,
        json.dumps(manifest, sort_keys=True),
    )


def _record_calibrator_artifact(
    conn: sqlite3.Connection,
    run_id: int,
    artifact: CalibratorArtifact,
) -> None:
    details = {
        "market": artifact.market,
        "calibrator": "platt",
        "a": artifact.calibrator.a,
        "b": artifact.calibrator.b,
        "n": artifact.n,
        "raw_mean": artifact.raw_mean,
        "calibrated_mean": artifact.calibrated_mean,
        "observed_rate": artifact.observed_rate,
        "delta_mean": artifact.delta_mean,
        "raw_error": artifact.raw_error,
        "calibrated_error": artifact.calibrated_error,
        "trained_from": "historical_oof_predictions",
        "model_version": MODEL_VERSION,
    }
    record_quality(
        conn,
        run_id,
        "calibrator_artifact",
        f"{artifact.market}_platt_coefficients",
        float(artifact.n),
        json.dumps(details, sort_keys=True),
    )
    record_quality(
        conn,
        run_id,
        "calibrator_sanity",
        f"{artifact.market}_mean_calibration_improved",
        1.0 if artifact.calibrated_error <= artifact.raw_error else 0.0,
        json.dumps(details, sort_keys=True),
    )


def _record_fixture_elo_quality(
    conn: sqlite3.Connection,
    run_id: int,
    fixtures: list[sqlite3.Row],
) -> None:
    resolved = [
        fixture
        for fixture in fixtures
        if fixture["home_id"] is not None and fixture["away_id"] is not None
    ]
    missing = 0
    elo_diffs: list[float] = []
    for fixture in resolved:
        home_elo = _latest_elo(conn, int(fixture["home_id"]))
        away_elo = _latest_elo(conn, int(fixture["away_id"]))
        if home_elo is None:
            missing += 1
            home_elo = 1500.0
        if away_elo is None:
            missing += 1
            away_elo = 1500.0
        elo_diffs.append(home_elo - away_elo)

    details = {
        "resolved_fixtures": len(resolved),
        "team_slots": len(resolved) * 2,
        "missing_team_rating_slots": missing,
        "fallback_rating": 1500.0,
        "neutral_assumption": "World Cup fixtures are treated as neutral-site matches",
        "min_elo_diff": min(elo_diffs) if elo_diffs else None,
        "max_elo_diff": max(elo_diffs) if elo_diffs else None,
    }
    for metric_name, value in [
        ("resolved_fixtures", len(resolved)),
        ("team_slots", len(resolved) * 2),
        ("missing_team_rating_slots", missing),
    ]:
        record_quality(
            conn,
            run_id,
            "fixture_elo",
            metric_name,
            float(value),
            json.dumps(details, sort_keys=True),
        )
