"""
Feature extraction for the ML ensemble.

Two modes:
- Collector CSVs: pre-computed indicator columns are present
- Binance API data: raw OHLCV only — indicators computed on the fly

Output shape: (n_rows, 10) float32 array, all values finite.

Feature list:
  0  rsi_norm         RSI normalized: (rsi - 50) / 50  → [-1, +1]
  1  bb_z             BB z-score of close
  2  macd_norm        MACD histogram / ATR × 100
  3  st_dir           SuperTrend direction  +1/-1 (0 if unknown)
  4  close_vs_ma7     (close - MA7) / ATR × 100
  5  close_vs_ma25    (close - MA25) / ATR × 100
  6  ret_1            1-bar return %
  7  ret_3            3-bar return %
  8  ret_5            5-bar return %
  9  vol_ratio        volume / 20-bar mean (clipped 0–5)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

FEATURE_NAMES = [
    "rsi_norm", "bb_z", "macd_norm", "st_dir",
    "close_vs_ma7", "close_vs_ma25",
    "ret_1", "ret_3", "ret_5", "vol_ratio",
]
N_FEATURES = len(FEATURE_NAMES)

# ── Adaptive label parameters per timeframe ────────────────────────────
# threshold_pct: minimum price move (%) to be labeled BUY/SELL (else HOLD)
# forward_n:     how many bars ahead to measure the move
# Rationale: daily candles move 2-4× more than hourly — fixed 0.5% labels
# almost everything on 1d as BUY/SELL and hobbles the classifier.
_TF_LABEL_PARAMS: dict[str, tuple[float, int]] = {
    # tf       threshold_pct  forward_n
    "1m":      (0.08,  8),
    "3m":      (0.12,  6),
    "5m":      (0.20,  6),
    "15m":     (0.30,  5),
    "30m":     (0.45,  4),
    "1h":      (0.60,  3),
    "2h":      (0.80,  3),
    "4h":      (1.20,  3),
    "6h":      (1.50,  3),
    "8h":      (1.80,  3),
    "12h":     (2.00,  3),
    "1d":      (2.50,  3),
    "3d":      (4.00,  3),
    "1w":      (6.00,  2),
}
_DEFAULT_LABEL_PARAMS = (0.50, 5)   # fallback for unknown TFs


def get_tf_label_params(timeframe: str) -> tuple[float, int]:
    """Return (threshold_pct, forward_n) for the given timeframe string."""
    return _TF_LABEL_PARAMS.get(timeframe.lower(), _DEFAULT_LABEL_PARAMS)


def extract_features(df: pd.DataFrame) -> np.ndarray:
    """
    Extract feature matrix from a OHLCV (+ optional indicator) DataFrame.
    Returns float32 array of shape (n_rows, N_FEATURES).
    NaN / Inf are replaced by 0.
    """
    df = df.reset_index(drop=True)
    close = df["close"].astype(float)
    high  = df["high"].astype(float)   if "high"  in df.columns else close
    low   = df["low"].astype(float)    if "low"   in df.columns else close
    volume = df["volume"].astype(float) if "volume" in df.columns else pd.Series(
        np.ones(len(df)), index=df.index
    )

    # ── ATR (14) ── used as normalizer
    if "atr" in df.columns:
        atr = df["atr"].astype(float)
    else:
        hl = high - low
        hc = (high - close.shift()).abs()
        lc = (low  - close.shift()).abs()
        atr = pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(14).mean()
    atr = atr.fillna(method="bfill").fillna(1.0).replace(0, 1e-8)

    # ── RSI (14) ──
    if "rsi" in df.columns:
        rsi = df["rsi"].astype(float)
    else:
        delta = close.diff()
        up   = delta.clip(lower=0)
        down = -delta.clip(upper=0)
        rsi  = 100 - (100 / (
            1 + up.rolling(14).mean() / (down.rolling(14).mean() + 1e-9)
        ))
    rsi_norm = (rsi - 50.0) / 50.0

    # ── Bollinger z-score ──
    if "bb_z" in df.columns:
        bb_z = df["bb_z"].astype(float)
    else:
        bb_ma  = close.rolling(20).mean()
        bb_std = close.rolling(20).std().replace(0, 1e-8)
        bb_z   = (close - bb_ma) / bb_std

    # ── MACD histogram (normalized by ATR) ──
    if "macd_histogram" in df.columns:
        macd_hist = df["macd_histogram"].astype(float)
    else:
        ema12     = close.ewm(span=12, adjust=False).mean()
        ema26     = close.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        macd_hist = macd_line - macd_line.ewm(span=9, adjust=False).mean()
    macd_norm = macd_hist / (atr + 1e-9) * 100.0

    # ── SuperTrend direction ──
    if "supertrend_dir" in df.columns:
        st_dir = df["supertrend_dir"].astype(float).fillna(0)
    else:
        st_dir = pd.Series(0.0, index=df.index)

    # ── Close vs MAs ──
    ma7  = df["ma7"].astype(float)  if "ma7"  in df.columns else close.rolling(7).mean()
    ma25 = df["ma25"].astype(float) if "ma25" in df.columns else close.rolling(25).mean()
    close_vs_ma7  = (close - ma7)  / (atr + 1e-9) * 100.0
    close_vs_ma25 = (close - ma25) / (atr + 1e-9) * 100.0

    # ── Returns ──
    ret_1 = close.pct_change(1) * 100.0
    ret_3 = close.pct_change(3) * 100.0
    ret_5 = close.pct_change(5) * 100.0

    # ── Volume ratio ──
    vol_mean = volume.rolling(20).mean().replace(0, 1e-8)
    vol_ratio = (volume / vol_mean).clip(0, 5)

    X = np.column_stack([
        rsi_norm.values,
        bb_z.values,
        macd_norm.values,
        st_dir.values,
        close_vs_ma7.values,
        close_vs_ma25.values,
        ret_1.values,
        ret_3.values,
        ret_5.values,
        vol_ratio.values,
    ]).astype(np.float32)

    return np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)


def extract_labels(
    df: pd.DataFrame,
    forward_n: int = 5,
    threshold_pct: float = 0.5,
    timeframe: str | None = None,
) -> np.ndarray:
    """
    Create integer labels from future price movement.
    BUY=1, SELL=-1, HOLD=0.
    Last `forward_n` rows will have NaN label (no future data).

    If `timeframe` is provided and forward_n / threshold_pct are at their
    defaults, the adaptive per-TF parameters are used automatically.
    """
    if timeframe is not None and forward_n == 5 and threshold_pct == 0.5:
        threshold_pct, forward_n = get_tf_label_params(timeframe)

    close = df["close"].astype(float)

    future_ret = (close.shift(-forward_n) / (close + 1e-9) - 1.0) * 100.0
    y = np.where(
        future_ret > threshold_pct, 1,
        np.where(future_ret < -threshold_pct, -1, 0)
    ).astype(np.int8)
    return y
