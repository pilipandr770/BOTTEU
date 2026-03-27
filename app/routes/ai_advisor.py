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

    if not symbol or len(symbol) > 20:
        return jsonify({"error": "Invalid symbol"}), 400

    from app.ai.scanner import scan_symbol
    try:
        result = scan_symbol(symbol)
        return jsonify(result)
    except Exception as exc:
        logger.exception("Scanner error for %s: %s", symbol, exc)
        return jsonify({"error": f"Scanner failed: {exc}"}), 500


@ai_bp.route("/analyze", methods=["POST"])
@login_required
@csrf.exempt
@limiter.limit("10 per hour")
def analyze():
    """Run full AI analysis (scanner + Claude advisor)."""
    data = request.get_json(silent=True) or {}
    symbol = data.get("symbol", "BTCUSDT").strip().upper()
    bot_id = data.get("bot_id")

    if not symbol or len(symbol) > 20:
        return jsonify({"error": "Invalid symbol"}), 400

    lang = session.get("lang", "en")

    from app.ai.scanner import scan_symbol
    from app.ai.advisor import analyze as ai_analyze

    try:
        scan_data = scan_symbol(symbol)
    except Exception as exc:
        logger.exception("Scanner error: %s", exc)
        return jsonify({"error": f"Scanner failed: {exc}"}), 500

    try:
        advice = ai_analyze(scan_data, lang=lang)
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
