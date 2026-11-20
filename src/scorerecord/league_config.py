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
    # Extra provider slugs for other phases of the same competition (second
    # half-season, playoffs). Verified against odds-api.io /leagues.
    odds_api_io_extra_slugs: tuple[str, ...] = ()

    @property
    def odds_api_io_slugs(self) -> tuple[str, ...]:
        return (self.odds_api_io_slug, *self.odds_api_io_extra_slugs)


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
    "LA_LIGA",
    "SERIE_A",
    "BUNDESLIGA",
    "LIGUE_1",
    "CHAMPIONSHIP",
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
        odds_api_io_extra_slugs=("mexico-liga-mx-clausura",),
    ),
    "MLS": LeagueConfig(
        code="MLS",
        label="MLS",
        competition="Major League Soccer",
        season="2026-2027",
        odds_api_io_sport="football",
        odds_api_io_slug="usa-mls",
        football_data_alias="MLS",
        odds_api_io_extra_slugs=("usa-mls-playoffs",),
    ),
    "ARGENTINA": LeagueConfig(
        code="ARGENTINA",
        label="Argentina",
        competition="Argentina Primera LPF Clausura",
        season="2026-2027",
        odds_api_io_sport="football",
        odds_api_io_slug="argentina-primera-lpf-clausura",
        football_data_alias="ARG",
        odds_api_io_extra_slugs=("argentina-primera-lpf-clausura-playoffs",),
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
    # European leagues. Slugs verified against odds-api.io /leagues on
    # 2026-11-20; Football-Data publishes results and closing odds for all five.
    "LA_LIGA": LeagueConfig(
        code="LA_LIGA",
        label="LaLiga",
        competition="Spain LaLiga",
        season="2026-2027",
        odds_api_io_sport="football",
        odds_api_io_slug="spain-laliga",
        football_data_alias="LA_LIGA",
    ),
    "SERIE_A": LeagueConfig(
        code="SERIE_A",
        label="Serie A",
        competition="Italy Serie A",
        season="2026-2027",
        odds_api_io_sport="football",
        odds_api_io_slug="italy-serie-a",
        football_data_alias="SERIE_A",
    ),
    "BUNDESLIGA": LeagueConfig(
        code="BUNDESLIGA",
        label="Bundesliga",
        competition="Germany Bundesliga",
        season="2026-2027",
        odds_api_io_sport="football",
        odds_api_io_slug="germany-bundesliga",
        football_data_alias="BUNDESLIGA",
    ),
    "LIGUE_1": LeagueConfig(
        code="LIGUE_1",
        label="Ligue 1",
        competition="France Ligue 1",
        season="2026-2027",
        odds_api_io_sport="football",
        odds_api_io_slug="france-ligue-1",
        football_data_alias="LIGUE_1",
    ),
    "CHAMPIONSHIP": LeagueConfig(
        code="CHAMPIONSHIP",
        label="Championship",
        competition="England Championship",
        season="2026-2027",
        odds_api_io_sport="football",
        odds_api_io_slug="england-championship",
        football_data_alias="CHAMPIONSHIP",
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
    "LA_LIGA": "LA_LIGA",
    "LALIGA": "LA_LIGA",
    "SPAIN": "LA_LIGA",
    "SPAIN_LALIGA": "LA_LIGA",
    "SERIE_A": "SERIE_A",
    "ITALY": "SERIE_A",
    "ITALY_SERIE_A": "SERIE_A",
    "BUNDESLIGA": "BUNDESLIGA",
    "GERMANY": "BUNDESLIGA",
    "GERMANY_BUNDESLIGA": "BUNDESLIGA",
    "LIGUE_1": "LIGUE_1",
    "LIGUE1": "LIGUE_1",
    "FRANCE": "LIGUE_1",
    "FRANCE_LIGUE_1": "LIGUE_1",
    "CHAMPIONSHIP": "CHAMPIONSHIP",
    "EFL_CHAMPIONSHIP": "CHAMPIONSHIP",
    "ENGLAND_CHAMPIONSHIP": "CHAMPIONSHIP",
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
