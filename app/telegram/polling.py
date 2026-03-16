"""
Telegram long-polling mode — for local/dev when no public HTTPS URL is available.

Usage:
    from app.telegram.polling import start_polling
    start_polling(flask_app)
"""
import asyncio
import logging
import threading

logger = logging.getLogger(__name__)

_polling_thread: threading.Thread | None = None


def start_polling(flask_app) -> None:
    """Start the Telegram bot in long-polling mode in a daemon thread.

    Safe to call multiple times (no-op if already running).
    """
    global _polling_thread
    if _polling_thread and _polling_thread.is_alive():
        return

    token = flask_app.config.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        logger.warning("Telegram polling skipped: TELEGRAM_BOT_TOKEN not set")
        return

    def _run() -> None:
        async def _main() -> None:
            # Build inside the coroutine so PTB asyncio objects bind to this loop
            from app.telegram.bot import build_application
            application = build_application(token=token, flask_app=flask_app)
            async with application:
                await application.start()
                # drop_pending_updates=False so messages sent while app was down
                # aren't silently discarded — the bot will process them on startup
                await application.updater.start_polling(drop_pending_updates=False)
                logger.info("Telegram polling active — awaiting updates …")
                await asyncio.Event().wait()  # block until daemon thread is killed

        try:
            asyncio.run(_main())
        except Exception:
            logger.exception("Telegram polling thread crashed")

    _polling_thread = threading.Thread(target=_run, daemon=True, name="telegram-polling")
    _polling_thread.start()
    logger.info("Telegram polling thread started")
