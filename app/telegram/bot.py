"""
Telegram Bot — entry point.
Registers handlers and (optionally) wraps them with a Flask app context
when running in polling mode.
"""
import logging
import os
from typing import Callable, Awaitable, Any

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


def _with_app_ctx(flask_app, func: Callable[..., Awaitable[Any]]) -> Callable:
    """Return an async wrapper that pushes a Flask app context around *func*."""
    async def _wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        with flask_app.app_context():
            await func(update, context)
    return _wrapper


def build_application(token: str | None = None, flask_app=None) -> Application:
    """Build a configured Application.

    Args:
        token:     Bot token; falls back to TELEGRAM_BOT_TOKEN env var.
        flask_app: If provided, every handler is wrapped to run inside a
                   Flask app context (needed for polling mode).
    """
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")

    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    handlers = [
        ("start",     cmd_start),
        ("status",    cmd_status),
        ("balance",   cmd_balance),
        ("start_bot", cmd_start_bot),
        ("stop_bot",  cmd_stop_bot),
        ("help",      cmd_help),
    ]
    for cmd, func in handlers:
        if flask_app is not None:
            func = _with_app_ctx(flask_app, func)
        app.add_handler(CommandHandler(cmd, func))

    return app
