"""
Unified bot tick logic — single source of truth for APScheduler and Celery runners.

Fixes applied vs original per-runner implementations:
  P2: Closed-candle signals (df.iloc[:-1] before indicators)            [in algorithms]
  P4/P6: Decimal PnL + FEE_RATE deduction                               [here]
  P1: Exchange SL/OCO orders after BUY, cancel before SELL              [here]
  Bug 2: Verify actual asset balance before SELL                        [here]
  Bug 4: Adaptive tick — skip if candle not yet closed (next_tick_at)   [here]
       Aligns ticks to candle-close boundaries → a 1d bot ticks once/day
       instead of 1440×/day, saving API quota.
  Bug 5: Telegram via requests.post() — no asyncio.run() in threads     [notifier]
  Risk: check_before_buy() enforced before every BUY                    [here]
"""
import logging
import math
from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

TIMEFRAME_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h",
    "8h": "8h", "12h": "12h", "1d": "1d", "3d": "3d",
    "1w": "1w", "1mo": "1M",
}

# Seconds per candle — used for adaptive tick alignment
TIMEFRAME_SECONDS = {
    "1m": 60,      "5m": 300,     "15m": 900,    "30m": 1800,
    "1h": 3600,    "2h": 7200,    "4h": 14400,   "6h": 21600,
    "8h": 28800,   "12h": 43200,  "1d": 86400,   "3d": 259200,
    "1w": 604800,  "1mo": 2592000,
}

REAL_TRADE_MIN  = 5.0              # minimum free quote balance in USDT/USDC to allow real orders
FEE_RATE        = Decimal("0.001") # Binance taker fee: 0.1% per side → ~0.2% round-trip
SUPPORTED_QUOTES = ("USDT", "USDC", "BTC", "ETH", "BNB", "BUSD")


def _get_base_asset(symbol: str) -> str:
    """BTCUSDT → BTC, ETHUSDC → ETH, etc."""
    symbol = symbol.upper()
    for q in SUPPORTED_QUOTES:
        if symbol.endswith(q):
            return symbol[: -len(q)]
    return symbol[:-4]  # fallback


# ── Main entry point ────────────────────────────────────────────────────────

def tick_bot(bot_id: int) -> None:
    """
    Process one tick for a single bot.
    Must be called inside an active Flask application context.
    Called every 60 s by the scheduler; skips silently when the current
    candle hasn't closed yet (adaptive tick — Bug 4).
    """
    from app.extensions import db
    from app.models.bot import Bot, BotStatus
    from app.models.bot_log import BotLog
    from app.models.order import Order, OrderSide, ExitReason
    from app.algorithms.base import get_algorithm
    from app.services.binance_client import get_client_for_user, get_quote_free_balance
    from app.services.order_manager import (
        place_market_order, place_smart_order,
        place_stop_loss_order,
        place_oco_sell_order, cancel_open_orders,
    )
    from app.services.telegram_notifier import notify_buy, notify_sell, notify_error
    from app.models.telegram_account import TelegramAccount

    bot: Bot | None = db.session.get(Bot, bot_id)
    if not bot or bot.status != BotStatus.RUNNING:
        return

    # ── Bug 4: Adaptive tick ──────────────────────────────────────────────
    timeframe      = bot.params.get("timeframe", "1h")
    tick_interval  = TIMEFRAME_SECONDS.get(timeframe, 3600)
    now_ts         = datetime.now(timezone.utc).timestamp()
    state_current  = dict(bot.state or {})
    next_tick_at   = state_current.get("next_tick_at", 0)

    # Check for TradingView signal override (clears tick gate)
    tv_signal = state_current.pop("tv_signal", None)

    if now_ts < next_tick_at and not tv_signal:
        logger.debug(
            "Bot %d: skipping — next %s candle in %ds",
            bot_id, timeframe, int(next_tick_at - now_ts),
        )
        return

    _chat_id: int | None = None   # declared early so except-block can use it

    try:
        client   = get_client_for_user(bot.user_id)
        interval = TIMEFRAME_MAP.get(timeframe, "1h")
        klines   = client.get_klines(symbol=bot.symbol, interval=interval, limit=202)

        df = pd.DataFrame(klines, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore",
        ])
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)

        # ── Generate signal ───────────────────────────────────────────────
        if tv_signal in ("BUY", "SELL"):
            # TradingView override — bypass algorithm, use TV signal directly
            signal    = tv_signal
            new_state = dict(state_current)
            if signal == "BUY":
                new_state.update({
                    "has_position": True,
                    "entry_price":  float(df["close"].iloc[-1]),
                    "max_price":    float(df["close"].iloc[-1]),
                    "exit_reason":  None,
                    "tp_trailing_active": False,
                })
            else:
                new_state.update({"has_position": False, "exit_reason": "SIGNAL"})
            new_state["_log"] = [(signal, f"📡 TradingView webhook signal: {signal}")]
        else:
            strategy  = get_algorithm(bot.algorithm)
            state     = dict(state_current)
            signal, new_state = strategy.generate_signal(df, state, bot.params)

        # ── Advance next-tick boundary (aligns to candle close) ───────────
        next_boundary = math.ceil(now_ts / tick_interval) * tick_interval
        new_state["next_tick_at"] = next_boundary

        # ── Balance check ─────────────────────────────────────────────────
        free_balance  = get_quote_free_balance(client, bot.symbol)
        position_size = float(bot.position_size_usdt)
        simulate      = free_balance < max(REAL_TRADE_MIN, position_size)

        # ── Flush logs ────────────────────────────────────────────────────
        log_entries: list[tuple[str, str]] = new_state.pop("_log", [])
        if simulate:
            log_entries.insert(0, ("INFO",
                f"🧪 [DEMO] Balance {free_balance:.4f} USDT < required {position_size:.2f} USDT — "
                f"demo mode (no real orders)"
            ))
        for level, msg in log_entries:
            db.session.add(BotLog(bot_id=bot.id, level=level, message=msg))

        # Trim to last 500 log entries
        oldest = (
            db.session.query(BotLog.id)
            .filter(BotLog.bot_id == bot.id)
            .order_by(BotLog.id.desc())
            .offset(499).limit(1).scalar()
        )
        if oldest:
            BotLog.query.filter(
                BotLog.bot_id == bot.id, BotLog.id < oldest
            ).delete(synchronize_session=False)

        current_price = Decimal(str(df["close"].iloc[-1]))

        # ── Telegram chat_id ──────────────────────────────────────────────
        _tg      = TelegramAccount.query.filter_by(user_id=bot.user_id, is_verified=True).first()
        _chat_id = _tg.chat_id if _tg else None

        # ── Risk Manager gate before BUY ──────────────────────────────────
        if signal == "BUY" and not state_current.get("has_position"):
            try:
                from app.services.risk_manager import check_before_buy
                allowed, reason = check_before_buy(bot.user_id, bot.id)
                if not allowed:
                    db.session.add(BotLog(bot_id=bot.id, level="WARN",
                        message=f"🚫 BUY blocked by Risk Manager: {reason}"))
                    signal = "HOLD"
                    new_state["has_position"] = False
            except Exception as _rm_exc:
                logger.debug("Risk manager check skipped (not yet available): %s", _rm_exc)

        # ── BUY ───────────────────────────────────────────────────────────
        if signal == "BUY" and not state_current.get("has_position"):
            quote_amount = Decimal(str(bot.position_size_usdt))
            if simulate:
                exec_qty = quote_amount / current_price if current_price else Decimal("0")
                db.session.add(Order(
                    bot_id=bot.id, symbol=bot.symbol, side=OrderSide.BUY,
                    price=current_price, qty=exec_qty, quote_qty=quote_amount,
                    is_simulated=True,
                ))
                db.session.add(BotLog(bot_id=bot.id, level="BUY",
                    message=(
                        f"🧪 [DEMO] Buy {bot.symbol}: price {float(current_price):.6f}, "
                        f"qty {float(exec_qty):.6f}"
                    )
                ))
                if _chat_id:
                    notify_buy(_chat_id, bot.symbol, float(current_price),
                               float(exec_qty), f"🧪 {bot.name} [DEMO]")
            else:
                use_limit = bot.params.get("order_type", "smart") != "market"
                resp      = place_smart_order(
                    client, bot.symbol, "BUY",
                    quote_amount=quote_amount, use_limit=use_limit,
                )
                exec_qty  = Decimal(resp.get("executedQty", "0"))
                exec_price = (
                    Decimal(resp.get("cummulativeQuoteQty", "0")) / exec_qty
                    if exec_qty else Decimal("0")
                )
                db.session.add(Order(
                    bot_id=bot.id,
                    binance_order_id=str(resp.get("orderId")),
                    symbol=bot.symbol, side=OrderSide.BUY,
                    price=exec_price, qty=exec_qty,
                    quote_qty=Decimal(resp.get("cummulativeQuoteQty", "0")),
                    is_simulated=False,
                ))
                if _chat_id:
                    notify_buy(_chat_id, bot.symbol, float(exec_price), float(exec_qty), bot.name)

                # P1: Place exchange SL/OCO so position is protected even if server dies
                sl_pct_p = bot.params.get("stop_loss_pct")
                tp_pct_p = bot.params.get("take_profit_pct")
                if sl_pct_p and exec_price and exec_qty:
                    sl_price = exec_price * (1 - Decimal(str(sl_pct_p)) / 100)
                    try:
                        if tp_pct_p:
                            tp_price = exec_price * (1 + Decimal(str(tp_pct_p)) / 100)
                            oco_r = place_oco_sell_order(client, bot.symbol, exec_qty, sl_price, tp_price)
                            new_state["oco_order_list_id"] = str(oco_r.get("orderListId", ""))
                            db.session.add(BotLog(bot_id=bot.id, level="INFO",
                                message=f"🛡 OCO placed: SL {float(sl_price):.6f} / TP {float(tp_price):.6f}"))
                        else:
                            sl_r = place_stop_loss_order(client, bot.symbol, exec_qty, sl_price)
                            new_state["sl_order_id"] = str(sl_r.get("orderId", ""))
                            db.session.add(BotLog(bot_id=bot.id, level="INFO",
                                message=f"🛡 Stop-loss order placed at {float(sl_price):.6f}"))
                    except Exception as _e:
                        logger.warning("Exchange SL/OCO failed for bot %d: %s", bot.id, _e)
                        db.session.add(BotLog(bot_id=bot.id, level="WARN",
                            message=f"⚠️ Exchange SL/TP skipped: {_e}"))

        # ── SELL ──────────────────────────────────────────────────────────
        elif signal == "SELL" and state_current.get("has_position"):
            last_buy = (
                Order.query.filter_by(bot_id=bot.id, side=OrderSide.BUY)
                .order_by(Order.created_at.desc()).first()
            )
            if last_buy:
                exit_reason_str = new_state.get("exit_reason", "SIGNAL")
                exit_reason = (
                    ExitReason[exit_reason_str]
                    if exit_reason_str in ExitReason.__members__
                    else ExitReason.SIGNAL
                )

                if simulate:
                    exec_qty   = last_buy.qty
                    exec_price = current_price
                    buy_fee    = last_buy.price * exec_qty * FEE_RATE
                    sell_fee   = exec_price * exec_qty * FEE_RATE
                    pnl_usdt   = (
                        (exec_price - last_buy.price) * exec_qty - buy_fee - sell_fee
                        if last_buy.price else Decimal("0")
                    )
                    pnl_pct = (
                        float(pnl_usdt / (last_buy.price * exec_qty) * 100)
                        if last_buy.price and exec_qty else 0.0
                    )
                    db.session.add(Order(
                        bot_id=bot.id, symbol=bot.symbol, side=OrderSide.SELL,
                        price=exec_price, qty=exec_qty,
                        quote_qty=exec_qty * exec_price,
                        exit_reason=exit_reason,
                        pnl_usdt=pnl_usdt,
                        pnl_pct=Decimal(str(round(pnl_pct, 4))),
                        is_simulated=True,
                    ))
                    db.session.add(BotLog(bot_id=bot.id, level="SELL",
                        message=(
                            f"🧪 [DEMO] Sell {bot.symbol}: price {float(exec_price):.6f}, "
                            f"P&L {pnl_pct:+.2f}%"
                        )
                    ))
                    if _chat_id:
                        notify_sell(_chat_id, bot.symbol, float(exec_price), float(exec_qty),
                                    f"🧪 {bot.name} [DEMO]", exit_reason_str, pnl_pct)

                else:
                    # Bug 2: verify actual on-exchange balance before selling
                    base_asset = _get_base_asset(bot.symbol)
                    sell_qty   = last_buy.qty
                    try:
                        bal = client.get_asset_balance(asset=base_asset)
                        actual_free = Decimal(bal.get("free", "0") if bal else "0")
                        if actual_free > Decimal("0"):
                            sell_qty = min(last_buy.qty, actual_free)
                            if actual_free < last_buy.qty * Decimal("0.99"):
                                db.session.add(BotLog(bot_id=bot.id, level="WARN",
                                    message=(
                                        f"⚠️ Balance mismatch: expected {float(last_buy.qty):.6f} "
                                        f"{base_asset}, exchange shows {float(actual_free):.6f} — "
                                        f"selling available amount"
                                    )))
                    except Exception as _bal_exc:
                        logger.warning("Balance check failed for bot %d: %s", bot.id, _bal_exc)

                    # Cancel exchange SL/OCO orders before placing market SELL
                    if state_current.get("oco_order_list_id") or state_current.get("sl_order_id"):
                        cancel_open_orders(client, bot.symbol)

                    use_limit = bot.params.get("order_type", "smart") != "market"
                    resp       = place_smart_order(
                        client, bot.symbol, "SELL",
                        quantity=sell_qty, use_limit=use_limit,
                    )
                    exec_qty   = Decimal(resp.get("executedQty", "0"))
                    exec_price = (
                        Decimal(resp.get("cummulativeQuoteQty", "0")) / exec_qty
                        if exec_qty else Decimal("0")
                    )
                    buy_fee  = last_buy.price * exec_qty * FEE_RATE
                    sell_fee = exec_price * exec_qty * FEE_RATE
                    pnl_usdt = (
                        (exec_price - last_buy.price) * exec_qty - buy_fee - sell_fee
                        if last_buy.price else Decimal("0")
                    )
                    pnl_pct = (
                        float(pnl_usdt / (last_buy.price * exec_qty) * 100)
                        if last_buy.price and exec_qty else 0.0
                    )
                    db.session.add(Order(
                        bot_id=bot.id,
                        binance_order_id=str(resp.get("orderId")),
                        symbol=bot.symbol, side=OrderSide.SELL,
                        price=exec_price, qty=exec_qty,
                        quote_qty=Decimal(resp.get("cummulativeQuoteQty", "0")),
                        exit_reason=exit_reason,
                        pnl_usdt=pnl_usdt,
                        pnl_pct=Decimal(str(round(pnl_pct, 4))),
                        is_simulated=False,
                    ))
                    if _chat_id:
                        notify_sell(_chat_id, bot.symbol, float(exec_price), float(exec_qty),
                                    bot.name, exit_reason_str, pnl_pct)

        bot.state = new_state
        db.session.commit()
        logger.debug("Bot %d tick: signal=%s simulate=%s", bot.id, signal, simulate)

    except Exception as exc:
        logger.exception("Tick error for bot %d: %s", bot_id, exc)
        try:
            from app.extensions import db
            from app.models.bot import Bot, BotStatus
            from app.models.bot_log import BotLog
            bot = db.session.get(Bot, bot_id)
            if bot:
                bot.status        = BotStatus.ERROR
                bot.error_message = str(exc)[:500]
                db.session.add(BotLog(
                    bot_id=bot_id,
                    level="ERROR",
                    message=f"❌ Tick error: {str(exc)[:400]}",
                ))
                db.session.commit()
            if _chat_id:
                from app.services.telegram_notifier import notify_error
                notify_error(_chat_id, bot.name, str(exc))
        except Exception:
            db.session.rollback()
