"""
Combined / Modular Strategy
────────────────────────────
Lets you assemble a bot from independent signal modules (MA Crossover, RSI,
Volatility Filter) and combine their entry signals with AND or OR logic.

Params:
    modules         : list  e.g. ["ma_crossover", "rsi", "volatility"]
    entry_logic     : "AND" | "OR"   (default: "OR")
    timeframe       : str            (used by the worker to fetch candles)

    # MA Crossover module (active when "ma_crossover" in modules):
    fast_ma         : int            (default: 7)
    slow_ma         : int            (default: 25)

    # RSI module (active when "rsi" in modules):
    rsi_period      : int            (default: 14)
    oversold        : float          (default: 30)
    overbought      : float          (default: 70)

    # Volatility Filter module (active when "volatility" in modules):
    vol_indicator   : "atr" | "bbw"  (default: "atr")
    vol_period      : int            (default: 14)
    vol_min_pct     : float          (default: 0.5)  — minimum ATR% or BBW%
                                     Below this value → no new entries allowed.

    # Shared risk management:
    stop_loss_pct   : float | None
    take_profit_pct : float | None
    trailing_tp_pct : float | None

Entry  : BUY  when enabled modules agree (AND) or any fires (OR)
         AND volatility filter passes (if enabled)
Exit   : SELL when any module signals exit  (always OR — safer)
         OR SL / TP / Trailing TP is hit
         Note: volatility filter does NOT block exits — only entries.
"""
import logging

import pandas as pd

from app.algorithms.base import BaseStrategy, Signal

logger = logging.getLogger(__name__)


# ── Shared indicator helpers (identical to standalone algos) ─────────────────

def _sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(window=length).mean()


def _rsi(series: pd.Series, length: int) -> pd.Series:
    """Wilder RSI via EWM."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _atr_pct(df: pd.DataFrame, length: int) -> float | None:
    """
    ATR% = ATR(length) / close * 100

    Measures the average candle range relative to price.
    Low value  → market is quiet / sideways → filter blocks entry.
    High value → market is moving  → entry allowed.
    """
    if len(df) < length + 1:
        return None
    high  = df["high"].astype(float)
    low   = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    true_range = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = true_range.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    atr_val = float(atr.iloc[-1])
    price   = float(close.iloc[-1])
    return (atr_val / price * 100) if price else None


def _bbw_pct(series: pd.Series, length: int, num_std: float = 2.0) -> float | None:
    """
    Bollinger Band Width % = (Upper − Lower) / Middle * 100

    Very low value → "BB squeeze" / low volatility → filter blocks entry.
    Expanding bands → volatility returning → entry allowed.
    """
    if len(series) < length:
        return None
    mid = series.rolling(window=length).mean()
    std = series.rolling(window=length).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    mid_v   = float(mid.iloc[-1])
    upper_v = float(upper.iloc[-1])
    lower_v = float(lower.iloc[-1])
    if mid_v == 0 or pd.isna(mid_v):
        return None
    return (upper_v - lower_v) / mid_v * 100


# ── Strategy ──────────────────────────────────────────────────────────────────

class CombinedStrategy(BaseStrategy):
    display_name = "Modular (MA + RSI)"
    stop_loss_required = False   # enforced in route based on active modules
    take_profit_available = True

    def generate_signal(
        self,
        df: pd.DataFrame,
        state: dict,
        params: dict,
    ) -> tuple[Signal, dict]:

        # ── Resolve modules ───────────────────────────────────────────────
        modules = params.get("modules", ["ma_crossover"])
        if isinstance(modules, str):
            # Stored as comma-separated string in older records
            modules = [m.strip() for m in modules.split(",") if m.strip()]

        entry_logic = str(params.get("entry_logic", "OR")).upper()

        sl_pct    = params.get("stop_loss_pct")
        tp_pct    = params.get("take_profit_pct")
        trail_pct = params.get("trailing_tp_pct")

        close = df["close"].astype(float)
        current_price = float(close.iloc[-1])

        # Always store last seen price for the detail page
        state["last_price"] = current_price

        has_position = state.get("has_position", False)
        entry_price  = state.get("entry_price", 0.0)
        max_price    = state.get("max_price", current_price)

        buy_signals  = []   # True/False per module
        sell_signals = []
        log_lines    = []   # (level, message) pairs collected this tick

        # ── MA Crossover module ───────────────────────────────────────────
        ma_buy = ma_sell = False
        if "ma_crossover" in modules:
            fast = int(params.get("fast_ma", 7))
            slow = int(params.get("slow_ma", 25))
            if len(df) >= slow + 2:
                ma_fast = _sma(close, fast)
                ma_slow = _sma(close, slow)
                if not ma_fast.dropna().empty and not ma_slow.dropna().empty:
                    fp, fc = float(ma_fast.iloc[-2]), float(ma_fast.iloc[-1])
                    sp, sc = float(ma_slow.iloc[-2]), float(ma_slow.iloc[-1])
                    ma_buy  = fp <= sp and fc > sc   # golden cross
                    ma_sell = fp >= sp and fc < sc   # death cross
                    buy_signals.append(ma_buy)
                    sell_signals.append(ma_sell)
                    trend = "above" if fc > sc else "below"
                    log_lines.append(("INFO",
                        f"MA{fast}={fc:.6f} {trend} MA{slow}={sc:.6f}"
                        + (" — 🟢 golden cross!" if ma_buy else
                           " — 🔴 death cross!" if ma_sell else
                           f" — {'uptrend' if fc > sc else 'downtrend'}")
                    ))
            else:
                buy_signals.append(False)
                sell_signals.append(False)
                log_lines.append(("WARN", f"MA: not enough candles ({len(df)} < {slow + 2})"))

        # ── RSI module ────────────────────────────────────────────────────
        rsi_buy = rsi_sell = False
        if "rsi" in modules:
            period    = int(params.get("rsi_period", 14))
            oversold  = float(params.get("oversold", 30))
            overbought = float(params.get("overbought", 70))
            if len(df) >= period + 2:
                rsi_s = _rsi(close, period).dropna()
                if len(rsi_s) >= 2:
                    rp, rc = float(rsi_s.iloc[-2]), float(rsi_s.iloc[-1])
                    # Buy: RSI crosses UP through oversold level
                    rsi_buy  = rp <= oversold and rc > oversold
                    # Sell: RSI crosses UP through overbought level
                    rsi_sell = rp <= overbought and rc > overbought
                    buy_signals.append(rsi_buy)
                    sell_signals.append(rsi_sell)
                    if rc <= oversold:
                        zone = f"oversold (≤{oversold:.0f})"
                    elif rc >= overbought:
                        zone = f"overbought (≥{overbought:.0f})"
                    else:
                        zone = "neutral zone"
                    log_lines.append(("INFO",
                        f"RSI({period})={rc:.1f} — {zone}"
                        + (" — 🟢 exiting oversold!" if rsi_buy else
                           " — 🔴 entering overbought!" if rsi_sell else "")
                    ))
                else:
                    buy_signals.append(False)
                    sell_signals.append(False)
                    log_lines.append(("WARN", f"RSI: insufficient data after dropna"))
            else:
                buy_signals.append(False)
                sell_signals.append(False)
                log_lines.append(("WARN", f"RSI: not enough candles ({len(df)} < {period + 2})"))

        if not buy_signals:
            state["_log"] = [("WARN", "No active modules — check bot settings")]
            return "HOLD", state

        # ── Volatility Filter module ───────────────────────────────────────
        # Checked AFTER signal modules so we can still log their values.
        # Blocks new ENTRIES when market is too quiet; never blocks EXITS.
        vol_blocked = False
        if "volatility" in modules and not has_position:
            vol_indicator = str(params.get("vol_indicator", "atr")).lower()
            vol_period    = int(params.get("vol_period", 14))
            vol_min_pct   = float(params.get("vol_min_pct", 0.5))

            if vol_indicator == "bbw":
                vol_value = _bbw_pct(close, vol_period)
                label = f"BBW({vol_period})"
            else:
                vol_value = _atr_pct(df, vol_period)
                label = f"ATR%({vol_period})"

            if vol_value is None:
                log_lines.append(("WARN", f"⏸ Volatility filter: not enough data for {label}"))
                vol_blocked = True
            elif vol_value < vol_min_pct:
                log_lines.append(("INFO",
                    f"⏸ Volatility filter: {label}={vol_value:.3f}% < min {vol_min_pct:.2f}% "
                    f"— market too quiet, entry blocked"
                ))
                vol_blocked = True
            else:
                log_lines.append(("INFO",
                    f"✅ Volatility filter: {label}={vol_value:.3f}% ≥ min {vol_min_pct:.2f}% — OK"
                ))
        # vol_blocked is False when volatility module is disabled or already in position (exits not blocked)
        do_buy  = (all(buy_signals) if entry_logic == "AND" else any(buy_signals)) and not vol_blocked
        do_sell = any(sell_signals)   # exit logic is always OR — vol filter never blocks exits

        # ── Exit checks (evaluated before entry) ──────────────────────────
        if has_position:
            # Track max price for trailing TP
            if current_price > max_price:
                state["max_price"] = current_price
                max_price = current_price

            # Stop-Loss
            if sl_pct and entry_price:
                if current_price <= entry_price * (1 - float(sl_pct) / 100):
                    sl_price = entry_price * (1 - float(sl_pct) / 100)
                    state.update(has_position=False, entry_price=0.0, max_price=0.0, exit_reason="STOP_LOSS")
                    log_lines.append(("SELL", f"🛑 Stop-loss: price {current_price:.6f} fell below SL {sl_price:.6f} (−{sl_pct}%) — selling"))
                    state["_log"] = log_lines
                    return "SELL", state

            # Take-Profit
            if tp_pct and entry_price:
                if current_price >= entry_price * (1 + float(tp_pct) / 100):
                    tp_price = entry_price * (1 + float(tp_pct) / 100)
                    state.update(has_position=False, entry_price=0.0, max_price=0.0, exit_reason="TAKE_PROFIT")
                    log_lines.append(("SELL", f"💰 Take-profit: price {current_price:.6f} reached TP {tp_price:.6f} (+{tp_pct}%) — selling"))
                    state["_log"] = log_lines
                    return "SELL", state

            # Trailing TP
            if trail_pct and max_price:
                if current_price <= max_price * (1 - float(trail_pct) / 100):
                    state.update(has_position=False, entry_price=0.0, max_price=0.0, exit_reason="TRAILING_TP")
                    log_lines.append(("SELL", f"📉 Trailing stop: retraced from peak {max_price:.6f} to {current_price:.6f} — selling"))
                    state["_log"] = log_lines
                    return "SELL", state

            # Signal-based exit
            if do_sell:
                state.update(has_position=False, entry_price=0.0, max_price=0.0, exit_reason="SIGNAL")
                logic_str = " + ".join(["MA" if "ma_crossover" in modules and ma_sell else "",
                                        "RSI" if "rsi" in modules and rsi_sell else ""])
                log_lines.append(("SELL", f"🔴 Exit signal ({logic_str.strip(' +')}) — selling at {current_price:.6f}"))
                state["_log"] = log_lines
                return "SELL", state

            # Still holding
            pnl_pct = (current_price - entry_price) / entry_price * 100 if entry_price else 0.0
            log_lines.append(("INFO", f"Position open: entry {entry_price:.6f}, now {current_price:.6f}, P&L {pnl_pct:+.2f}%"))
            state["_log"] = log_lines

        # ── Entry ─────────────────────────────────────────────────────────
        else:
            if do_buy:
                state.update(
                    has_position=True,
                    entry_price=current_price,
                    max_price=current_price,
                    exit_reason=None,
                )
                logic_str = ("all modules agree" if entry_logic == "AND"
                             else "any module signals")
                log_lines.append(("BUY", f"🟢 Buy ({logic_str}, logic {entry_logic}) — entry at {current_price:.6f}"))
                state["_log"] = log_lines
                return "BUY", state

            state["_log"] = log_lines  # just indicators, no action

        return "HOLD", state
