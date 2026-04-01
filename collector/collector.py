"""
BOTTEU Data Collector — streams 1m klines from Binance WebSocket,
aggregates to multiple timeframes, computes indicators, and cleans data.

Supports multiple symbols via SYMBOLS env (comma-separated).
Robust reconnect logic with exponential backoff.

Configurable via environment variables:
    SYMBOLS         Comma-separated pairs (default: BTCUSDT)
    DATA_DIR        Output directory (default: /app/data)
    ROLL_WINDOW     Max rows per CSV (default: 7770 = ~5.4 days of 1m data)
    MAX_RECONNECTS  Max sequential reconnect attempts before long sleep (default: 20)
"""
import os
import logging
import asyncio
import signal as _signal
import sys

import pandas as pd
import numpy as np
from binance import AsyncClient, BinanceSocketManager
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Configuration ───────────────────────────────────────────────────────────

_symbols_raw = os.environ.get("SYMBOLS", os.environ.get("SYMBOL", "BTCUSDT"))
SYMBOLS: list[str] = [s.strip().upper() for s in _symbols_raw.split(",") if s.strip()]

DATA_DIR = os.environ.get("DATA_DIR", "data")
ROLL_WINDOW = int(os.environ.get("ROLL_WINDOW", "7770"))
MAX_RECONNECTS = int(os.environ.get("MAX_RECONNECTS", "20"))

os.makedirs(DATA_DIR, exist_ok=True)

AGGREGATES = {
    "5m":  "5min",
    "15m": "15min",
    "30m": "30min",
    "1h":  "1h",
    "4h":  "4h",
    "1d":  "1D",
}

# ── Indicator columns ───────────────────────────────────────────────────────

IND_COLS = [
    "rsi", "ema12", "ema26", "ma7", "ma25",
    "macd", "macd_signal", "macd_histogram",
    "obv", "atr",
    "bb_ma", "bb_std", "bb_upper", "bb_lower", "bb_z",
    "supertrend_dir",
]
PRICE_COLS = ["open", "high", "low", "close", "volume"]


# ── Indicators ──────────────────────────────────────────────────────────────

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    # RSI (14)
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    roll_up = up.rolling(14).mean()
    roll_down = down.rolling(14).mean()
    rs = roll_up / (roll_down + 1e-9)
    df["rsi"] = 100 - (100 / (1 + rs))

    # EMAs
    df["ema12"] = close.ewm(span=12, adjust=False).mean()
    df["ema26"] = close.ewm(span=26, adjust=False).mean()

    # SMAs
    df["ma7"] = close.rolling(window=7).mean()
    df["ma25"] = close.rolling(window=25).mean()

    # MACD + signal + histogram
    df["macd"] = df["ema12"] - df["ema26"]
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_histogram"] = df["macd"] - df["macd_signal"]

    # OBV
    sign = np.sign(close.diff())
    df["obv"] = (sign * volume).cumsum()

    # ATR (14)
    high_low = high - low
    high_close = (high - close.shift()).abs()
    low_close = (low - close.shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr"] = true_range.rolling(14).mean()

    # Bollinger Bands (20, 2σ)
    bb_ma = close.rolling(window=20).mean()
    bb_std = close.rolling(window=20).std()
    df["bb_ma"] = bb_ma
    df["bb_std"] = bb_std
    df["bb_upper"] = bb_ma + 2 * bb_std
    df["bb_lower"] = bb_ma - 2 * bb_std
    df["bb_z"] = (close - bb_ma) / (bb_std + 1e-9)

    # SuperTrend direction (ATR=10, mult=3)
    atr_st = true_range.rolling(10).mean()
    hl2 = (high + low) / 2
    upper_band = hl2 + 3 * atr_st
    lower_band = hl2 - 3 * atr_st
    direction = pd.Series(1, index=df.index, dtype=int)
    final_upper = upper_band.copy()
    final_lower = lower_band.copy()
    for i in range(1, len(df)):
        if not np.isnan(final_lower.iloc[i - 1]):
            final_lower.iloc[i] = max(lower_band.iloc[i], final_lower.iloc[i - 1])
        if not np.isnan(final_upper.iloc[i - 1]):
            final_upper.iloc[i] = min(upper_band.iloc[i], final_upper.iloc[i - 1])
        if direction.iloc[i - 1] == 1:
            direction.iloc[i] = -1 if close.iloc[i] < final_lower.iloc[i] else 1
        else:
            direction.iloc[i] = 1 if close.iloc[i] > final_upper.iloc[i] else -1
    df["supertrend_dir"] = direction

    return df


# ── Data cleaning ───────────────────────────────────────────────────────────

def clean_data(df: pd.DataFrame, min_rolling: int = 30, min_rows: int = 50) -> pd.DataFrame:
    df_clean = df.iloc[min_rolling:].copy()
    existing = [c for c in IND_COLS if c in df_clean.columns]
    df_clean[existing] = df_clean[existing].ffill()
    for col in existing:
        median_val = df_clean[col].median(skipna=True)
        df_clean[col] = df_clean[col].fillna(median_val)
    df_clean = df_clean.ffill().bfill()
    df_clean = df_clean.dropna(subset=PRICE_COLS)
    if len(df_clean) < min_rows:
        return pd.DataFrame()
    return df_clean


# ── Aggregation ─────────────────────────────────────────────────────────────

def aggregate_and_save(df_1m: pd.DataFrame, freq: str, filename: str) -> None:
    df = df_1m.copy()
    if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize("UTC")
    df = df.set_index("timestamp")
    ohlc = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    agg = df[PRICE_COLS].resample(freq).agg(ohlc).dropna().reset_index()
    agg = add_indicators(agg)
    agg = clean_data(agg, min_rolling=30, min_rows=10)
    if len(agg) > ROLL_WINDOW:
        agg = agg.iloc[-ROLL_WINDOW:].reset_index(drop=True)
    if not agg.empty:
        agg.to_csv(filename, index=False)


# ── Per-symbol file paths ───────────────────────────────────────────────────

def _filepath_1m(symbol: str) -> str:
    return os.path.join(DATA_DIR, f"{symbol.lower()}_1m_clean.csv")


def _filepath_tf(symbol: str, tf: str) -> str:
    return os.path.join(DATA_DIR, f"{symbol.lower()}_{tf}_clean.csv")


# ── Symbol stream (one coroutine per symbol) ────────────────────────────────

async def stream_symbol(client: AsyncClient, symbol: str):
    """
    Stream 1m klines for a single symbol with robust reconnect.
    Never exits — reconnects with exponential backoff on any failure.
    """
    columns = ["timestamp", "open", "high", "low", "close", "volume"]
    filepath = _filepath_1m(symbol)

    # Load existing data
    if os.path.exists(filepath):
        df = pd.read_csv(filepath, parse_dates=["timestamp"])
        if not df.empty:
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
            if df["timestamp"].dt.tz is None:
                df["timestamp"] = df["timestamp"].dt.tz_localize("UTC")
    else:
        df = pd.DataFrame(columns=columns)

    logger.info("🟢 [%s] Stream started. Existing rows: %d", symbol, len(df))

    reconnect_count = 0
    backoff = 5  # seconds

    while True:
        try:
            bm = BinanceSocketManager(client)
            socket = bm.kline_socket(symbol=symbol.lower(), interval=AsyncClient.KLINE_INTERVAL_1MINUTE)

            async with socket as s:
                reconnect_count = 0
                backoff = 5
                logger.info("🔗 [%s] WebSocket connected", symbol)

                while True:
                    msg = await s.recv()
                    if "k" not in msg:
                        continue
                    k = msg["k"]
                    if not k["x"]:  # Only process closed candles
                        continue

                    row = {
                        "timestamp": datetime.fromtimestamp(k["T"] / 1000, tz=timezone.utc),
                        "open": float(k["o"]),
                        "high": float(k["h"]),
                        "low": float(k["l"]),
                        "close": float(k["c"]),
                        "volume": float(k["v"]),
                    }

                    if df.empty:
                        df = pd.DataFrame([row])
                    else:
                        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)

                    # Rolling window — trim old data to keep it fresh
                    if len(df) > ROLL_WINDOW:
                        df = df.iloc[-ROLL_WINDOW:].reset_index(drop=True)

                    df = add_indicators(df)

                    if len(df) >= 30:
                        df_clean = clean_data(df, min_rolling=30, min_rows=30)
                        if not df_clean.empty:
                            df_clean.to_csv(filepath, index=False)
                            logger.info(
                                "✅ [%s] %d rows. close=%.2f time=%s",
                                symbol, len(df_clean), row["close"], row["timestamp"],
                            )
                            for tf_key, rule in AGGREGATES.items():
                                fname = _filepath_tf(symbol, tf_key)
                                aggregate_and_save(df_clean, rule, fname)
                    else:
                        logger.info("⏳ [%s] Warming up (%d/%d rows)...", symbol, len(df), 30)

                    await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            logger.info("🛑 [%s] Stream cancelled", symbol)
            return

        except Exception as exc:
            reconnect_count += 1
            logger.warning(
                "⚠️ [%s] WebSocket error (attempt %d): %s",
                symbol, reconnect_count, exc,
            )

            if reconnect_count >= MAX_RECONNECTS:
                # Long sleep before another cycle of attempts
                long_sleep = 300  # 5 minutes
                logger.error(
                    "🔴 [%s] %d reconnects failed. Sleeping %ds before retry cycle...",
                    symbol, MAX_RECONNECTS, long_sleep,
                )
                await asyncio.sleep(long_sleep)
                reconnect_count = 0
                backoff = 5
            else:
                # Exponential backoff: 5, 10, 20, 40, ... capped at 120s
                wait = min(backoff, 120)
                logger.info("🔄 [%s] Reconnecting in %ds...", symbol, wait)
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, 120)


# ── Main ────────────────────────────────────────────────────────────────────

async def main():
    logger.info("=" * 60)
    logger.info("BOTTEU Collector starting")
    logger.info("Symbols: %s", ", ".join(SYMBOLS))
    logger.info("Data dir: %s", DATA_DIR)
    logger.info("Roll window: %d rows (~%.1f days of 1m data)", ROLL_WINDOW, ROLL_WINDOW / 1440)
    logger.info("=" * 60)

    client = await AsyncClient.create()

    # Create one task per symbol
    tasks = []
    for symbol in SYMBOLS:
        task = asyncio.create_task(stream_symbol(client, symbol), name=f"stream-{symbol}")
        tasks.append(task)

    # Graceful shutdown on SIGTERM / SIGINT
    loop = asyncio.get_running_loop()

    def _shutdown():
        logger.info("🛑 Shutdown signal received, cancelling streams...")
        for t in tasks:
            t.cancel()

    for sig in (_signal.SIGTERM, _signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await client.close_connection()
        logger.info("Collector shut down cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        sys.exit(0)
