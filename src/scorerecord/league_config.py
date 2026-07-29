from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LeagueConfig:
    code: str
    label: str
    competition: str
    season: str
    odds_api_io_sport: str
    odds_api_io_slug: str
    football_data_alias: str | None = None


CLUB_FORECAST_LEAGUES = (
    "EPL",
    "LIGA_MX",
    "MLS",
    "ARGENTINA",
    "BRASILEIRAO",
    "LA_LIGA",
    "SERIE_A",
    "BUNDESLIGA",
    "LIGUE_1",
    "CHAMPIONSHIP",
)

GUI_LEAGUE_ORDER = (
    "LIGA_MX",
    "MLS",
    "EPL",
    "ARGENTINA",
    "BRASILEIRAO",
)

PROVIDER_WATCHLIST_LEAGUES = (
    "CHAMPIONS_LEAGUE",
    "EUROPA_LEAGUE",
    "LIBERTADORES",
    "SUDAMERICANA",
)

LEAGUE_CONFIGS: dict[str, LeagueConfig] = {
    "EPL": LeagueConfig(
        code="EPL",
        label="Premier League",
        competition="English Premier League",
        season="2026-2027",
        odds_api_io_sport="football",
        odds_api_io_slug="england-premier-league",
        football_data_alias="EPL",
    ),
    "LIGA_MX": LeagueConfig(
        code="LIGA_MX",
        label="Liga MX",
        competition="Liga MX",
        season="2026-2027",
        odds_api_io_sport="football",
        odds_api_io_slug="mexico-liga-mx-apertura",
        football_data_alias="LIGA_MX",
    ),
    "MLS": LeagueConfig(
        code="MLS",
        label="MLS",
        competition="Major League Soccer",
        season="2026-2027",
        odds_api_io_sport="football",
        odds_api_io_slug="usa-mls",
        football_data_alias="MLS",
    ),
    "ARGENTINA": LeagueConfig(
        code="ARGENTINA",
        label="Argentina",
        competition="Argentina Primera LPF Clausura",
        season="2026-2027",
        odds_api_io_sport="football",
        odds_api_io_slug="argentina-primera-lpf-clausura",
        football_data_alias="ARG",
    ),
    "BRASILEIRAO": LeagueConfig(
        code="BRASILEIRAO",
        label="Brasileirao",
        competition="Brazil Brasileiro Serie A",
        season="2026-2027",
        odds_api_io_sport="football",
        odds_api_io_slug="brazil-brasileiro-serie-a",
        football_data_alias="BRA",
    ),
    # Provider-visible competitions that are useful to track, but not yet safe
    # to surface as forecast pilots until they have an explicit model artifact
    # and bridged historical scope in ScoreRecord.
    "CHAMPIONS_LEAGUE": LeagueConfig(
        code="CHAMPIONS_LEAGUE",
        label="Champions League",
        competition="UEFA Champions League Qualification",
        season="2026-2027",
        odds_api_io_sport="football",
        odds_api_io_slug="international-clubs-uefa-champions-league-qualification",
        football_data_alias=None,
    ),
    "EUROPA_LEAGUE": LeagueConfig(
        code="EUROPA_LEAGUE",
        label="Europa League",
        competition="UEFA Europa League Qualification",
        season="2026-2027",
        odds_api_io_sport="football",
        odds_api_io_slug="international-clubs-uefa-europa-league-qualification",
        football_data_alias=None,
    ),
    "LIBERTADORES": LeagueConfig(
        code="LIBERTADORES",
        label="Libertadores",
        competition="CONMEBOL Libertadores Knockout Stage",
        season="2026-2027",
        odds_api_io_sport="football",
        odds_api_io_slug="international-clubs-conmebol-libertadores-knockout-stage",
        football_data_alias=None,
    ),
    "SUDAMERICANA": LeagueConfig(
        code="SUDAMERICANA",
        label="Sudamericana",
        competition="CONMEBOL Sudamericana Knockout Stage",
        season="2026-2027",
        odds_api_io_sport="football",
        odds_api_io_slug="international-clubs-conmebol-sudamericana-knockout-stage",
        football_data_alias=None,
    ),
}

_ALIASES = {
    "ALL": "ALL",
    "TODO": "ALL",
    "EPL": "EPL",
    "PREMIER_LEAGUE": "EPL",
    "ENGLISH_PREMIER_LEAGUE": "EPL",
    "LIGA_MX": "LIGA_MX",
    "LIGAMX": "LIGA_MX",
    "MEX": "LIGA_MX",
    "MEXICO": "LIGA_MX",
    "MEXICO_LIGA_MX": "LIGA_MX",
    "MLS": "MLS",
    "USA": "MLS",
    "USA_MLS": "MLS",
    "MAJOR_LEAGUE_SOCCER": "MLS",
    "ARG": "ARGENTINA",
    "ARGENTINA": "ARGENTINA",
    "ARGENTINA_LIGA_PROFESIONAL": "ARGENTINA",
    "ARGENTINA_PRIMERA_LPF": "ARGENTINA",
    "ARGENTINA_PRIMERA_LPF_CLAUSURA": "ARGENTINA",
    "BRA": "BRASILEIRAO",
    "BRASIL": "BRASILEIRAO",
    "BRAZIL": "BRASILEIRAO",
    "BRASILEIRAO": "BRASILEIRAO",
    "BRASIL_SERIE_A": "BRASILEIRAO",
    "BRAZIL_SERIE_A": "BRASILEIRAO",
    "BRAZIL_BRASILEIRO_SERIE_A": "BRASILEIRAO",
    "UCL": "CHAMPIONS_LEAGUE",
    "CHAMPIONS": "CHAMPIONS_LEAGUE",
    "CHAMPIONS_LEAGUE": "CHAMPIONS_LEAGUE",
    "UEFA_CHAMPIONS_LEAGUE": "CHAMPIONS_LEAGUE",
    "UEL": "EUROPA_LEAGUE",
    "EUROPA": "EUROPA_LEAGUE",
    "EUROPA_LEAGUE": "EUROPA_LEAGUE",
    "UEFA_EUROPA_LEAGUE": "EUROPA_LEAGUE",
    "CONMEBOL": "LIBERTADORES",
    "LIBERTADORES": "LIBERTADORES",
    "COPA_LIBERTADORES": "LIBERTADORES",
    "SUDAMERICANA": "SUDAMERICANA",
    "COPA_SUDAMERICANA": "SUDAMERICANA",
}


def normalize_league_code(value: str | None, *, default: str = "ALL") -> str:
    if not value:
        return default
    key = value.strip().upper().replace("-", "_").replace(" ", "_")
    return _ALIASES.get(key, key)


def gui_league_filter(value: str | None) -> str:
    code = normalize_league_code(value)
    if code == "ALL" or code in GUI_LEAGUE_ORDER:
        return code
    return "ALL"


def league_config(value: str) -> LeagueConfig:
    code = normalize_league_code(value, default=value)
    try:
        return LEAGUE_CONFIGS[code]
    except KeyError as exc:
        supported = ", ".join(sorted(LEAGUE_CONFIGS))
        raise ValueError(f"Unsupported club forecast league '{value}'. Supported: {supported}") from exc


def league_label(value: str | None) -> str:
    if not value:
        return "Otras competiciones"
    code = normalize_league_code(value, default=value)
    config = LEAGUE_CONFIGS.get(code)
    return config.label if config else value
