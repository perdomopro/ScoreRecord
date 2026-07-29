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
