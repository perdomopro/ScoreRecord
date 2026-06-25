from scorerecord.features.elo import _actual, _expected, _goal_multiplier


def test_actual_result_score() -> None:
    assert _actual(2, 1) == 1.0
    assert _actual(1, 1) == 0.5
    assert _actual(0, 1) == 0.0


def test_expected_rating_symmetry() -> None:
    favorite = _expected(1600, 1500)
    underdog = _expected(1500, 1600)
    assert favorite > 0.5
    assert round(favorite + underdog, 8) == 1.0


def test_goal_multiplier_scales_large_wins() -> None:
    assert _goal_multiplier(1, 0) == 1.0
    assert _goal_multiplier(4, 0) > 1.0
