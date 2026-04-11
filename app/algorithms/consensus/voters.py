"""
Consensus Voters — each function scores one indicator on one timeframe.

Every voter returns a float in [-1.0 … +1.0]:
    +1.0  = strong BUY signal
     0.0  = neutral / no signal
    -1.0  = strong SELL signal

Voters also return the raw indicator value for logging.

The signal is *graduated* — not binary.  A RSI of 25 is a stronger BUY
than RSI of 35.  An MA cross with a 2% gap is stronger than 0.1%.
"""
from __future__ import annotations

import logging
import numpy as np
import pandas as pd
from typing import Tuple

logger = logging.getLogger(__name__)


# ── Helper: safe series access ──────────────────────────────────────────────

def _last(series: pd.Series, offset: int = 0) -> float:
    """Return value at position -(1+offset), or NaN if unavailable."""
    idx = -(1 + offset)
    if len(series) < abs(idx):
        return float("nan")
    return float(series.iloc[idx])


# ── Indicator computations ──────────────────────────────────────────────────

def _sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length, min_periods=length).mean()


def _ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def _rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    roll_up = up.ewm(alpha=1 / length, min_periods=length).mean()
    roll_down = down.ewm(alpha=1 / length, min_periods=length).mean()
    rs = roll_up / (roll_down + 1e-10)
    return 100 - (100 / (1 + rs))


def _macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = _ema(series, fast)
    ema_slow = _ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = _ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.rolling(length, min_periods=length).mean()


def _bollinger(series: pd.Series, length: int = 20, num_std: float = 2.0):
    ma = _sma(series, length)
    std = series.rolling(length, min_periods=length).std()
    upper = ma + num_std * std
    lower = ma - num_std * std
    bbw_pct = ((upper - lower) / (ma + 1e-10)) * 100
    bb_z = (series - ma) / (std + 1e-10)
    return ma, upper, lower, bbw_pct, bb_z


def _supertrend(df: pd.DataFrame, atr_period: int = 10, multiplier: float = 3.0):
    """Compute SuperTrend direction: +1 = bullish, -1 = bearish."""
    atr = _atr(df, atr_period)
    hl2 = (df["high"] + df["low"]) / 2
    upper_band = hl2 + multiplier * atr
    lower_band = hl2 - multiplier * atr

    direction = pd.Series(1, index=df.index, dtype=int)
    final_upper = upper_band.copy()
    final_lower = lower_band.copy()

    for i in range(1, len(df)):
        if not np.isnan(final_lower.iat[i - 1]):
            if lower_band.iat[i] > final_lower.iat[i - 1]:
                final_lower.iat[i] = lower_band.iat[i]
            else:
                final_lower.iat[i] = final_lower.iat[i - 1]
        if not np.isnan(final_upper.iat[i - 1]):
            if upper_band.iat[i] < final_upper.iat[i - 1]:
                final_upper.iat[i] = upper_band.iat[i]
            else:
                final_upper.iat[i] = final_upper.iat[i - 1]

        if direction.iat[i - 1] == 1:
            if df["close"].iat[i] < final_lower.iat[i]:
                direction.iat[i] = -1
            else:
                direction.iat[i] = 1
        else:
            if df["close"].iat[i] > final_upper.iat[i]:
                direction.iat[i] = 1
            else:
                direction.iat[i] = -1

    return direction


def _obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume."""
    sign = np.sign(df["close"].diff())
    return (sign * df["volume"]).cumsum()


# ── Voter functions ─────────────────────────────────────────────────────────
# Each returns (signal: float, raw_value: float)

def vote_ma_cross(
    df: pd.DataFrame,
    fast_period: int = 7,
    slow_period: int = 25,
) -> Tuple[float, float]:
    """
    MA Crossover voter.
    Signal strength = % gap between fast & slow MAs.
    """
    if len(df) < slow_period + 3:
        return 0.0, 0.0

    fast = _sma(df["close"], fast_period)
    slow = _sma(df["close"], slow_period)

    fast_val = _last(fast)
    slow_val = _last(slow)

    if np.isnan(fast_val) or np.isnan(slow_val) or slow_val == 0:
        return 0.0, 0.0

    gap_pct = (fast_val - slow_val) / slow_val * 100  # e.g. +0.5%

    # Previous gap for crossover detection
    fast_prev = _last(fast, 1)
    slow_prev = _last(slow, 1)
    if not np.isnan(fast_prev) and not np.isnan(slow_prev) and slow_prev != 0:
        gap_prev = (fast_prev - slow_prev) / slow_prev * 100
        # Fresh cross amplifies signal
        if gap_prev <= 0 < gap_pct:
            signal = min(gap_pct * 10 + 0.3, 1.0)
            return signal, gap_pct
        if gap_prev >= 0 > gap_pct:
            signal = max(gap_pct * 10 - 0.3, -1.0)
            return signal, gap_pct

    # Ongoing trend — signal based on gap magnitude
    signal = max(-1.0, min(1.0, gap_pct * 5))  # 0.2% gap → signal ±1.0
    return signal, gap_pct


def vote_rsi(
    df: pd.DataFrame,
    period: int = 14,
    oversold: float = 30.0,
    overbought: float = 70.0,
) -> Tuple[float, float]:
    """
    RSI voter.
    Graduated: RSI 20 → +1.0 (strong buy), RSI 80 → -1.0 (strong sell).
    RSI 40-60 → weak signal proportional to distance from 50.
    """
    if len(df) < period + 3:
        return 0.0, 50.0

    rsi = _rsi(df["close"], period)
    rsi_val = _last(rsi)

    if np.isnan(rsi_val):
        return 0.0, 50.0

    if rsi_val <= oversold:
        # Strong BUY: scale from 0.5 (at 30) to 1.0 (at 0)
        signal = 0.5 + 0.5 * (oversold - rsi_val) / oversold
        return min(signal, 1.0), rsi_val
    elif rsi_val >= overbought:
        # Strong SELL: scale from -0.5 (at 70) to -1.0 (at 100)
        signal = -0.5 - 0.5 * (rsi_val - overbought) / (100 - overbought)
        return max(signal, -1.0), rsi_val
    else:
        # Mild signal based on distance from 50
        signal = (50 - rsi_val) / 50 * 0.3
        return signal, rsi_val


def vote_macd(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> Tuple[float, float]:
    """
    MACD voter.
    Signal based on histogram magnitude + crossover detection.
    """
    if len(df) < slow + signal_period + 3:
        return 0.0, 0.0

    macd_line, signal_line, histogram = _macd(df["close"], fast, slow, signal_period)

    hist_val = _last(histogram)
    hist_prev = _last(histogram, 1)
    price = _last(df["close"])

    if np.isnan(hist_val) or np.isnan(hist_prev) or price == 0:
        return 0.0, 0.0

    # Normalize histogram as % of price
    hist_pct = hist_val / price * 100

    # Crossover detection
    if hist_prev <= 0 < hist_val:
        # Bullish cross — amplified signal
        signal = min(0.5 + abs(hist_pct) * 10, 1.0)
        return signal, hist_pct
    elif hist_prev >= 0 > hist_val:
        # Bearish cross — amplified signal
        signal = max(-0.5 - abs(hist_pct) * 10, -1.0)
        return signal, hist_pct

    # Ongoing momentum
    signal = max(-1.0, min(1.0, hist_pct * 8))
    return signal, hist_pct


def vote_supertrend(
    df: pd.DataFrame,
    atr_period: int = 10,
    multiplier: float = 3.0,
) -> Tuple[float, float]:
    """
    SuperTrend voter.
    Direction flip = strong signal. Ongoing direction = moderate signal.
    """
    if len(df) < atr_period + 5:
        return 0.0, 0.0

    direction = _supertrend(df, atr_period, multiplier)

    dir_val = int(direction.iloc[-1])
    dir_prev = int(direction.iloc[-2]) if len(direction) >= 2 else dir_val

    # Direction flip = strong signal
    if dir_prev == -1 and dir_val == 1:
        return 0.9, float(dir_val)   # Bullish flip
    if dir_prev == 1 and dir_val == -1:
        return -0.9, float(dir_val)  # Bearish flip

    # Ongoing direction = moderate signal, decays slightly with duration
    # Count consecutive bars in same direction
    consecutive = 1
    for i in range(len(direction) - 2, max(0, len(direction) - 20), -1):
        if int(direction.iloc[i]) == dir_val:
            consecutive += 1
        else:
            break

    # Decay: fresh trend = 0.7, 10+ bars old = 0.4
    strength = max(0.4, 0.7 - consecutive * 0.03)
    return strength * dir_val, float(dir_val)


def vote_bb(
    df: pd.DataFrame,
    length: int = 20,
    num_std: float = 2.0,
) -> Tuple[float, float]:
    """
    Bollinger Bands voter.
    Price below lower band = BUY (mean-reversion).
    Price above upper band = SELL.
    BB Z-score gives graduated signal.
    """
    if len(df) < length + 3:
        return 0.0, 0.0

    _, upper, lower, _, bb_z = _bollinger(df["close"], length, num_std)

    z_val = _last(bb_z)

    if np.isnan(z_val):
        return 0.0, 0.0

    # Z < -2: strong BUY (below lower band)
    # Z > +2: strong SELL (above upper band)
    # Z around 0: neutral
    if z_val <= -2.0:
        signal = min(0.5 + (abs(z_val) - 2) * 0.25, 1.0)
        return signal, z_val
    elif z_val >= 2.0:
        signal = max(-0.5 - (z_val - 2) * 0.25, -1.0)
        return signal, z_val
    else:
        # Mild: scale linearly in [-2, +2] → [-0.5, +0.5]
        signal = -z_val / 4.0
        return signal, z_val


def vote_obv(
    df: pd.DataFrame,
    ma_length: int = 20,
) -> Tuple[float, float]:
    """
    OBV (On-Balance Volume) voter.
    OBV above its MA = volume confirms uptrend → BUY.
    OBV below MA = volume confirms downtrend → SELL.
    """
    if len(df) < ma_length + 5:
        return 0.0, 0.0

    obv = _obv(df)
    obv_ma = _sma(obv, ma_length)

    obv_val = _last(obv)
    obv_ma_val = _last(obv_ma)

    if np.isnan(obv_val) or np.isnan(obv_ma_val) or obv_ma_val == 0:
        return 0.0, 0.0

    # Deviation as % of OBV MA
    deviation = (obv_val - obv_ma_val) / (abs(obv_ma_val) + 1e-10)
    signal = max(-1.0, min(1.0, deviation * 3))
    return signal, deviation


def get_atr_pct(df: pd.DataFrame, length: int = 14) -> float:
    """Compute ATR as percentage of current price."""
    if len(df) < length + 3:
        return 0.0
    atr_series = _atr(df, length)
    atr_val = _last(atr_series)
    price = _last(df["close"])
    if np.isnan(atr_val) or np.isnan(price) or price == 0:
        return 0.0
    return atr_val / price * 100


# ── Registry ────────────────────────────────────────────────────────────────

VOTER_REGISTRY: dict[str, callable] = {
    "ma_cross":   vote_ma_cross,
    "rsi":        vote_rsi,
    "macd":       vote_macd,
    "supertrend": vote_supertrend,
    "bb":         vote_bb,
    "obv":        vote_obv,
}


def get_voter_names() -> list[str]:
    return list(VOTER_REGISTRY.keys())
