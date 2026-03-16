"""
Celery application factory.
Import `celery_app` in tasks and Celery CLI.
"""
import os
from celery import Celery
from celery.schedules import crontab

from app import create_app

flask_app = create_app(os.environ.get("FLASK_ENV", "production"))

celery_app = Celery(flask_app.name)
celery_app.config_from_object(
    {
        "broker_url": flask_app.config["CELERY_BROKER_URL"],
        "result_backend": flask_app.config["CELERY_RESULT_BACKEND"],
        "task_serializer": "json",
        "result_serializer": "json",
        "accept_content": ["json"],
        "timezone": "UTC",
        "enable_utc": True,
        # Beat schedule — every minute check all RUNNING bots
        "beat_schedule": {
            "run-all-bots": {
                "task": "app.workers.bot_runner.run_all_bots",
                "schedule": 60.0,  # every 60 seconds
            },
        },
    }
)


class ContextTask(celery_app.Task):
    """Run tasks inside Flask application context."""

    def __call__(self, *args, **kwargs):
        with flask_app.app_context():
            return super().__call__(*args, **kwargs)


celery_app.Task = ContextTask
