"""
Telegram notification service — sends messages to a user's linked chat.
Called from the bot runner after trade events.

Uses synchronous requests.post() instead of asyncio to avoid event-loop
conflicts inside APScheduler background threads (Bug 5).
"""
import logging

import requests
from flask import current_app

logger = logging.getLogger(__name__)


def _get_token() -> str:
    return current_app.config.get("TELEGRAM_BOT_TOKEN", "")


def notify_user(chat_id: int, text: str) -> None:
    """Send a Telegram message synchronously. Silently logs errors (never raises)."""
    token = _get_token()
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as exc:
        logger.warning("Telegram notify failed for chat_id=%s: %s", chat_id, exc)


# ── Formatted notification helpers ──────────────────────────────────────────

def notify_buy(chat_id: int, symbol: str, price: float, qty: float, bot_name: str) -> None:
    notify_user(
        chat_id,
        f"🟢 <b>BUY</b> executed\n"
        f"Bot: {bot_name}\n"
        f"Pair: <code>{symbol}</code>\n"
        f"Price: <b>{price:.8f}</b>\n"
        f"Qty: {qty:.8f}",
    )


def notify_sell(
    chat_id: int,
    symbol: str,
    price: float,
    qty: float,
    bot_name: str,
    reason: str,
    pnl_pct: float,
) -> None:
    emoji = "🔴" if pnl_pct < 0 else "✅"
    notify_user(
        chat_id,
        f"{emoji} <b>SELL</b> executed\n"
        f"Bot: {bot_name}\n"
        f"Pair: <code>{symbol}</code>\n"
        f"Price: <b>{price:.8f}</b>\n"
        f"Qty: {qty:.8f}\n"
        f"Reason: <i>{reason}</i>\n"
        f"P&amp;L: <b>{pnl_pct:+.2f}%</b>",
    )


def notify_error(chat_id: int, bot_name: str, error: str) -> None:
    notify_user(
        chat_id,
        f"⚠️ <b>Bot Error</b>\n"
        f"Bot: {bot_name}\n"
        f"Error: <code>{error[:400]}</code>",
    )
