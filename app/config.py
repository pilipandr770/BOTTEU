import os
import sys
from dotenv import load_dotenv

load_dotenv()

# CURL_CA_BUNDLE / REQUESTS_CA_BUNDLE are Windows-only SSL fixes.
# On Linux (Render/Docker) these paths don't exist — unset them to avoid errors.
if sys.platform != "win32":
    os.environ.pop("CURL_CA_BUNDLE", None)
    os.environ.pop("REQUESTS_CA_BUNDLE", None)

# ── Project-level base directory ───────────────────────────────────────────
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
    # Subscription plan price IDs (create in Stripe Dashboard → Products)
    STRIPE_PRICE_ID_BASIC = os.environ.get("STRIPE_PRICE_ID_BASIC", "")   # €200/mo
    STRIPE_PRICE_ID_PRO   = os.environ.get("STRIPE_PRICE_ID_PRO", "")     # €500/mo
    STRIPE_PRICE_ID_ELITE = os.environ.get("STRIPE_PRICE_ID_ELITE", "")   # €1000/mo
    # One-time consultation payment link (Stripe Payment Link or Price ID)
    STRIPE_PRICE_ID_CONSULTATION = os.environ.get("STRIPE_PRICE_ID_CONSULTATION", "")  # €100

    # App
    APP_URL = os.environ.get("APP_URL", "http://localhost:5000")

    # Anthropic AI Advisor
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

    # Render static outbound IPs (comma-separated) to show users for Binance whitelisting
    RENDER_OUTBOUND_IPS = [
        ip.strip()
        for ip in os.environ.get("RENDER_OUTBOUND_IPS", "").split(",")
        if ip.strip()
    ]

    # Flask-Babel
    LANGUAGES = ["en", "de"]
    BABEL_DEFAULT_LOCALE = "en"
    BABEL_DEFAULT_TIMEZONE = "UTC"

    # Flask-Limiter
    RATELIMIT_DEFAULT = "200 per day;50 per hour"
    RATELIMIT_STORAGE_URI = os.environ.get("REDIS_URL", "memory://")
    RATELIMIT_STORAGE_URL = RATELIMIT_STORAGE_URI

    # ── Session / Cookie security ──────────────────────────────────────────
    SESSION_COOKIE_HTTPONLY = True      # JS cannot read the session cookie
    SESSION_COOKIE_SAMESITE = "Lax"    # CSRF mitigation for cross-site requests
    SESSION_COOKIE_SECURE = False       # Overridden to True in ProductionConfig
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SECURE = False      # Overridden to True in ProductionConfig
    REMEMBER_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = 86400  # 24 h in seconds

    # ── Filesystem paths (can be overridden per deploy via env vars) ──────
    DATA_DIR = os.environ.get("DATA_DIR", os.path.join(_BASE_DIR, "data"))
    ML_MODELS_DIR = os.environ.get(
        "ML_MODELS_DIR",
        os.path.join(_BASE_DIR, "instance", "ml_models"),
    )

    # ── Collector HTTP service URL ─────────────────────────────────────────
    # When set, the web app fetches CSVs from the collector over HTTP as a
    # fallback when no local file exists (used on Render where shared volumes
    # are not available between services).
    # Example: https://botteu-collector.onrender.com
    COLLECTOR_BASE_URL: str = os.environ.get("COLLECTOR_BASE_URL", "").rstrip("/")


class DevelopmentConfig(Config):
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "sqlite:///botteu_dev.db")


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    @classmethod
    def _validate(cls):
        import os
        if os.environ.get("SECRET_KEY", "") in ("", "dev-secret-key-change-in-production"):
            raise RuntimeError(
                "SECRET_KEY env var is not set or still uses the default dev value. "
                "Set a strong random SECRET_KEY before running in production."
            )


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False


config_map = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}
