from scorerecord.features.build import MatchFeature
from scorerecord.models.dixon_coles import DixonColes


def _match(index: int, home: int, away: int, home_score: int, away_score: int) -> MatchFeature:
    return MatchFeature(
        source_match_id=f"m-{index}",
        match_date=f"2021-01-{index + 1:02d}",
        home_id=home,
        away_id=away,
        home_score=home_score,
        away_score=away_score,
        neutral=0,
        tournament="Friendly",
        elo_home_pre=1500.0,
        elo_away_pre=1500.0,
    )


def test_dixon_coles_predicts_valid_probabilities() -> None:
    matches = [
        _match(1, 1, 2, 2, 0),
        _match(2, 2, 1, 1, 1),
        _match(3, 1, 3, 3, 1),
        _match(4, 3, 2, 0, 2),
        _match(5, 2, 3, 1, 0),
    ]
    model = DixonColes(epochs=5, rho_grid=5, seed=1).fit(matches)
    probs = model.predict_proba(matches)
    assert len(probs) == len(matches)
    for row in probs:
        assert len(row) == 3
        assert all(0.0 <= value <= 1.0 for value in row)
        assert abs(sum(row) - 1.0) < 1e-8
