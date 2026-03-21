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

import numpy as np
import pandas as pd

from app.algorithms.base import BaseStrategy, Signal

logger = logging.getLogger(__name__)


# ── Shared indicator helpers (identical to standalone algos) ─────────────────

def _sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(window=length).mean()


def _ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


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


def _adx(df: pd.DataFrame, length: int = 14) -> float | None:
    """
    Average Directional Index — measures trend strength (0-100).
    ADX > 25 = trending market (good for MA/MACD/SuperTrend).
    ADX < 20 = ranging/choppy market (good for RSI/BB Bounce).
    """
    if len(df) < length * 2 + 1:
        return None
    high  = df["high"].astype(float)
    low   = df["low"].astype(float)
    close = df["close"].astype(float)

    prev_high  = high.shift(1)
    prev_low   = low.shift(1)
    prev_close = close.shift(1)

    # Directional Movement
    up_move   = high - prev_high
    down_move = prev_low - low
    plus_dm   = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm  = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    # True Range
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    # Wilder smoothing (EWM with alpha=1/length)
    alpha = 1.0 / length
    atr14    = tr.ewm(alpha=alpha, min_periods=length, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(alpha=alpha, min_periods=length, adjust=False).mean() / atr14
    minus_di = 100 * minus_dm.ewm(alpha=alpha, min_periods=length, adjust=False).mean() / atr14

    # DX → ADX
    dx  = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1)
    adx = dx.ewm(alpha=alpha, min_periods=length, adjust=False).mean()

    val = float(adx.iloc[-1])
    return val if not pd.isna(val) else None


def _macd_signals(close: pd.Series, fast: int, slow: int, signal: int):
    """Returns (bullish_cross, bearish_cross, macd_val, signal_val)."""
    ema_fast    = _ema(close, fast)
    ema_slow    = _ema(close, slow)
    macd_line   = ema_fast - ema_slow
    signal_line = _ema(macd_line, signal)
    if len(macd_line.dropna()) < 2:
        return False, False, None, None
    mp, mc = float(macd_line.iloc[-2]), float(macd_line.iloc[-1])
    sp, sc = float(signal_line.iloc[-2]), float(signal_line.iloc[-1])
    return (mp <= sp and mc > sc), (mp >= sp and mc < sc), mc, sc


def _supertrend_direction(df: pd.DataFrame, period: int, multiplier: float):
    """Returns (dir_prev, dir_curr, st_val). dir: +1 bullish, -1 bearish.
    Uses numpy arrays to avoid pandas CoW O(n²) slowdown."""
    high  = df["high"].to_numpy(dtype=float)
    low   = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    n = len(close)
    if n < 2:
        return 0, 0, 0.0
    prev_close = np.empty(n)
    prev_close[0] = close[0]
    prev_close[1:] = close[:-1]
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
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
        upper_band[i] = (
            upper_basic[i]
            if upper_basic[i] < upper_band[i - 1] or close[i - 1] > upper_band[i - 1]
            else upper_band[i - 1]
        )
        lower_band[i] = (
            lower_basic[i]
            if lower_basic[i] > lower_band[i - 1] or close[i - 1] < lower_band[i - 1]
            else lower_band[i - 1]
        )
        if direction[i - 1] == -1:
            direction[i] = 1 if close[i] > upper_band[i] else -1
        else:
            direction[i] = -1 if close[i] < lower_band[i] else 1
        st_line[i] = lower_band[i] if direction[i] == 1 else upper_band[i]
    if n < 2:
        return 0, 0, 0.0
    return int(direction[-2]), int(direction[-1]), float(st_line[-1])


def _bb_signals(close: pd.Series, period: int, num_std: float):
    """Returns (bounce_entry, upper_exit, mid_v, upper_v, lower_v)."""
    mid   = close.rolling(window=period).mean()
    std   = close.rolling(window=period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    if mid.dropna().empty or len(close) < 2:
        return False, False, None, None, None
    mid_v   = float(mid.iloc[-1])
    upper_v = float(upper.iloc[-1])
    lower_v = float(lower.iloc[-1])
    close_prev = float(close.iloc[-2])
    lower_prev = float(lower.iloc[-2]) if len(lower.dropna()) >= 2 else lower_v
    bounce = close_prev < lower_prev and float(close.iloc[-1]) > lower_v
    upper_exit = float(close.iloc[-1]) >= upper_v
    return bounce, upper_exit, mid_v, upper_v, lower_v


# ── Strategy ──────────────────────────────────────────────────────────────────

class CombinedStrategy(BaseStrategy):
    display_name = "Modular Strategy (MA · RSI · MACD · ST · BB)"
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

        # Use live (unclosed) candle price for SL/TP; compute indicators on closed candles only.
        current_price = float(df["close"].iloc[-1])
        df = df.iloc[:-1].copy()

        close = df["close"].astype(float)

        # Always store last seen price for the detail page
        state["last_price"] = current_price

        if not isinstance(state, dict):
            state = {}
        has_position = state.get("has_position", False)
        entry_price  = state.get("entry_price") or 0.0
        max_price    = state.get("max_price") or current_price

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

        # ── MACD module ───────────────────────────────────────────────────
        macd_buy = macd_sell = False
        if "macd" in modules:
            macd_fast   = int(params.get("macd_fast", 12))
            macd_slow   = int(params.get("macd_slow", 26))
            macd_signal = int(params.get("macd_signal", 9))
            min_c = macd_slow + macd_signal + 3
            if len(df) >= min_c:
                macd_buy, macd_sell, mc_val, sc_val = _macd_signals(
                    close, macd_fast, macd_slow, macd_signal)
                buy_signals.append(macd_buy)
                sell_signals.append(macd_sell)
                if mc_val is not None:
                    trend = "above" if mc_val > sc_val else "below"
                    log_lines.append(("INFO",
                        f"MACD({macd_fast},{macd_slow},{macd_signal})={mc_val:.6f} {trend} signal={sc_val:.6f}"
                        + (" — 🟢 bullish cross!" if macd_buy else
                           " — 🔴 bearish cross!" if macd_sell else "")
                    ))
            else:
                buy_signals.append(False)
                sell_signals.append(False)
                log_lines.append(("WARN", f"MACD: not enough candles ({len(df)} < {min_c})"))

        # ── SuperTrend module ─────────────────────────────────────────────
        st_buy = st_sell = False
        if "supertrend" in modules:
            st_period = int(params.get("st_period", 10))
            st_mult   = float(params.get("st_multiplier", 3.0))
            min_c = st_period * 2 + 3
            if len(df) >= min_c:
                try:
                    dir_prev, dir_curr, st_val = _supertrend_direction(df, st_period, st_mult)
                    st_buy  = dir_prev == -1 and dir_curr == 1
                    st_sell = dir_prev ==  1 and dir_curr == -1
                    buy_signals.append(st_buy)
                    sell_signals.append(st_sell)
                    trend = "🟢 Bullish" if dir_curr == 1 else "🔴 Bearish"
                    log_lines.append(("INFO",
                        f"SuperTrend({st_period},{st_mult}) {trend}, ST={st_val:.6f}"
                        + (" — bullish flip!" if st_buy else
                           " — bearish flip!" if st_sell else "")
                    ))
                except Exception as exc:
                    buy_signals.append(False)
                    sell_signals.append(False)
                    log_lines.append(("WARN", f"SuperTrend: error — {exc}"))
            else:
                buy_signals.append(False)
                sell_signals.append(False)
                log_lines.append(("WARN", f"SuperTrend: not enough candles ({len(df)} < {min_c})"))

        # ── Bollinger Bands Bounce module ─────────────────────────────────
        bb_buy = bb_sell = False
        if "bb_bounce" in modules:
            bb_period = int(params.get("bb_period", 20))
            bb_std    = float(params.get("bb_std", 2.0))
            bb_exit   = str(params.get("bb_exit", "middle")).lower()
            if len(df) >= bb_period + 3:
                bb_buy, bb_upper_exit, bb_mid, bb_upper, bb_lower = _bb_signals(close, bb_period, bb_std)
                if bb_mid is not None:
                    # Sell: price reached middle (or upper if configured)
                    if bb_exit == "upper":
                        bb_sell = bb_upper_exit
                    else:
                        bb_sell = has_position and float(close.iloc[-1]) >= bb_mid
                    buy_signals.append(bb_buy)
                    sell_signals.append(bb_sell)
                    bbw = (bb_upper - bb_lower) / bb_mid * 100 if bb_mid else 0
                    log_lines.append(("INFO",
                        f"BB({bb_period},{bb_std}): lower={bb_lower:.6f} mid={bb_mid:.6f} upper={bb_upper:.6f} BBW={bbw:.2f}%"
                        + (" — 🟢 bounce!" if bb_buy else
                           " — 🎯 exit target!" if bb_sell else "")
                    ))
                else:
                    buy_signals.append(False)
                    sell_signals.append(False)
                    log_lines.append(("WARN", "BB Bounce: insufficient data"))
            else:
                buy_signals.append(False)
                sell_signals.append(False)
                log_lines.append(("WARN", f"BB Bounce: not enough candles ({len(df)} < {bb_period + 3})"))

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

        # ── ADX Trend Strength Filter ──────────────────────────────────────
        # When enabled, blocks trend-following entries (MA/MACD/ST) in ranging
        # market and blocks mean-reversion entries (RSI/BB) in trending market.
        adx_blocked = False
        if "adx_filter" in modules and not has_position:
            adx_period = int(params.get("adx_period", 14))
            adx_thresh = float(params.get("adx_threshold", 25))
            adx_val = _adx(df, adx_period)
            if adx_val is None:
                log_lines.append(("WARN", f"⏸ ADX filter: not enough data for ADX({adx_period})"))
                adx_blocked = True
            else:
                # Determine which type of strategies are active
                trend_modules = {"ma_crossover", "macd", "supertrend"} & set(modules)
                reversion_modules = {"rsi", "bb_bounce"} & set(modules)
                is_trending = adx_val >= adx_thresh

                if trend_modules and not reversion_modules:
                    # Only trend strategies → block in ranging market
                    if not is_trending:
                        adx_blocked = True
                        log_lines.append(("INFO",
                            f"⏸ ADX({adx_period})={adx_val:.1f} < {adx_thresh:.0f} — "
                            f"ranging market, trend entries blocked"))
                    else:
                        log_lines.append(("INFO",
                            f"✅ ADX({adx_period})={adx_val:.1f} ≥ {adx_thresh:.0f} — trending, OK"))
                elif reversion_modules and not trend_modules:
                    # Only reversion strategies → block in trending market
                    if is_trending:
                        adx_blocked = True
                        log_lines.append(("INFO",
                            f"⏸ ADX({adx_period})={adx_val:.1f} ≥ {adx_thresh:.0f} — "
                            f"trending market, mean-reversion entries blocked"))
                    else:
                        log_lines.append(("INFO",
                            f"✅ ADX({adx_period})={adx_val:.1f} < {adx_thresh:.0f} — ranging, OK"))
                else:
                    # Mixed → just log, don't block (user chose both)
                    label = "trending" if is_trending else "ranging"
                    log_lines.append(("INFO",
                        f"📊 ADX({adx_period})={adx_val:.1f} — {label} "
                        f"(threshold {adx_thresh:.0f}), mixed modules active"))

        do_buy  = (all(buy_signals) if entry_logic == "AND" else any(buy_signals)) and not vol_blocked and not adx_blocked
        do_sell = any(sell_signals)   # exit logic is always OR — filters never block exits

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

            # Take-Profit — if trailing also configured, TP activates trailing mode
            if tp_pct and entry_price:
                if current_price >= entry_price * (1 + float(tp_pct) / 100):
                    tp_price = entry_price * (1 + float(tp_pct) / 100)
                    if trail_pct:
                        if not state.get("tp_trailing_active"):
                            state["tp_trailing_active"] = True
                            state["max_price"] = current_price
                            max_price = current_price
                            log_lines.append(("INFO", f"💰 TP {tp_price:.6f} reached — trailing stop activated at {current_price:.6f}"))
                    else:
                        state.update(has_position=False, entry_price=0.0, max_price=0.0, exit_reason="TAKE_PROFIT", tp_trailing_active=False)
                        log_lines.append(("SELL", f"💰 Take-profit: price {current_price:.6f} reached TP {tp_price:.6f} (+{tp_pct}%) — selling"))
                        state["_log"] = log_lines
                        return "SELL", state

            # Trailing TP (standalone, or after TP has activated it)
            if trail_pct and max_price and (not tp_pct or state.get("tp_trailing_active")):
                if current_price <= max_price * (1 - float(trail_pct) / 100):
                    state.update(has_position=False, entry_price=0.0, max_price=0.0, exit_reason="TRAILING_TP", tp_trailing_active=False)
                    log_lines.append(("SELL", f"📉 Trailing stop: retraced from peak {max_price:.6f} to {current_price:.6f} — selling"))
                    state["_log"] = log_lines
                    return "SELL", state

            # Signal-based exit
            if do_sell:
                state.update(has_position=False, entry_price=0.0, max_price=0.0, exit_reason="SIGNAL")
                parts = []
                if "ma_crossover" in modules and ma_sell:   parts.append("MA")
                if "rsi" in modules and rsi_sell:           parts.append("RSI")
                if "macd" in modules and macd_sell:         parts.append("MACD")
                if "supertrend" in modules and st_sell:     parts.append("ST")
                if "bb_bounce" in modules and bb_sell:      parts.append("BB")
                logic_str = " + ".join(parts) if parts else "signal"
                log_lines.append(("SELL", f"🔴 Exit signal ({logic_str}) — selling at {current_price:.6f}"))
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
                    tp_trailing_active=False,
                )
                logic_str = ("all modules agree" if entry_logic == "AND"
                             else "any module signals")
                log_lines.append(("BUY", f"🟢 Buy ({logic_str}, logic {entry_logic}) — entry at {current_price:.6f}"))
                state["_log"] = log_lines
                return "BUY", state

            state["_log"] = log_lines  # just indicators, no action

        return "HOLD", state
