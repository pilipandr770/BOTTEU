"""
Multi-Timeframe Data Pipeline for the Consensus Engine.

Two data sources:
1. Binance API (get_klines) — real-time, used by bot runner
2. Collector CSV files — pre-computed, cleaned, with indicators

The pipeline loads data for all configured timeframes, caches it in
bot state, and refreshes only when a candle closes on each TF.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import pandas as pd
from binance.client import Client

logger = logging.getLogger(__name__)

# Seconds per timeframe — used for cache invalidation
TF_SECONDS: dict[str, int] = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}

# How many candles to fetch per timeframe (enough for all indicators)
TF_CANDLE_LIMIT: dict[str, int] = {
    "1m": 200, "5m": 200, "15m": 200, "30m": 200,
    "1h": 200, "4h": 200, "1d": 200,
}

# Binance kline interval strings
TF_BINANCE_INTERVAL: dict[str, str] = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "4h": "4h", "1d": "1d",
}

# Collector CSV directory (Docker volume mount)
COLLECTOR_DATA_DIR = os.environ.get("COLLECTOR_DATA_DIR", "collector/data")


def _klines_to_df(klines: list) -> pd.DataFrame:
    """Convert Binance klines response to a OHLCV DataFrame."""
    rows = []
    for k in klines:
        rows.append({
            "timestamp": pd.Timestamp(k[0], unit="ms", tz="UTC"),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def fetch_multi_tf_binance(
    client: Client,
    symbol: str,
    timeframes: list[str],
) -> dict[str, pd.DataFrame]:
    """
    Fetch OHLCV data from Binance for multiple timeframes.
    Returns {timeframe: DataFrame} dict.
    """
    result = {}
    for tf in timeframes:
        interval = TF_BINANCE_INTERVAL.get(tf)
        if not interval:
            logger.warning("Unknown timeframe: %s", tf)
            continue
        limit = TF_CANDLE_LIMIT.get(tf, 200)
        try:
            klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
            df = _klines_to_df(klines)
            if not df.empty:
                result[tf] = df
                logger.debug("Fetched %d candles for %s %s", len(df), symbol, tf)
            else:
                logger.warning("Empty klines for %s %s", symbol, tf)
        except Exception as exc:
            logger.error("Failed to fetch %s %s: %s", symbol, tf, exc)
    return result


def load_collector_csv(
    symbol: str,
    timeframes: list[str],
    data_dir: str | None = None,
) -> dict[str, pd.DataFrame]:
    """
    Load pre-computed clean CSVs from the collector container.
    Files are expected at: {data_dir}/btc_eur_{tf}_clean.csv

    This is the preferred source when available — data is already cleaned,
    has indicators pre-computed, and covers longer history.
    """
    result = {}
    base_dir = data_dir or COLLECTOR_DATA_DIR

    # Map symbol to collector file prefix
    # Collector saves as {symbol_lower}_{tf}_clean.csv
    prefix = symbol.lower()

    for tf in timeframes:
        filename = os.path.join(base_dir, f"{prefix}_{tf}_clean.csv")
        if not os.path.exists(filename):
            logger.debug("Collector CSV not found: %s", filename)
            continue
        try:
            df = pd.read_csv(filename, parse_dates=["timestamp"])
            if not df.empty:
                # Ensure required OHLCV columns exist
                required = {"open", "high", "low", "close", "volume"}
                if required.issubset(df.columns):
                    result[tf] = df
                    logger.debug("Loaded %d rows from collector for %s", len(df), tf)
                else:
                    logger.warning("Collector CSV missing columns: %s", filename)
        except Exception as exc:
            logger.error("Failed to load collector CSV %s: %s", filename, exc)

    return result


def load_collector_signals(data_dir: str | None = None) -> list[dict]:
    """
    Load ML model signals from the collector's signals/ directories.
    Returns list of {"model": str, "tf": str, "signal": int, "timestamp": str}.
    """
    import json
    base_dir = data_dir or COLLECTOR_DATA_DIR
    signals = []

    for subdir in ("signals", "signals_river"):
        signals_dir = os.path.join(base_dir, subdir)
        if not os.path.isdir(signals_dir):
            continue
        for filename in os.listdir(signals_dir):
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(signals_dir, filename)
            try:
                with open(filepath, "r") as f:
                    data = json.load(f)
                if isinstance(data, dict) and "signal" in data:
                    signals.append(data)
            except Exception as exc:
                logger.debug("Could not load signal file %s: %s", filename, exc)

    return signals


def should_refresh_tf(tf: str, last_refresh_ts: float) -> bool:
    """Check if enough time has passed to refresh this timeframe's data."""
    interval = TF_SECONDS.get(tf, 60)
    return (time.time() - last_refresh_ts) >= interval


def get_multi_tf_data(
    client: Optional[Client],
    symbol: str,
    timeframes: list[str],
    state: dict,
    use_collector: bool = False,
    collector_data_dir: str | None = None,
) -> dict[str, pd.DataFrame]:
    """
    Smart data loader: checks cache, only refreshes stale timeframes.

    Caching is done through bot state:
        state["mtf_cache"] = {
            "5m": {"last_ts": 1234567890, "data_hash": "..."},
            ...
        }

    Priority:
    1. Collector CSVs (if use_collector=True and files exist)
    2. Binance API (real-time, always available as fallback)
    """
    cache = state.get("mtf_cache", {})
    result = {}

    # Determine which TFs need refresh
    stale_tfs = []
    for tf in timeframes:
        tf_cache = cache.get(tf, {})
        last_ts = tf_cache.get("last_ts", 0)
        if should_refresh_tf(tf, last_ts):
            stale_tfs.append(tf)
        # For non-stale TFs, we'll still need their data — they'll be
        # fetched from Binance but we won't update the cache timestamp

    if not stale_tfs and not result:
        # Everything fresh but we need data — fetch all
        stale_tfs = timeframes[:]

    # Source 1: Collector CSVs (longer history, pre-cleaned)
    if use_collector:
        collector_data = load_collector_csv(symbol, stale_tfs, collector_data_dir)
        for tf, df in collector_data.items():
            result[tf] = df
            if tf in stale_tfs:
                stale_tfs.remove(tf)

    # Source 2: Binance API for remaining TFs
    if stale_tfs and client is not None:
        binance_data = fetch_multi_tf_binance(client, symbol, stale_tfs)
        result.update(binance_data)

    # Update cache timestamps
    now = time.time()
    for tf in result:
        if tf not in cache:
            cache[tf] = {}
        cache[tf]["last_ts"] = now
        cache[tf]["rows"] = len(result[tf])

    state["mtf_cache"] = cache

    return result
