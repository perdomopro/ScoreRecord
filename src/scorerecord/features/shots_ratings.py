"""Prequential Exponential Moving Average ratings for process stats (Goals, Shots, SOT)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime


@dataclass
class TeamProcessState:
    team: str
    last_match_date: datetime
    goals_for: float
    goals_against: float
    shots_for: float
    shots_against: float
    sot_for: float
    sot_against: float


class ProcessRatingTracker:
    def __init__(self, half_life_days: float = 365.0):
        self.half_life_days = half_life_days
        self.decay_constant = math.log(2) / half_life_days
        self.team_states: dict[str, TeamProcessState] = {}
        
    def _decay(self, current: float, delta_days: float) -> float:
        """Decay a rating over delta_days."""
        # This gives weight to the old value.
        # But this is a moving average.
        return current

    def get_ratings(self, team: str, match_date: datetime) -> dict[str, float]:
        """Get pre-match ratings for a team, decayed to match_date.
        
        If team has no history, return 0.0 for all (or perhaps a league average later, but 0.0 is fine for regression if we use an intercept or treat it as baseline).
        Wait, moving averages usually initialize at the first observation.
        For a prototype, returning 0.0 is fine if the model handles the intercept.
        Actually, let's return the last known average, decayed?
        Wait, if we use EMA:
        EMA_t = EMA_{t-1} * w + X_t * (1 - w)
        For forecasting *before* X_t, the best estimate is just EMA_{t-1}.
        Decaying an average towards 0 over time implies reversion to 0, which for goals is wrong (should revert to league average).
        For simplicity, a pure EWMA predictor for X_t is just EMA_{t-1}.
        So the time delta doesn't necessarily decay the *value* of the average towards 0, it just reduces the *weight* of past observations relative to the new one.
        Let's implement a standard EWMA where:
        Value = sum(w_i * X_i) / sum(w_i)
        This is robust.
        """
        state = self.team_states.get(team)
        if not state:
            return {
                "goals_for": 0.0,
                "goals_against": 0.0,
                "shots_for": 0.0,
                "shots_against": 0.0,
                "sot_for": 0.0,
                "sot_against": 0.0,
            }
            
        return {
            "goals_for": state.goals_for,
            "goals_against": state.goals_against,
            "shots_for": state.shots_for,
            "shots_against": state.shots_against,
            "sot_for": state.sot_for,
            "sot_against": state.sot_against,
        }

@dataclass
class EWMATeamState:
    team: str
    last_match_date: datetime
    # sum of weights * values
    sum_w_goals_for: float = 0.0
    sum_w_goals_against: float = 0.0
    sum_w_shots_for: float = 0.0
    sum_w_shots_against: float = 0.0
    sum_w_sot_for: float = 0.0
    sum_w_sot_against: float = 0.0
    # sum of weights
    sum_w: float = 0.0


class ProcessRatingTrackerEWMA:
    """Calculates continuous-time Exponential Weighted Moving Average (EWMA) of process stats."""
    
    def __init__(self, half_life_days: float = 365.0):
        self.half_life_days = half_life_days
        self.decay_constant = math.log(2) / half_life_days
        self.team_states: dict[str, EWMATeamState] = {}
        # Global averages for initialization
        self.global_sum_w = 0.0
        self.global_sum_w_goals_for = 0.0
        self.global_sum_w_shots_for = 0.0
        self.global_sum_w_sot_for = 0.0
        # Since average goals for = average goals against globally, we can use the same.

    def _get_average(self, sum_w_val: float, sum_w: float, global_mean: float) -> float:
        if sum_w <= 0.0:
            return global_mean
        return sum_w_val / sum_w

    def get_ratings(self, team: str, match_date: datetime) -> dict[str, float]:
        """Return the pre-match EWMA expectation for the team."""
        state = self.team_states.get(team)
        
        # Calculate global means as fallback
        g_goals = self.global_sum_w_goals_for / self.global_sum_w if self.global_sum_w > 0 else 1.3
        g_shots = self.global_sum_w_shots_for / self.global_sum_w if self.global_sum_w > 0 else 12.0
        g_sot = self.global_sum_w_sot_for / self.global_sum_w if self.global_sum_w > 0 else 4.0
        
        if not state:
            return {
                "goals_for": g_goals,
                "goals_against": g_goals,
                "shots_for": g_shots,
                "shots_against": g_shots,
                "sot_for": g_sot,
                "sot_against": g_sot,
            }
            
        # The EWMA value right before the match is the same as the EWMA value right after the last match,
        # because both numerator and denominator decay by the same factor exp(-lambda * delta_t).
        # Thus, the ratio (the expectation) remains constant between matches!
        
        return {
            "goals_for": self._get_average(state.sum_w_goals_for, state.sum_w, g_goals),
            "goals_against": self._get_average(state.sum_w_goals_against, state.sum_w, g_goals),
            "shots_for": self._get_average(state.sum_w_shots_for, state.sum_w, g_shots),
            "shots_against": self._get_average(state.sum_w_shots_against, state.sum_w, g_shots),
            "sot_for": self._get_average(state.sum_w_sot_for, state.sum_w, g_sot),
            "sot_against": self._get_average(state.sum_w_sot_against, state.sum_w, g_sot),
        }

    def update(
        self,
        team: str,
        match_date: datetime,
        goals_for: float,
        goals_against: float,
        shots_for: float,
        shots_against: float,
        sot_for: float,
        sot_against: float,
    ) -> None:
        """Update the EWMA state for a team after a match."""
        state = self.team_states.get(team)
        if not state:
            state = EWMATeamState(team=team, last_match_date=match_date)
            self.team_states[team] = state
            
        delta_days = (match_date - state.last_match_date).total_seconds() / 86400.0
        if delta_days < 0:
            delta_days = 0.0  # Prevent negative decay if matches are out of order/same day
            
        decay = math.exp(-self.decay_constant * delta_days)
        
        # Apply time decay to historical sums
        state.sum_w_goals_for *= decay
        state.sum_w_goals_against *= decay
        state.sum_w_shots_for *= decay
        state.sum_w_shots_against *= decay
        state.sum_w_sot_for *= decay
        state.sum_w_sot_against *= decay
        state.sum_w *= decay
        
        # Add the new observation with weight 1.0
        state.sum_w_goals_for += goals_for
        state.sum_w_goals_against += goals_against
        state.sum_w_shots_for += shots_for
        state.sum_w_shots_against += shots_against
        state.sum_w_sot_for += sot_for
        state.sum_w_sot_against += sot_against
        state.sum_w += 1.0
        
        state.last_match_date = match_date
        
        # Update global sums for sensible initialization (decayed slightly just for stability, or keep it simple)
        # For simplicity, just add them as an expanding window mean
        self.global_sum_w_goals_for += goals_for
        self.global_sum_w_shots_for += shots_for
        self.global_sum_w_sot_for += sot_for
        self.global_sum_w += 1.0
