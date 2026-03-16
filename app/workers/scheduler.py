"""
In-process APScheduler — bot tick engine without Celery.

Runs every 60 seconds in a background thread; core tick logic lives in
app/workers/core/tick.py (single source of truth shared with Celery runner).
"""
import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _tick_all(app) -> None:
    with app.app_context():
        from app.models.bot import Bot, BotStatus
        from app.workers.core.tick import tick_bot
        bots = Bot.query.filter_by(status=BotStatus.RUNNING).all()
        logger.info("Scheduler: ticking %d bot(s)", len(bots))
        for bot in bots:
            tick_bot(bot.id)


# ── Public API ────────────────────────────────────────────────────────────────

def start_scheduler(app) -> None:
    """Start the background scheduler. Safe to call multiple times (idempotent).

    With Werkzeug reloader (debug=True) only starts in the child process.
    In production (no reloader) always starts.
    """
    global _scheduler
    if _scheduler is not None:
        return  # Already running

    # Avoid double-start with Werkzeug reloader:
    # parent process has WERKZEUG_RUN_MAIN unset
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        logger.debug("Scheduler: skipping start in Werkzeug reloader parent")
        return

    import atexit
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        func=_tick_all,
        args=[app],
        trigger="interval",
        seconds=60,
        id="tick_all_bots",
        max_instances=1,
        replace_existing=True,
        misfire_grace_time=30,
    )
    _scheduler.start()
    atexit.register(lambda: _scheduler.shutdown(wait=False))
    logger.info("Bot scheduler started (interval=60s)")

    # Start Telegram in polling mode when there is no real webhook URL
    # (i.e. local dev or the placeholder hasn't been replaced yet)
    webhook_url = app.config.get("TELEGRAM_WEBHOOK_URL", "")
    if not webhook_url or "yourdomain.com" in webhook_url:
        try:
            from app.telegram.polling import start_polling
            start_polling(app)
        except Exception:
            logger.warning("Telegram polling could not start", exc_info=True)
