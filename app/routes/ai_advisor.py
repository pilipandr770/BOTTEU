"""
AI Advisor blueprint — routes for scanner, advisor, autopilot, and consultation history.
"""
import json
import logging
from datetime import datetime, timezone

from flask import Blueprint, render_template, request, jsonify, session
from flask_login import login_required, current_user
from flask_babel import gettext as _

from app.extensions import db, limiter, csrf
from app.models.bot import Bot
from app.models.ai_consultation import AIConsultation

logger = logging.getLogger(__name__)

ai_bp = Blueprint("ai_advisor", __name__, url_prefix="/ai")


@ai_bp.route("/")
@login_required
def index():
    """AI Advisor dashboard."""
    bots = Bot.query.filter_by(user_id=current_user.id).all()
    consultations = (
        AIConsultation.query
        .filter_by(user_id=current_user.id)
        .order_by(AIConsultation.created_at.desc())
        .limit(20)
        .all()
    )
    return render_template(
        "ai/index.html",
        bots=bots,
        consultations=consultations,
    )


@ai_bp.route("/scan", methods=["POST"])
@login_required
@csrf.exempt
@limiter.limit("10 per hour")
def scan():
    """Run multi-timeframe scanner for a symbol."""
    data = request.get_json(silent=True) or {}
    symbol = data.get("symbol", "BTCUSDT").strip().upper()
    mode = data.get("mode", "swing")
    if mode not in ("intraday", "swing"):
        mode = "swing"

    if not symbol or len(symbol) > 20:
        return jsonify({"error": "Invalid symbol"}), 400

    from app.ai.scanner import scan_symbol
    try:
        result = scan_symbol(symbol, mode=mode)
        return jsonify(result)
    except Exception as exc:
        logger.exception("Scanner error for %s: %s", symbol, exc)
        return jsonify({"error": f"Scanner failed: {exc}"}), 500


@ai_bp.route("/analyze", methods=["POST"])
@login_required
@csrf.exempt
@limiter.limit("10 per hour")
def analyze():
    """Run full AI analysis (scanner + Claude advisor). Requires Pro or Elite plan."""
    sub = current_user.subscription
    if not (sub and sub.has_ai):
        return jsonify({"error": "AI advisor requires a Pro or Elite subscription."}), 403

    data = request.get_json(silent=True) or {}
    symbol = data.get("symbol", "BTCUSDT").strip().upper()
    bot_id = data.get("bot_id")
    mode = data.get("mode", "swing")
    if mode not in ("intraday", "swing"):
        mode = "swing"

    if not symbol or len(symbol) > 20:
        return jsonify({"error": "Invalid symbol"}), 400

    lang = session.get("lang", "en")

    from app.ai.scanner import scan_symbol
    from app.ai.advisor import analyze as ai_analyze

    try:
        scan_data = scan_symbol(symbol, mode=mode)
    except Exception as exc:
        logger.exception("Scanner error: %s", exc)
        return jsonify({"error": f"Scanner failed: {exc}"}), 500

    try:
        advice = ai_analyze(scan_data, lang=lang, mode=mode)
    except Exception as exc:
        logger.exception("Advisor error: %s", exc)
        return jsonify({"error": f"AI analysis failed: {exc}"}), 500

    # Save consultation to DB
    try:
        consultation = AIConsultation(
            user_id=current_user.id,
            bot_id=int(bot_id) if bot_id else None,
            symbol=symbol,
            market_regime=advice.get("market_regime"),
            recommended_algorithm=advice.get("recommended_algorithm"),
            recommended_params=advice.get("recommended_params"),
            recommended_timeframe=advice.get("recommended_timeframe"),
            confidence_score=advice.get("confidence"),
            reasoning=advice.get("reasoning"),
            signal_matrix={
                tf: tf_data.get("signals", {})
                for tf, tf_data in scan_data.get("timeframes", {}).items()
                if isinstance(tf_data, dict) and "signals" in tf_data
            },
            backtest_results=scan_data.get("best_combinations", [])[:10],
            applied=False,
        )
        db.session.add(consultation)
        db.session.commit()
        advice["consultation_id"] = consultation.id
    except Exception as exc:
        logger.error("Failed to save consultation: %s", exc)
        db.session.rollback()

    advice["scan_data"] = scan_data
    return jsonify(advice)


@ai_bp.route("/apply", methods=["POST"])
@login_required
@csrf.exempt
def apply_to_bot():
    """Apply AI recommendation to a specific bot."""
    data = request.get_json(silent=True) or {}
    bot_id = data.get("bot_id")
    consultation_id = data.get("consultation_id")

    if not bot_id or not consultation_id:
        return jsonify({"error": "bot_id and consultation_id required"}), 400

    bot = Bot.query.filter_by(id=int(bot_id), user_id=current_user.id).first()
    if not bot:
        return jsonify({"error": "Bot not found"}), 404

    consultation = AIConsultation.query.filter_by(
        id=int(consultation_id), user_id=current_user.id
    ).first()
    if not consultation:
        return jsonify({"error": "Consultation not found"}), 404

    # Apply recommendation
    old_algo = bot.algorithm
    old_params = dict(bot.params) if bot.params else {}

    new_algo = consultation.recommended_algorithm or bot.algorithm
    new_params = dict(consultation.recommended_params) if consultation.recommended_params else {}
    new_tf = consultation.recommended_timeframe or old_params.get("timeframe", "1h")

    # Preserve risk settings
    for key in ("stop_loss_pct", "take_profit_pct", "trailing_tp_pct"):
        if key not in new_params and key in old_params:
            new_params[key] = old_params[key]
    new_params["timeframe"] = new_tf

    bot.algorithm = new_algo
    bot.params = new_params
    bot.state = {
        "has_position": False,
        "_log": [
            f"AI recommendation applied: {old_algo} → {new_algo} "
            f"(TF: {old_params.get('timeframe', '?')} → {new_tf})"
        ],
    }

    consultation.applied = True

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return jsonify({"error": f"Failed to apply: {exc}"}), 500

    return jsonify({
        "success": True,
        "message": f"Strategy switched to {new_algo} on {new_tf}",
        "old": {"algorithm": old_algo, "params": old_params},
        "new": {"algorithm": new_algo, "params": new_params},
    })


@ai_bp.route("/toggle-autopilot", methods=["POST"])
@login_required
@csrf.exempt
def toggle_autopilot():
    """Enable/disable AI autopilot for a specific bot."""
    data = request.get_json(silent=True) or {}
    bot_id = data.get("bot_id")
    enabled = data.get("enabled", False)

    if not bot_id:
        return jsonify({"error": "bot_id required"}), 400

    bot = Bot.query.filter_by(id=int(bot_id), user_id=current_user.id).first()
    if not bot:
        return jsonify({"error": "Bot not found"}), 404

    params = dict(bot.params) if bot.params else {}
    params["ai_autopilot"] = bool(enabled)
    bot.params = params

    db.session.commit()

    return jsonify({
        "success": True,
        "ai_autopilot": bool(enabled),
        "message": _("AI Autopilot enabled") if enabled else _("AI Autopilot disabled"),
    })


@ai_bp.route("/create-bot", methods=["POST"])
@login_required
@csrf.exempt
def create_bot_from_ai():
    """Create a new bot pre-filled with AI recommendation settings."""
    from app.models.api_key import ApiKey
    from app.models.subscription import Plan
    from app.services.binance_client import get_cached_symbols

    data = request.get_json(silent=True) or {}
    consultation_id = data.get("consultation_id")
    bot_name = (data.get("bot_name") or "").strip()
    position_size_usdt = data.get("position_size_usdt", 50)

    if not consultation_id:
        return jsonify({"error": "consultation_id required"}), 400

    consultation = AIConsultation.query.filter_by(
        id=int(consultation_id), user_id=current_user.id
    ).first()
    if not consultation:
        return jsonify({"error": "Consultation not found"}), 404

    # Validate bot name
    if not bot_name:
        algo_label = (consultation.recommended_algorithm or "bot").upper()
        bot_name = f"AI {algo_label} {consultation.symbol}"
    if len(bot_name) > 100:
        return jsonify({"error": "Bot name too long (max 100 characters)"}), 400

    # Validate position size
    try:
        position_size_usdt = float(position_size_usdt)
        if position_size_usdt < 1:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid position size"}), 400

    # Check API key
    api_key_record = ApiKey.query.filter_by(user_id=current_user.id, is_valid=True).first()
    if not api_key_record:
        return jsonify({"error": "Please add and verify your Binance API key first."}), 400

    # Check bot limit vs plan
    sub = current_user.subscription
    if not (sub and sub.has_ai):
        return jsonify({"error": "AI auto-bot creation requires a Pro or Elite subscription."}), 403
    bot_count = Bot.query.filter_by(user_id=current_user.id).count()
    if bot_count >= sub.max_bots:
        return jsonify({"error": f"Your plan allows {sub.max_bots} bot(s). Upgrade to add more."}), 403

    algorithm = consultation.recommended_algorithm or "rsi"
    rec_params = dict(consultation.recommended_params) if consultation.recommended_params else {}
    timeframe = consultation.recommended_timeframe or rec_params.get("timeframe", "1h")
    rec_params["timeframe"] = timeframe
    rec_params["modules"] = [algorithm]
    rec_params.setdefault("entry_logic", "OR")

    bot = Bot(
        user_id=current_user.id,
        name=bot_name,
        symbol=consultation.symbol,
        algorithm=algorithm,
        params=rec_params,
        state={
            "_log": [
                f"Created from AI consultation #{consultation.id} "
                f"(confidence: {consultation.confidence_score}%, regime: {consultation.market_regime})"
            ]
        },
        position_size_usdt=position_size_usdt,
    )
    db.session.add(bot)

    consultation.applied = True

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.exception("create_bot_from_ai failed: %s", exc)
        return jsonify({"error": f"Failed to create bot: {exc}"}), 500

    return jsonify({
        "success": True,
        "bot_id": bot.id,
        "bot_name": bot.name,
        "message": f"Bot '{bot.name}' created with {algorithm} strategy on {timeframe}",
    })


@ai_bp.route("/history")
@login_required
def history():
    """Return consultation history as JSON."""
    page = request.args.get("page", 1, type=int)
    per_page = 20
    pagination = (
        AIConsultation.query
        .filter_by(user_id=current_user.id)
        .order_by(AIConsultation.created_at.desc())
        .paginate(page=page, per_page=per_page, error_out=False)
    )
    items = [
        {
            "id": c.id,
            "symbol": c.symbol,
            "market_regime": c.market_regime,
            "recommended_algorithm": c.recommended_algorithm,
            "recommended_timeframe": c.recommended_timeframe,
            "confidence_score": c.confidence_score,
            "reasoning": c.reasoning,
            "applied": c.applied,
            "bot_id": c.bot_id,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in pagination.items
    ]
    return jsonify({"items": items, "total": pagination.total, "page": page})
