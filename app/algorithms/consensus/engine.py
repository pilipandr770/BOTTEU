"""
Consensus Scoring Engine — the mathematical core.

Takes a list of Vote objects (signal, weight) from all voters across all
timeframes and produces a single normalized score in [-100, +100].

The score is compared against user-defined thresholds to produce BUY/SELL/HOLD.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Vote:
    """A single vote from one indicator on one timeframe."""
    voter: str          # e.g. "ma_cross", "rsi", "macd"
    timeframe: str      # e.g. "5m", "1h", "4h"
    signal: float       # [-1.0 … +1.0]  direction + strength
    weight: float       # absolute importance (tf_weight × indicator_weight)
    raw_value: float = 0.0   # raw indicator value for logging
    confidence: float = 1.0  # optional multiplier (e.g. ATR-based)

    @property
    def contribution(self) -> float:
        """Weighted contribution: signal × weight × confidence."""
        return self.signal * self.weight * self.confidence


@dataclass
class ConsensusResult:
    """Output of the consensus engine."""
    raw_score: float            # sum of all contributions
    total_weight: float         # sum of |weight × confidence| for normalization
    normalized_score: float     # [-100 … +100]
    decision: str               # "BUY", "SELL", "HOLD"
    votes: List[Vote] = field(default_factory=list)
    buy_votes: int = 0
    sell_votes: int = 0
    neutral_votes: int = 0

    def to_dict(self) -> dict:
        return {
            "raw_score": round(self.raw_score, 4),
            "total_weight": round(self.total_weight, 4),
            "normalized_score": round(self.normalized_score, 2),
            "decision": self.decision,
            "buy_votes": self.buy_votes,
            "sell_votes": self.sell_votes,
            "neutral_votes": self.neutral_votes,
            "votes": [
                {
                    "voter": v.voter,
                    "tf": v.timeframe,
                    "signal": round(v.signal, 4),
                    "weight": round(v.weight, 2),
                    "raw": round(v.raw_value, 4),
                    "contrib": round(v.contribution, 4),
                }
                for v in self.votes
            ],
        }


# ── Default weight matrices ────────────────────────────────────────────────

# Timeframe weights: longer TF = stronger signal
DEFAULT_TF_WEIGHTS: dict[str, float] = {
    "1m":  0.5,
    "5m":  1.0,
    "15m": 2.0,
    "30m": 3.0,
    "1h":  4.0,
    "4h":  5.0,
    "1d":  6.0,
}

# Indicator weights: directional indicators weighted higher
DEFAULT_INDICATOR_WEIGHTS: dict[str, float] = {
    "ma_cross":   2.0,   # MA crossover — strong trend signal
    "rsi":        1.5,   # RSI — mean-reversion signal
    "macd":       2.0,   # MACD — momentum signal
    "supertrend": 2.5,   # SuperTrend — adaptive trend
    "bb":         1.5,   # Bollinger Bands — volatility/reversion
    "obv":        1.0,   # On-Balance Volume — volume confirmation
}

# Volatility modifier: ATR doesn't vote directly but scales other votes
# High ATR → trending market → scale up directional signals
# Low ATR → flat market → scale down
ATR_NEUTRAL_PCT = 1.5   # ATR% considered "normal"


def compute_consensus(
    votes: List[Vote],
    entry_threshold: float = 60.0,
    exit_threshold: float = -40.0,
    volatility_modifier: Optional[float] = None,
) -> ConsensusResult:
    """
    Aggregate all votes into a single consensus decision.

    Parameters
    ----------
    votes : list[Vote]
        All individual indicator votes across all timeframes.
    entry_threshold : float
        Normalized score >= this → BUY.  User-configurable.
    exit_threshold : float
        Normalized score <= this → SELL. User-configurable.
    volatility_modifier : float | None
        If provided, scales all contributions.
        > 1.0 means high volatility (amplify signals)
        < 1.0 means low volatility (dampen signals)

    Returns
    -------
    ConsensusResult with decision, score, and vote breakdown.
    """
    if not votes:
        return ConsensusResult(
            raw_score=0.0,
            total_weight=0.0,
            normalized_score=0.0,
            decision="HOLD",
            votes=[],
        )

    vol_scale = volatility_modifier if volatility_modifier is not None else 1.0

    raw_score = 0.0
    total_weight = 0.0
    buy_count = 0
    sell_count = 0
    neutral_count = 0

    for vote in votes:
        contrib = vote.contribution * vol_scale
        raw_score += contrib
        total_weight += abs(vote.weight * vote.confidence)

        if vote.signal > 0.1:
            buy_count += 1
        elif vote.signal < -0.1:
            sell_count += 1
        else:
            neutral_count += 1

    # Normalize to [-100, +100]
    if total_weight > 0:
        normalized = (raw_score / total_weight) * 100.0
    else:
        normalized = 0.0

    # Clamp
    normalized = max(-100.0, min(100.0, normalized))

    # Decision
    if normalized >= entry_threshold:
        decision = "BUY"
    elif normalized <= exit_threshold:
        decision = "SELL"
    else:
        decision = "HOLD"

    result = ConsensusResult(
        raw_score=raw_score,
        total_weight=total_weight,
        normalized_score=normalized,
        decision=decision,
        votes=votes,
        buy_votes=buy_count,
        sell_votes=sell_count,
        neutral_votes=neutral_count,
    )

    logger.debug(
        "Consensus: score=%.2f (raw=%.4f / weight=%.4f) → %s  "
        "[BUY:%d SELL:%d HOLD:%d]",
        normalized, raw_score, total_weight, decision,
        buy_count, sell_count, neutral_count,
    )

    return result


def build_weight_matrix(
    timeframes: list[str],
    indicators: list[str],
    custom_tf_weights: dict[str, float] | None = None,
    custom_indicator_weights: dict[str, float] | None = None,
) -> dict[tuple[str, str], float]:
    """
    Build a (timeframe, indicator) → combined_weight lookup.

    Combined weight = tf_weight × indicator_weight.
    Users can override defaults through custom dicts.
    """
    tf_w = {**DEFAULT_TF_WEIGHTS, **(custom_tf_weights or {})}
    ind_w = {**DEFAULT_INDICATOR_WEIGHTS, **(custom_indicator_weights or {})}

    matrix = {}
    for tf in timeframes:
        for ind in indicators:
            matrix[(tf, ind)] = tf_w.get(tf, 1.0) * ind_w.get(ind, 1.0)

    return matrix


def compute_volatility_modifier(atr_pct: float) -> float:
    """
    Convert ATR% (relative to price) into a volatility scaling factor.

    - ATR ~1.5% → modifier = 1.0 (neutral)
    - ATR > 3%  → modifier = ~1.4 (amplify signals — strong trend)
    - ATR < 0.5% → modifier = ~0.7 (dampen — flat market, noise)
    """
    if atr_pct <= 0:
        return 0.7
    ratio = atr_pct / ATR_NEUTRAL_PCT
    # Logarithmic scaling: smooth, bounded
    import math
    modifier = 0.7 + 0.3 * math.log1p(ratio)
    return max(0.5, min(1.5, modifier))
