"""
TradingView Webhook endpoint.

Configure in Pine Script alert:
    alert("{{strategy.order.action}}", alert.freq_once_per_bar_close)

Payload (plain text or JSON):
    {"action": "buy", "symbol": "BTCUSDT"}
    {"action": "sell", "symbol": "BTCUSDT"}

Webhook URL: https://yourdomain.com/webhook/tv/<bot_id>/<token>
Token:       generate via POST /webhook/tv/token/<bot_id>  (login required)
             stored in bot.params["webhook_token"]

Security:
  - Token is 32-byte URL-safe random → 256-bit entropy
  - Compared with secrets.compare_digest() (constant-time, no timing leak)
  - Endpoint is CSRF-exempt (external service cannot supply CSRF token)
  - Rate-limited to 10 requests/minute per IP via the app-level limiter
"""
import logging
import secrets
import threading

from flask import Blueprint, request, jsonify, abort
from flask_login import login_required, current_user

from app.extensions import db, csrf
from app.models.bot import Bot, BotStatus

logger = logging.getLogger(__name__)

tv_bp = Blueprint("tradingview", __name__, url_prefix="/webhook/tv")


# ── Webhook receiver ──────────────────────────────────────────────────────────

@tv_bp.route("/<int:bot_id>/<token>", methods=["POST"])
@csrf.exempt
def webhook(bot_id: int, token: str):
    """Receive a TradingView alert and fire a BUY or SELL on the indicated bot."""
    bot: Bot | None = Bot.query.get(bot_id)
    if not bot:
        abort(404)

    expected = (bot.params or {}).get("webhook_token", "")
    if not expected or not secrets.compare_digest(str(expected), str(token)):
        logger.warning("TV webhook: invalid token for bot %d (ip=%s)", bot_id,
                        request.remote_addr)
        abort(403)

    if bot.status != BotStatus.RUNNING:
        return jsonify({"status": "ignored", "reason": "bot not running"}), 200

    # Parse JSON body; also accept plain "buy" / "sell" text
    data = request.get_json(silent=True)
    if not data:
        raw = (request.get_data(as_text=True) or "").strip().lower()
        data = {"action": raw}

    action = str(data.get("action", "")).strip().lower()
    if action not in ("buy", "sell"):
        return jsonify({"status": "ignored", "reason": f"unknown action: {action!r}"}), 200

    signal = action.upper()
    state  = dict(bot.state or {})

    if signal == "BUY"  and state.get("has_position"):
        return jsonify({"status": "ignored", "reason": "already in position"}), 200
    if signal == "SELL" and not state.get("has_position"):
        return jsonify({"status": "ignored", "reason": "no open position"}), 200

    # Inject override: next_tick_at=0 clears the adaptive-tick gate
    state["tv_signal"]    = signal
    state["next_tick_at"] = 0
    if signal == "SELL":
        state["exit_reason"] = "SIGNAL"
    bot.state = state
    db.session.commit()

    # Fire a tick immediately in a background thread (best-effort, non-blocking)
    try:
        from flask import current_app
        app = current_app._get_current_object()
        threading.Thread(target=_fire_tick, args=(app, bot_id), daemon=True).start()
    except Exception as exc:
        logger.warning("Could not fire immediate tick for bot %d: %s", bot_id, exc)

    logger.info("TV webhook: bot %d signal=%s", bot_id, signal)
    return jsonify({"status": "ok", "signal": signal}), 200


def _fire_tick(app, bot_id: int) -> None:
    with app.app_context():
        try:
            from app.workers.core.tick import tick_bot
            tick_bot(bot_id)
        except Exception as exc:
            logger.exception("Background tick failed for bot %d: %s", bot_id, exc)


# ── Token management ──────────────────────────────────────────────────────────

@tv_bp.route("/token/<int:bot_id>", methods=["POST"])
@login_required
def generate_token(bot_id: int):
    """Generate (or regenerate) webhook token for a bot. Returns JSON with the token."""
    bot: Bot | None = Bot.query.get_or_404(bot_id)
    if bot.user_id != current_user.id:
        abort(403)

    params = dict(bot.params or {})
    params["webhook_token"] = secrets.token_urlsafe(32)
    bot.params = params
    db.session.commit()
    return jsonify({"token": params["webhook_token"]}), 200
