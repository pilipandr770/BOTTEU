"""
MA Crossover Strategy — MA(fast) × MA(slow)

Entry:  fast MA crosses ABOVE slow MA → BUY
Exit:   fast MA crosses BELOW slow MA → SELL

Stop-Loss:   Optional (not required — the cross-down IS the exit signal).
Take-Profit: Optional — algorithm exits if TP price is reached OR on cross-down.
Trailing TP: Optional — tracks max price since entry, exits if price drops by trailing_delta%.

Default params:
    timeframe       : "1h"
    fast_ma         : 7
    slow_ma         : 25
    stop_loss_pct   : None   (disabled)
    take_profit_pct : None   (disabled)
    trailing_tp_pct : None   (disabled)
"""
import logging

import pandas as pd

from app.algorithms.base import BaseStrategy, Signal


def _sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(window=length).mean()

logger = logging.getLogger(__name__)


class MACrossoverStrategy(BaseStrategy):
    display_name = "MA Crossover (MA7 × MA25)"
    stop_loss_required = False
    take_profit_available = True

    def generate_signal(
        self,
        df: pd.DataFrame,
        state: dict,
        params: dict,
    ) -> tuple[Signal, dict]:
        fast = int(params.get("fast_ma", 7))
        slow = int(params.get("slow_ma", 25))
        sl_pct = params.get("stop_loss_pct")        # e.g. 2.0 → 2%
        tp_pct = params.get("take_profit_pct")
        trail_pct = params.get("trailing_tp_pct")

        if len(df) < slow + 2:
            logger.debug("MA Crossover: not enough candles (%d < %d)", len(df), slow + 2)
            state["_log"] = [("WARN", f"Not enough candles ({len(df)} < {slow + 2}) — waiting for data")]
            return "HOLD", state

        close = df["close"].astype(float)

        # Calculate moving averages
        ma_fast = _sma(close, fast)
        ma_slow = _sma(close, slow)

        if ma_fast.dropna().empty or ma_slow.dropna().empty:
            state["_log"] = [("WARN", "Failed to calculate MA — insufficient data")]
            return "HOLD", state

        fast_prev, fast_curr = float(ma_fast.iloc[-2]), float(ma_fast.iloc[-1])
        slow_prev, slow_curr = float(ma_slow.iloc[-2]), float(ma_slow.iloc[-1])
        current_price = float(close.iloc[-1])

        # Always store last seen price for the detail page
        state["last_price"] = current_price

        has_position = state.get("has_position", False)
        entry_price = state.get("entry_price", 0.0)
        max_price = state.get("max_price", current_price)

        # ── Exit logic (checked first) ────────────────────────────────────
        if has_position:
            # Track max price for trailing TP
            if current_price > max_price:
                state["max_price"] = current_price
                max_price = current_price

            # Stop-Loss
            if sl_pct and entry_price:
                sl_price = entry_price * (1 - float(sl_pct) / 100)
                if current_price <= sl_price:
                    state.update({"has_position": False, "exit_reason": "STOP_LOSS"})
                    state["_log"] = [("SELL", f"🛑 Stop-loss: price {current_price:.6f} fell below SL {sl_price:.6f} (−{sl_pct}%) — selling")]
                    return "SELL", state

            # Take-Profit
            if tp_pct and entry_price:
                tp_price = entry_price * (1 + float(tp_pct) / 100)
                if current_price >= tp_price:
                    state.update({"has_position": False, "exit_reason": "TAKE_PROFIT"})
                    state["_log"] = [("SELL", f"💰 Take-profit: price {current_price:.6f} reached TP {tp_price:.6f} (+{tp_pct}%) — selling")]
                    return "SELL", state

            # Trailing Take-Profit
            if trail_pct and max_price:
                trail_price = max_price * (1 - float(trail_pct) / 100)
                if current_price <= trail_price:
                    state.update({"has_position": False, "exit_reason": "TRAILING_TP"})
                    state["_log"] = [("SELL", f"📉 Trailing stop: retraced from peak {max_price:.6f} to {current_price:.6f} — selling")]
                    return "SELL", state

            # MA cross-down → exit signal
            cross_down = (fast_prev >= slow_prev) and (fast_curr < slow_curr)
            if cross_down:
                state.update({"has_position": False, "exit_reason": "SIGNAL"})
                state["_log"] = [("SELL", f"🔴 Death cross: MA{fast}={fast_curr:.6f} dropped below MA{slow}={slow_curr:.6f} — selling")]
                return "SELL", state

            # Still holding
            pnl_pct = (current_price - entry_price) / entry_price * 100 if entry_price else 0.0
            trend = "above" if fast_curr > slow_curr else "below"
            state["_log"] = [("INFO", (
                f"📊 MA{fast}={fast_curr:.6f} {trend} MA{slow}={slow_curr:.6f} — "
                f"{'uptrend' if fast_curr > slow_curr else 'downtrend'}, "
                f"holding position (entry {entry_price:.6f}, now {current_price:.6f}, P&L {pnl_pct:+.2f}%)"
            ))]

        # ── Entry logic ───────────────────────────────────────────────────
        else:
            cross_up = (fast_prev <= slow_prev) and (fast_curr > slow_curr)
            if cross_up:
                state.update({
                    "has_position": True,
                    "entry_price": current_price,
                    "max_price": current_price,
                    "exit_reason": None,
                })
                state["_log"] = [("BUY", f"🟢 Golden cross: MA{fast}={fast_curr:.6f} crossed above MA{slow}={slow_curr:.6f} — buying at {current_price:.6f}")]
                return "BUY", state

            if fast_curr < slow_curr:
                state["_log"] = [("INFO", f"📊 MA{fast}={fast_curr:.6f} < MA{slow}={slow_curr:.6f} — downtrend, holding stable, waiting for reversal")]
            else:
                state["_log"] = [("INFO", f"📊 MA{fast}={fast_curr:.6f} > MA{slow}={slow_curr:.6f} — uptrend, no crossover, waiting for signal")]

        return "HOLD", state
