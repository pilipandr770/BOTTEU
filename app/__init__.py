"""
Flask Application Factory
"""
import json
import logging
import os
import threading
from datetime import datetime, timezone
from flask import Flask, request, session

from app.config import config_map
from app.extensions import db, login_manager, mail, migrate, csrf, limiter, babel, init_redis


class _JSONFormatter(logging.Formatter):
    """Emit one JSON object per log record — structured, Render/Docker friendly."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time":    datetime.now(timezone.utc).isoformat(),
            "level":   record.levelname,
            "logger":  record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _configure_logging(debug: bool = False) -> None:
    """Replace the root handler with a JSON formatter for structured output."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    # Remove existing handlers (e.g. basicConfig default StreamHandler)
    for h in root.handlers[:]:
        root.removeHandler(h)
    handler = logging.StreamHandler()
    handler.setFormatter(_JSONFormatter())
    root.addHandler(handler)


def create_app(config_name: str | None = None) -> Flask:
    if config_name is None:
        config_name = os.environ.get("FLASK_ENV", "development")

    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    cfg = config_map.get(config_name, config_map["development"])
    if hasattr(cfg, "_validate"):
        cfg._validate()
    app.config.from_object(cfg)

    _configure_logging(debug=app.config.get("DEBUG", False))

    # ── Extensions ────────────────────────────────────────────
    db.init_app(app)
    login_manager.init_app(app)
    mail.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    limiter.init_app(app)
    babel.init_app(app, locale_selector=_get_locale)
    init_redis(app.config.get("REDIS_URL", "redis://localhost:6379/0"))

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
    from app.routes.ai_advisor import ai_bp
    from app.routes.ml import ml_bp

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
    app.register_blueprint(ai_bp)
    app.register_blueprint(ml_bp)

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

    # ── Inline Binance Collector Thread ────────────────────────────────────
    # Streams 1m klines → CSVs → all timeframes, running inside this process.
    # Enabled when COLLECTOR_SYMBOLS is set (non-empty).
    _collector_thread_started = threading.Event()

    @app.before_request
    def _ensure_collector_thread():
        if _collector_thread_started.is_set():
            return
        symbols_cfg = app.config.get("COLLECTOR_SYMBOLS", "").strip()
        if not symbols_cfg:
            return
        _collector_thread_started.set()  # prevent double-start

        symbols = [s.strip().upper() for s in symbols_cfg.split(",") if s.strip()]
        data_dir = app.config.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
        roll_window = int(app.config.get("COLLECTOR_ROLL_WINDOW", 7770))

        def _run_collector():
            import sys as _sys
            import asyncio as _asyncio

            # Add collector/ directory to sys.path so `import collector` works.
            collector_dir = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "collector")
            )
            if collector_dir not in _sys.path:
                _sys.path.insert(0, collector_dir)

            os.makedirs(data_dir, exist_ok=True)

            # Set env vars before the module is imported (module reads them at import time).
            os.environ["DATA_DIR"] = data_dir
            os.environ["SYMBOLS"] = ",".join(symbols)
            os.environ["ROLL_WINDOW"] = str(roll_window)
            os.environ["HTTP_PORT"] = "0"   # no HTTP server in embedded mode

            try:
                import collector as _col
                # Override any already-resolved module-level constants.
                _col.DATA_DIR = data_dir
                _col.SYMBOLS = symbols
                _col.ROLL_WINDOW = roll_window
                _col.HTTP_PORT = 0
                app.logger.info(
                    "Collector thread starting: symbols=%s data_dir=%s",
                    symbols, data_dir,
                )
                _asyncio.run(_col.main())
            except Exception:
                app.logger.exception("Collector thread crashed — will not restart automatically")

        t_col = threading.Thread(target=_run_collector, daemon=True, name="botteu-collector")
        t_col.start()

    # ── Telegram webhook auto-registration (production only) ─────────────────
    _tg_webhook = app.config.get("TELEGRAM_WEBHOOK_URL", "")
    _tg_token   = app.config.get("TELEGRAM_BOT_TOKEN", "")
    if _tg_webhook and _tg_token and "yourdomain.com" not in _tg_webhook:
        try:
            import requests as _req
            from app.routes.telegram_webhook import _webhook_secret
            _resp = _req.post(
                f"https://api.telegram.org/bot{_tg_token}/setWebhook",
                json={"url": _tg_webhook, "secret_token": _webhook_secret(_tg_token)},
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
