"""
Telegram command handlers.
All handlers run inside a Flask app context (polling: via _with_app_ctx wrapper;
webhook: Flask request context is already active).
"""
import logging
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


# ── Shared helpers ────────────────────────────────────────────────────────────

def _main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 My Bots", callback_data="cb_status"),
            InlineKeyboardButton("💰 Balance", callback_data="cb_balance"),
        ],
        [
            InlineKeyboardButton("▶️ Start bot", callback_data="cb_start_hint"),
            InlineKeyboardButton("⏹ Stop bot",  callback_data="cb_stop_hint"),
        ],
        [InlineKeyboardButton("❓ Help", callback_data="cb_help")],
    ])


async def _send_status(reply_fn, chat_id: int) -> None:
    from app.models.telegram_account import TelegramAccount
    from app.models.bot import Bot, BotStatus as BS

    tg = TelegramAccount.query.filter_by(chat_id=chat_id, is_verified=True).first()
    if not tg:
        await reply_fn("⚠️ Account not linked. Use /start &lt;code&gt; to link.")
        return

    bots = Bot.query.filter_by(user_id=tg.user_id).all()
    if not bots:
        await reply_fn("You have no bots configured. Visit your BOTTEU dashboard.",
                       reply_markup=_main_menu())
        return

    lines = ["🤖 <b>Your Bots</b>\n"]
    for bot in bots:
        icon = ("🟢" if bot.status == BS.RUNNING
                else "🔴" if bot.status == BS.ERROR
                else "⚫")
        lines.append(
            f"{icon} <b>#{bot.id}</b> {bot.name} — {bot.symbol} "
            f"[{bot.algorithm}] <i>{bot.status.value}</i>"
        )
    lines.append("\n<i>Use /start_bot &lt;id&gt; or /stop_bot &lt;id&gt; to control bots.</i>")
    await reply_fn("\n".join(lines), parse_mode="HTML", reply_markup=_main_menu())


async def _send_balance(reply_fn, chat_id: int) -> None:
    from app.models.telegram_account import TelegramAccount
    from app.services.binance_client import get_client_for_user
    from binance.exceptions import BinanceAPIException

    tg = TelegramAccount.query.filter_by(chat_id=chat_id, is_verified=True).first()
    if not tg:
        await reply_fn("⚠️ Account not linked.")
        return

    try:
        client = get_client_for_user(tg.user_id)
        account = client.get_account()
        balances = [
            b for b in account["balances"]
            if float(b["free"]) > 0 or float(b["locked"]) > 0
        ]
        if not balances:
            await reply_fn("Your Spot wallet is empty.", reply_markup=_main_menu())
            return

        lines = ["💰 <b>Spot Balance</b>\n"]
        for b in balances[:15]:
            lines.append(
                f"<code>{b['asset']:<8}</code> "
                f"Free: {float(b['free']):.6f}  "
                f"Locked: {float(b['locked']):.6f}"
            )
        await reply_fn("\n".join(lines), parse_mode="HTML", reply_markup=_main_menu())

    except BinanceAPIException as exc:
        await reply_fn(f"❌ Binance error: {exc.message}", reply_markup=_main_menu())
    except Exception as exc:
        await reply_fn(f"❌ Error: {exc}", reply_markup=_main_menu())


# ── PTB error handler ─────────────────────────────────────────────────────────

async def ptb_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log exceptions and notify the user so they never see silent failures."""
    logger.error("PTB handler exception for update %s", update, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "❌ Something went wrong. Please try again or use /help."
            )
        except Exception:
            pass


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 <b>BOTTEU Bot Commands</b>\n\n"
        "/start &lt;code&gt;     — Link your BOTTEU account with a 6-digit code\n"
        "/status             — List all your bots and their status\n"
        "/balance            — Show Binance Spot balance\n"
        "/start_bot &lt;id&gt;   — Start a bot by ID\n"
        "/stop_bot &lt;id&gt;    — Stop a bot by ID\n"
        "/help               — This message",
        parse_mode="HTML",
        reply_markup=_main_menu(),
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
            "👋 <b>Welcome to BOTTEU!</b>\n\n"
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
        await update.message.reply_text(
            "❌ Invalid code. Please generate a new one in your BOTTEU cabinet."
        )
        return

    expires_at = tg_account.link_code_expires_at
    if expires_at:
        # SQLite may return naive UTC datetimes — normalise before comparing
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < now:
            await update.message.reply_text(
                "⏰ Code expired. Please generate a new one in your BOTTEU cabinet."
            )
            return

    tg_account.chat_id = update.effective_chat.id
    tg_account.is_verified = True
    tg_account.link_code = None
    db.session.commit()

    await update.message.reply_text(
        "✅ <b>Account linked successfully!</b>\n"
        "You will now receive trade notifications here.\n\n"
        "What would you like to do?",
        parse_mode="HTML",
        reply_markup=_main_menu(),
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_status(update.message.reply_text, update.effective_chat.id)


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_balance(update.message.reply_text, update.effective_chat.id)


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
    await update.message.reply_text(
        f"⏹ Bot #{bot_id} <b>{bot.name}</b> stopped.",
        parse_mode="HTML",
        reply_markup=_main_menu(),
    )


# ── Inline keyboard callback handler ─────────────────────────────────────────

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline-keyboard button presses."""
    query = update.callback_query
    await query.answer()  # remove the loading spinner in Telegram

    chat_id = query.from_user.id
    # reply_fn that sends a NEW message (keeps the original menu message intact)
    reply_fn = query.message.reply_text

    data = query.data

    if data == "cb_status":
        await _send_status(reply_fn, chat_id)

    elif data == "cb_balance":
        await _send_balance(reply_fn, chat_id)

    elif data == "cb_help":
        await reply_fn(
            "🤖 <b>BOTTEU Bot Commands</b>\n\n"
            "/start &lt;code&gt;     — Link your account\n"
            "/status             — List bots\n"
            "/balance            — Binance Spot balance\n"
            "/start_bot &lt;id&gt;   — Start a bot\n"
            "/stop_bot &lt;id&gt;    — Stop a bot\n"
            "/help               — This message",
            parse_mode="HTML",
            reply_markup=_main_menu(),
        )

    elif data == "cb_start_hint":
        await reply_fn(
            "▶️ To start a bot, first check its ID with 📊 My Bots, then send:\n"
            "<code>/start_bot &lt;id&gt;</code>",
            parse_mode="HTML",
            reply_markup=_main_menu(),
        )

    elif data == "cb_stop_hint":
        await reply_fn(
            "⏹ To stop a bot, first check its ID with 📊 My Bots, then send:\n"
            "<code>/stop_bot &lt;id&gt;</code>",
            parse_mode="HTML",
            reply_markup=_main_menu(),
        )
