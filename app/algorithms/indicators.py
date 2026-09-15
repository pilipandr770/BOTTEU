"""
Shared indicator math used by more than one strategy module.
Keeping this in one place avoids subtly-different duplicate
implementations of the same indicator drifting apart over time.
"""
from __future__ import annotations

import pandas as pd


def adx(df: pd.DataFrame, length: int = 14) -> float | None:
    """
    Average Directional Index — measures trend strength (0-100), independent
    of direction. ADX > 25 = trending market (good for trend-following
    entries). ADX < 20 = ranging/choppy market (whipsaw risk).
    """
    if len(df) < length * 2 + 1:
        return None
    high  = df["high"].astype(float)
    low   = df["low"].astype(float)
    close = df["close"].astype(float)

    prev_high  = high.shift(1)
    prev_low   = low.shift(1)
    prev_close = close.shift(1)

    up_move   = high - prev_high
    down_move = prev_low - low
    plus_dm   = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm  = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    alpha = 1.0 / length
    atr14    = tr.ewm(alpha=alpha, min_periods=length, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(alpha=alpha, min_periods=length, adjust=False).mean() / atr14
    minus_di = 100 * minus_dm.ewm(alpha=alpha, min_periods=length, adjust=False).mean() / atr14

    dx  = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1)
    adx_series = dx.ewm(alpha=alpha, min_periods=length, adjust=False).mean()

    val = float(adx_series.iloc[-1])
    return val if not pd.isna(val) else None
