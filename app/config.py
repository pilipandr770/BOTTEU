import os
import sys
from dotenv import load_dotenv

load_dotenv()

# CURL_CA_BUNDLE / REQUESTS_CA_BUNDLE are Windows-only SSL fixes.
# On Linux (Render/Docker) these paths don't exist — unset them to avoid errors.
if sys.platform != "win32":
    os.environ.pop("CURL_CA_BUNDLE", None)
    os.environ.pop("REQUESTS_CA_BUNDLE", None)

class Config:
    # Core
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")
    DEBUG = False
    TESTING = False

    # Database
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "sqlite:///botteu_dev.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # If DB_SCHEMA is set, apply search_path so all tables go into that schema
    _db_schema = os.environ.get("DB_SCHEMA")
    SQLALCHEMY_ENGINE_OPTIONS = (
        {"connect_args": {"options": f"-csearch_path={_db_schema}"}}
        if _db_schema else {}
    )

    # Redis / Celery
    REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
    CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")

    # Security — Fernet key for API credentials
    FERNET_KEY = os.environ.get("FERNET_KEY", "")

    # Email
    MAIL_SERVER = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", 587))
    MAIL_USE_TLS = os.environ.get("MAIL_USE_TLS", "True") == "True"
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER", "BOTTEU <andrii.it.info@gmail.com>")

    # Telegram
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_WEBHOOK_URL = os.environ.get("TELEGRAM_WEBHOOK_URL", "")

    # Stripe
    STRIPE_PUBLIC_KEY = os.environ.get("STRIPE_PUBLIC_KEY", "")
    STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    STRIPE_PRICE_ID_PRO = os.environ.get("STRIPE_PRICE_ID_PRO", "")

    # App
    APP_URL = os.environ.get("APP_URL", "http://localhost:5000")

    # Flask-Babel
    LANGUAGES = ["en", "de"]
    BABEL_DEFAULT_LOCALE = "en"
    BABEL_DEFAULT_TIMEZONE = "UTC"

    # Flask-Limiter
    RATELIMIT_DEFAULT = "200 per day;50 per hour"
    RATELIMIT_STORAGE_URL = os.environ.get("REDIS_URL", "memory://")

    # ── Session / Cookie security ──────────────────────────────────────────
    SESSION_COOKIE_HTTPONLY = True      # JS cannot read the session cookie
    SESSION_COOKIE_SAMESITE = "Lax"    # CSRF mitigation for cross-site requests
    SESSION_COOKIE_SECURE = False       # Overridden to True in ProductionConfig
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SECURE = False      # Overridden to True in ProductionConfig
    REMEMBER_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = 86400  # 24 h in seconds


class DevelopmentConfig(Config):
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "sqlite:///botteu_dev.db")


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False


config_map = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}
