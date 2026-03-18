"""
MACD Strategy — Moving Average Convergence / Divergence

Entry:  MACD line crosses ABOVE signal line → BUY
Exit:   MACD line crosses BELOW signal line → SELL

Params:
    macd_fast   : int   (default: 12)  — fast EMA period
    macd_slow   : int   (default: 26)  — slow EMA period
    macd_signal : int   (default:  9)  — signal EMA period
    stop_loss_pct   : float | None
    take_profit_pct : float | None
    trailing_tp_pct : float | None
"""
import logging

import pandas as pd

from app.algorithms.base import BaseStrategy, Signal

logger = logging.getLogger(__name__)


def _ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def _macd(close: pd.Series, fast: int, slow: int, signal: int):
    """Returns (macd_line, signal_line, histogram) as Series."""
    ema_fast   = _ema(close, fast)
    ema_slow   = _ema(close, slow)
    macd_line  = ema_fast - ema_slow
    signal_line = _ema(macd_line, signal)
    histogram  = macd_line - signal_line
    return macd_line, signal_line, histogram


class MACDStrategy(BaseStrategy):
    display_name = "MACD Crossover"
    stop_loss_required = False
    take_profit_available = True

    def generate_signal(
        self,
        df: pd.DataFrame,
        state: dict,
        params: dict,
    ) -> tuple[Signal, dict]:
        fast   = int(params.get("macd_fast", 12))
        slow   = int(params.get("macd_slow", 26))
        signal = int(params.get("macd_signal", 9))
        sl_pct    = params.get("stop_loss_pct")
        tp_pct    = params.get("take_profit_pct")
        trail_pct = params.get("trailing_tp_pct")

        min_candles = slow + signal + 3
        if len(df) < min_candles:
            state["_log"] = [("WARN", f"MACD: not enough candles ({len(df)} < {min_candles})")]
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

        macd_line, signal_line, histogram = _macd(close, fast, slow, signal)
        if macd_line.dropna().empty or signal_line.dropna().empty:
            state["_log"] = [("WARN", "MACD: insufficient data after calculation")]
            return "HOLD", state

        mp, mc = float(macd_line.iloc[-2]),  float(macd_line.iloc[-1])
        sp, sc = float(signal_line.iloc[-2]), float(signal_line.iloc[-1])
        hist_c = float(histogram.iloc[-1])

        bullish_cross = mp <= sp and mc > sc   # MACD crosses above signal
        bearish_cross = mp >= sp and mc < sc   # MACD crosses below signal

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

            if bearish_cross:
                state.update({"has_position": False, "exit_reason": "SIGNAL"})
                state["_log"] = [("SELL",
                    f"🔴 MACD bearish cross: MACD={mc:.6f} crossed below signal={sc:.6f} — selling at {current_price:.6f}")]
                return "SELL", state

            pnl_pct = (current_price - entry_price) / entry_price * 100 if entry_price else 0.0
            trend = "above" if mc > sc else "below"
            state["_log"] = [("INFO",
                f"📊 MACD={mc:.6f} {trend} signal={sc:.6f}, hist={hist_c:+.6f} | "
                f"Holding entry {entry_price:.6f}, now {current_price:.6f}, P&L {pnl_pct:+.2f}%")]

        # ── Entry logic ───────────────────────────────────────────────────
        else:
            if bullish_cross:
                state.update({
                    "has_position": True,
                    "entry_price": current_price,
                    "max_price": current_price,
                    "exit_reason": None,
                    "tp_trailing_active": False,
                })
                state["_log"] = [("BUY",
                    f"🟢 MACD bullish cross: MACD={mc:.6f} crossed above signal={sc:.6f} — buying at {current_price:.6f}")]
                return "BUY", state

            trend = "above" if mc > sc else "below"
            state["_log"] = [("INFO",
                f"📊 MACD={mc:.6f} {trend} signal={sc:.6f}, hist={hist_c:+.6f} — waiting for crossover")]

        return "HOLD", state
