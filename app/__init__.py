"""
Flask Application Factory
"""
import os
from flask import Flask, request, session

from app.config import config_map
from app.extensions import db, login_manager, mail, migrate, csrf, limiter, babel


def create_app(config_name: str | None = None) -> Flask:
    if config_name is None:
        config_name = os.environ.get("FLASK_ENV", "development")

    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.config.from_object(config_map.get(config_name, config_map["development"]))

    # ── Extensions ────────────────────────────────────────────
    db.init_app(app)
    login_manager.init_app(app)
    mail.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    limiter.init_app(app)
    babel.init_app(app, locale_selector=_get_locale)

    # Inject get_locale into every Jinja2 template
    from flask_babel import get_locale as babel_get_locale
    app.jinja_env.globals["get_locale"] = babel_get_locale

    # ── Register Blueprints ───────────────────────────────────
    from app.routes.auth import auth_bp
    from app.routes.dashboard import dashboard_bp
    from app.routes.bots import bots_bp
    from app.routes.backtest import backtest_bp
    from app.routes.subscriptions import subscriptions_bp
    from app.routes.legal import legal_bp
    from app.routes.guides import guides_bp
    from app.routes.telegram_webhook import telegram_bp
    from app.routes.admin import admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(bots_bp)
    app.register_blueprint(backtest_bp)
    app.register_blueprint(subscriptions_bp)
    app.register_blueprint(legal_bp)
    app.register_blueprint(guides_bp)
    app.register_blueprint(telegram_bp)
    app.register_blueprint(admin_bp)

    # Landing page
    from flask import render_template
    @app.route("/home")
    @app.route("/landing")
    def landing():
        return render_template("landing.html")

    # Health-check (used by Docker HEALTHCHECK and load-balancers)
    from flask import jsonify
    @app.route("/health")
    def health():
        return jsonify({"status": "ok"}), 200

    # ── In-process Bot Scheduler (replaces Celery for simple deployments) ────
    from app.workers.scheduler import start_scheduler
    start_scheduler(app)

    # ── Security Headers ──────────────────────────────────────
    @app.after_request
    def set_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        csp = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdn.plot.ly; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "img-src 'self' data:; "
            "font-src 'self' https://cdn.jsdelivr.net; "
            "connect-src 'self';"
        )
        response.headers["Content-Security-Policy"] = csp
        return response

    return app


def _get_locale() -> str:
    # 1. User explicitly chose language
    lang = session.get("lang")
    if lang in ("en", "de"):
        return lang
    # 2. Browser preference
    return request.accept_languages.best_match(["en", "de"], default="en")
