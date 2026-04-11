"""
Unit tests for collector/collector.py — indicator computation.

Tests add_indicators() output columns, dtypes, and NaN behaviour.
No Binance WebSocket connection needed.
"""
from __future__ import annotations

import sys
import os

import numpy as np
import pandas as pd
import pytest

# ── Make collector importable without running the async main ──────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collector"))


def _make_df(n: int = 100) -> pd.DataFrame:
    """Generate synthetic OHLCV data for `n` rows."""
    rng = np.random.default_rng(0)
    close = 30_000.0 + np.cumsum(rng.normal(0, 100, n))
    high  = close + rng.uniform(50, 300, n)
    low   = close - rng.uniform(50, 300, n)
    open_ = close + rng.normal(0, 50, n)
    vol   = rng.uniform(1, 10, n)

    df = pd.DataFrame({
        "open":   open_,
        "high":   high,
        "low":    low,
        "close":  close,
        "volume": vol,
    })
    df.index = pd.date_range("2024-01-01", periods=n, freq="1min")
    return df


EXPECTED_INDICATOR_COLS = [
    "rsi", "ema12", "ema26", "ma7", "ma25",
    "macd", "macd_signal", "macd_histogram",
    "obv", "atr",
    "bb_ma", "bb_std", "bb_upper", "bb_lower", "bb_z",
    "supertrend_dir",
]


class TestAddIndicators:
    def test_returns_all_expected_columns(self):
        from collector import add_indicators
        df = _make_df(100)
        result = add_indicators(df)
        for col in EXPECTED_INDICATOR_COLS:
            assert col in result.columns, f"Missing column: {col}"

    def test_original_price_columns_preserved(self):
        from collector import add_indicators
        df = _make_df(100)
        result = add_indicators(df)
        for col in ("open", "high", "low", "close", "volume"):
            assert col in result.columns

    def test_supertrend_dir_values_are_plus_minus_one(self):
        from collector import add_indicators
        df = _make_df(200)
        result = add_indicators(df)
        unique_vals = set(result["supertrend_dir"].dropna().unique())
        assert unique_vals.issubset({-1, 1}), f"Unexpected supertrend values: {unique_vals}"

    def test_rsi_within_0_100(self):
        from collector import add_indicators
        df = _make_df(150)
        result = add_indicators(df)
        rsi = result["rsi"].dropna()
        assert (rsi >= 0).all(), "RSI has values < 0"
        assert (rsi <= 100).all(), "RSI has values > 100"

    def test_no_nan_in_price_cols(self):
        from collector import add_indicators
        df = _make_df(100)
        result = add_indicators(df)
        for col in ("open", "high", "low", "close"):
            assert result[col].isna().sum() == 0, f"{col} has NaN after add_indicators"

    def test_atr_is_non_negative(self):
        from collector import add_indicators
        df = _make_df(150)
        result = add_indicators(df)
        atr = result["atr"].dropna()
        assert (atr >= 0).all(), "ATR has negative values"

    def test_bb_upper_above_lower(self):
        from collector import add_indicators
        df = _make_df(150)
        result = add_indicators(df)
        subset = result.dropna(subset=["bb_upper", "bb_lower"])
        assert (subset["bb_upper"] >= subset["bb_lower"]).all(), \
            "BB upper < lower detected"

    def test_output_length_unchanged(self):
        from collector import add_indicators
        df = _make_df(80)
        result = add_indicators(df)
        assert len(result) == len(df)

    def test_does_not_modify_input(self):
        from collector import add_indicators
        df = _make_df(80)
        original_cols = list(df.columns)
        _ = add_indicators(df)
        assert list(df.columns) == original_cols, "add_indicators mutated the input DataFrame"


class TestCleanData:
    def test_removes_leading_nan_rows(self):
        from collector import add_indicators, clean_data
        df = _make_df(100)
        df_ind = add_indicators(df)
        cleaned = clean_data(df_ind, min_rolling=30, min_rows=10)
        assert len(cleaned) < len(df_ind)

    def test_returns_empty_df_when_too_few_rows(self):
        from collector import add_indicators, clean_data
        df = _make_df(40)
        df_ind = add_indicators(df)
        # min_rows = 50 forces empty return on 40-row output
        cleaned = clean_data(df_ind, min_rolling=30, min_rows=50)
        assert cleaned.empty

    def test_no_nan_in_indicator_columns_after_cleaning(self):
        from collector import add_indicators, clean_data, IND_COLS
        df = _make_df(200)
        df_ind = add_indicators(df)
        cleaned = clean_data(df_ind, min_rolling=30, min_rows=10)
        if cleaned.empty:
            pytest.skip("Cleaned DataFrame is empty — increase n")
        existing = [c for c in IND_COLS if c in cleaned.columns]
        nan_counts = cleaned[existing].isna().sum()
        assert nan_counts.sum() == 0, f"NaN found after clean_data:\n{nan_counts[nan_counts > 0]}"
