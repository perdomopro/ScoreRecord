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
    "LA_LIGA",
    "SERIE_A",
    "BUNDESLIGA",
    "LIGUE_1",
    "CHAMPIONSHIP",
)

GUI_LEAGUE_ORDER = ("LIGA_MX", "MLS", "EPL")

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
