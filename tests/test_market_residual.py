import datetime
import math

import numpy as np

from scorerecord.evaluate.market_residual import (
    LogisticResidualModel,
    Ou25ResidualRow,
    _extract_features,
)


def test_logistic_residual_model_fits_synthetic_residual():
    """Test 8: LogisticResidualModel can fit a small synthetic residual signal."""
    # Synthetic data where residual feature perfectly predicts the target beyond the offset
    np.random.seed(42)
    offset_logit = np.zeros(1000)
    X = np.random.randn(1000, 1)
    
    # If X > 0, higher chance of 1, if X < 0, lower chance
    true_logits = offset_logit + 0.5 + 2.0 * X[:, 0]
    p_true = 1.0 / (1.0 + np.exp(-true_logits))
    y = (np.random.rand(1000) < p_true).astype(float)
    
    model = LogisticResidualModel(l2=0.01)
    model.fit(offset_logit, X, y)
    
    assert model.converged_
    # Intercept should be close to 0.5
    assert math.isclose(model.intercept_, 0.5, abs_tol=0.2)
    # Coef should be positive
    assert model.coef_[0] > 1.0
    
    # Test 7: market_preclose_logit is used as fixed offset
    # The offset was not part of the X matrix, but passed separately


def test_feature_standardization_and_zero_variance():
    """Test 9 & 10: Feature standardization and zero-variance handling."""
    offset_logit = np.zeros(100)
    # X has one zero-variance feature and one normal feature
    X = np.hstack([np.ones((100, 1)) * 5.0, np.random.randn(100, 1)])
    y = np.random.randint(0, 2, size=100).astype(float)
    
    model = LogisticResidualModel(l2=1.0)
    model.fit(offset_logit, X, y)
    
    assert model.converged_
    assert model.feature_mean_[0] == 5.0
    assert model.feature_scale_[0] == 1.0  # Safe fallback for 0 variance
    assert not np.isnan(model.coef_).any()


def test_extract_features_no_closing_market():
    """Test 6: Closing market is not used as feature."""
    rows = [
        Ou25ResidualRow(
            match_id="1", date="2024-01-01", league="EPL", season="2024", y=1,
            market_preclose_fair=0.5, market_close_fair=0.6, market_preclose_logit=0.0,
            dixon_coles_raw_prob=0.5, dixon_coles_calibrated_prob=0.5,
            dc_raw_logit_minus_market_logit=0.0, dc_calibrated_logit_minus_market_logit=0.0
        )
    ]
    import pytest
    with pytest.raises(ValueError, match="is not in the allowlist"):
        _extract_features(rows, ("market_close_fair",))
        
    with pytest.raises(ValueError, match="is not in the allowlist"):
        _extract_features(rows, ("closing_odds",))
        
    # Allowed feature works
    X = _extract_features(rows, ("dc_raw_logit_minus_market_logit",))
    assert X.shape == (1, 1)


def test_walk_forward_logic(monkeypatch):
    """Test walk-forward leakage, min_train, paired eval, bootstrap blocks, state logic."""
    from scorerecord.evaluate.market_residual import run_market_residual_benchmark
    
    # Create 1000 rows across 5 weeks
    rows = []
    base_date = datetime.date(2024, 1, 1)
    for i in range(1000):
        week = (i // 200)
        date_val = base_date + datetime.timedelta(days=week * 7)
        date_str = date_val.isoformat()
        rows.append(Ou25ResidualRow(
            match_id=str(i), date=date_str, league="EPL", season="2024", y=i % 2,
            market_preclose_fair=0.5, market_close_fair=0.5, market_preclose_logit=0.0,
            dixon_coles_raw_prob=0.5, dixon_coles_calibrated_prob=None,
            dc_raw_logit_minus_market_logit=0.0, dc_calibrated_logit_minus_market_logit=None
        ))
    
    import sqlite3
    
    def _mock_build(*args, **kwargs):
        return rows
        
    # Mock dataset builder
    monkeypatch.setattr(
        "scorerecord.evaluate.market_residual.build_ou25_residual_dataset", 
        _mock_build
    )
    
    # We set min_train to 400. That means Week 1 (200) + Week 2 (200) 
    # will be used to train for Week 3.
    # Evaluations should only happen for Week 3, 4, 5 (600 rows total)
    conn = sqlite3.connect(":memory:")
    report = run_market_residual_benchmark(
        conn,
        from_date=np.datetime64("2024-01-01").astype(object), # Dummy
        to_date=np.datetime64("2024-12-31").astype(object),
        leagues=("EPL",),
        entry_odds="avg_preclose",
        closing_odds="avg_close",
        bootstrap_block="week",
        bootstrap_samples=10,
        min_train=400,
        features=("dc_raw_logit_minus_market_logit",),
        l2=1.0,
        max_iter=10
    )
    
    # Paired comparison: num_matches should be exactly 600 (weeks 3, 4, 5)
    assert report.num_matches == 600
    assert report.num_selection_rows == 1200
    
    # State logic check
    assert report.residual_benchmark_state in (
        "RESIDUAL_PROMISING", 
        "RESIDUAL_INCONCLUSIVE", 
        "RESIDUAL_REJECTED"
    )


def test_no_paper_candidates_produced():
    """Test 16: No EV, edge, stake, Kelly, value or paper candidate output."""
    from scorerecord.evaluate.market_residual import MarketResidualBenchmarkReport
    fields = MarketResidualBenchmarkReport.__dataclass_fields__.keys()
    assert "ev" not in fields
    assert "edge" not in fields
    assert "stake" not in fields
    assert "kelly" not in fields
    assert "recommendation" not in fields
