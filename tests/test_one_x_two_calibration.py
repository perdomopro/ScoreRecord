from scorerecord.evaluate.one_x_two_calibration import _calibration_status


class _Result:
    def __init__(self, logloss: float, ece: float) -> None:
        self.logloss = logloss
        self.ece = ece


def test_one_x_two_calibration_status_ok_when_dc_beats_marginal_and_ece_low() -> None:
    assert (
        _calibration_status(_Result(logloss=0.95, ece=0.03), _Result(logloss=1.02, ece=0.0))
        == "CALIBRATION_OK"
    )


def test_one_x_two_calibration_status_warn_when_ece_mid() -> None:
    assert (
        _calibration_status(_Result(logloss=0.95, ece=0.07), _Result(logloss=1.02, ece=0.0))
        == "CALIBRATION_WARN"
    )


def test_one_x_two_calibration_status_review_when_dc_loses_to_marginal() -> None:
    assert (
        _calibration_status(_Result(logloss=1.08, ece=0.03), _Result(logloss=1.02, ece=0.0))
        == "REVIEW_REQUIRED"
    )
