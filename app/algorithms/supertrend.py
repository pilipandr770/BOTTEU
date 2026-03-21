"""
SuperTrend Strategy

A trend-following indicator that combines ATR with a dynamic support/resistance band.
Flips bullish when price closes above the upper band → BUY signal.
Flips bearish when price closes below the lower band → SELL signal.
The band itself acts as a trailing adaptive stop-loss.

Params:
    st_period       : int   (default: 10)  — ATR smoothing period
    st_multiplier   : float (default: 3.0) — band width = multiplier × ATR
    stop_loss_pct   : float | None         — hard SL as backup
    take_profit_pct : float | None
    trailing_tp_pct : float | None
"""
import logging

import pandas as pd
import numpy as np

from app.algorithms.base import BaseStrategy, Signal

logger = logging.getLogger(__name__)


def _supertrend(df: pd.DataFrame, period: int, multiplier: float):
    """
    Returns (direction Series, st_line Series).
    direction: +1 = bullish, -1 = bearish.
    Uses numpy arrays internally to avoid pandas CoW O(n²) slowdown.
    """
    high  = df["high"].to_numpy(dtype=float)
    low   = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    n = len(close)

    # True Range (numpy)
    prev_close = np.empty(n)
    prev_close[0] = close[0]
    prev_close[1:] = close[:-1]
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))

    # Wilder ATR (EMA with alpha=1/period)
    alpha = 1.0 / period
    atr = np.empty(n)
    atr[0] = tr[0]
    for i in range(1, n):
        atr[i] = alpha * tr[i] + (1.0 - alpha) * atr[i - 1]

    hl2 = (high + low) / 2.0
    upper_basic = hl2 + multiplier * atr
    lower_basic = hl2 - multiplier * atr

    upper_band = upper_basic.copy()
    lower_band = lower_basic.copy()
    direction  = np.ones(n, dtype=np.int8)
    st_line    = np.zeros(n)

    for i in range(1, n):
        # Upper band: only tighten downward
        upper_band[i] = (
            upper_basic[i]
            if upper_basic[i] < upper_band[i - 1] or close[i - 1] > upper_band[i - 1]
            else upper_band[i - 1]
        )
        # Lower band: only tighten upward
        lower_band[i] = (
            lower_basic[i]
            if lower_basic[i] > lower_band[i - 1] or close[i - 1] < lower_band[i - 1]
            else lower_band[i - 1]
        )
        # Direction
        if direction[i - 1] == -1:
            direction[i] = 1 if close[i] > upper_band[i] else -1
        else:
            direction[i] = -1 if close[i] < lower_band[i] else 1
        st_line[i] = lower_band[i] if direction[i] == 1 else upper_band[i]

    idx = df.index
    return pd.Series(direction.astype(int), index=idx), pd.Series(st_line, index=idx)


class SuperTrendStrategy(BaseStrategy):
    display_name = "SuperTrend"
    stop_loss_required = False   # SuperTrend itself is an adaptive stop
    take_profit_available = True

    # ── Precomputation (backtest optimisation) ────────────────────────────
    def precompute(self, df: pd.DataFrame, params: dict) -> pd.DataFrame:
        """
        Pre-compute SuperTrend on the *entire* dataset once (O(n)).
        The backtest route calls this before the loop so that generate_signal
        can do an O(1) column look-up instead of an O(n) recalculation per
        candle, reducing overall complexity from O(n²) to O(n).
        """
        period     = int(params.get("st_period", 10))
        multiplier = float(params.get("st_multiplier", 3.0))
        try:
            direction, st_line = _supertrend(df, period, multiplier)
            df = df.copy()
            df["__st_direction"] = direction.values
            df["__st_line"]      = st_line.values
        except Exception as exc:  # noqa: BLE001
            logger.warning("SuperTrend precompute failed, will fall back to per-candle: %s", exc)
        return df

    def generate_signal(
        self,
        df: pd.DataFrame,
        state: dict,
        params: dict,
    ) -> tuple[Signal, dict]:
        period     = int(params.get("st_period", 10))
        multiplier = float(params.get("st_multiplier", 3.0))
        sl_pct    = params.get("stop_loss_pct")
        tp_pct    = params.get("take_profit_pct")
        trail_pct = params.get("trailing_tp_pct")

        min_candles = period * 2 + 3
        if len(df) < min_candles:
            state["_log"] = [("WARN", f"SuperTrend: not enough candles ({len(df)} < {min_candles})")]
            return "HOLD", state

        current_price = float(df["close"].iloc[-1])
        df_closed = df.iloc[:-1].copy()

        state["last_price"] = current_price

        if not isinstance(state, dict):
            state = {}
        has_position = state.get("has_position", False)
        entry_price  = state.get("entry_price") or 0.0
        max_price    = state.get("max_price") or current_price

        # Use precomputed columns when available (backtest O(n²) → O(n))
        if "__st_direction" in df_closed.columns:
            direction = df_closed["__st_direction"]
            st_line   = df_closed["__st_line"]
        else:
            try:
                direction, st_line = _supertrend(df_closed, period, multiplier)
            except Exception as exc:
                logger.exception("SuperTrend calculation error: %s", exc)
                state["_log"] = [("WARN", f"SuperTrend: calculation error — {exc}")]
                return "HOLD", state

        if len(direction) == 0 or (hasattr(direction, 'dropna') and direction.dropna().empty):
            state["_log"] = [("WARN", "SuperTrend: insufficient data after calculation")]
            return "HOLD", state

        dir_prev = int(direction.iloc[-2]) if len(direction) >= 2 else 0
        dir_curr = int(direction.iloc[-1])
        st_val   = float(st_line.iloc[-1])

        bullish_flip = dir_prev == -1 and dir_curr == 1   # bearish → bullish
        bearish_flip = dir_prev ==  1 and dir_curr == -1  # bullish → bearish

        trend_label = "🟢 Bullish" if dir_curr == 1 else "🔴 Bearish"

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

            if bearish_flip:
                state.update({"has_position": False, "exit_reason": "SIGNAL"})
                state["_log"] = [("SELL",
                    f"🔴 SuperTrend flipped bearish — ST={st_val:.6f} — selling at {current_price:.6f}")]
                return "SELL", state

            pnl_pct = (current_price - entry_price) / entry_price * 100 if entry_price else 0.0
            state["_log"] = [("INFO",
                f"📊 SuperTrend({period},{multiplier}) {trend_label}, ST={st_val:.6f} | "
                f"Holding entry {entry_price:.6f}, now {current_price:.6f}, P&L {pnl_pct:+.2f}%")]

        # ── Entry logic ───────────────────────────────────────────────────
        else:
            if bullish_flip:
                state.update({
                    "has_position": True,
                    "entry_price": current_price,
                    "max_price": current_price,
                    "exit_reason": None,
                    "tp_trailing_active": False,
                })
                state["_log"] = [("BUY",
                    f"🟢 SuperTrend flipped bullish — ST={st_val:.6f} — buying at {current_price:.6f}")]
                return "BUY", state

            state["_log"] = [("INFO",
                f"📊 SuperTrend({period},{multiplier}) {trend_label}, ST={st_val:.6f} — "
                f"waiting for {'bullish flip' if dir_curr == -1 else 'continuation'}")]

        return "HOLD", state
