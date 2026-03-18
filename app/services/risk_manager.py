"""
Risk Manager — enforces global trading limits per user.

check_before_buy(user_id, bot_id) → (allowed: bool, reason: str)
    Call this before every BUY. Returns False when a limit is breached.

emergency_stop(user_id, reason) → int
    Stops all running bots for the user and logs the event.
"""
import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func

logger = logging.getLogger(__name__)


def check_before_buy(user_id: int, bot_id: int) -> tuple[bool, str]:
    """
    Returns (allowed, reason).
    Fails open (returns True) if the risk_configs table doesn't exist yet
    so that pre-migration deployments don't break.
    """
    from app.extensions import db
    from app.models.bot import Bot

    try:
        from app.models.risk_config import RiskConfig
        rc = RiskConfig.query.filter_by(user_id=user_id).first()
    except Exception:
        return True, ""   # table not yet migrated

    if not rc or not rc.enabled:
        return True, ""

    user_bots = Bot.query.filter_by(user_id=user_id).all()
    bot_ids   = [b.id for b in user_bots]

    # ── Max simultaneous open positions ──────────────────────────────────
    if rc.max_open_positions is not None:
        open_count = sum(1 for b in user_bots if (b.state or {}).get("has_position"))
        if open_count >= int(rc.max_open_positions):
            return False, f"Max open positions ({rc.max_open_positions}) already reached"

    # ── Max daily loss ────────────────────────────────────────────────────
    if rc.max_daily_loss_pct is not None:
        from app.models.order import Order, OrderSide
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

        daily_pnl = db.session.query(func.sum(Order.pnl_usdt)).filter(
            Order.bot_id.in_(bot_ids),
            Order.side   == OrderSide.SELL,
            Order.created_at >= today,
        ).scalar() or Decimal("0")

        invested_today = db.session.query(func.sum(Order.quote_qty)).filter(
            Order.bot_id.in_(bot_ids),
            Order.side   == OrderSide.BUY,
            Order.created_at >= today,
        ).scalar() or Decimal("0")

        if float(invested_today) > 0:
            daily_loss_pct = float(daily_pnl) / float(invested_today) * 100
            if daily_loss_pct < -float(rc.max_daily_loss_pct):
                return (
                    False,
                    f"Daily loss limit ({float(rc.max_daily_loss_pct):.1f}%) exceeded "
                    f"(current: {daily_loss_pct:.2f}%)",
                )

    # ── Max drawdown (peak equity → current equity) ──────────────────────
    if rc.max_drawdown_pct is not None:
        from app.models.order import Order, OrderSide
        orders = (
            Order.query
            .filter(Order.bot_id.in_(bot_ids), Order.side == OrderSide.SELL)
            .order_by(Order.created_at)
            .with_entities(Order.pnl_usdt)
            .all()
        )
        equity = 1000.0
        peak   = 1000.0
        max_dd = 0.0
        for (pnl,) in orders:
            equity += float(pnl or 0)
            peak    = max(peak, equity)
            if peak > 0:
                dd = (peak - equity) / peak * 100
                max_dd = max(max_dd, dd)
        if max_dd >= float(rc.max_drawdown_pct):
            return (
                False,
                f"Max drawdown ({float(rc.max_drawdown_pct):.1f}%) reached "
                f"(current drawdown: {max_dd:.2f}%)",
            )

    return True, ""


def emergency_stop(user_id: int, reason: str) -> int:
    """Stop all running bots for a user. Returns count of stopped bots."""
    from app.extensions import db
    from app.models.bot import Bot, BotStatus
    from app.models.bot_log import BotLog

    running = Bot.query.filter_by(user_id=user_id, status=BotStatus.RUNNING).all()
    for b in running:
        b.status        = BotStatus.STOPPED
        b.error_message = f"Risk Manager: {reason}"
        db.session.add(BotLog(bot_id=b.id, level="WARN",
            message=f"🛑 Emergency stop triggered: {reason}"))
    db.session.commit()
    logger.warning("Emergency stop for user %d (%d bots): %s", user_id, len(running), reason)
    return len(running)
