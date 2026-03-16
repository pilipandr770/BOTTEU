"""
Combined / Modular Strategy
────────────────────────────
Lets you assemble a bot from independent signal modules (MA Crossover, RSI)
and combine their entry signals with AND or OR logic.

Params:
    modules         : list  e.g. ["ma_crossover", "rsi"]
    entry_logic     : "AND" | "OR"   (default: "OR")
    timeframe       : str            (used by the worker to fetch candles)

    # MA Crossover module (active when "ma_crossover" in modules):
    fast_ma         : int            (default: 7)
    slow_ma         : int            (default: 25)

    # RSI module (active when "rsi" in modules):
    rsi_period      : int            (default: 14)
    oversold        : float          (default: 30)
    overbought      : float          (default: 70)

    # Shared risk management:
    stop_loss_pct   : float | None
    take_profit_pct : float | None
    trailing_tp_pct : float | None

Entry  : BUY  when enabled modules agree (AND) or any fires (OR)
Exit   : SELL when any module signals exit  (always OR — safer)
         OR SL / TP / Trailing TP is hit
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

        # ── Combine entry signals ─────────────────────────────────────────
        do_buy  = all(buy_signals)  if entry_logic == "AND" else any(buy_signals)
        do_sell = any(sell_signals)   # exit logic is always OR

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
