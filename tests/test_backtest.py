from scorerecord.evaluate.backtest import (
    BacktestBet,
    HistoricalPrediction,
    _calibration_rows,
    _select_backtest_bets,
    edge_bucket,
    max_drawdown,
    odds_bucket,
    prob_bucket,
)
from scorerecord.features.build import MatchFeature


def _match(match_id: str = "m1") -> MatchFeature:
    return MatchFeature(
        source_match_id=match_id,
        match_date="2022-06-01",
        home_id=1,
        away_id=2,
        home_score=1,
        away_score=0,
        neutral=1,
        tournament="FIFA World Cup",
        elo_home_pre=1600.0,
        elo_away_pre=1550.0,
    )


def _prediction(
    *,
    fixture_id: str = "m1",
    selection: str = "home",
    model_prob: float = 0.55,
    market_prob: float = 0.50,
    edge_ev: float = 0.10,
    score: float = 0.10,
    won: int = 1,
    reason: str = "",
) -> HistoricalPrediction:
    return HistoricalPrediction(
        match=_match(fixture_id),
        market="1X2",
        selection=selection,
        model_prob=model_prob,
        market_prob=market_prob,
        edge_ev=edge_ev,
        edge_prob=model_prob - market_prob,
        recommendation_score=score,
        high_edge_reason=reason,
        odds_quality_flags="",
        won=won,
    )


def test_backtest_buckets_and_drawdown() -> None:
    assert edge_bucket(0.03) == "2.5-5.0%"
    assert odds_bucket(3.4) == "3.00-5.00"
    assert prob_bucket(0.62) == "60-75%"
    assert max_drawdown([1.0, -0.5, -1.0, 0.25]) == -1.5


def test_backtest_selection_compares_edge_and_score_rankings() -> None:
    rows = [
        _prediction(selection="home", edge_ev=0.12, score=0.06),
        _prediction(selection="away", edge_ev=0.08, score=0.08),
    ]

    bets = _select_backtest_bets(
        rows,
        min_edge=0.05,
        max_edge=0.25,
        min_prob=0.15,
        max_picks_per_fixture=1,
    )

    by_strategy = {bet.ranking_strategy: bet.prediction.selection for bet in bets}
    assert by_strategy == {"edge_ev": "home", "recommendation_score": "away"}


def test_backtest_selection_excludes_critical_reason() -> None:
    rows = [
        _prediction(reason="model_market_disagreement,long_odds_tail"),
        _prediction(fixture_id="m2", selection="away", edge_ev=0.08, score=0.08),
    ]

    bets = _select_backtest_bets(
        rows,
        min_edge=0.05,
        max_edge=0.25,
        min_prob=0.15,
        max_picks_per_fixture=1,
    )

    assert all(bet.prediction.match.source_match_id == "m2" for bet in bets)


def test_calibration_rows_flag_large_supported_errors() -> None:
    rows = [
        _prediction(model_prob=0.40, market_prob=0.35, won=0)
        for _ in range(60)
    ]

    calibration = _calibration_rows(rows)

    assert len(calibration) == 1
    assert calibration[0]["problem"] == 1
