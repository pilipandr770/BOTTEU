"""
Bot Runner — core Celery task.

Execution flow per bot tick:
    1. Load bot from DB
    2. Fetch OHLCV from Binance WebSocket / REST
    3. Run algorithm → signal
    4. If BUY/SELL → place market order via OrderManager
    5. Record order in DB
    6. Send Telegram notification
    7. Persist updated bot state
"""
import logging
from decimal import Decimal

import pandas as pd
from binance.exceptions import BinanceAPIException

from app.workers.celery_app import celery_app
from app.extensions import db
from app.models.bot import Bot, BotStatus
from app.models.bot_log import BotLog
from app.models.order import Order, OrderSide, ExitReason
from app.models.telegram_account import TelegramAccount
from app.algorithms.base import get_algorithm
from app.services.binance_client import get_client_for_user, get_quote_free_balance
from app.services.order_manager import place_market_order
from app.services import notify_buy, notify_sell, notify_error

logger = logging.getLogger(__name__)

# Binance kline interval mapping
TIMEFRAME_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m",
    "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1d",
}

# Minimum free balance (in quote currency) required for real trading
REAL_TRADE_MIN_BALANCE = 5.0  # USDT / USDC


def _fetch_ohlcv(client, symbol: str, timeframe: str, limit: int = 200) -> pd.DataFrame:
    interval = TIMEFRAME_MAP.get(timeframe, "1h")
    klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(klines, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    return df


def _get_chat_id(user_id: int) -> int | None:
    tg = TelegramAccount.query.filter_by(user_id=user_id, is_verified=True).first()
    return tg.chat_id if tg else None


def _write_logs(bot_id: int, entries: list[tuple[str, str]]) -> None:
    """Persist log entries and trim to last 500."""
    for level, msg in entries:
        db.session.add(BotLog(bot_id=bot_id, level=level, message=msg))
    oldest_keep_id = (
        db.session.query(BotLog.id)
        .filter(BotLog.bot_id == bot_id)
        .order_by(BotLog.id.desc())
        .offset(499)
        .limit(1)
        .scalar()
    )
    if oldest_keep_id:
        BotLog.query.filter(
            BotLog.bot_id == bot_id,
            BotLog.id < oldest_keep_id,
        ).delete(synchronize_session=False)


@celery_app.task(name="app.workers.bot_runner.run_bot", bind=True, max_retries=3)
def run_bot(self, bot_id: int) -> None:
    bot: Bot | None = db.session.get(Bot, bot_id)
    if not bot or bot.status != BotStatus.RUNNING:
        return

    chat_id = _get_chat_id(bot.user_id)

    try:
        client = get_client_for_user(bot.user_id)
        timeframe = bot.params.get("timeframe", "1h")
        df = _fetch_ohlcv(client, bot.symbol, timeframe)

        strategy = get_algorithm(bot.algorithm)
        state = bot.state or {}
        signal, new_state = strategy.generate_signal(df, state, bot.params)

        # ── Check balance → decide real vs simulation ─────────────────────
        free_balance = get_quote_free_balance(client, bot.symbol)
        position_size = float(bot.position_size_usdt)
        simulate = free_balance < max(REAL_TRADE_MIN_BALANCE, position_size)

        # ── Write human-readable log entries ─────────────────────────────
        log_entries = new_state.pop("_log", [])

        # Prefix simulation notice on first log entry of a tick
        if simulate and log_entries:
            log_entries.insert(0, ("INFO",
                f"🧪 [DEMO] Balance {free_balance:.2f} < {position_size:.2f} — demo mode (no real orders)"
            ))

        _write_logs(bot.id, log_entries)

        # ── BUY ───────────────────────────────────────────────────────────
        if signal == "BUY" and not state.get("has_position", False):
            quote_amount = Decimal(str(bot.position_size_usdt))
            current_price = Decimal(str(float(df["close"].iloc[-1])))

            if simulate:
                # Paper trade — no real order placed
                exec_price = current_price
                exec_qty   = quote_amount / exec_price if exec_price else Decimal("0")
                order = Order(
                    bot_id=bot.id,
                    symbol=bot.symbol,
                    side=OrderSide.BUY,
                    price=exec_price,
                    qty=exec_qty,
                    quote_qty=quote_amount,
                    is_simulated=True,
                )
                db.session.add(order)
                db.session.add(BotLog(bot_id=bot.id, level="BUY",
                    message=f"🧪 [DEMO] Buy {bot.symbol}: price {float(exec_price):.6f}, qty {float(exec_qty):.6f}"))
            else:
                response = place_market_order(client, bot.symbol, "BUY", quote_amount=quote_amount)
                exec_qty = Decimal(response.get("executedQty", "0"))
                exec_price = (
                    Decimal(response.get("cummulativeQuoteQty", "0")) / exec_qty
                    if exec_qty else Decimal("0")
                )
                order = Order(
                    bot_id=bot.id,
                    binance_order_id=str(response.get("orderId")),
                    symbol=bot.symbol,
                    side=OrderSide.BUY,
                    price=exec_price,
                    qty=exec_qty,
                    quote_qty=Decimal(response.get("cummulativeQuoteQty", "0")),
                    is_simulated=False,
                )
                db.session.add(order)
                if chat_id:
                    notify_buy(chat_id, bot.symbol, float(exec_price), float(exec_qty), bot.name)

        # ── SELL ──────────────────────────────────────────────────────────
        elif signal == "SELL" and state.get("has_position", False):
            last_buy = (
                Order.query
                .filter_by(bot_id=bot.id, side=OrderSide.BUY)
                .order_by(Order.created_at.desc())
                .first()
            )
            if last_buy:
                current_price = Decimal(str(float(df["close"].iloc[-1])))
                exit_reason_str = new_state.get("exit_reason", "SIGNAL")
                exit_reason = ExitReason[exit_reason_str] if exit_reason_str in ExitReason.__members__ else ExitReason.SIGNAL

                if simulate:
                    exec_price = current_price
                    exec_qty   = last_buy.qty
                    pnl_usdt   = (exec_price - last_buy.price) * exec_qty if last_buy.price else Decimal("0")
                    pnl_pct    = float((exec_price - last_buy.price) / last_buy.price * 100) if last_buy.price else 0.0
                    order = Order(
                        bot_id=bot.id,
                        symbol=bot.symbol,
                        side=OrderSide.SELL,
                        price=exec_price,
                        qty=exec_qty,
                        quote_qty=exec_qty * exec_price,
                        exit_reason=exit_reason,
                        pnl_usdt=pnl_usdt,
                        pnl_pct=Decimal(str(round(pnl_pct, 4))),
                        is_simulated=True,
                    )
                    db.session.add(order)
                    db.session.add(BotLog(bot_id=bot.id, level="SELL",
                        message=f"🧪 [DEMO] Sell {bot.symbol}: price {float(exec_price):.6f}, P&L {pnl_pct:+.2f}%"))
                else:
                    response = place_market_order(client, bot.symbol, "SELL", quantity=last_buy.qty)
                    exec_qty = Decimal(response.get("executedQty", "0"))
                    exec_price = (
                        Decimal(response.get("cummulativeQuoteQty", "0")) / exec_qty
                        if exec_qty else Decimal("0")
                    )
                    pnl_usdt = (exec_price - last_buy.price) * exec_qty if last_buy.price else Decimal("0")
                    pnl_pct  = float((exec_price - last_buy.price) / last_buy.price * 100) if last_buy.price else 0.0
                    order = Order(
                        bot_id=bot.id,
                        binance_order_id=str(response.get("orderId")),
                        symbol=bot.symbol,
                        side=OrderSide.SELL,
                        price=exec_price,
                        qty=exec_qty,
                        quote_qty=Decimal(response.get("cummulativeQuoteQty", "0")),
                        exit_reason=exit_reason,
                        pnl_usdt=pnl_usdt,
                        pnl_pct=Decimal(str(round(pnl_pct, 4))),
                        is_simulated=False,
                    )
                    db.session.add(order)
                    if chat_id:
                        notify_sell(chat_id, bot.symbol, float(exec_price),
                                    float(exec_qty), bot.name, exit_reason_str, pnl_pct)

        bot.state = new_state
        db.session.commit()

    except BinanceAPIException as exc:
        logger.error("BinanceAPIException for bot %d: %s", bot_id, exc)
        bot.status = BotStatus.ERROR
        bot.error_message = str(exc.message)
        db.session.commit()
        if chat_id:
            notify_error(chat_id, bot.name, str(exc.message))
        raise self.retry(exc=exc, countdown=120)

    except Exception as exc:
        logger.exception("Unexpected error for bot %d: %s", bot_id, exc)
        bot.status = BotStatus.ERROR
        bot.error_message = str(exc)
        db.session.commit()
        if chat_id:
            notify_error(chat_id, bot.name, str(exc))


@celery_app.task(name="app.workers.bot_runner.run_all_bots")
def run_all_bots() -> None:
    """Dispatched every 60s by Celery Beat — spawns individual tasks per running bot."""
    running_bots = Bot.query.filter_by(status=BotStatus.RUNNING).all()
    for bot in running_bots:
        run_bot.delay(bot.id)
    logger.info("Dispatched %d bot tasks", len(running_bots))
