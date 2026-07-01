import math

import pytest

from scorerecord.evaluate.scoring import (
    binary_brier,
    binary_logloss,
    block_bootstrap_mean,
    club_week_block,
    multiclass_expected_calibration_error,
    multiclass_brier,
    multiclass_logloss,
    normalize_probabilities,
    worldcup_match_date_block,
)


def test_one_x_two_scores_one_match_row() -> None:
    probabilities = [0.60, 0.25, 0.15]

    assert math.isclose(multiclass_brier(probabilities, 0), 0.245)
    assert math.isclose(multiclass_logloss(probabilities, 0), -math.log(0.60))


def test_probabilities_sum_to_one_after_multiclass_normalization() -> None:
    normalized = normalize_probabilities([0.42, 0.31, 0.35])

    assert math.isclose(sum(normalized), 1.0)
    assert math.isclose(
        multiclass_logloss([0.42, 0.31, 0.35], 0),
        -math.log(normalized[0]),
    )


def test_multiclass_ece_uses_one_vs_rest_records() -> None:
    ece = multiclass_expected_calibration_error(
        [([0.80, 0.10, 0.10], 0), ([0.80, 0.10, 0.10], 2)],
        n_bins=5,
    )

    assert ece > 0


def test_binary_scores_one_market_row() -> None:
    assert math.isclose(binary_brier(0.57, 1), (0.57 - 1.0) ** 2)
    assert math.isclose(binary_logloss(0.57, 1), -math.log(0.57))


def test_scoring_rejects_wrong_units() -> None:
    with pytest.raises(ValueError):
        multiclass_brier([0.5, 0.5], 0)
    with pytest.raises(ValueError):
        binary_logloss(0.5, 2)


def test_block_bootstrap_is_deterministic_and_reports_unit_counts() -> None:
    interval = block_bootstrap_mean(
        {"2026-06-11": [1.0, 2.0], "2026-06-12": [3.0]},
        iterations=50,
        seed=7,
    )

    assert interval.mean == 2.0
    assert interval.n_blocks == 2
    assert interval.n_rows == 3
    assert interval.lower <= interval.mean <= interval.upper


def test_block_keys_match_phase_two_contract() -> None:
    assert club_week_block("2024-08-17", "EPL") == "EPL:2024-W33"
    assert worldcup_match_date_block("2026-06-11T19:00:00+00:00") == "2026-06-11"
