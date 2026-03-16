"""
Telegram command handlers.
All handlers run inside a Flask app context (set up in routes/telegram_webhook.py).
"""
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 <b>BOTTEU Bot Commands</b>\n\n"
        "/start <code>  — Link your BOTTEU account with a 6-digit code\n"
        "/status        — List your running bots\n"
        "/balance       — Show Binance Spot balance\n"
        "/start_bot &lt;id&gt; — Start a bot by ID\n"
        "/stop_bot &lt;id&gt;  — Stop a bot by ID\n"
        "/help          — This message",
        parse_mode="HTML",
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start <6-digit-code>
    Links this Telegram chat to a BOTTEU user account.
    """
    from app.extensions import db
    from app.models.telegram_account import TelegramAccount

    args = context.args
    if not args:
        await update.message.reply_text(
            "👋 Welcome to BOTTEU!\n\n"
            "To link your account, go to your <b>BOTTEU cabinet → Telegram</b> "
            "and copy the 6-digit code, then send:\n"
            "<code>/start 123456</code>",
            parse_mode="HTML",
        )
        return

    code = args[0].strip()
    now = datetime.now(timezone.utc)

    tg_account = TelegramAccount.query.filter_by(link_code=code).first()

    if not tg_account:
        await update.message.reply_text("❌ Invalid code. Please generate a new one in your BOTTEU cabinet.")
        return

    if tg_account.link_code_expires_at and tg_account.link_code_expires_at < now:
        await update.message.reply_text("⏰ Code expired. Please generate a new one in your BOTTEU cabinet.")
        return

    tg_account.chat_id = update.effective_chat.id
    tg_account.is_verified = True
    tg_account.link_code = None
    db.session.commit()

    await update.message.reply_text(
        "✅ <b>Account linked successfully!</b>\n"
        "You will now receive trade notifications here.",
        parse_mode="HTML",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from app.models.telegram_account import TelegramAccount
    from app.models.bot import Bot, BotStatus

    tg = TelegramAccount.query.filter_by(chat_id=update.effective_chat.id, is_verified=True).first()
    if not tg:
        await update.message.reply_text("⚠️ Account not linked. Use /start <code> to link.")
        return

    bots = Bot.query.filter_by(user_id=tg.user_id).all()
    if not bots:
        await update.message.reply_text("You have no bots configured. Visit your BOTTEU dashboard.")
        return

    lines = ["🤖 <b>Your Bots</b>\n"]
    for bot in bots:
        icon = "🟢" if bot.status == BotStatus.RUNNING else ("🔴" if bot.status == BotStatus.ERROR else "⚫")
        lines.append(f"{icon} <b>#{bot.id}</b> {bot.name} — {bot.symbol} [{bot.algorithm}] {bot.status.value}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from app.models.telegram_account import TelegramAccount
    from app.services.binance_client import get_client_for_user
    from binance.exceptions import BinanceAPIException

    tg = TelegramAccount.query.filter_by(chat_id=update.effective_chat.id, is_verified=True).first()
    if not tg:
        await update.message.reply_text("⚠️ Account not linked.")
        return

    try:
        client = get_client_for_user(tg.user_id)
        account = client.get_account()
        balances = [
            b for b in account["balances"]
            if float(b["free"]) > 0 or float(b["locked"]) > 0
        ]
        if not balances:
            await update.message.reply_text("Your Spot wallet is empty.")
            return

        lines = ["💰 <b>Spot Balance</b>\n"]
        for b in balances[:15]:  # limit to 15 assets
            lines.append(f"<code>{b['asset']:<8}</code> Free: {float(b['free']):.6f}  Locked: {float(b['locked']):.6f}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except BinanceAPIException as exc:
        await update.message.reply_text(f"❌ Binance error: {exc.message}")
    except Exception as exc:
        await update.message.reply_text(f"❌ Error: {exc}")


async def cmd_start_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from app.extensions import db
    from app.models.telegram_account import TelegramAccount
    from app.models.bot import Bot, BotStatus

    tg = TelegramAccount.query.filter_by(chat_id=update.effective_chat.id, is_verified=True).first()
    if not tg:
        await update.message.reply_text("⚠️ Account not linked.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /start_bot <id>")
        return

    try:
        bot_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid bot ID.")
        return

    bot = Bot.query.filter_by(id=bot_id, user_id=tg.user_id).first()
    if not bot:
        await update.message.reply_text("Bot not found.")
        return

    bot.status = BotStatus.RUNNING
    bot.error_message = None
    db.session.commit()
    await update.message.reply_text(f"✅ Bot #{bot_id} <b>{bot.name}</b> started.", parse_mode="HTML")


async def cmd_stop_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from app.extensions import db
    from app.models.telegram_account import TelegramAccount
    from app.models.bot import Bot, BotStatus

    tg = TelegramAccount.query.filter_by(chat_id=update.effective_chat.id, is_verified=True).first()
    if not tg:
        await update.message.reply_text("⚠️ Account not linked.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /stop_bot <id>")
        return

    try:
        bot_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid bot ID.")
        return

    bot = Bot.query.filter_by(id=bot_id, user_id=tg.user_id).first()
    if not bot:
        await update.message.reply_text("Bot not found.")
        return

    bot.status = BotStatus.STOPPED
    db.session.commit()
    await update.message.reply_text(f"⏹ Bot #{bot_id} <b>{bot.name}</b> stopped.", parse_mode="HTML")
