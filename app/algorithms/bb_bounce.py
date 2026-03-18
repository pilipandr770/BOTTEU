"""
Bollinger Bands Bounce Strategy (Mean Reversion)

Entry:  Previous candle closes BELOW lower band AND current candle closes ABOVE lower band
        (price bounces back up through the lower band) → BUY

Exit:   Price reaches the MIDDLE band (mean reversion complete) → SELL
        OR price touches/exceeds UPPER band → SELL (overbought extension)
        OR Stop-Loss / Take-Profit / Trailing TP

Params:
    bb_period       : int   (default: 20)  — SMA period for the middle band
    bb_std          : float (default: 2.0) — standard deviation multiplier
    bb_exit         : "middle" | "upper"   (default: "middle") — which band triggers exit
    stop_loss_pct   : float (RECOMMENDED — if bounce fails and price keeps dropping)
    take_profit_pct : float | None
    trailing_tp_pct : float | None
"""
import logging

import pandas as pd

from app.algorithms.base import BaseStrategy, Signal

logger = logging.getLogger(__name__)


def _bollinger(close: pd.Series, period: int, num_std: float):
    """Returns (middle, upper, lower) band Series."""
    mid   = close.rolling(window=period).mean()
    std   = close.rolling(window=period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return mid, upper, lower


class BBBounceStrategy(BaseStrategy):
    display_name = "Bollinger Bands Bounce"
    stop_loss_required = True    # mean-reversion can fail badly without a SL
    take_profit_available = True

    def generate_signal(
        self,
        df: pd.DataFrame,
        state: dict,
        params: dict,
    ) -> tuple[Signal, dict]:
        period   = int(params.get("bb_period", 20))
        num_std  = float(params.get("bb_std", 2.0))
        bb_exit  = str(params.get("bb_exit", "middle")).lower()
        sl_pct    = params.get("stop_loss_pct")
        tp_pct    = params.get("take_profit_pct")
        trail_pct = params.get("trailing_tp_pct")

        min_candles = period + 3
        if len(df) < min_candles:
            state["_log"] = [("WARN", f"BB Bounce: not enough candles ({len(df)} < {min_candles})")]
            return "HOLD", state

        current_price = float(df["close"].iloc[-1])
        df_closed = df.iloc[:-1].copy()
        close = df_closed["close"].astype(float)

        state["last_price"] = current_price

        if not isinstance(state, dict):
            state = {}
        has_position = state.get("has_position", False)
        entry_price  = state.get("entry_price") or 0.0
        max_price    = state.get("max_price") or current_price

        mid, upper, lower = _bollinger(close, period, num_std)
        if mid.dropna().empty:
            state["_log"] = [("WARN", "BB Bounce: insufficient data after calculation")]
            return "HOLD", state

        mid_v   = float(mid.iloc[-1])
        upper_v = float(upper.iloc[-1])
        lower_v = float(lower.iloc[-1])
        close_prev = float(close.iloc[-2]) if len(close) >= 2 else current_price
        lower_prev = float(lower.iloc[-2]) if len(lower.dropna()) >= 2 else lower_v

        bbw_pct = (upper_v - lower_v) / mid_v * 100 if mid_v else 0.0

        # Bounce condition: candle [-2] closed below lower, candle [-1] closed above lower
        bounce_entry = close_prev < lower_prev and float(close.iloc[-1]) > lower_v
        # Upper band exit condition
        upper_exit   = float(close.iloc[-1]) >= upper_v

        # ── Exit logic ────────────────────────────────────────────────────
        if has_position:
            if current_price > max_price:
                state["max_price"] = current_price
                max_price = current_price

            if sl_pct and entry_price:
                sl_price = entry_price * (1 - float(sl_pct) / 100)
                if current_price <= sl_price:
                    state.update({"has_position": False, "exit_reason": "STOP_LOSS"})
                    state["_log"] = [("SELL",
                        f"🛑 Stop-loss: {current_price:.6f} ≤ SL {sl_price:.6f} (−{sl_pct}%) — selling")]
                    return "SELL", state

            if tp_pct and entry_price:
                tp_price = entry_price * (1 + float(tp_pct) / 100)
                if current_price >= tp_price:
                    if trail_pct:
                        if not state.get("tp_trailing_active"):
                            state["tp_trailing_active"] = True
                            state["max_price"] = current_price
                            state["_log"] = [("INFO",
                                f"💰 TP {tp_price:.6f} reached — trailing activated at {current_price:.6f}")]
                    else:
                        state.update({"has_position": False, "exit_reason": "TAKE_PROFIT",
                                      "tp_trailing_active": False})
                        state["_log"] = [("SELL",
                            f"💰 Take-profit: {current_price:.6f} ≥ TP {tp_price:.6f} (+{tp_pct}%) — selling")]
                        return "SELL", state

            if trail_pct and max_price and (not tp_pct or state.get("tp_trailing_active")):
                trail_price = max_price * (1 - float(trail_pct) / 100)
                if current_price <= trail_price:
                    state.update({"has_position": False, "exit_reason": "TRAILING_TP",
                                  "tp_trailing_active": False})
                    state["_log"] = [("SELL",
                        f"📉 Trailing stop: retraced from {max_price:.6f} to {current_price:.6f} — selling")]
                    return "SELL", state

            # Signal-based exit: middle band or upper band
            exit_signal = False
            exit_reason_str = ""
            if bb_exit == "upper" and upper_exit:
                exit_signal = True
                exit_reason_str = f"price {current_price:.6f} reached upper BB {upper_v:.6f}"
            elif current_price >= mid_v:
                exit_signal = True
                exit_reason_str = f"price {current_price:.6f} reached middle BB {mid_v:.6f}"

            if exit_signal:
                state.update({"has_position": False, "exit_reason": "SIGNAL"})
                state["_log"] = [("SELL", f"🎯 BB mean reversion: {exit_reason_str} — selling")]
                return "SELL", state

            pnl_pct = (current_price - entry_price) / entry_price * 100 if entry_price else 0.0
            dist_to_mid = (mid_v - current_price) / current_price * 100
            state["_log"] = [("INFO",
                f"📊 BB({period},{num_std}): lower={lower_v:.6f} mid={mid_v:.6f} upper={upper_v:.6f} "
                f"BBW={bbw_pct:.2f}% | Holding entry {entry_price:.6f}, now {current_price:.6f}, "
                f"P&L {pnl_pct:+.2f}%, {dist_to_mid:.2f}% to mid")]

        # ── Entry logic ───────────────────────────────────────────────────
        else:
            if bounce_entry:
                state.update({
                    "has_position": True,
                    "entry_price": current_price,
                    "max_price": current_price,
                    "exit_reason": None,
                    "tp_trailing_active": False,
                })
                state["_log"] = [("BUY",
                    f"🟢 BB bounce: closed below lower ({lower_prev:.6f}) then above ({lower_v:.6f}) "
                    f"— buying at {current_price:.6f}")]
                return "BUY", state

            # Position of current price relative to bands
            if current_price < lower_v:
                zone = f"🔻 below lower band ({lower_v:.6f}) — watching for bounce"
            elif current_price > upper_v:
                zone = f"🔺 above upper band ({upper_v:.6f}) — overbought, no entry"
            else:
                pct_pos = (current_price - lower_v) / (upper_v - lower_v) * 100 if (upper_v != lower_v) else 50
                zone = f"inside bands ({pct_pos:.0f}% from lower)"

            state["_log"] = [("INFO",
                f"📊 BB({period},{num_std}): lower={lower_v:.6f} mid={mid_v:.6f} upper={upper_v:.6f} "
                f"BBW={bbw_pct:.2f}% | Price {zone}")]

        return "HOLD", state
