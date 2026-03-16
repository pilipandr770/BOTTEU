"""Dashboard blueprint — main user overview."""
from flask import Blueprint, render_template, jsonify
from flask_login import login_required, current_user

from app.models.bot import Bot, BotStatus
from app.models.order import Order, OrderSide
from app.models.api_key import ApiKey
from app.services.binance_client import get_spot_balance

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
@dashboard_bp.route("/dashboard")
@login_required
def index():
    bots = Bot.query.filter_by(user_id=current_user.id).order_by(Bot.created_at.desc()).all()
    running_count = sum(1 for b in bots if b.status == BotStatus.RUNNING)

    # Last 10 trades across all bots
    recent_orders = (
        Order.query
        .join(Bot)
        .filter(Bot.user_id == current_user.id, Order.is_simulated == False)
        .order_by(Order.created_at.desc())
        .limit(10)
        .all()
    )

    # Total P&L (all SELL orders)
    sell_orders = (
        Order.query
        .join(Bot)
        .filter(
            Bot.user_id == current_user.id,
            Order.side == OrderSide.SELL,
            Order.pnl_usdt.isnot(None),
            Order.is_simulated == False,
        )
        .all()
    )
    total_pnl = sum(float(o.pnl_usdt or 0) for o in sell_orders)

    has_api_key = ApiKey.query.filter_by(user_id=current_user.id, is_valid=True).first() is not None

    return render_template(
        "dashboard/index.html",
        bots=bots,
        running_count=running_count,
        recent_orders=recent_orders,
        total_pnl=total_pnl,
        has_api_key=has_api_key,
    )


@dashboard_bp.route("/balance")
@login_required
def balance():
    """JSON endpoint — returns spot balances for the current user."""
    result = get_spot_balance(current_user.id)
    return jsonify(result)

