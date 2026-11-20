from scorerecord.league_config import (
    GUI_LEAGUE_ORDER,
    PROVIDER_WATCHLIST_LEAGUES,
    gui_league_filter,
    league_config,
    league_label,
    normalize_league_code,
)


def test_new_domestic_pilot_leagues_are_gui_supported() -> None:
    assert "ARGENTINA" in GUI_LEAGUE_ORDER
    assert "BRASILEIRAO" in GUI_LEAGUE_ORDER
    assert gui_league_filter("argentina-primera-lpf-clausura") == "ARGENTINA"
    assert gui_league_filter("brazil-brasileiro-serie-a") == "BRASILEIRAO"
    assert league_label("ARGENTINA") == "Argentina"
    assert league_label("BRASILEIRAO") == "Brasileirao"


def test_provider_watchlist_is_not_primary_gui_filter() -> None:
    assert "CHAMPIONS_LEAGUE" in PROVIDER_WATCHLIST_LEAGUES
    assert "EUROPA_LEAGUE" in PROVIDER_WATCHLIST_LEAGUES
    assert "LIBERTADORES" in PROVIDER_WATCHLIST_LEAGUES
    assert "SUDAMERICANA" in PROVIDER_WATCHLIST_LEAGUES
    assert gui_league_filter("champions") == "ALL"
    assert normalize_league_code("conmebol") == "LIBERTADORES"


def test_odds_api_io_slugs_are_catalog_backed() -> None:
    assert league_config("ARGENTINA").odds_api_io_slug == "argentina-primera-lpf-clausura"
    assert league_config("BRASILEIRAO").odds_api_io_slug == "brazil-brasileiro-serie-a"
    assert (
        league_config("CHAMPIONS_LEAGUE").odds_api_io_slug
        == "international-clubs-uefa-champions-league-qualification"
    )
    assert (
        league_config("EUROPA_LEAGUE").odds_api_io_slug
        == "international-clubs-uefa-europa-league-qualification"
    )


def test_european_leagues_are_configured_end_to_end() -> None:
    from scorerecord.ingest.football_data import FOOTBALL_DATA_LEAGUES
    from scorerecord.league_config import CLUB_FORECAST_LEAGUES

    expected = {
        "LA_LIGA": ("spain-laliga", "SP1", "LaLiga"),
        "SERIE_A": ("italy-serie-a", "I1", "Serie A"),
        "BUNDESLIGA": ("germany-bundesliga", "D1", "Bundesliga"),
        "LIGUE_1": ("france-ligue-1", "F1", "Ligue 1"),
        "CHAMPIONSHIP": ("england-championship", "E1", "Championship"),
    }
    for code, (slug, fd_code, label) in expected.items():
        config = league_config(code)
        assert config.odds_api_io_slug == slug
        assert FOOTBALL_DATA_LEAGUES[config.football_data_alias] == fd_code
        assert league_label(code) == label
        assert code in GUI_LEAGUE_ORDER and code in CLUB_FORECAST_LEAGUES
        assert gui_league_filter(slug.replace("-", "_")) in (code, "ALL")
    assert normalize_league_code("laliga") == "LA_LIGA"
    assert normalize_league_code("ligue-1") == "LIGUE_1"
    # Every dashboard league can be refreshed by the daily loop.
    assert set(GUI_LEAGUE_ORDER) <= set(CLUB_FORECAST_LEAGUES)
