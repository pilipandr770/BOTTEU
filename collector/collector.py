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
    HTTP_PORT       Port to serve CSV files over HTTP (default: $PORT or 8080)
                    Set to 0 to disable the HTTP server.
"""
import os
import http.server
import logging
import asyncio
import signal as _signal
import sys
import threading

import pandas as pd
import numpy as np
from binance import AsyncClient, BinanceSocketManager
from datetime import datetime, timezone

# ── Optional River online-learning dependency ───────────────────────────────
try:
    from river import tree, drift, metrics, preprocessing
    RIVER_AVAILABLE = True
except ImportError:  # pragma: no cover
    RIVER_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

if not RIVER_AVAILABLE:  # warn once after logger is configured
    logger.warning("river not installed — online ML disabled (pip install river)")

# ── Configuration ───────────────────────────────────────────────────────────

_symbols_raw = os.environ.get("SYMBOLS", os.environ.get("SYMBOL", "BTCUSDT"))
SYMBOLS: list[str] = [s.strip().upper() for s in _symbols_raw.split(",") if s.strip()]

DATA_DIR = os.environ.get("DATA_DIR", "data")
ROLL_WINDOW = int(os.environ.get("ROLL_WINDOW", "7770"))
MAX_RECONNECTS = int(os.environ.get("MAX_RECONNECTS", "20"))
# Render sets $PORT for web services; HTTP_PORT allows explicit override; 0 = disabled
HTTP_PORT = int(os.environ.get("PORT", os.environ.get("HTTP_PORT", "8080")))
COLLECTOR_API_TOKEN: str = os.environ.get("COLLECTOR_API_TOKEN", "")

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
    # TODO: fully vectorize with np.where in a future pass (currently 3-5× faster than .iloc)
    for i in range(1, len(df)):
        if not np.isnan(final_lower.iat[i - 1]):
            final_lower.iat[i] = max(lower_band.iat[i], final_lower.iat[i - 1])
        if not np.isnan(final_upper.iat[i - 1]):
            final_upper.iat[i] = min(upper_band.iat[i], final_upper.iat[i - 1])
        if direction.iat[i - 1] == 1:
            direction.iat[i] = -1 if close.iat[i] < final_lower.iat[i] else 1
        else:
            direction.iat[i] = 1 if close.iat[i] > final_upper.iat[i] else -1
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


# ── Atomic CSV write ───────────────────────────────────────────────────────

def _atomic_write_csv(df: pd.DataFrame, filepath: str) -> None:
    """Write *df* to *filepath* atomically via a tmp file + os.replace().

    Ensures the HTTP server never serves a partial/corrupt CSV even if the
    writer is preempted mid-write.
    """
    tmp = filepath + ".tmp"
    df.to_csv(tmp, index=False)
    os.replace(tmp, filepath)


# ── River online-learning model ─────────────────────────────────────────────

_RIVER_FEATURES = ["rsi", "macd", "bb_z", "atr", "supertrend_dir"]


class RiverMLModel:
    """Per-symbol online classifier that learns from every incoming candle.

    Uses a Hoeffding Tree with ADWIN drift detector.  The target label is
    the sign of the *next* close-to-close return: +1 (up) or -1 (down/flat).

    The model and its metrics are persisted to ``<data_dir>/river_<symbol>.pkl``
    so they survive collector restarts.
    """

    def __init__(self, symbol: str, data_dir: str) -> None:
        self.symbol = symbol.upper()
        self._pkl_path = os.path.join(data_dir, f"river_{symbol.lower()}.pkl")
        self._state: dict = self._load()

    # ── Persistence ────────────────────────────────────────────────────────

    def _load(self) -> dict:
        import pickle
        if os.path.exists(self._pkl_path):
            try:
                with open(self._pkl_path, "rb") as fh:
                    state = pickle.load(fh)
                logger.info("[River][%s] Loaded model (%d samples)", self.symbol, state.get("n", 0))
                return state
            except Exception as exc:
                logger.warning("[River][%s] Could not load model: %s — starting fresh", self.symbol, exc)
        return self._new_state()

    def _new_state(self) -> dict:
        if not RIVER_AVAILABLE:
            return {}
        return {
            "model": preprocessing.StandardScaler() | tree.HoeffdingAdaptiveTreeClassifier(),
            "drift": drift.ADWIN(),
            "metric": metrics.Accuracy(),
            "n": 0,
            "prev_close": None,
        }

    def _save(self) -> None:
        import pickle
        try:
            tmp = self._pkl_path + ".tmp"
            with open(tmp, "wb") as fh:
                pickle.dump(self._state, fh)
            os.replace(tmp, self._pkl_path)
        except Exception as exc:
            logger.warning("[River][%s] Could not save model: %s", self.symbol, exc)

    # ── Update ──────────────────────────────────────────────────────────────

    def update(self, df: pd.DataFrame) -> int | None:
        """Learn from the latest row and return a signal: +1 (up), -1 (down), or None."""
        if not RIVER_AVAILABLE or df.empty:
            return None

        row = df.iloc[-1]
        missing = [c for c in _RIVER_FEATURES if c not in df.columns]
        if missing:
            return None

        x = {c: float(row[c]) for c in _RIVER_FEATURES if not pd.isna(row[c])}
        if len(x) < len(_RIVER_FEATURES):
            return None

        state = self._state
        prev_close = state.get("prev_close")
        cur_close = float(row["close"])

        # Label the *previous* sample with the actual outcome now that we know it
        if prev_close is not None and "pending_x" in state:
            y = 1 if cur_close > prev_close else -1
            state["model"].learn_one(state["pending_x"], y)
            y_pred = state["model"].predict_one(state["pending_x"])
            if y_pred is not None:
                state["metric"].update(y, y_pred)
            # ADWIN drift detection on error signal (1 = error, 0 = correct)
            error = int(y_pred != y) if y_pred is not None else 0
            state["drift"].update(error)
            if state["drift"].drift_detected:
                logger.info("[River][%s] Drift detected — resetting model", self.symbol)
                new = self._new_state()
                new["n"] = state["n"]
                state.update(new)

        state["pending_x"] = x
        state["prev_close"] = cur_close
        state["n"] = state.get("n", 0) + 1

        # Predict next candle direction
        signal = state["model"].predict_one(x)

        if state["n"] % 100 == 0:
            acc = state["metric"].get() * 100
            logger.info(
                "[River][%s] n=%d  accuracy=%.1f%%  signal=%s",
                self.symbol, state["n"], acc, signal,
            )
            self._save()

        return signal


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
        _atomic_write_csv(agg, filename)


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

    # One River online-learning model per symbol (survives reconnects)
    river_model = RiverMLModel(symbol=symbol, data_dir=DATA_DIR)

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
                            _atomic_write_csv(df_clean, filepath)
                            river_signal = river_model.update(df_clean)
                            logger.info(
                                "✅ [%s] %d rows. close=%.2f time=%s signal=%s",
                                symbol, len(df_clean), row["close"], row["timestamp"], river_signal,
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


# ── CSV HTTP File Server ────────────────────────────────────────────────────

class _CSVHandler(http.server.SimpleHTTPRequestHandler):
    """Serves DATA_DIR over HTTP. Adds /health and /status endpoints.
    When COLLECTOR_API_TOKEN is set, all non-health endpoints require a
    valid ``Authorization: Bearer <token>`` header.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DATA_DIR, **kwargs)

    def _is_authorized(self) -> bool:
        """Return True if the request carries a valid Bearer token or no token is configured."""
        if not COLLECTOR_API_TOKEN:
            return True
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        return auth[len("Bearer "):] == COLLECTOR_API_TOKEN

    def do_GET(self):
        # /health is always public — Render uses it for health checks.
        if self.path in ("/health", "/health/"):
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # All endpoints beyond /health require auth when a token is configured.
        if not self._is_authorized():
            self.send_error(401, "Unauthorized")
            return
        if self.path in ("/status", "/status/"):
            import json as _json
            import time as _time
            csv_files = [
                f for f in os.listdir(DATA_DIR)
                if f.endswith(".csv") and not f.endswith(".tmp")
            ]
            body = _json.dumps({
                "status": "ok",
                "symbols": SYMBOLS,
                "csv_files": len(csv_files),
                "ts": int(_time.time()),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # Block directory listing — only serve .csv and .json files
        import pathlib
        p = pathlib.PurePosixPath(self.path.split("?")[0])
        if p.suffix not in (".csv", ".json") and p.name not in ("", "."):
            self.send_error(403, "Only CSV and JSON files are served")
            return
        super().do_GET()

    def log_message(self, fmt, *args):  # noqa: N802
        logger.debug("HTTP " + fmt, *args)


def _start_csv_http_server(port: int) -> None:
    """Start a background thread serving DATA_DIR over HTTP on *port*."""
    if port == 0:
        logger.info("CSV HTTP server disabled (HTTP_PORT=0)")
        return
    try:
        server = http.server.HTTPServer(("0.0.0.0", port), _CSVHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True, name="csv-http")
        t.start()
        logger.info("📡 CSV HTTP server started on port %d  (serving %s)", port, DATA_DIR)
    except OSError as exc:
        logger.error("Could not start CSV HTTP server on port %d: %s", port, exc)


# ── Main ────────────────────────────────────────────────────────────────────

async def main():
    logger.info("=" * 60)
    logger.info("BOTTEU Collector starting")
    logger.info("Symbols: %s", ", ".join(SYMBOLS))
    logger.info("Data dir: %s", DATA_DIR)
    logger.info("Roll window: %d rows (~%.1f days of 1m data)", ROLL_WINDOW, ROLL_WINDOW / 1440)
    logger.info("=" * 60)

    # Start HTTP file server for Render (serves CSVs to web service)
    _start_csv_http_server(HTTP_PORT)

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
