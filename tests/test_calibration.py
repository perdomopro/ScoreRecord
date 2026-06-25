from scorerecord.models.calibration import PlattCalibrator


def test_platt_calibrator_keeps_probabilities_in_range() -> None:
    calibrator = PlattCalibrator(epochs=20).fit(
        [0.1, 0.2, 0.8, 0.9],
        [0, 0, 1, 1],
    )
    for prob in [0.01, 0.25, 0.5, 0.75, 0.99]:
        calibrated = calibrator.transform(prob)
        assert 0.0 < calibrated < 1.0


def test_platt_calibrator_preserves_ordering() -> None:
    calibrator = PlattCalibrator(epochs=20).fit(
        [0.1, 0.2, 0.8, 0.9],
        [0, 0, 1, 1],
    )
    assert calibrator.transform(0.2) < calibrator.transform(0.8)
