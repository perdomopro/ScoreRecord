from scorerecord.evaluate.walk_forward import (
    PredictionRecord,
    expected_calibration_error,
    multiclass_brier,
    multiclass_logloss,
)
from scorerecord.features.build import MatchFeature


def _match(outcome: int) -> MatchFeature:
    home_score, away_score = {
        0: (1, 0),
        1: (0, 0),
        2: (0, 1),
    }[outcome]
    return MatchFeature(
        source_match_id=f"match-{outcome}",
        match_date="2021-01-01",
        home_id=1,
        away_id=2,
        home_score=home_score,
        away_score=away_score,
        neutral=0,
        tournament="Friendly",
        elo_home_pre=1500.0,
        elo_away_pre=1500.0,
    )


def test_metric_helpers() -> None:
    assert multiclass_brier([1.0, 0.0, 0.0], 0) == 0.0
    assert multiclass_logloss([0.8, 0.1, 0.1], 0) < multiclass_logloss(
        [0.1, 0.8, 0.1],
        0,
    )


def test_ece_penalizes_uniform_on_imbalanced_outcomes() -> None:
    records = [
        PredictionRecord(match=_match(0), prob=[1 / 3, 1 / 3, 1 / 3]),
        PredictionRecord(match=_match(0), prob=[1 / 3, 1 / 3, 1 / 3]),
        PredictionRecord(match=_match(0), prob=[1 / 3, 1 / 3, 1 / 3]),
    ]
    ece, _ = expected_calibration_error(records, n_bins=10)
    assert ece > 0.4
