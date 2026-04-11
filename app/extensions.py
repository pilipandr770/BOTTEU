"""
Shared Flask extensions — initialised here to avoid circular imports.
Imported and registered in app/__init__.py (app factory).
"""
import logging

import redis as _redis_lib
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_mail import Mail
from flask_migrate import Migrate
from flask_wtf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_babel import Babel

db = SQLAlchemy()
login_manager = LoginManager()
mail = Mail()
migrate = Migrate()
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address)
babel = Babel()

login_manager.login_view = "auth.login"
login_manager.login_message_category = "warning"

_log = logging.getLogger(__name__)
_redis_pool: _redis_lib.ConnectionPool | None = None


def init_redis(url: str) -> None:
    """Initialise the shared Redis connection pool. Called from the app factory."""
    global _redis_pool
    try:
        _redis_pool = _redis_lib.ConnectionPool.from_url(url, decode_responses=False, max_connections=20)
    except Exception as exc:
        _log.warning("Redis pool init failed: %s — SSE pub/sub disabled.", exc)
        _redis_pool = None


def get_redis() -> "_redis_lib.Redis | None":
    """Return a Redis client from the shared pool, or *None* if Redis is unavailable."""
    if _redis_pool is None:
        return None
    try:
        return _redis_lib.Redis(connection_pool=_redis_pool)
    except Exception as exc:
        _log.debug("Redis client error: %s", exc)
        return None
