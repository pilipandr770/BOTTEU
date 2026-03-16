"""Bots blueprint — CRUD, start/stop, API key management, Telegram linking."""
from datetime import datetime, timezone

import bcrypt
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from flask_babel import gettext as _

from app.extensions import db
from app.models.bot import Bot, BotStatus
from app.models.bot_log import BotLog
from app.models.order import Order, OrderSide
from app.models.api_key import ApiKey
from app.models.telegram_account import TelegramAccount
from app.models.subscription import Plan
from app.algorithms.base import list_algorithms
from app.services.encryption import encrypt
from app.services.binance_client import validate_api_key, get_cached_symbols

bots_bp = Blueprint("bots", __name__, url_prefix="/bots")


def _check_bot_limit():
    sub = current_user.subscription
    if sub and sub.is_active_pro:
        return True
    bot_count = Bot.query.filter_by(user_id=current_user.id).count()
    return bot_count < 1  # Free: 1 bot


# ── API Key Management ────────────────────────────────────────────────────────

@bots_bp.route("/api-key", methods=["GET", "POST"])
@login_required
def api_key():
    existing = ApiKey.query.filter_by(user_id=current_user.id).first()

    if request.method == "POST":
        action = request.form.get("action")

        if action == "save":
            api_key_val = request.form.get("api_key", "").strip()
            api_secret_val = request.form.get("api_secret", "").strip()

            if existing:
                # Allow blank = keep existing encrypted value
                if api_key_val:
                    existing.encrypted_api_key = encrypt(api_key_val)
                if api_secret_val:
                    existing.encrypted_api_secret = encrypt(api_secret_val)
                existing.is_valid = False
            else:
                if not api_key_val or not api_secret_val:
                    flash(_("Both API Key and Secret are required."), "danger")
                    return render_template("bots/api_key.html", existing=existing)
                new_key = ApiKey(
                    user_id=current_user.id,
                    encrypted_api_key=encrypt(api_key_val),
                    encrypted_api_secret=encrypt(api_secret_val),
                )
                db.session.add(new_key)

            db.session.commit()
            flash(_("API Key saved. Test the connection to verify."), "success")
            return redirect(url_for("bots.api_key"))

        elif action == "delete":
            if existing:
                db.session.delete(existing)
                db.session.commit()
            flash(_("API Key deleted."), "info")
            return redirect(url_for("bots.api_key"))

    return render_template("bots/api_key.html", existing=existing)


@bots_bp.route("/api-key/test", methods=["POST"])
@login_required
def test_api_key():
    success, message = validate_api_key(current_user.id)
    symbols = get_cached_symbols(current_user.id)
    return jsonify({
        "success": success,
        "message": message,
        "symbol_count": len(symbols),
    })


@bots_bp.route("/symbols")
@login_required
def symbols():
    """Return cached symbol list for the current user as JSON.
    Optional ?quote=USDT filter. Used by the bot create form."""
    quote = request.args.get("quote", "").upper()
    all_symbols = get_cached_symbols(current_user.id)
    if quote:
        all_symbols = [s for s in all_symbols if s["quote"] == quote]
    return jsonify(all_symbols)


# ── Telegram Linking ──────────────────────────────────────────────────────────

@bots_bp.route("/telegram", methods=["GET", "POST"])
@login_required
def telegram():
    tg = TelegramAccount.query.filter_by(user_id=current_user.id).first()
    if not tg:
        tg = TelegramAccount(user_id=current_user.id)
        db.session.add(tg)
        db.session.commit()

    if request.method == "POST":
        tg.generate_link_code()
        db.session.commit()

    return render_template("bots/telegram.html", tg=tg)


# ── Bot CRUD ─────────────────────────────────────────────────────────────────

@bots_bp.route("/")
@login_required
def index():
    from app.models.order import Order
    bots = Bot.query.filter_by(user_id=current_user.id).order_by(Bot.created_at.desc()).all()
    bot_ids = [b.id for b in bots]
    orders = (
        Order.query.filter(Order.bot_id.in_(bot_ids))
        .order_by(Order.created_at.desc())
        .limit(50)
        .all()
    ) if bot_ids else []
    return render_template("bots/index.html", bots=bots, orders=orders)


@bots_bp.route("/create", methods=["GET", "POST"])
@login_required
def create():
    if not _check_bot_limit():
        flash(_("Free plan allows 1 bot. Upgrade to Pro for unlimited bots."), "warning")
        return redirect(url_for("subscriptions.plans"))

    if not ApiKey.query.filter_by(user_id=current_user.id, is_valid=True).first():
        flash(_("Please add and verify your Binance API key first."), "warning")
        return redirect(url_for("bots.api_key"))

    if request.method == "POST":
        name   = request.form.get("name", "").strip()
        symbol = request.form.get("symbol", "").strip().upper()

        if not name or not symbol:
            flash(_("Bot name and trading pair are required."), "danger")
            return render_template("bots/create.html")

        # ── Modules ──────────────────────────────────────────────────────
        modules = request.form.getlist("modules")   # list of checked values
        if not modules:
            flash(_("Enable at least one signal module."), "danger")
            return render_template("bots/create.html")

        # ── Collect all param_* fields ────────────────────────────────────
        params: dict = {}
        for key, val in request.form.items():
            if key.startswith("param_") and val.strip():
                param_name = key[len("param_"):]
                try:
                    params[param_name] = float(val) if "." in val else int(val)
                except ValueError:
                    params[param_name] = val

        # Store modules list and determine algorithm key
        params["modules"] = modules
        params["entry_logic"] = params.get("entry_logic", "OR")

        if len(modules) == 1 and modules[0] != "combined":
            # Single module: use dedicated strategy for clarity
            algorithm = modules[0]
        else:
            algorithm = "combined"

        # Validate SL required when RSI is active
        if "rsi" in modules and not params.get("stop_loss_pct"):
            flash(_("Stop-Loss is required when using the RSI module."), "danger")
            return render_template("bots/create.html",
                                   cached_symbols=get_cached_symbols(current_user.id))

        position_size_usdt = request.form.get("position_size_usdt", "50")
        try:
            position_size_usdt = float(position_size_usdt)
        except ValueError:
            position_size_usdt = 50.0

        bot = Bot(
            user_id=current_user.id,
            name=name,
            symbol=symbol,
            algorithm=algorithm,
            params=params,
            state={},
            position_size_usdt=position_size_usdt,
        )
        db.session.add(bot)
        db.session.commit()
        flash(_("Bot created successfully!"), "success")
        return redirect(url_for("bots.index"))

    cached_symbols = get_cached_symbols(current_user.id)
    return render_template("bots/create.html", cached_symbols=cached_symbols)


@bots_bp.route("/<int:bot_id>/toggle", methods=["POST"])
@login_required
def toggle(bot_id: int):
    bot = Bot.query.filter_by(id=bot_id, user_id=current_user.id).first_or_404()
    if bot.status == BotStatus.RUNNING:
        bot.status = BotStatus.STOPPED
        flash(_(f"Bot '{bot.name}' stopped."), "info")
    else:
        bot.status = BotStatus.RUNNING
        bot.error_message = None
        flash(_(f"Bot '{bot.name}' started."), "success")
    db.session.commit()
    return redirect(url_for("bots.index"))


@bots_bp.route("/<int:bot_id>/delete", methods=["POST"])
@login_required
def delete(bot_id: int):
    bot = Bot.query.filter_by(id=bot_id, user_id=current_user.id).first_or_404()
    db.session.delete(bot)
    db.session.commit()
    flash(_("Bot deleted."), "info")
    return redirect(url_for("bots.index"))


# ── Bot Detail Page ───────────────────────────────────────────────────────────

@bots_bp.route("/<int:bot_id>")
@login_required
def detail(bot_id: int):
    bot = Bot.query.filter_by(id=bot_id, user_id=current_user.id).first_or_404()

    # Compute trade statistics from closed (SELL) orders
    sell_orders = Order.query.filter_by(bot_id=bot_id, side=OrderSide.SELL).all()
    total_trades = len(sell_orders)
    wins = sum(1 for o in sell_orders if o.pnl_usdt and float(o.pnl_usdt) > 0)
    total_pnl = sum(float(o.pnl_usdt) for o in sell_orders if o.pnl_usdt)
    win_rate = round(wins / total_trades * 100) if total_trades else 0

    # Recent logs — newest first (for initial render)
    logs = (
        BotLog.query.filter_by(bot_id=bot_id)
        .order_by(BotLog.id.desc())
        .limit(100)
        .all()
    )

    # Recent orders — newest first
    orders = (
        Order.query.filter_by(bot_id=bot_id)
        .order_by(Order.created_at.desc())
        .limit(50)
        .all()
    )

    return render_template(
        "bots/detail.html",
        bot=bot,
        logs=logs,
        orders=orders,
        total_trades=total_trades,
        wins=wins,
        total_pnl=total_pnl,
        win_rate=win_rate,
    )


@bots_bp.route("/<int:bot_id>/logs")
@login_required
def bot_logs_api(bot_id: int):
    """Return log entries newer than ?after=<id> as JSON (for polling)."""
    bot = Bot.query.filter_by(id=bot_id, user_id=current_user.id).first_or_404()
    after = request.args.get("after", 0, type=int)
    entries = (
        BotLog.query
        .filter(BotLog.bot_id == bot.id, BotLog.id > after)
        .order_by(BotLog.id.asc())
        .limit(50)
        .all()
    )
    return jsonify([{
        "id":      e.id,
        "level":   e.level,
        "message": e.message,
        "time":    e.created_at.strftime("%d.%m %H:%M:%S"),
    } for e in entries])

