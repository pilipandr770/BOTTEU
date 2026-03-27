"""
Level 1 — Multi-Timeframe Scanner.

Collects OHLCV data on multiple timeframes, runs every algorithm on each,
and performs quick backtests. Produces a structured dict for the AI Advisor.

Key notes:
- yfinance does NOT support "4h" interval → we fetch "1h" and resample.
- Ticker fallback: BTC-USDT → BTC-USD if Yahoo Finance lacks the USDT pair.
- Uses explicit start/end dates (more reliable than period= in yfinance 2.x).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

from app.algorithms.base import get_algorithm

logger = logging.getLogger(__name__)

TIMEFRAMES = ["5m", "15m", "1h", "4h", "1d"]
ALGORITHM_KEYS = ["ma_crossover", "rsi", "macd", "supertrend", "bb_bounce"]

# yfinance config per display-timeframe.
# yfinance does NOT support "4h" natively → fetch "1h" + resample.
# days_back: history window (balance between backtest quality and speed).
_TF_CONFIG: dict[str, dict] = {
    "5m":  {"yf_interval": "5m",  "days_back": 7,   "resample": None},
    "15m": {"yf_interval": "15m", "days_back": 30,  "resample": None},
    "1h":  {"yf_interval": "1h",  "days_back": 180, "resample": None},
    "4h":  {"yf_interval": "1h",  "days_back": 365, "resample": "4h"},
    "1d":  {"yf_interval": "1d",  "days_back": 730, "resample": None},
}

MIN_CANDLES = 20

# Default params per algorithm (sensible defaults)
DEFAULT_PARAMS: dict[str, dict] = {
    "ma_crossover": {"fast_ma": 7, "slow_ma": 25, "stop_loss_pct": 2.0, "take_profit_pct": 4.0},
    "rsi":          {"rsi_period": 14, "oversold": 30, "overbought": 70, "stop_loss_pct": 2.0, "take_profit_pct": 4.0},
    "macd":         {"macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "stop_loss_pct": 2.0, "take_profit_pct": 4.0},
    "supertrend":   {"st_period": 10, "st_multiplier": 3.0, "stop_loss_pct": 2.0, "take_profit_pct": 4.0},
    "bb_bounce":    {"bb_period": 20, "bb_std": 2.0, "bb_exit": "middle", "stop_loss_pct": 2.0, "take_profit_pct": 4.0},
}

# Param variations to test during quick-backtest
PARAM_VARIANTS: dict[str, list[dict]] = {
    "ma_crossover": [
        {"fast_ma": 5, "slow_ma": 20, "stop_loss_pct": 1.5, "take_profit_pct": 3.0},
        {"fast_ma": 7, "slow_ma": 25, "stop_loss_pct": 2.0, "take_profit_pct": 4.0},
        {"fast_ma": 10, "slow_ma": 30, "stop_loss_pct": 2.5, "take_profit_pct": 5.0},
    ],
    "rsi": [
        {"rsi_period": 10, "oversold": 25, "overbought": 75, "stop_loss_pct": 1.5, "take_profit_pct": 3.0},
        {"rsi_period": 14, "oversold": 30, "overbought": 70, "stop_loss_pct": 2.0, "take_profit_pct": 4.0},
        {"rsi_period": 21, "oversold": 35, "overbought": 65, "stop_loss_pct": 2.5, "take_profit_pct": 5.0},
    ],
    "macd": [
        {"macd_fast": 8, "macd_slow": 21, "macd_signal": 5, "stop_loss_pct": 1.5, "take_profit_pct": 3.0},
        {"macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "stop_loss_pct": 2.0, "take_profit_pct": 4.0},
        {"macd_fast": 16, "macd_slow": 36, "macd_signal": 12, "stop_loss_pct": 3.0, "take_profit_pct": 6.0},
    ],
    "supertrend": [
        {"st_period": 7, "st_multiplier": 2.0, "stop_loss_pct": 1.5, "take_profit_pct": 3.0},
        {"st_period": 10, "st_multiplier": 3.0, "stop_loss_pct": 2.0, "take_profit_pct": 4.0},
        {"st_period": 14, "st_multiplier": 4.0, "stop_loss_pct": 3.0, "take_profit_pct": 6.0},
    ],
    "bb_bounce": [
        {"bb_period": 15, "bb_std": 1.5, "bb_exit": "middle", "stop_loss_pct": 1.5, "take_profit_pct": 3.0},
        {"bb_period": 20, "bb_std": 2.0, "bb_exit": "middle", "stop_loss_pct": 2.0, "take_profit_pct": 4.0},
        {"bb_period": 25, "bb_std": 2.5, "bb_exit": "upper", "stop_loss_pct": 2.5, "take_profit_pct": 5.0},
    ],
}

FEE_RATE = 0.001  # 0.1%


def _to_yahoo_ticker(symbol: str) -> str:
    """Convert Binance symbol to Yahoo Finance ticker (e.g. BTCUSDT → BTC-USDT)."""
    symbol = symbol.upper()
    for quote in ("USDT", "BTC", "ETH", "BNB", "BUSD"):
        if symbol.endswith(quote):
            base = symbol[: -len(quote)]
            return f"{base}-{quote}"
    return symbol


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame | None:
    """Normalize yfinance output to standard lowercase OHLCV DataFrame."""
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0].lower() for col in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    if "adj close" in df.columns:
        df = df.rename(columns={"adj close": "close"})
    required = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    if "close" not in required:
        return None
    df = df[required].dropna().reset_index()
    date_col = df.columns[0]
    df = df.rename(columns={date_col: "date"})
    return df if not df.empty else None


def _download_yf(ticker: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame | None:
    """Single yfinance download attempt — returns normalized df or None."""
    try:
        raw = yf.download(
            ticker,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            interval=interval,
            progress=False,
            auto_adjust=True,
        )
        return _normalize_df(raw)
    except Exception as exc:
        logger.debug("yfinance error %s %s: %s", ticker, interval, exc)
        return None


def _resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample an OHLCV DataFrame to a higher timeframe (e.g. 1h → 4h)."""
    tmp = df.copy()
    tmp["date"] = pd.to_datetime(tmp["date"])
    tmp = tmp.set_index("date")
    agg: dict = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in tmp.columns:
        agg["volume"] = "sum"
    return tmp.resample(rule).agg(agg).dropna().reset_index()


def _fetch_ohlcv(symbol: str, tf: str) -> pd.DataFrame | None:
    """
    Fetch OHLCV for a display timeframe.
    Handles 4h via 1h resample and USDT→USD ticker fallback.
    """
    cfg = _TF_CONFIG[tf]
    yf_interval = cfg["yf_interval"]
    days_back = cfg["days_back"]
    resample = cfg.get("resample")

    end = datetime.utcnow()
    start = end - timedelta(days=days_back)

    primary = _to_yahoo_ticker(symbol)
    df = _download_yf(primary, yf_interval, start, end)

    # Fallback: -USDT → -USD (Yahoo Finance has BTC-USD more reliably than BTC-USDT)
    if df is None or len(df) < MIN_CANDLES:
        alt = primary.replace("-USDT", "-USD").replace("-BUSD", "-USD")
        if alt != primary:
            logger.info("Ticker fallback: %s → %s (%s %s)", primary, alt, symbol, tf)
            df2 = _download_yf(alt, yf_interval, start, end)
            if df2 is not None and len(df2) > (len(df) if df is not None else 0):
                df = df2

    if df is None or len(df) < MIN_CANDLES:
        got = len(df) if df is not None else 0
        logger.warning("Insufficient data: %s %s — got %d candles (need %d)", symbol, tf, got, MIN_CANDLES)
        return None

    # Resample if needed (e.g. 1h → 4h)
    if resample:
        try:
            df = _resample_ohlcv(df, resample)
        except Exception as exc:
            logger.warning("Resample failed %s %s→%s: %s", symbol, yf_interval, resample, exc)
            return None

    return df if len(df) >= MIN_CANDLES else None


def _quick_backtest(df: pd.DataFrame, algorithm_key: str, params: dict) -> dict:
    """
    Run a fast backtest over the given DataFrame.
    Returns stats dict with trades, win_rate, return_pct, max_drawdown_pct, sharpe.
    """
    try:
        strategy = get_algorithm(algorithm_key)
    except ValueError:
        return {"error": f"Unknown algorithm: {algorithm_key}"}

    if hasattr(strategy, "precompute"):
        df = strategy.precompute(df.copy(), params)

    state: dict = {}
    equity = 1000.0
    initial = equity
    has_position = False
    entry_price = 0.0
    trades_count = 0
    wins = 0
    equity_peak = equity
    max_dd = 0.0

    for i in range(len(df)):
        window = df.iloc[: i + 1]
        try:
            signal, state = strategy.generate_signal(window.copy(), state, params)
        except Exception:
            continue

        price = float(df["close"].iloc[i])

        if signal == "BUY" and not has_position:
            has_position = True
            entry_price = price
        elif signal == "SELL" and has_position:
            has_position = False
            pnl_pct = (price - entry_price) / entry_price - FEE_RATE * 2
            equity *= (1 + pnl_pct)
            trades_count += 1
            if pnl_pct > 0:
                wins += 1
            equity_peak = max(equity_peak, equity)
            dd = (equity_peak - equity) / equity_peak * 100
            max_dd = max(max_dd, dd)

    total_return = (equity - initial) / initial * 100
    win_rate = (wins / trades_count * 100) if trades_count > 0 else 0

    # Sharpe approximation (simple)
    sharpe = 0.0
    if trades_count >= 2 and total_return != 0:
        avg_return = total_return / trades_count
        sharpe = avg_return / max(max_dd, 1.0)

    return {
        "trades": trades_count,
        "win_rate": round(win_rate, 1),
        "return_pct": round(total_return, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe": round(sharpe, 3),
    }


def _current_signal(df: pd.DataFrame, algorithm_key: str, params: dict) -> str:
    """Get current signal from algorithm on full dataframe."""
    try:
        strategy = get_algorithm(algorithm_key)
        if hasattr(strategy, "precompute"):
            df = strategy.precompute(df.copy(), params)
        signal, _ = strategy.generate_signal(df.copy(), {}, params)
        return signal
    except Exception as exc:
        logger.debug("Signal generation failed for %s: %s", algorithm_key, exc)
        return "HOLD"


def _compute_market_indicators(df: pd.DataFrame) -> dict:
    """Compute basic market indicators from OHLCV data."""
    if df is None or len(df) < 20:
        return {}
    closes = df["close"].astype(float)
    highs = df["high"].astype(float)
    lows = df["low"].astype(float)

    # Volatility (ATR%)
    tr = pd.concat([
        highs - lows,
        (highs - closes.shift(1)).abs(),
        (lows - closes.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr14 = tr.rolling(14).mean().iloc[-1]
    atr_pct = (atr14 / closes.iloc[-1]) * 100

    # Trend direction (SMA20 vs SMA50)
    sma20 = closes.rolling(20).mean().iloc[-1]
    sma50 = closes.rolling(50).mean().iloc[-1] if len(closes) >= 50 else sma20
    price = closes.iloc[-1]

    # RSI
    delta = closes.diff()
    gain = delta.clip(lower=0).ewm(span=14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(span=14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi_val = rsi.iloc[-1]

    # Bollinger Band Width
    bb_sma = closes.rolling(20).mean()
    bb_std = closes.rolling(20).std()
    bb_upper = bb_sma + 2 * bb_std
    bb_lower = bb_sma - 2 * bb_std
    bbw = ((bb_upper - bb_lower) / bb_sma * 100).iloc[-1]

    # Price changes
    pct_1d = ((price - closes.iloc[-2]) / closes.iloc[-2] * 100) if len(closes) >= 2 else 0
    pct_7d = ((price - closes.iloc[-8]) / closes.iloc[-8] * 100) if len(closes) >= 8 else 0

    return {
        "price": round(float(price), 2),
        "atr_pct": round(float(atr_pct), 3),
        "sma20": round(float(sma20), 2),
        "sma50": round(float(sma50), 2),
        "rsi": round(float(rsi_val), 1),
        "bbw_pct": round(float(bbw), 2),
        "price_above_sma20": bool(price > sma20),
        "price_above_sma50": bool(price > sma50),
        "sma20_above_sma50": bool(sma20 > sma50),
        "pct_change_1d": round(float(pct_1d), 2),
        "pct_change_7d": round(float(pct_7d), 2),
    }


def scan_symbol(symbol: str) -> dict[str, Any]:
    """
    Main scanner entry point.
    Returns a comprehensive dict with signals, backtests, and market indicators
    across all timeframes and algorithms.
    """
    result: dict[str, Any] = {
        "symbol": symbol.upper(),
        "timeframes": {},
        "best_combinations": [],
    }

    all_combos: list[dict] = []

    for tf in TIMEFRAMES:
        df = _fetch_ohlcv(symbol, tf)
        if df is None:
            result["timeframes"][tf] = {"error": f"No data available for {tf}"}
            continue

        tf_result: dict[str, Any] = {
            "candles": len(df),
            "market": _compute_market_indicators(df),
            "signals": {},
            "backtests": {},
        }

        for algo_key in ALGORITHM_KEYS:
            params = DEFAULT_PARAMS[algo_key]

            # Current signal
            sig = _current_signal(df, algo_key, params)
            tf_result["signals"][algo_key] = sig

            # Quick backtest with default params
            bt = _quick_backtest(df, algo_key, params)
            tf_result["backtests"][algo_key] = {"default": bt}

            # Test param variants
            variants = PARAM_VARIANTS.get(algo_key, [])
            for idx, var_params in enumerate(variants):
                var_bt = _quick_backtest(df, algo_key, var_params)
                tf_result["backtests"][algo_key][f"variant_{idx}"] = var_bt

                all_combos.append({
                    "algorithm": algo_key,
                    "timeframe": tf,
                    "params": var_params,
                    "variant": f"variant_{idx}",
                    **var_bt,
                })

            # Add default to combos too
            all_combos.append({
                "algorithm": algo_key,
                "timeframe": tf,
                "params": params,
                "variant": "default",
                **bt,
            })

        result["timeframes"][tf] = tf_result

    # Rank combinations by a composite score
    for combo in all_combos:
        if "error" in combo or combo.get("trades", 0) < 2:
            combo["score"] = -999
            continue
        # Score: weighted mix of return, win_rate, drawdown, sharpe
        combo["score"] = (
            combo.get("return_pct", 0) * 0.3
            + combo.get("win_rate", 0) * 0.3
            - combo.get("max_drawdown_pct", 0) * 0.2
            + combo.get("sharpe", 0) * 20 * 0.2
        )

    all_combos.sort(key=lambda x: x.get("score", -999), reverse=True)
    result["best_combinations"] = [c for c in all_combos if c.get("score", -999) > -999][:10]

    return result
