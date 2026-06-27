"""Common dataclasses for real historical odds providers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class HistoricalEvent:
    source: str
    source_event_id: str
    league_code: str
    season: str
    match_date: date
    home_team: str
    away_team: str
    home_goals: int | None
    away_goals: int | None
    result_1x2: str | None
    raw_json: str


@dataclass(frozen=True)
class HistoricalOddsQuote:
    source: str
    source_event_id: str
    bookmaker: str
    market: str
    selection: str
    line: float | None
    entry_odds: float | None
    entry_odds_type: str | None
    closing_odds: float | None
    closing_odds_type: str | None
    is_real_historical_odds: bool = True
    is_timestamp_exact: bool = False
