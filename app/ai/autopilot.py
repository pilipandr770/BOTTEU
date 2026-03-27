"""
Level 3 — AI Autopilot.

Periodically checks running bots, runs the scanner + advisor,
and optionally switches bot strategy/params if a better one is found.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.extensions import db
from app.models.bot import Bot, BotStatus

logger = logging.getLogger(__name__)


def evaluate_bot(bot: Bot) -> dict[str, Any] | None:
    """
    Run scanner + advisor for a bot's symbol and compare
    current settings with the AI recommendation.

    Returns a dict describing proposed changes, or None if no change needed.
    """
    from app.ai.scanner import scan_symbol
    from app.ai.advisor import analyze

    logger.info("Autopilot: evaluating bot %d (%s %s)", bot.id, bot.symbol, bot.algorithm)

    try:
        scan_data = scan_symbol(bot.symbol)
        advice = analyze(scan_data)
    except Exception as exc:
        logger.error("Autopilot: scan/analyze failed for bot %d: %s", bot.id, exc)
        return None

    if "error" in advice:
        logger.warning("Autopilot: AI returned error for bot %d: %s", bot.id, advice["error"])
        return None

    rec_algo = advice.get("recommended_algorithm", "")
    rec_tf = advice.get("recommended_timeframe", "")
    rec_params = advice.get("recommended_params", {})
    confidence = advice.get("confidence", 0)

    # Only suggest changes if confidence >= 60 and recommendation differs
    current_tf = bot.params.get("timeframe", "1h") if bot.params else "1h"
    algo_changed = rec_algo != bot.algorithm
    tf_changed = rec_tf != current_tf
    # Check if key params changed significantly
    params_changed = False
    if rec_params and bot.params:
        for key, val in rec_params.items():
            if key in ("stop_loss_pct", "take_profit_pct", "trailing_tp_pct", "timeframe"):
                continue
            old_val = bot.params.get(key)
            if old_val is not None and val != old_val:
                params_changed = True
                break

    needs_change = (algo_changed or tf_changed or params_changed) and confidence >= 60

    return {
        "bot_id": bot.id,
        "bot_name": bot.name,
        "symbol": bot.symbol,
        "current": {
            "algorithm": bot.algorithm,
            "timeframe": current_tf,
            "params": bot.params,
        },
        "recommended": {
            "algorithm": rec_algo,
            "timeframe": rec_tf,
            "params": rec_params,
        },
        "confidence": confidence,
        "reasoning": advice.get("reasoning", ""),
        "market_regime": advice.get("market_regime", ""),
        "needs_change": needs_change,
        "risks": advice.get("risks", []),
    }


def apply_recommendation(bot_id: int, recommendation: dict) -> bool:
    """
    Apply AI recommendation to a bot. Updates algorithm, timeframe, and params.
    Preserves risk settings (SL/TP) and resets trading state.
    """
    from app.models.ai_consultation import AIConsultation

    bot = Bot.query.get(bot_id)
    if not bot:
        return False

    rec = recommendation.get("recommended", {})
    old_algorithm = bot.algorithm
    old_params = dict(bot.params) if bot.params else {}

    new_algo = rec.get("algorithm", bot.algorithm)
    new_params = rec.get("params", {})
    new_tf = rec.get("timeframe", old_params.get("timeframe", "1h"))

    # Preserve user's risk settings if not in recommendation
    for key in ("stop_loss_pct", "take_profit_pct", "trailing_tp_pct"):
        if key not in new_params and key in old_params:
            new_params[key] = old_params[key]

    new_params["timeframe"] = new_tf

    # Apply changes
    bot.algorithm = new_algo
    bot.params = new_params

    # Reset trading state (no open position carry-over to new strategy)
    bot.state = {
        "has_position": False,
        "_log": [
            f"AI Autopilot switched strategy: {old_algorithm} → {new_algo} "
            f"(TF: {old_params.get('timeframe', '?')} → {new_tf}, "
            f"confidence: {recommendation.get('confidence', 0)}%)"
        ],
    }

    # Log the consultation
    consultation = AIConsultation(
        user_id=bot.user_id,
        bot_id=bot.id,
        symbol=bot.symbol,
        market_regime=recommendation.get("market_regime", ""),
        recommended_algorithm=new_algo,
        recommended_params=new_params,
        recommended_timeframe=new_tf,
        confidence_score=recommendation.get("confidence", 0),
        reasoning=recommendation.get("reasoning", ""),
        signal_matrix=None,
        backtest_results=None,
        applied=True,
    )
    db.session.add(consultation)
    db.session.commit()

    logger.info(
        "Autopilot: applied recommendation to bot %d: %s→%s, TF=%s, confidence=%d%%",
        bot.id, old_algorithm, new_algo, new_tf, recommendation.get("confidence", 0)
    )
    return True


def run_autopilot():
    """
    Main autopilot loop — called periodically (e.g. every 4 hours).
    Only processes bots that have autopilot enabled.
    """
    bots = Bot.query.filter_by(status=BotStatus.RUNNING).all()
    results = []

    for bot in bots:
        # Check if autopilot is enabled for this bot
        if not bot.params or not bot.params.get("ai_autopilot"):
            continue

        evaluation = evaluate_bot(bot)
        if evaluation and evaluation.get("needs_change"):
            apply_recommendation(bot.id, evaluation)
            results.append({"bot_id": bot.id, "action": "switched", **evaluation})
        elif evaluation:
            results.append({"bot_id": bot.id, "action": "kept", **evaluation})

    logger.info("Autopilot run completed: %d bots evaluated", len(results))
    return results
