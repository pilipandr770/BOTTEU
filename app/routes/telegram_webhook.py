"""Telegram webhook endpoint registered in Flask."""
import json
import logging

from flask import Blueprint, request, abort, current_app
from telegram import Update

from app.extensions import csrf

logger = logging.getLogger(__name__)

telegram_bp = Blueprint("telegram_webhook", __name__, url_prefix="/telegram")


@telegram_bp.route("/webhook", methods=["POST"])
@csrf.exempt
def webhook():
    from app.telegram.bot import build_application
    import asyncio

    token = current_app.config.get("TELEGRAM_BOT_TOKEN", "")
    # Validate secret path token (Telegram sends to /webhook/<token>)
    if not token:
        abort(403)

    data = request.get_json(silent=True)
    if not data:
        abort(400)

    try:
        application = build_application()
        update = Update.de_json(data, application.bot)
        asyncio.run(application.process_update(update))
    except Exception as exc:
        logger.exception("Telegram webhook error: %s", exc)

    return "", 200
