"""Unit tests for RiverMLModel in collector/collector.py."""
from __future__ import annotations
import os, sys, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "collector"))

import numpy as np
import pandas as pd
import pytest


def _make_df(n: int = 100) -> pd.DataFrame:
    rng   = np.random.default_rng(42)
    dates = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")
    close = 40000 + rng.normal(0, 100, n).cumsum()
    return pd.DataFrame({
        "timestamp":       dates,
        "open":            close - rng.uniform(0, 50, n),
        "high":            close + rng.uniform(0, 80, n),
        "low":             close - rng.uniform(0, 80, n),
        "close":           close,
        "volume":          rng.uniform(1, 10, n),
        "rsi":             rng.uniform(30, 70, n),
        "macd_histogram":  rng.normal(0, 10, n),
        "bb_z":            rng.normal(0, 1, n),
        "supertrend_dir":  rng.choice([-1, 1], n),
        "atr":             rng.uniform(50, 200, n),
        "obv":             rng.normal(0, 1000, n),
    })


class TestRiverMLModel:
    def test_init_disabled_when_river_unavailable(self, monkeypatch):
        import collector as col
        monkeypatch.setattr(col, "RIVER_AVAILABLE", False)
        with tempfile.TemporaryDirectory() as d:
            m = col.RiverMLModel("BTCUSDT", d)
            assert m.model is None
            assert m.update(_make_df()) == 0

    def test_signals_river_dir_created_on_init(self):
        import collector as col
        if not col.RIVER_AVAILABLE:
            pytest.skip("River not installed")
        with tempfile.TemporaryDirectory() as d:
            col.RiverMLModel("BTCUSDT", d)
            assert os.path.isdir(os.path.join(d, "signals_river"))

    def test_update_returns_valid_signal(self):
        import collector as col
        if not col.RIVER_AVAILABLE:
            pytest.skip("River not installed")
        with tempfile.TemporaryDirectory() as d:
            m   = col.RiverMLModel("BTCUSDT", d)
            df  = _make_df(80)
            sig = 999
            for i in range(3, len(df)):
                sig = m.update(df.iloc[: i + 1])
            assert sig in (-1, 0, 1)

    def test_signal_json_written_after_update(self):
        import collector as col
        if not col.RIVER_AVAILABLE:
            pytest.skip("River not installed")
        with tempfile.TemporaryDirectory() as d:
            m = col.RiverMLModel("BTCUSDT", d)
            m.update(_make_df(60))
            path = os.path.join(d, "signals_river", "btcusdt_river.json")
            assert os.path.exists(path)
            data = json.loads(open(path).read())
            assert "signal"    in data
            assert "n_seen"    in data
            assert "symbol"    in data
            assert data["symbol"]   == "BTCUSDT"
            assert data["signal"]   in (-1, 0, 1)

    def test_signal_json_has_no_tmp_file(self):
        import collector as col
        if not col.RIVER_AVAILABLE:
            pytest.skip("River not installed")
        with tempfile.TemporaryDirectory() as d:
            m = col.RiverMLModel("BTCUSDT", d)
            m.update(_make_df(60))
            tmp = os.path.join(d, "signals_river", "btcusdt_river.json.tmp")
            assert not os.path.exists(tmp)

    def test_n_seen_increments(self):
        import collector as col
        if not col.RIVER_AVAILABLE:
            pytest.skip("River not installed")
        with tempfile.TemporaryDirectory() as d:
            m  = col.RiverMLModel("BTCUSDT", d)
            df = _make_df(20)
            for i in range(3, len(df)):
                m.update(df.iloc[: i + 1])
            assert m.n_seen > 0

    def test_accuracy_none_before_50_samples(self):
        import collector as col
        if not col.RIVER_AVAILABLE:
            pytest.skip("River not installed")
        with tempfile.TemporaryDirectory() as d:
            m  = col.RiverMLModel("BTCUSDT", d)
            df = _make_df(30)
            for i in range(3, len(df)):
                m.update(df.iloc[: i + 1])
            path = os.path.join(d, "signals_river", "btcusdt_river.json")
            data = json.loads(open(path).read())
            assert data["accuracy"] is None   # n_seen < 50

    def test_atomic_write_no_tmp_left(self):
        import collector as col
        with tempfile.TemporaryDirectory() as d:
            df   = _make_df(10)
            path = os.path.join(d, "test.csv")
            col._atomic_write_csv(df, path)
            assert     os.path.exists(path)
            assert not os.path.exists(path + ".tmp")
            result = pd.read_csv(path)
            assert len(result) == len(df)
