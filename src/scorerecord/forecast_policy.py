"""Shared forecast-only selection and signal policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeVar

FORECAST_MARKETS = ("OU1.5", "OU2.5", "OU3.5", "BTTS")
SIGNAL_NO_SIGNAL = "NO_SIGNAL"
SIGNAL_LEAN = "LEAN"
SIGNAL_MODERATE = "MODERATE"
SIGNAL_STRONG = "STRONG"


class ForecastCandidate(Protocol):
    market: str
    prob: float


TForecastCandidate = TypeVar("TForecastCandidate", bound=ForecastCandidate)


@dataclass(frozen=True)
class SignalStrength:
    code: str
    label: str
    css_class: str


def select_primary_forecast(
    markets: list[TForecastCandidate],
) -> TForecastCandidate | None:
    if not markets:
        return None
    return max(markets, key=lambda market: (market.prob, _market_rank(market.market)))


def select_primary_forecasts(
    markets: list[TForecastCandidate],
) -> list[TForecastCandidate]:
    goal_primary = select_primary_forecast(
        [market for market in markets if market.market == "OU2.5"]
    )
    btts_primary = select_primary_forecast(
        [market for market in markets if market.market == "BTTS"]
    )
    return [market for market in [goal_primary, btts_primary] if market is not None]


def signal_strength(prob: float) -> SignalStrength:
    delta = abs(prob - 0.50)
    if delta < 0.03:
        return SignalStrength(SIGNAL_NO_SIGNAL, "Sin senal clara", "signal-no-signal")
    if delta < 0.08:
        return SignalStrength(SIGNAL_LEAN, "Senal debil", "signal-lean")
    if delta < 0.15:
        return SignalStrength(SIGNAL_MODERATE, "Senal moderada", "signal-moderate")
    if delta < 0.20:
        return SignalStrength(SIGNAL_STRONG, "Senal fuerte", "signal-strong")
    return SignalStrength(SIGNAL_STRONG, "Senal muy fuerte", "signal-strong")


def _market_rank(market: str) -> int:
    order = {market_name: index for index, market_name in enumerate(FORECAST_MARKETS)}
    return -order.get(market, 99)
