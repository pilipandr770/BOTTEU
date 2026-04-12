"""
Unit tests for app/algorithms/consensus/engine.py

Tests compute_consensus() with various vote combinations.
No real database or Binance connection needed.
"""
from __future__ import annotations

import pytest


def _vote(signal: float, weight: float = 1.0, tf: str = "1h", voter: str = "test"):
    from app.algorithms.consensus.engine import Vote
    return Vote(voter=voter, timeframe=tf, signal=signal, weight=weight, raw_value=signal)


class TestComputeConsensus:
    def test_empty_votes_returns_hold(self):
        from app.algorithms.consensus.engine import compute_consensus
        result = compute_consensus([])
        assert result.decision == "HOLD"
        assert result.normalized_score == 0.0

    def test_all_buy_votes_produces_buy(self):
        from app.algorithms.consensus.engine import compute_consensus
        votes = [_vote(1.0, weight=2.0) for _ in range(5)]
        result = compute_consensus(votes, entry_threshold=60.0)
        assert result.decision == "BUY"
        assert result.normalized_score > 60.0

    def test_all_sell_votes_produces_sell(self):
        from app.algorithms.consensus.engine import compute_consensus
        votes = [_vote(-1.0, weight=2.0) for _ in range(5)]
        result = compute_consensus(votes, exit_threshold=-40.0)
        assert result.decision == "SELL"
        assert result.normalized_score < -40.0

    def test_mixed_votes_may_produce_hold(self):
        from app.algorithms.consensus.engine import compute_consensus
        votes = [_vote(1.0), _vote(-1.0), _vote(0.0)]
        result = compute_consensus(votes, entry_threshold=60.0, exit_threshold=-40.0)
        assert result.decision == "HOLD"  # balanced → below threshold

    def test_volatility_modifier_amplifies_score(self):
        from app.algorithms.consensus.engine import compute_consensus
        votes = [_vote(0.5, weight=2.0) for _ in range(3)]

        result_normal = compute_consensus(votes, entry_threshold=60.0, volatility_modifier=1.0)
        result_high   = compute_consensus(votes, entry_threshold=60.0, volatility_modifier=2.0)

        # Higher modifier → higher normalized score
        assert result_high.normalized_score > result_normal.normalized_score

    def test_volatility_modifier_none_is_same_as_one(self):
        from app.algorithms.consensus.engine import compute_consensus
        votes = [_vote(0.8, weight=3.0) for _ in range(4)]

        result_none = compute_consensus(votes, volatility_modifier=None)
        result_one  = compute_consensus(votes, volatility_modifier=1.0)

        assert abs(result_none.normalized_score - result_one.normalized_score) < 1e-9

    def test_vote_counts_are_correct(self):
        from app.algorithms.consensus.engine import compute_consensus
        votes = [
            _vote(1.0, voter="v1"),
            _vote(1.0, voter="v2"),
            _vote(-1.0, voter="v3"),
            _vote(0.0, voter="v4"),
        ]
        result = compute_consensus(votes)
        assert result.buy_votes  == 2
        assert result.sell_votes == 1
        assert result.neutral_votes == 1

    def test_normalized_score_clamped_to_100(self):
        from app.algorithms.consensus.engine import compute_consensus
        # Extreme buy votes
        votes = [_vote(1.0, weight=100.0) for _ in range(10)]
        result = compute_consensus(votes)
        assert result.normalized_score <= 100.0
        assert result.normalized_score >= -100.0

    def test_entry_threshold_controls_buy_decision(self):
        from app.algorithms.consensus.engine import compute_consensus
        votes = [_vote(1.0, weight=2.0) for _ in range(3)]  # score ~ 100
        result_high_threshold = compute_consensus(votes, entry_threshold=99.0)
        result_low_threshold  = compute_consensus(votes, entry_threshold=10.0)

        # Low threshold → same score is enough for BUY
        assert result_low_threshold.decision == "BUY"

    def test_result_to_dict_contains_expected_keys(self):
        from app.algorithms.consensus.engine import compute_consensus
        votes = [_vote(1.0)]
        result = compute_consensus(votes)
        d = result.to_dict()
        assert "normalized_score" in d
        assert "decision" in d
        assert "raw_score" in d
        assert "total_weight" in d


class TestBuildWeightMatrix:
    def test_returns_dict_with_combined_weights(self):
        from app.algorithms.consensus.engine import build_weight_matrix
        tfs = ["1h", "4h"]
        inds = ["ma_cross", "rsi"]
        matrix = build_weight_matrix(tfs, inds)
        assert ("1h", "ma_cross") in matrix
        assert ("4h", "rsi") in matrix

    def test_custom_weights_override_defaults(self):
        from app.algorithms.consensus.engine import build_weight_matrix
        custom_tf  = {"1h": 10.0}
        custom_ind = {"rsi": 5.0}
        matrix = build_weight_matrix(["1h"], ["rsi"], custom_tf, custom_ind)
        assert matrix[("1h", "rsi")] == pytest.approx(50.0)  # 10 × 5


class TestComputeVolatilityModifier:
    """
    compute_volatility_modifier(atr_pct) uses:
        modifier = 0.7 + 0.3 * log1p(atr_pct / ATR_NEUTRAL_PCT)
    clamped to [0.5, 1.5].  ATR_NEUTRAL_PCT = 1.5.
    """

    def test_neutral_atr_returns_value_in_range(self):
        """ATR = 1.5% (neutral) → ~0.908 (within valid [0.5, 1.5] range)."""
        from app.algorithms.consensus.engine import compute_volatility_modifier
        result = compute_volatility_modifier(1.5)
        assert 0.5 < result < 1.5
        assert result == pytest.approx(0.908, abs=0.01)

    def test_high_atr_amplifies(self):
        """ATR = 3.0% → modifier > modifier for 1.5% (amplified signals)."""
        from app.algorithms.consensus.engine import compute_volatility_modifier
        neutral = compute_volatility_modifier(1.5)
        high    = compute_volatility_modifier(3.0)
        assert high > neutral

    def test_low_atr_dampens(self):
        """ATR = 0.3% → modifier < modifier for 1.5% (dampened signals)."""
        from app.algorithms.consensus.engine import compute_volatility_modifier
        neutral = compute_volatility_modifier(1.5)
        low     = compute_volatility_modifier(0.3)
        assert low < neutral

    def test_zero_atr_returns_minimum(self):
        """ATR ≤ 0 → hardcoded 0.7 (floor before logarithm branch)."""
        from app.algorithms.consensus.engine import compute_volatility_modifier
        assert compute_volatility_modifier(0.0)  == pytest.approx(0.7)
        assert compute_volatility_modifier(-1.0) == pytest.approx(0.7)

    def test_very_high_atr_clamped_to_max(self):
        """Extreme ATR (100%) must not exceed the upper clamp of 1.5."""
        from app.algorithms.consensus.engine import compute_volatility_modifier
        assert compute_volatility_modifier(100.0) <= 1.5

    def test_very_low_atr_clamped_to_min(self):
        """Near-zero ATR must not fall below the lower clamp of 0.5."""
        from app.algorithms.consensus.engine import compute_volatility_modifier
        assert compute_volatility_modifier(0.001) >= 0.5

    def test_high_atr_absolute_value(self):
        """ATR = 3.0% → ~1.030 (cross-check with formula)."""
        import math
        from app.algorithms.consensus.engine import compute_volatility_modifier, ATR_NEUTRAL_PCT
        expected = 0.7 + 0.3 * math.log1p(3.0 / ATR_NEUTRAL_PCT)
        expected = max(0.5, min(1.5, expected))
        assert compute_volatility_modifier(3.0) == pytest.approx(expected, abs=1e-9)

    def test_low_atr_absolute_value(self):
        """ATR = 0.3% → ~0.755 (cross-check with formula)."""
        import math
        from app.algorithms.consensus.engine import compute_volatility_modifier, ATR_NEUTRAL_PCT
        expected = 0.7 + 0.3 * math.log1p(0.3 / ATR_NEUTRAL_PCT)
        expected = max(0.5, min(1.5, expected))
        assert compute_volatility_modifier(0.3) == pytest.approx(expected, abs=1e-9)


class TestRiverVoteIntegration:
    """Test River signal → Vote conversion logic (no collector needed)."""

    def _river_vote(self, signal: int, accuracy=None, n_seen: int = 100):
        """Simulate what consensus_strategy.py does with a River signal dict."""
        from app.algorithms.consensus.engine import Vote

        if signal == 0:
            return None  # HOLD signals are skipped

        signal_float = float(signal)
        if accuracy is not None and n_seen >= 50:
            confidence = max(0.3, min(1.0, float(accuracy) * 1.5))
        else:
            confidence = 0.5

        return Vote(
            voter="river_hoeffding",
            timeframe="1m",
            signal=signal_float,
            weight=2.5,
            raw_value=signal_float,
            confidence=confidence,
        )

    def test_river_buy_signal_added_to_votes(self):
        from app.algorithms.consensus.engine import compute_consensus
        vote = self._river_vote(signal=1, accuracy=0.65, n_seen=200)
        assert vote is not None
        assert vote.signal == 1.0
        result = compute_consensus([vote])
        assert result.buy_votes == 1

    def test_river_sell_signal_added_to_votes(self):
        from app.algorithms.consensus.engine import compute_consensus
        vote = self._river_vote(signal=-1, accuracy=0.60, n_seen=150)
        assert vote is not None
        assert vote.signal == -1.0
        result = compute_consensus([vote])
        assert result.sell_votes == 1

    def test_river_hold_signal_returns_none(self):
        vote = self._river_vote(signal=0)
        assert vote is None  # HOLD votes are skipped — correct

    def test_confidence_scales_with_accuracy(self):
        vote_low  = self._river_vote(signal=1, accuracy=0.51, n_seen=100)
        vote_high = self._river_vote(signal=1, accuracy=0.75, n_seen=100)
        assert vote_high.confidence > vote_low.confidence

    def test_warmup_uses_low_confidence(self):
        vote = self._river_vote(signal=1, accuracy=None, n_seen=10)
        assert vote.confidence == 0.5  # warming up

    def test_river_weight_lower_than_sgd_ensemble(self):
        """River (2.5) should contribute less than full SGD ensemble (3.0×3=9.0)."""
        from app.algorithms.consensus.engine import compute_consensus, Vote
        river_vote = self._river_vote(signal=1, accuracy=0.7, n_seen=500)
        sgd_votes = [
            Vote(voter=f"ml_sgd_{i}", timeframe="1h",
                 signal=1.0, weight=3.0, raw_value=1.0, confidence=0.67)
            for i in range(3)
        ]
        all_votes = [river_vote] + sgd_votes
        result = compute_consensus(all_votes)
        # River + SGD all BUY → strong BUY signal
        assert result.decision == "BUY"
        # River alone should not dominate (contribution < SGD total)
        river_contrib = river_vote.signal * river_vote.weight * river_vote.confidence
        sgd_contrib   = sum(v.signal * v.weight * v.confidence for v in sgd_votes)
        assert abs(river_contrib) < abs(sgd_contrib)

    def test_conflicting_river_and_indicators_reduce_score(self):
        """River SELL against technical BUY votes should reduce final score."""
        from app.algorithms.consensus.engine import compute_consensus, Vote
        tech_votes = [
            Vote(voter="ma_cross",   timeframe="1h", signal=1.0, weight=8.0,  raw_value=1.0),
            Vote(voter="supertrend", timeframe="1h", signal=1.0, weight=10.0, raw_value=1.0),
        ]
        river_sell = self._river_vote(signal=-1, accuracy=0.65, n_seen=300)

        result_without_river = compute_consensus(tech_votes)
        result_with_river    = compute_consensus(tech_votes + [river_sell])

        # Adding a SELL vote must reduce the score
        assert result_with_river.normalized_score < result_without_river.normalized_score
