"""
Flask Application Factory
"""
import os
import threading
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
    from app.routes.risk import risk_bp
    from app.routes.tradingview import tv_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(bots_bp)
    app.register_blueprint(backtest_bp)
    app.register_blueprint(subscriptions_bp)
    app.register_blueprint(legal_bp)
    app.register_blueprint(guides_bp)
    app.register_blueprint(telegram_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(risk_bp)
    app.register_blueprint(tv_bp)

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

    # ── In-process Bot Scheduler ────────────────────────────────────────────
    # Plain threading.Thread loop — survives gunicorn fork, no APScheduler.
    _tick_thread_started = threading.Event()

    @app.before_request
    def _ensure_tick_thread():
        if _tick_thread_started.is_set():
            return
        _tick_thread_started.set()   # set first to prevent double-start
        def _loop():
            import time
            app.logger.info("Bot tick thread started (interval=60s)")
            while True:
                time.sleep(60)
                try:
                    with app.app_context():
                        from app.models.bot import Bot, BotStatus
                        from app.workers.core.tick import tick_bot
                        bots = Bot.query.filter_by(status=BotStatus.RUNNING).all()
                        app.logger.info("Tick: processing %d running bot(s)", len(bots))
                        for bot in bots:
                            tick_bot(bot.id)
                except Exception as exc:
                    app.logger.exception("Tick loop error: %s", exc)
        t = threading.Thread(target=_loop, daemon=True, name="bot-tick")
        t.start()

    # ── Telegram webhook auto-registration (production only) ─────────────────
    _tg_webhook = app.config.get("TELEGRAM_WEBHOOK_URL", "")
    _tg_token   = app.config.get("TELEGRAM_BOT_TOKEN", "")
    if _tg_webhook and _tg_token and "yourdomain.com" not in _tg_webhook:
        try:
            import requests as _req
            _resp = _req.post(
                f"https://api.telegram.org/bot{_tg_token}/setWebhook",
                json={"url": _tg_webhook},
                timeout=10,
            )
            app.logger.info("Telegram webhook registered: %s", _resp.json())
        except Exception as _exc:
            app.logger.warning("Telegram webhook registration failed: %s", _exc)

    # ── Security Headers ──────────────────────────────────────
    @app.after_request
    def set_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # HSTS — only effective over HTTPS (ignored over HTTP)
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        csp = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdn.plot.ly; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "img-src 'self' data:; "
            "font-src 'self' https://cdn.jsdelivr.net; "
            "connect-src 'self' https://cdn.jsdelivr.net; "
            "frame-ancestors 'none';"
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
