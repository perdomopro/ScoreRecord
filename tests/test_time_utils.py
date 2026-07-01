from scorerecord.time_utils import format_utc, parse_kickoff_utc


def test_parse_kickoff_utc_from_openfootball_offset() -> None:
    parsed = parse_kickoff_utc(kickoff_local="2026-06-11 13:00 UTC-6")

    assert format_utc(parsed) == "2026-06-11T19:00:00+00:00"
