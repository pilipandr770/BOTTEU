"""Telegram webhook endpoint registered in Flask."""
import asyncio
import logging

from flask import Blueprint, request, abort, current_app
from telegram import Update

from app.extensions import csrf

logger = logging.getLogger(__name__)

telegram_bp = Blueprint("telegram_webhook", __name__, url_prefix="/telegram")


def _get_or_create_loop() -> asyncio.AbstractEventLoop:
    """Return the running loop or create a new one."""
    try:
        return asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


@telegram_bp.route("/webhook", methods=["POST"])
@csrf.exempt
def webhook():
    from app.telegram.bot import build_application

    token = current_app.config.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        abort(403)

    data = request.get_json(silent=True)
    if not data:
        abort(400)

    try:
        application = build_application(token)
        update = Update.de_json(data, application.bot)
        loop = _get_or_create_loop()
        loop.run_until_complete(application.process_update(update))
    except Exception as exc:
        logger.exception("Telegram webhook error: %s", exc)

    return "", 200
