"""
Bot Runner — Celery task wrapper.

Core tick logic lives in app/workers/core/tick.py (shared with APScheduler runner).
This module just wraps it as a Celery task with retry support.
"""
import logging

from app.workers.celery_app import celery_app
from app.extensions import db
from app.models.bot import Bot, BotStatus

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.bot_runner.run_bot", bind=True, max_retries=3)
def run_bot(self, bot_id: int) -> None:
    """Celery task: run one tick for the given bot."""
    from app.workers.core.tick import tick_bot
    try:
        tick_bot(bot_id)
    except Exception as exc:
        logger.exception("run_bot task error for bot %d: %s", bot_id, exc)
        raise self.retry(exc=exc, countdown=120)


@celery_app.task(name="app.workers.bot_runner.run_all_bots")
def run_all_bots() -> None:
    """Dispatched every 60s by Celery Beat — spawns individual tasks per running bot."""
    running_bots = Bot.query.filter_by(status=BotStatus.RUNNING).all()
    for bot in running_bots:
        run_bot.delay(bot.id)
    logger.info("Dispatched %d bot tasks", len(running_bots))


