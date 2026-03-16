"""
Telegram Bot — webhook mode entry point.
Registers handlers and sets the webhook URL on startup.
"""
import logging
import os

from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes

from app.telegram.handlers import (
    cmd_start,
    cmd_status,
    cmd_balance,
    cmd_start_bot,
    cmd_stop_bot,
    cmd_help,
)

logger = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    """Set bot commands visible in Telegram menu."""
    await application.bot.set_my_commands([
        BotCommand("start", "Link your BOTTEU account"),
        BotCommand("status", "Show running bots"),
        BotCommand("balance", "Show Spot balance"),
        BotCommand("start_bot", "Start a bot: /start_bot <id>"),
        BotCommand("stop_bot", "Stop a bot: /stop_bot <id>"),
        BotCommand("help", "Show available commands"),
    ])


def build_application() -> Application:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")

    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("start_bot", cmd_start_bot))
    app.add_handler(CommandHandler("stop_bot", cmd_stop_bot))
    app.add_handler(CommandHandler("help", cmd_help))

    return app
