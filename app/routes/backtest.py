"""
Backtesting blueprint.
Uses yfinance (works from Germany — bypasses Binance geo-restriction).
Runs selected algorithm on historical OHLCV, returns Plotly chart + stats.
"""
import json
import logging
from datetime import datetime

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import yfinance as yf
from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required, current_user

from app.algorithms.base import list_algorithms, get_algorithm
from app.extensions import limiter, csrf

logger = logging.getLogger(__name__)

backtest_bp = Blueprint("backtest", __name__, url_prefix="/backtest")

FEE_RATE = 0.001  # 0.1% Binance taker fee per side

# Binance symbol → Yahoo Finance ticker
def _to_yahoo_ticker(symbol: str) -> str:
    """Convert BTCUSDT → BTC-USDT for Yahoo Finance."""
    symbol = symbol.upper()
    for quote in ("USDT", "BTC", "ETH", "BNB", "BUSD"):
        if symbol.endswith(quote):
            base = symbol[: -len(quote)]
            return f"{base}-{quote}"
    return symbol  # fallback


@backtest_bp.route("/")
@login_required
def index():
    algorithms = list_algorithms()
    return render_template("backtest/index.html", algorithms=algorithms)


@backtest_bp.route("/run", methods=["POST"])
@login_required
@csrf.exempt
@limiter.limit("20 per hour")
def run():
    data = request.get_json(silent=True) or {}
    symbol = data.get("symbol", "BTC-USDT").strip().upper()
    start_date = data.get("start_date", "")
    end_date = data.get("end_date", "")
    algorithm_key = data.get("algorithm", "ma_crossover")
    params = data.get("params", {})
    interval = data.get("interval", "1d")
    if interval == "4h":
        return jsonify({"error": "4h is not supported by Yahoo Finance. Please use 1h or 1d instead."}), 400
    try:
        initial_capital = float(data.get("initial_capital", 1000))
        if initial_capital <= 0:
            initial_capital = 1000.0
    except (TypeError, ValueError):
        initial_capital = 1000.0
    try:
        fee_rate = float(data.get("fee_rate", 0.001))
        if fee_rate < 0 or fee_rate > 0.1:
            fee_rate = 0.001
    except (TypeError, ValueError):
        fee_rate = 0.001
    try:
        slippage_pct = float(data.get("slippage_pct", 0.05))
        if slippage_pct < 0 or slippage_pct > 5:
            slippage_pct = 0.05
    except (TypeError, ValueError):
        slippage_pct = 0.05

    # ── Fetch historical data ─────────────────────────────────────────────
    yahoo_ticker = _to_yahoo_ticker(symbol)
    try:
        df = yf.download(
            yahoo_ticker,
            start=start_date or None,
            end=end_date or None,
            interval=interval,
            progress=False,
            auto_adjust=True,
        )
    except Exception as exc:
        return jsonify({"error": f"Failed to download data: {exc}"}), 500

    if df is None or df.empty:
        return jsonify({"error": f"No data found for {yahoo_ticker}. Check the symbol or date range."}), 404

    # ── Normalize columns (yfinance 0.2.x returns MultiIndex) ────────────
    if isinstance(df.columns, pd.MultiIndex):
        # ('Close', 'BTC-USD') → 'close'
        df.columns = [col[0].lower() for col in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]

    # auto_adjust=True: column is 'close', not 'adj close'
    if "adj close" in df.columns:
        df = df.rename(columns={"adj close": "close"})

    required = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[required].dropna().reset_index()
    # Rename date index regardless of DatetimeIndex name
    date_col = df.columns[0]
    df = df.rename(columns={date_col: "date"})

    # ── Run algorithm ─────────────────────────────────────────────────────
    try:
        strategy = get_algorithm(algorithm_key)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # Pre-compute indicators once (O(n)) to avoid O(n²) per-candle recalculation
    if hasattr(strategy, "precompute"):
        df = strategy.precompute(df, params)

    state: dict = {}
    trades = []
    equity = initial_capital
    total_fees_usdt = 0.0
    equity_curve = []
    has_position = False
    entry_price = 0.0
    entry_idx = 0

    try:
      for i in range(len(df)):
        window = df.iloc[: i + 1]
        signal, state = strategy.generate_signal(window.copy(), state, params)

        current_close = float(df["close"].iloc[i])
        date_val = str(df.iloc[i].get("date", i))[:10]

        if signal == "BUY" and not has_position:
            has_position = True
            entry_price = current_close * (1 + slippage_pct / 100)  # slippage: buy slightly higher
            entry_idx = i
            trades.append({
                "type": "BUY",
                "date": date_val,
                "price": round(entry_price, 6),
                "idx": i,
            })

        elif signal == "SELL" and has_position:
            has_position = False
            exit_price = current_close * (1 - slippage_pct / 100)  # slippage: sell slightly lower
            pnl_pct = (exit_price - entry_price) / entry_price * 100
            # Round-trip fee: fee_rate% buy + fee_rate% sell
            fee_pct = fee_rate * 2 * 100
            pnl_pct_net = pnl_pct - fee_pct
            trade_value = equity  # full position value before this trade
            fee_usdt = trade_value * fee_rate * 2
            total_fees_usdt += fee_usdt
            equity *= 1 + pnl_pct_net / 100
            reason = state.get("exit_reason", "SIGNAL")
            trades.append({
                "type": "SELL",
                "date": date_val,
                "price": round(exit_price, 6),
                "idx": i,
                "pnl_pct": round(pnl_pct_net, 2),
                "pnl_pct_gross": round(pnl_pct, 2),
                "fee_usdt": round(fee_usdt, 4),
                "reason": reason,
            })

        equity_curve.append({"date": date_val, "equity": round(equity, 2)})
    except Exception as exc:
        logger.exception("Backtest loop error at candle %d: %s", i, exc)
        return jsonify({"error": f"Backtest calculation error: {exc}"}), 500
    # ── Build Plotly chart ────────────────────────────────────────────────
    buy_dates = [t["date"] for t in trades if t["type"] == "BUY"]
    buy_prices = [t["price"] for t in trades if t["type"] == "BUY"]
    sell_dates = [t["date"] for t in trades if t["type"] == "SELL"]
    sell_prices = [t["price"] for t in trades if t["type"] == "SELL"]

    dates = df.get("date", df.get("index", pd.RangeIndex(len(df)))).astype(str).tolist()

    fig = go.Figure()

    # Candlestick
    fig.add_trace(go.Candlestick(
        x=dates,
        open=df["open"].tolist(),
        high=df["high"].tolist(),
        low=df["low"].tolist(),
        close=df["close"].tolist(),
        name="Price",
        increasing_line_color="#26a69a",
        decreasing_line_color="#ef5350",
    ))

    # BUY markers
    if buy_dates:
        fig.add_trace(go.Scatter(
            x=buy_dates, y=buy_prices,
            mode="markers",
            marker=dict(symbol="triangle-up", size=14, color="#00e676"),
            name="BUY",
        ))

    # SELL markers
    if sell_dates:
        fig.add_trace(go.Scatter(
            x=sell_dates, y=sell_prices,
            mode="markers",
            marker=dict(symbol="triangle-down", size=14, color="#ff1744"),
            name="SELL",
        ))

    fig.update_layout(
        template="plotly_dark",
        xaxis_rangeslider_visible=False,
        height=520,
        margin=dict(l=0, r=0, t=30, b=0),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )

    # ── Stats ─────────────────────────────────────────────────────────────
    sell_trades = [t for t in trades if t["type"] == "SELL"]
    wins = [t for t in sell_trades if t.get("pnl_pct", 0) > 0]
    win_rate = (len(wins) / len(sell_trades) * 100) if sell_trades else 0

    # Compound total return from equity curve
    total_return = (equity - initial_capital) / initial_capital * 100
    profit_usdt = equity - initial_capital

    # Exit reason breakdown
    exit_reasons: dict = {}
    for t in sell_trades:
        r = t.get("reason", "SIGNAL")
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    # Max drawdown
    eq_vals = [e["equity"] for e in equity_curve]
    max_dd = 0.0
    if eq_vals:
        peak = eq_vals[0]
        for v in eq_vals:
            peak = max(peak, v)
            dd = (peak - v) / peak * 100
            max_dd = max(max_dd, dd)

    # Sharpe & Sortino ratios (per-trade, risk-free rate = 0)
    returns = np.array([t["pnl_pct"] / 100 for t in sell_trades])
    sharpe = sortino = 0.0
    if len(returns) >= 2:
        std = np.std(returns, ddof=1)
        sharpe = float(np.mean(returns) / std) if std > 0 else 0.0
        downside = returns[returns < 0]
        d_std = np.std(downside, ddof=1) if len(downside) >= 2 else 0.0
        sortino = float(np.mean(returns) / d_std) if d_std > 0 else 0.0

    stats = {
        "total_trades": len(sell_trades),
        "win_rate": round(win_rate, 1),
        "total_return_pct": round(total_return, 2),
        "profit_usdt": round(profit_usdt, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "initial_capital": round(initial_capital, 2),
        "final_equity": round(equity, 2),
        "total_fees_usdt": round(total_fees_usdt, 2),
        "fee_rate_pct": round(fee_rate * 100, 4),
        "slippage_pct": round(slippage_pct, 4),
        "exit_reasons": exit_reasons,
        "sharpe_ratio": round(sharpe, 3),
        "sortino_ratio": round(sortino, 3),
    }

    return jsonify({
        "chart": fig.to_json(),
        "trades": trades[-50:],  # last 50 for table
        "stats": stats,
        "equity_curve": equity_curve[-200:],
    })


@backtest_bp.route("/walkforward", methods=["POST"])
@login_required
@csrf.exempt
@limiter.limit("10 per hour")
def walkforward():
    """
    Walk-forward cross-validation backtest using the ML ensemble.

    Accepts JSON:
        symbol    — e.g. "BTCUSDT"
        timeframe — e.g. "1h"
        n_splits  — number of TimeSeriesSplit folds (default: 5)

    Returns per-fold accuracy, PnL, and Sharpe ratio estimate.
    """
    from sklearn.model_selection import TimeSeriesSplit

    from app.ml.features import extract_features, extract_labels, get_tf_label_params
    from app.ml.ensemble import MLEnsemble
    from app.ml.trainer import ML_MODELS_DIR

    data = request.get_json(silent=True) or {}
    symbol    = data.get("symbol", "BTCUSDT").strip().upper()
    timeframe = data.get("timeframe", "1h").strip()
    n_splits  = max(2, min(int(data.get("n_splits", 5)), 10))

    # ── Locate collector CSV ──────────────────────────────────────────────
    import os as _os
    data_dir = _os.environ.get(
        "DATA_DIR",
        _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(__file__))), "data"),
    )
    csv_path = _os.path.join(data_dir, f"{symbol.lower()}_{timeframe}_clean.csv")

    if not _os.path.exists(csv_path):
        return jsonify({"error": f"Collector CSV not found: {csv_path}. Run the collector first."}), 404

    try:
        df = pd.read_csv(csv_path, parse_dates=["timestamp"])
    except Exception as exc:
        return jsonify({"error": f"CSV read error: {exc}"}), 500

    if len(df) < 200:
        return jsonify({"error": f"Not enough rows ({len(df)}) in CSV for walk-forward (need ≥ 200)."}), 400

    threshold_pct, forward_n = get_tf_label_params(timeframe)

    try:
        X_full = extract_features(df)
        y_full = extract_labels(df, forward_n=forward_n, threshold_pct=threshold_pct)
        X_full = X_full[:-forward_n]
        y_full = y_full[:-forward_n]
        finite_mask = np.isfinite(X_full).all(axis=1)
        X_full = X_full[finite_mask]
        y_full = y_full[finite_mask]
    except Exception as exc:
        return jsonify({"error": f"Feature extraction failed: {exc}"}), 500

    if len(X_full) < n_splits * 20:
        return jsonify({"error": f"Not enough clean samples ({len(X_full)}) for {n_splits} folds."}), 400

    tscv = TimeSeriesSplit(n_splits=n_splits)
    fold_results = []

    for fold_idx, (train_idx, test_idx) in enumerate(tscv.split(X_full)):
        X_train, X_test = X_full[train_idx], X_full[test_idx]
        y_train, y_test = y_full[train_idx], y_full[test_idx]

        if len(X_train) < 30 or len(X_test) < 5:
            continue

        ens = MLEnsemble(store_dir=ML_MODELS_DIR, key=f"_wf_{symbol.lower()}_{timeframe}_fold{fold_idx}")
        try:
            train_stats = ens.fit(X_train, y_train)
        except Exception as exc:
            fold_results.append({"fold": fold_idx + 1, "error": str(exc)})
            continue

        # ── Evaluate on test set ──
        correct = 0
        pnl_returns: list[float] = []
        for j in range(len(X_test)):
            majority, _, _ = ens.predict_one(X_test[j])
            actual = int(y_test[j])
            if majority == actual:
                correct += 1
            # PnL simulation: +1 label → assume +threshold_pct return, -1 → negative
            if majority != 0:
                ret = float(majority) * threshold_pct
                pnl_returns.append(ret)

        accuracy = correct / len(X_test) if X_test.size > 0 else 0.0
        total_pnl = sum(pnl_returns)

        arr = np.array(pnl_returns)
        sharpe = 0.0
        if len(arr) >= 2:
            std = float(np.std(arr, ddof=1))
            sharpe = float(np.mean(arr) / std) if std > 0 else 0.0

        fold_results.append({
            "fold":           fold_idx + 1,
            "train_samples":  len(X_train),
            "test_samples":   len(X_test),
            "accuracy":       round(accuracy, 4),
            "total_pnl_pct":  round(total_pnl, 4),
            "sharpe":         round(sharpe, 4),
            "label_dist":     train_stats.get("label_dist", {}),
        })

    if not fold_results:
        return jsonify({"error": "No valid folds produced."}), 500

    valid = [f for f in fold_results if "error" not in f]
    avg_acc    = round(float(np.mean([f["accuracy"]    for f in valid])), 4) if valid else 0.0
    avg_pnl    = round(float(np.mean([f["total_pnl_pct"] for f in valid])), 4) if valid else 0.0
    avg_sharpe = round(float(np.mean([f["sharpe"]      for f in valid])), 4) if valid else 0.0

    return jsonify({
        "symbol":         symbol,
        "timeframe":      timeframe,
        "n_splits":       n_splits,
        "total_samples":  len(X_full),
        "folds":          fold_results,
        "summary": {
            "avg_accuracy":    avg_acc,
            "avg_total_pnl_pct": avg_pnl,
            "avg_sharpe":      avg_sharpe,
        },
    })
