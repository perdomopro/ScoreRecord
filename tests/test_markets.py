import numpy as np

from scorerecord.models.markets import (
    btts_probability,
    double_chance_probabilities,
    one_x_two_probabilities,
    over_probability,
    top_exact_scores,
    under_probability,
)


def test_score_grid_market_aggregations() -> None:
    grid = np.array(
        [
            [0.10, 0.10, 0.05],
            [0.20, 0.15, 0.05],
            [0.25, 0.05, 0.05],
        ]
    )
    p_home, p_draw, p_away = one_x_two_probabilities(grid)
    assert round(p_home, 8) == 0.50
    assert round(p_draw, 8) == 0.30
    assert round(p_away, 8) == 0.20

    assert round(over_probability(grid, 2.5), 8) == 0.15
    assert round(under_probability(grid, 2.5), 8) == 0.85
    assert round(btts_probability(grid), 8) == 0.30

    double_chance = double_chance_probabilities(grid)
    assert round(double_chance["1X"], 8) == 0.80
    assert round(double_chance["12"], 8) == 0.70
    assert round(double_chance["X2"], 8) == 0.50


def test_top_exact_scores_are_sorted() -> None:
    grid = np.array([[0.2, 0.1], [0.4, 0.3]])
    scores = top_exact_scores(grid, top_n=2)
    assert [score.selection for score in scores] == ["1-0", "1-1"]
