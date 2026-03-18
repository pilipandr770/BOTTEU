"""Telegram webhook endpoint registered in Flask."""
import asyncio
import hashlib
import logging

from flask import Blueprint, request, abort, current_app
from telegram import Update

from app.extensions import csrf

logger = logging.getLogger(__name__)

telegram_bp = Blueprint("telegram_webhook", __name__, url_prefix="/telegram")


def _webhook_secret(token: str) -> str:
    """Derive a stable 64-char hex secret from the bot token (SHA-256)."""
    return hashlib.sha256(token.encode()).hexdigest()


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

    # Verify that the request actually came from Telegram.
    # We register a secret_token when calling setWebhook; Telegram sends it
    # back in every update as the X-Telegram-Bot-Api-Secret-Token header.
    expected_secret = _webhook_secret(token)
    incoming_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if incoming_secret != expected_secret:
        logger.warning(
            "Telegram webhook: invalid secret from %s", request.remote_addr
        )
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
