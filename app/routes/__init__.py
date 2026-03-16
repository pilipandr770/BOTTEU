from app.routes.auth import auth_bp
from app.routes.dashboard import dashboard_bp
from app.routes.bots import bots_bp
from app.routes.backtest import backtest_bp
from app.routes.subscriptions import subscriptions_bp
from app.routes.legal import legal_bp
from app.routes.guides import guides_bp
from app.routes.telegram_webhook import telegram_bp

__all__ = [
    "auth_bp", "dashboard_bp", "bots_bp", "backtest_bp",
    "subscriptions_bp", "legal_bp", "guides_bp", "telegram_bp",
]
