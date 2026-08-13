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
    """Write DataFrame to CSV atomically using tmp → os.replace."""
    tmp = filepath + ".tmp"
    try:
        df.to_csv(tmp, index=False)
        os.replace(tmp, filepath)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ── River online-learning model ─────────────────────────────────────────────


class RiverMLModel:
    """
    Online ML using River HoeffdingTreeClassifier + ADWIN drift detection.
    Trains on each closed 1m candle. Saves signal as JSON to signals_river/.
    signal: 1=BUY, -1=SELL, 0=HOLD
    """
    FEATURE_COLS = ["rsi", "macd_histogram", "bb_z", "supertrend_dir", "atr", "obv"]

    def __init__(self, symbol: str, data_dir: str):
        self.symbol = symbol
        self.signals_dir = os.path.join(data_dir, "signals_river")
        os.makedirs(self.signals_dir, exist_ok=True)

        if not RIVER_AVAILABLE:
            self.model = None
            return

        self.scaler  = preprocessing.StandardScaler()
        self.model   = tree.HoeffdingTreeClassifier(
            grace_period=50, delta=1e-5, tau=0.05, leaf_prediction="nba",
        )
        self.drift   = drift.ADWIN(delta=0.002)
        self.metric  = metrics.Accuracy()
        self.n_seen  = 0
        self.last_signal = 0

    def _extract_features(self, row: pd.Series) -> dict:
        feats = {}
        for col in self.FEATURE_COLS:
            val = row.get(col, 0.0)
            feats[col] = float(val) if pd.notna(val) else 0.0
        return feats

    def _make_label(self, current_close: float, prev_close: float) -> int:
        if prev_close <= 0:
            return 0
        change_pct = (current_close - prev_close) / prev_close * 100
        if change_pct >= 0.2:
            return 1
        if change_pct <= -0.2:
            return -1
        return 0

    def update(self, df: pd.DataFrame) -> int:
        if self.model is None or len(df) < 3:
            return 0
        try:
            row        = df.iloc[-1]
            prev_close = float(df.iloc[-2]["close"])
            curr_close = float(row["close"])

            x = self._extract_features(row)
            self.scaler.learn_one(x)
            x_scaled = self.scaler.transform_one(x)

            signal = self.model.predict_one(x_scaled) if self.n_seen >= 50 else 0
            if signal is None:
                signal = 0

            if len(df) >= 3:
                prev_row     = df.iloc[-2]
                prev_x       = self._extract_features(prev_row)
                prev_x_sc    = self.scaler.transform_one(prev_x)
                label        = self._make_label(curr_close, prev_close)
                self.model.learn_one(prev_x_sc, label)
                self.metric.update(label, self.model.predict_one(prev_x_sc) or 0)

            self.drift.update(float(curr_close))
            if self.drift.drift_detected:
                logger.info("[%s] ADWIN drift detected — resetting River model", self.symbol)
                self.model  = tree.HoeffdingTreeClassifier(
                    grace_period=50, delta=1e-5, tau=0.05, leaf_prediction="nba",
                )
                self.n_seen = 0

            self.n_seen     += 1
            self.last_signal = int(signal)
            self._save_signal(signal, row)
            return int(signal)

        except Exception as exc:
            logger.warning("[%s] River update error: %s", self.symbol, exc)
            return 0

    def _save_signal(self, signal: int, row: pd.Series) -> None:
        import json
        accuracy = None
        if self.n_seen >= 50:
            try:
                accuracy = round(self.metric.get(), 4)
            except Exception:
                pass
        data = {
            "model":     "river_hoeffding",
            "symbol":    self.symbol,
            "tf":        "1m",
            "signal":    int(signal),
            "accuracy":  accuracy,
            "n_seen":    self.n_seen,
            "timestamp": str(row.get("timestamp", "")),
        }
        filepath = os.path.join(self.signals_dir, f"{self.symbol.lower()}_river.json")
        tmp = filepath + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, filepath)


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
                    # Binance's websocket can go silently dead (TCP connection
                    # drops without a close frame) — plain `await s.recv()` then
                    # hangs forever with no exception, so the reconnect/backoff
                    # logic below never triggers. Reproduced live: connected once
                    # and then produced zero further log lines (not even the
                    # "Warming up" line that fires on every processed message)
                    # for 19+ hours straight. A kline stream should push updates
                    # every few seconds for an active pair, so treat a minute of
                    # silence as a dead connection and force a reconnect.
                    msg = await asyncio.wait_for(s.recv(), timeout=60)
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
                            if river_signal != 0:
                                logger.info(
                                    "🤖 [%s] River signal: %s (n_seen=%d)",
                                    symbol,
                                    "BUY" if river_signal == 1 else "SELL",
                                    river_model.n_seen,
                                )
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
            if isinstance(exc, asyncio.TimeoutError):
                logger.warning(
                    "⏱️ [%s] No message received in 60s — connection likely dead, reconnecting (attempt %d)",
                    symbol, reconnect_count,
                )
            else:
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
            symbols_info = {}
            for sym in SYMBOLS:
                fp = _filepath_1m(sym)
                symbols_info[sym] = {
                    "exists":        os.path.exists(fp),
                    "size_kb":       round(os.path.getsize(fp) / 1024, 1) if os.path.exists(fp) else 0,
                    "last_modified": os.path.getmtime(fp) if os.path.exists(fp) else None,
                }
                # River signal info
                river_fp = os.path.join(DATA_DIR, "signals_river", f"{sym.lower()}_river.json")
                if os.path.exists(river_fp):
                    try:
                        with open(river_fp) as f:
                            rd = _json.load(f)
                        symbols_info[sym]["river_signal"]   = rd.get("signal")
                        symbols_info[sym]["river_accuracy"] = rd.get("accuracy")
                        symbols_info[sym]["river_n_seen"]   = rd.get("n_seen", 0)
                    except Exception:
                        pass
            body = _json.dumps({
                "status":      "ok",
                "symbols":     symbols_info,
                "data_dir":    DATA_DIR,
                "roll_window": ROLL_WINDOW,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # Allow .csv and .json files, including from signals/ and signals_river/ subdirs
        import pathlib
        p = pathlib.PurePosixPath(self.path.split("?")[0])
        allowed_dirs = {"", ".", "signals", "signals_river"}
        parts = [part for part in str(p).strip("/").split("/") if part]
        if len(parts) > 2:
            self.send_error(403, "Path depth not allowed")
            return
        if p.suffix not in (".csv", ".json"):
            if p.name not in ("", "."):
                self.send_error(403, "Only CSV and JSON files are served")
                return
        if len(parts) == 2 and parts[0] not in allowed_dirs:
            self.send_error(403, "Directory not allowed")
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
