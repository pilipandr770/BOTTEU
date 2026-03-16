"""
In-process APScheduler — bot tick engine without Celery.

Runs every 60 seconds in a background thread, processes all RUNNING bots.
No Redis / Celery needed. Works out-of-the-box with `python run.py`.

Real vs Demo logic:
    - If free quote balance >= position_size_usdt  → REAL order on Binance
    - Otherwise                                    → DEMO order (is_simulated=True)
    Logs always written; demo ticks are prefixed with 🧪 [ДЕМО].
"""
import logging
import os
from decimal import Decimal

import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None

TIMEFRAME_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h",
    "8h": "8h", "12h": "12h", "1d": "1d", "3d": "3d",
    "1w": "1w", "1mo": "1M",
}

# Minimum free quote balance to allow real trading
REAL_TRADE_MIN = 5.0  # USDT / USDC


# ── Core tick ────────────────────────────────────────────────────────────────

def _tick_bot(bot_id: int) -> None:
    """Process one bot tick. Must be called inside an app context."""
    from app.extensions import db
    from app.models.bot import Bot, BotStatus
    from app.models.bot_log import BotLog
    from app.models.order import Order, OrderSide, ExitReason
    from app.algorithms.base import get_algorithm
    from app.services.binance_client import get_client_for_user, get_quote_free_balance
    from app.services.order_manager import place_market_order

    bot: Bot | None = db.session.get(Bot, bot_id)
    if not bot or bot.status != BotStatus.RUNNING:
        return

    try:
        client = get_client_for_user(bot.user_id)
        interval = TIMEFRAME_MAP.get(bot.params.get("timeframe", "1h"), "1h")
        klines = client.get_klines(symbol=bot.symbol, interval=interval, limit=200)

        df = pd.DataFrame(klines, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore",
        ])
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)

        strategy = get_algorithm(bot.algorithm)
        state = bot.state or {}
        signal, new_state = strategy.generate_signal(df, state, bot.params)

        # ── Balance check ─────────────────────────────────────────────────
        free_balance = get_quote_free_balance(client, bot.symbol)
        position_size = float(bot.position_size_usdt)
        simulate = free_balance < max(REAL_TRADE_MIN, position_size)

        # ── Build log entries ─────────────────────────────────────────────
        log_entries: list[tuple[str, str]] = new_state.pop("_log", [])

        if simulate:
            log_entries.insert(0, ("INFO",
                f"🧪 [ДЕМО] Баланс {free_balance:.4f} USDT < нужно {position_size:.2f} USDT — "
                f"реальных сделок нет, идёт демо-торговля"
            ))

        for level, msg in log_entries:
            db.session.add(BotLog(bot_id=bot.id, level=level, message=msg))

        # Trim logs to last 500
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

        current_price = Decimal(str(float(df["close"].iloc[-1])))

        # ── BUY ───────────────────────────────────────────────────────────
        if signal == "BUY" and not state.get("has_position", False):
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
                        f"🧪 [ДЕМО] Покупка {bot.symbol}: цена {float(current_price):.6f}, "
                        f"объём {float(exec_qty):.6f} (демо-ордер, Binance не использован)"
                    )
                ))
            else:
                resp = place_market_order(client, bot.symbol, "BUY", quote_amount=quote_amount)
                exec_qty = Decimal(resp.get("executedQty", "0"))
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

        # ── SELL ──────────────────────────────────────────────────────────
        elif signal == "SELL" and state.get("has_position", False):
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
                    exec_qty = last_buy.qty
                    exec_price = current_price
                    pnl_usdt = (exec_price - last_buy.price) * exec_qty if last_buy.price else Decimal("0")
                    pnl_pct = float(
                        (exec_price - last_buy.price) / last_buy.price * 100
                    ) if last_buy.price else 0.0
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
                            f"🧪 [ДЕМО] Продажа {bot.symbol}: цена {float(exec_price):.6f}, "
                            f"P&L {pnl_pct:+.2f}% (демо-ордер)"
                        )
                    ))
                else:
                    resp = place_market_order(client, bot.symbol, "SELL", quantity=last_buy.qty)
                    exec_qty = Decimal(resp.get("executedQty", "0"))
                    exec_price = (
                        Decimal(resp.get("cummulativeQuoteQty", "0")) / exec_qty
                        if exec_qty else Decimal("0")
                    )
                    pnl_usdt = (exec_price - last_buy.price) * exec_qty if last_buy.price else Decimal("0")
                    pnl_pct = float(
                        (exec_price - last_buy.price) / last_buy.price * 100
                    ) if last_buy.price else 0.0
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

        bot.state = new_state
        db.session.commit()
        logger.debug("Bot %d tick: signal=%s simulate=%s", bot.id, signal, simulate)

    except Exception as exc:
        logger.exception("Tick error for bot %d: %s", bot_id, exc)
        try:
            from app.models.bot import BotStatus
            from app.extensions import db
            bot.status = BotStatus.ERROR
            bot.error_message = str(exc)[:500]
            db.session.commit()
        except Exception:
            from app.extensions import db
            db.session.rollback()


def _tick_all(app) -> None:
    with app.app_context():
        from app.models.bot import Bot, BotStatus
        bots = Bot.query.filter_by(status=BotStatus.RUNNING).all()
        logger.info("Scheduler: ticking %d bot(s)", len(bots))
        for bot in bots:
            _tick_bot(bot.id)


# ── Public API ────────────────────────────────────────────────────────────────

def start_scheduler(app) -> None:
    """Start the background scheduler. Safe to call multiple times (idempotent).

    With Werkzeug reloader (debug=True) only starts in the child process.
    In production (no reloader) always starts.
    """
    global _scheduler
    if _scheduler is not None:
        return  # Already running

    # Avoid double-start with Werkzeug reloader:
    # parent process has WERKZEUG_RUN_MAIN unset
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        logger.debug("Scheduler: skipping start in Werkzeug reloader parent")
        return

    import atexit
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        func=_tick_all,
        args=[app],
        trigger="interval",
        seconds=60,
        id="tick_all_bots",
        max_instances=1,
        replace_existing=True,
        misfire_grace_time=30,
    )
    _scheduler.start()
    atexit.register(lambda: _scheduler.shutdown(wait=False))
    logger.info("Bot scheduler started (interval=60s)")
