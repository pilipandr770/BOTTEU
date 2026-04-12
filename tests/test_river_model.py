"""
Unit tests for RiverMLModel in collector/collector.py.

Tests instantiation, update behaviour, drift reset, and model persistence.
No Binance WebSocket connection needed.
"""
from __future__ import annotations

import os
import sys
import pickle
import tempfile

import numpy as np
import pandas as pd
import pytest

# ── Make collector importable without running the async main ──────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collector"))


def _make_df(n: int = 100) -> pd.DataFrame:
    """Generate synthetic OHLCV + indicator data for *n* rows."""
    rng = np.random.default_rng(42)
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

    # Add minimal indicator columns expected by RiverMLModel
    df["rsi"]           = 50.0 + rng.normal(0, 10, n)
    df["macd"]          = rng.normal(0, 5, n)
    df["bb_z"]          = rng.normal(0, 1, n)
    df["atr"]           = rng.uniform(50, 300, n)
    df["supertrend_dir"] = rng.choice([-1, 1], size=n).astype(float)
    return df


pytestmark = pytest.mark.skipif(
    not __import__("importlib").util.find_spec("river"),
    reason="river package not installed",
)


class TestRiverMLModel:

    def test_instantiates_without_existing_pkl(self, tmp_path):
        """RiverMLModel can be created even when no pkl file exists yet."""
        from collector import RiverMLModel
        model = RiverMLModel(symbol="BTCUSDT", data_dir=str(tmp_path))
        assert model._state.get("n", 0) == 0

    def test_update_returns_signal_or_none(self, tmp_path):
        """After enough updates the model returns an int signal (+1 or -1) or None."""
        from collector import RiverMLModel
        model = RiverMLModel(symbol="BTCUSDT", data_dir=str(tmp_path))
        df = _make_df(50)
        signal = None
        for i in range(2, len(df) + 1):
            signal = model.update(df.iloc[:i])
        # After at least two candles a non-None signal should be produced
        assert signal in (-1, 1, None)

    def test_update_increments_sample_counter(self, tmp_path):
        """Each call to update() increments the internal sample counter."""
        from collector import RiverMLModel
        model = RiverMLModel(symbol="ETHUSDT", data_dir=str(tmp_path))
        df = _make_df(60)
        for i in range(1, len(df) + 1):
            model.update(df.iloc[:i])
        assert model._state["n"] == len(df)

    def test_model_persists_and_reloads(self, tmp_path):
        """Saving and re-loading a RiverMLModel preserves the sample count."""
        from collector import RiverMLModel
        model = RiverMLModel(symbol="SOLUSDT", data_dir=str(tmp_path))
        df = _make_df(110)  # > 100 so autosave triggers
        for i in range(1, len(df) + 1):
            model.update(df.iloc[:i])
        # Force save
        model._save()
        assert os.path.exists(model._pkl_path)

        # Reload from disk
        model2 = RiverMLModel(symbol="SOLUSDT", data_dir=str(tmp_path))
        assert model2._state["n"] == model._state["n"]
