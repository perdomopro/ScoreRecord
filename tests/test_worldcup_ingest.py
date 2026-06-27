from scorerecord.ingest.worldcup import _is_placeholder


def test_worldcup_knockout_placeholders_are_not_teams() -> None:
    for name in ["W73", "L101", "1B", "2L", "3A/B/C/D/F", "3C/3D/3F/3G/3H"]:
        assert _is_placeholder(name)


def test_worldcup_real_team_names_are_not_placeholders() -> None:
    for name in ["Brazil", "Canada", "South Africa", "Cote d'Ivoire"]:
        assert not _is_placeholder(name)
