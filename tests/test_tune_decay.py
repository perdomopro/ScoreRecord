from scorerecord.evaluate.tune_decay import DecayTrial, best_trial, half_life_to_xi


def test_half_life_to_xi_gets_smaller_for_longer_memory() -> None:
    assert half_life_to_xi(1.0) > half_life_to_xi(3.0)


def test_best_trial_prefers_close_logloss() -> None:
    trials = [
        DecayTrial(1.0, 0.00190, 100, 0.90, 1.04, 20, 0.02),
        DecayTrial(3.0, 0.00063, 100, 0.91, 1.02, 20, 0.03),
    ]
    assert best_trial(trials).half_life_years == 3.0
