"""
Consensus Strategy — Multi-Timeframe Weighted Voting BaseStrategy.

Integrates the consensus engine, voters, and multi-TF data pipeline
into the bot runner's standard BaseStrategy interface.

When the bot runner calls generate_signal(df, state, params), this strategy:
1. Fetches data for all configured timeframes (cached, smart refresh)
2. Runs all voter functions on each timeframe
3. Computes volatility modifier from ATR
4. Aggregates votes through the consensus engine
5. Returns BUY/SELL/HOLD with full vote breakdown in state

Parameters (user-configurable):
    timeframes:              ["5m", "15m", "30m", "1h", "4h", "1d"]
    entry_threshold:         60   (score >= this → BUY)
    exit_threshold:          -40  (score <= this → SELL)
    tf_weights:              {"5m": 1.0, "15m": 2.0, ...}
    indicator_weights:       {"ma_cross": 2.0, "rsi": 1.5, ...}
    use_collector:           false  (use collector CSVs)
    use_ml_signals:          false  (include ML model votes)
"""
from __future__ import annotations

import logging
from typing import Literal

import pandas as pd

from app.algorithms.base import BaseStrategy

Signal = Literal["BUY", "SELL", "HOLD"]

logger = logging.getLogger(__name__)

DEFAULT_TIMEFRAMES = ["5m", "15m", "30m", "1h", "4h", "1d"]


class ConsensusStrategy(BaseStrategy):
    display_name = "Consensus (Multi-TF)"
    stop_loss_required = True
    take_profit_available = True

    # ── Internal flag: this strategy manages its own multi-TF data ──
    multi_timeframe = True

    def generate_signal(
        self,
        df: pd.DataFrame,
        state: dict,
        params: dict,
    ) -> tuple[Signal, dict]:
        from app.algorithms.consensus.engine import (
            Vote,
            build_weight_matrix,
            compute_consensus,
            compute_volatility_modifier,
        )
        from app.algorithms.consensus.voters import (
            VOTER_REGISTRY,
            get_atr_pct,
        )
        from app.algorithms.consensus.data import load_collector_signals

        # ── Configuration ──
        # Consensus params may be nested under params["consensus"] (from create form)
        # or at top level (legacy / direct assignment)
        _c = params.get("consensus", {})
        timeframes        = params.get("timeframes") or _c.get("timeframes", DEFAULT_TIMEFRAMES)
        entry_threshold   = float(params.get("entry_threshold")   or _c.get("entry_threshold", 60))
        exit_threshold    = float(params.get("exit_threshold")    or _c.get("exit_threshold", -40))
        use_collector     = params.get("use_collector")     or _c.get("use_collector", False)
        use_ml_signals    = params.get("use_ml_signals")    or _c.get("use_ml_signals", False)
        custom_tf_weights = params.get("tf_weights")        or _c.get("tf_weights")
        custom_indicator_weights = (
            params.get("indicator_weights") or _c.get("indicator_weights")
        )

        indicator_names = list(VOTER_REGISTRY.keys())
        weight_matrix = build_weight_matrix(
            timeframes, indicator_names,
            custom_tf_weights, custom_indicator_weights,
        )

        # ── Multi-TF data ──
        # The bot runner provides `df` for the primary TF.
        # Additional TF data is in state["mtf_data"] (populated by tick.py).
        mtf_data: dict[str, pd.DataFrame] = state.get("mtf_data", {})

        # If mtf_data is empty (first tick or fallback), use the primary df
        # with the bot's configured timeframe
        primary_tf = params.get("timeframe", "1h")
        if not mtf_data:
            mtf_data = {primary_tf: df}

        # ── Collect votes ──
        all_votes: list[Vote] = []
        avg_atr_pct = 0.0
        atr_count = 0

        for tf in timeframes:
            tf_df = mtf_data.get(tf)
            if tf_df is None or tf_df.empty:
                logger.debug("Consensus: no data for %s, skipping", tf)
                continue

            # ATR for volatility modifier (aggregate across TFs)
            tf_atr = get_atr_pct(tf_df)
            if tf_atr > 0:
                avg_atr_pct += tf_atr
                atr_count += 1

            # Run each voter
            for voter_name, voter_fn in VOTER_REGISTRY.items():
                try:
                    signal_val, raw_val = voter_fn(tf_df, **_voter_params(voter_name, params))
                    weight = weight_matrix.get((tf, voter_name), 1.0)
                    vote = Vote(
                        voter=voter_name,
                        timeframe=tf,
                        signal=signal_val,
                        weight=weight,
                        raw_value=raw_val,
                    )
                    all_votes.append(vote)
                except Exception as exc:
                    logger.debug("Voter %s on %s failed: %s", voter_name, tf, exc)

        # ── ML ensemble votes (3 in-process models) ──
        if use_ml_signals:
            try:
                from app.ml.trainer import get_ml_votes, streaming_update

                ml_weight = float(params.get("ml_weight", 3.0))
                # symbol comes from state (injected by tick.py) or params fallback
                symbol = state.get("symbol") or params.get("symbol", "BTCUSDT")

                # Streaming update every tick — models learn continuously
                streaming_update(symbol, primary_tf, mtf_data)

                ml_votes = get_ml_votes(
                    symbol=symbol,
                    mtf_data=mtf_data,
                    primary_tf=primary_tf,
                    ml_weight=ml_weight,
                )
                all_votes.extend(ml_votes)
                state["ml_votes"] = len(ml_votes)
                state["ml_individual"] = [
                    {"voter": v.voter, "signal": int(v.signal)}
                    for v in ml_votes
                ]
            except Exception as exc:
                logger.warning("ML ensemble failed: %s", exc)
                state["ml_votes"] = 0

        # ── Volatility modifier ──
        # Always compute a modifier; fall back to mid-volatility (1.5%) when
        # no valid ATR data was collected (e.g. first tick, empty TFs).
        if atr_count > 0:
            avg_atr_pct /= atr_count
        else:
            avg_atr_pct = 1.5  # default mid-volatility fallback
        vol_modifier = compute_volatility_modifier(avg_atr_pct)

        # ── Consensus ──
        result = compute_consensus(
            votes=all_votes,
            entry_threshold=entry_threshold,
            exit_threshold=exit_threshold,
            volatility_modifier=vol_modifier,
        )

        # ── Update state ──
        state["consensus_score"] = result.normalized_score
        state["consensus_decision"] = result.decision
        state["consensus_votes"] = len(all_votes)
        state["consensus_buy_votes"] = result.buy_votes
        state["consensus_sell_votes"] = result.sell_votes
        state["consensus_vol_modifier"] = round(vol_modifier or 1.0, 3)
        state["consensus_detail"] = result.to_dict()

        # Position management: use BUY/SELL from consensus
        has_position = state.get("has_position", False)

        if not has_position and result.decision == "BUY":
            return "BUY", state
        elif has_position and result.decision == "SELL":
            state["exit_reason"] = "CONSENSUS"
            return "SELL", state
        else:
            return "HOLD", state


def _voter_params(voter_name: str, params: dict) -> dict:
    """Extract voter-specific params from the bot params dict."""
    voter_params = {}

    if voter_name == "ma_cross":
        if "fast_ma" in params:
            voter_params["fast_period"] = int(params["fast_ma"])
        if "slow_ma" in params:
            voter_params["slow_period"] = int(params["slow_ma"])

    elif voter_name == "rsi":
        if "rsi_period" in params:
            voter_params["period"] = int(params["rsi_period"])
        if "oversold" in params:
            voter_params["oversold"] = float(params["oversold"])
        if "overbought" in params:
            voter_params["overbought"] = float(params["overbought"])

    elif voter_name == "macd":
        if "macd_fast" in params:
            voter_params["fast"] = int(params["macd_fast"])
        if "macd_slow" in params:
            voter_params["slow"] = int(params["macd_slow"])
        if "macd_signal" in params:
            voter_params["signal_period"] = int(params["macd_signal"])

    elif voter_name == "supertrend":
        if "st_atr_period" in params:
            voter_params["atr_period"] = int(params["st_atr_period"])
        if "st_multiplier" in params:
            voter_params["multiplier"] = float(params["st_multiplier"])

    elif voter_name == "bb":
        if "bb_length" in params:
            voter_params["length"] = int(params["bb_length"])
        if "bb_std" in params:
            voter_params["num_std"] = float(params["bb_std"])

    elif voter_name == "obv":
        if "obv_ma_length" in params:
            voter_params["ma_length"] = int(params["obv_ma_length"])

    return voter_params
