"""
RSI Strategy

Entry:  RSI drops below `oversold` → BUY (oversold bounce)
Exit:   RSI rises above `overbought` → SELL signal
        OR Stop-Loss  (mandatory — RSI has no inherent exit price)
        OR Take-Profit (optional)
        OR Trailing TP (optional)

Default params:
    timeframe       : "1h"
    rsi_period      : 14
    oversold        : 30    (or 20 for conservative mode)
    overbought      : 70    (or 80 for conservative mode)
    stop_loss_pct   : 3.0   (REQUIRED — validate in form)
    take_profit_pct : None
    trailing_tp_pct : None
"""
import logging

import pandas as pd

from app.algorithms.base import BaseStrategy, Signal


def _rsi(series: pd.Series, length: int) -> pd.Series:
    """Wilder RSI using EWM (matches standard RSI formula)."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    # Natural float division: x/0 = inf → 100/(1+inf)=0 → RSI=100 ✓
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

logger = logging.getLogger(__name__)


class RSIStrategy(BaseStrategy):
    display_name = "RSI (Oversold / Overbought)"
    stop_loss_required = True    # SL is mandatory for RSI — no cross-based exit
    take_profit_available = True

    def generate_signal(
        self,
        df: pd.DataFrame,
        state: dict,
        params: dict,
    ) -> tuple[Signal, dict]:
        period = int(params.get("rsi_period", 14))
        oversold = float(params.get("oversold", 30))
        overbought = float(params.get("overbought", 70))
        sl_pct = params.get("stop_loss_pct")         # Required
        tp_pct = params.get("take_profit_pct")
        trail_pct = params.get("trailing_tp_pct")

        if len(df) < period + 2:
            logger.debug("RSI: not enough candles (%d < %d)", len(df), period + 2)
            state["_log"] = [("WARN", f"Not enough candles ({len(df)} < {period + 2}) — waiting for data")]
            return "HOLD", state

        close = df["close"].astype(float)
        rsi_series = _rsi(close, period)
        if rsi_series.dropna().empty:
            state["_log"] = [("WARN", "Failed to calculate RSI — insufficient data")]
            return "HOLD", state

        rsi_prev = float(rsi_series.iloc[-2]) if len(rsi_series.dropna()) >= 2 else None
        rsi_now = float(rsi_series.iloc[-1])
        current_price = float(close.iloc[-1])

        # Always store last seen price for the detail page
        state["last_price"] = current_price

        has_position = state.get("has_position", False)
        entry_price = state.get("entry_price", 0.0)
        max_price = state.get("max_price", current_price)

        # ── Exit logic ────────────────────────────────────────────────────
        if has_position:
            if current_price > max_price:
                state["max_price"] = current_price
                max_price = current_price

            # Stop-Loss (mandatory)
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

            # RSI overbought → exit signal
            if rsi_now >= overbought:
                state.update({"has_position": False, "exit_reason": "SIGNAL"})
                state["_log"] = [("SELL", f"🔴 RSI({period})={rsi_now:.1f} overbought (≥{overbought:.0f}) — selling at {current_price:.6f}")]
                return "SELL", state

            # Still holding
            pnl_pct = (current_price - entry_price) / entry_price * 100 if entry_price else 0.0
            if rsi_now >= overbought * 0.85:
                zone = f"approaching overbought zone ({overbought:.0f})"
            elif rsi_now <= oversold * 1.15:
                zone = f"near oversold zone ({oversold:.0f})"
            else:
                zone = "neutral zone"
            state["_log"] = [("INFO", (
                f"📊 RSI({period})={rsi_now:.1f} — {zone}. "
                f"Holding position (entry {entry_price:.6f}, now {current_price:.6f}, P&L {pnl_pct:+.2f}%)"
            ))]

        # ── Entry logic ───────────────────────────────────────────────────
        else:
            if rsi_now <= oversold:
                state.update({
                    "has_position": True,
                    "entry_price": current_price,
                    "max_price": current_price,
                    "exit_reason": None,
                })
                prev_str = f"{rsi_prev:.1f} → " if rsi_prev is not None else ""
                state["_log"] = [("BUY", f"🟢 RSI({period})={prev_str}{rsi_now:.1f} — oversold (≤{oversold:.0f}) — buying at {current_price:.6f}")]
                return "BUY", state

            if rsi_now >= overbought:
                state["_log"] = [("INFO", f"📊 RSI({period})={rsi_now:.1f} — overbought (≥{overbought:.0f}), waiting for correction")]
            elif rsi_now <= oversold * 1.3:
                state["_log"] = [("INFO", f"📊 RSI({period})={rsi_now:.1f} — approaching oversold zone ({oversold:.0f}), watching")]
            else:
                state["_log"] = [("INFO", f"📊 RSI({period})={rsi_now:.1f} — neutral zone, waiting for oversold < {oversold:.0f}")]

        return "HOLD", state
