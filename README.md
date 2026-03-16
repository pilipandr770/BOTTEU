# BOTTEU — Automated Binance Spot Trading Bot

> **Not financial advice. All trading involves significant risk of loss.**

## Overview

BOTTEU is a web application that automates Binance Spot trading for individual users. Users register, add their encrypted Binance API key, select a trading algorithm, and let bots run 24/7 with Telegram notifications.

**Spot only. No Futures. No leverage.**

Key features:
- 📊 **Real-time bot logs** — every tick produces human-readable log entries (MA values, RSI, SL/TP hits)
- 🧪 **Automatic simulation mode** — if the spot balance is below the order threshold, the bot runs in demo mode (no real orders placed, all trades logged as `🧪 DEMO`)
- 💰 **Live spot balance widget** — shows free balance per asset and whether real or demo trading is active
- 🤖 **In-process scheduler** — APScheduler runs bot ticks every 60 seconds inside Flask (no Celery / Redis required)

---

## Tech Stack

| Layer          | Technology |
|----------------|------------|
| Backend        | Flask 3 + Gunicorn (gthread worker) |
| Database       | PostgreSQL (prod) / SQLite (dev) · SQLAlchemy · Flask-Migrate |
| Bot Scheduler  | APScheduler 3.10 (in-process background thread) |
| Trading        | python-binance, pandas |
| Historical Data | yfinance (Yahoo Finance) |
| Visualization  | Plotly |
| Telegram       | python-telegram-bot v21 (webhook) |
| Security       | Fernet AES encryption, bcrypt, Flask-WTF CSRF, Flask-Limiter |
| i18n           | Flask-Babel (EN + DE) |
| Payments       | Stripe |
| Deploy         | Docker + Docker Compose + Nginx + Certbot (Let's Encrypt) |

---

## Project Structure

```
BOTTEU/
├── app/
│   ├── __init__.py          # Flask app factory + APScheduler start
│   ├── config.py            # Config classes (dev / prod / test)
│   ├── extensions.py        # SQLAlchemy, Login, Babel, etc.
│   ├── models/              # User, ApiKey, Bot, BotLog, Order, Subscription, TelegramAccount
│   ├── routes/              # auth, dashboard, bots, backtest, subscriptions, legal, guides
│   ├── services/            # encryption, binance_client, order_manager, telegram_notifier
│   ├── algorithms/          # base (registry), ma_crossover, rsi, combined
│   ├── workers/
│   │   ├── scheduler.py     # APScheduler tick engine (replaces Celery)
│   │   └── bot_runner.py    # Core bot logic (signal → order)
│   ├── telegram/            # bot, handlers
│   ├── templates/           # Jinja2 HTML templates
│   ├── static/              # CSS, JS
│   └── translations/        # EN + DE strings (Flask-Babel)
├── nginx/nginx.conf
├── docker-compose.yml
├── Dockerfile
├── entrypoint.sh            # DB migrate → Gunicorn start
├── run.py
├── requirements.txt
└── .env.example
```

---

## Quick Start (Development)

### 1. Clone and set up environment

```bash
git clone https://github.com/pilipandr770/BOTTEU.git
cd BOTTEU
cp .env.example .env
# Edit .env — at minimum: SECRET_KEY, FERNET_KEY, DATABASE_URL
```

### 2. Generate required secrets

```bash
# Fernet key (encrypts stored Binance API keys)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Flask secret key
python -c "import secrets; print(secrets.token_hex(32))"
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Initialize database

```bash
flask db upgrade        # applies all existing migrations
```

### 5. Run development server

```bash
python run.py
# Bot scheduler starts automatically in the background (APScheduler)
# No separate Celery / Redis process needed
```

Open http://localhost:5000

---

## Production Deployment (Docker)

```bash
cp .env.example .env
# Fill in ALL values — especially SECRET_KEY, FERNET_KEY, POSTGRES_PASSWORD

docker compose up -d --build
```

Services started:
| Service | Role |
|---------|------|
| `postgres` | PostgreSQL 16 database |
| `web` | Flask app + Gunicorn + APScheduler |
| `nginx` | Reverse proxy (port 80/443) |
| `certbot` | Auto-renews TLS certificates |

> **Why only 1 Gunicorn worker?**  
> APScheduler runs inside the Flask process. Multiple workers would each start their own scheduler → duplicate bot ticks → double orders. `gthread` workers provide concurrency via threads instead.

### First-time SSL (Let's Encrypt)

```bash
# Issue certificate (run once):
docker compose --profile certbot run --rm certbot

# After cert is issued, reload nginx:
docker compose exec nginx nginx -s reload
```

### Set up Telegram webhook

```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://yourdomain.com/telegram/webhook"
```

---

## Simulation (Demo) Mode

If the free spot balance (USDT / USDC) is below `max(5 USDT, position_size_usdt)`:

- The bot **analyses the market normally** and writes full logs
- All trades are placed as **demo orders** (`is_simulated=True`) — no real Binance API calls
- Logs are prefixed with `🧪 [ДЕМО]`
- The dashboard shows a yellow **"Simulation mode"** banner explaining the deficit

To switch to real trading: top up the Binance Spot wallet to at least the configured position size.

---

## Bot Logs

Each tick produces human-readable log lines visible on the bot detail page (`/bots/<id>`):

```
📊 MA7=73 356 > MA25=71 859 — восходящий тренд, нет пересечения, ждём сигнала
🟢 Золотой крест: MA7 пересекла MA25 снизу вверх — покупаем по 97.36
🛑 Стоп-лосс: цена 64 100 упала ниже SL 64 500 (−2%) — продаём
🧪 [ДЕМО] Баланс 0.0001 USDT < нужно 50 USDT — реальных сделок нет
```

Log panel auto-refreshes every 30 seconds via polling.

---

## Algorithms

### MA Crossover (MA7 × MA25)
- **BUY**: Fast MA crosses above Slow MA (golden cross)
- **SELL**: Death cross OR optional SL / TP / Trailing TP

### RSI
- **BUY**: RSI drops below oversold threshold (default 30)
- **SELL**: RSI rises above overbought threshold (default 70) OR Stop-Loss (required)

### Combined (MA + RSI)
- Combines both signals with AND / OR logic (configurable)

### Adding New Algorithms
1. Create `app/algorithms/my_algo.py` extending `BaseStrategy`
2. Register in `app/algorithms/base.py` → `_build_registry()`
3. Add parameter form section in `templates/bots/create.html`
4. Emit `state["_log"] = [(level, message), …]` for log entries

---

## Security

- API keys encrypted with Fernet (AES-128-CBC); master key in `.env` only
- API Secret never displayed after saving
- CSRF tokens on all forms (Flask-WTF)
- Rate limiting on auth endpoints (Flask-Limiter)
- HTTPS enforced via Nginx + Let's Encrypt
- GDPR: account deletion anonymizes all personal data (Art. 17)
- Binance API: whitelist the bot's IP and enable Spot read/trade only

---

## Legal

- `/legal/terms` — Terms of Service  
- `/legal/privacy` — Privacy Policy  
- `/legal/disclaimer` — Risk Disclaimer  
- `/legal/impressum` — Impressum (German legal requirement)

---

## License

Proprietary. All rights reserved. © 2026 BOTTEU.


---

## Project Structure

```
BOTTEU/
├── app/
│   ├── __init__.py         # Flask app factory
│   ├── config.py           # Config classes (dev/prod/test)
│   ├── extensions.py       # SQLAlchemy, Login, Babel, etc.
│   ├── models/             # User, ApiKey, Bot, Order, Subscription, TelegramAccount
│   ├── routes/             # auth, dashboard, bots, backtest, subscriptions, legal, guides
│   ├── services/           # encryption, binance_client, order_manager, telegram_notifier
│   ├── algorithms/         # base (registry), ma_crossover, rsi
│   ├── workers/            # celery_app, bot_runner
│   ├── telegram/           # bot, handlers
│   ├── templates/          # Jinja2 HTML templates
│   ├── static/             # CSS, JS
│   └── translations/       # EN + DE strings (Flask-Babel)
├── nginx/nginx.conf
├── docker-compose.yml
├── Dockerfile
├── run.py
├── requirements.txt
└── .env.example
```

---

## Quick Start (Development)

### 1. Clone and set up environment

```bash
git clone <repo>
cd BOTTEU
cp .env.example .env
# Edit .env with your keys
```

### 2. Generate Fernet key

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Paste the output into FERNET_KEY in .env
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Initialize database

```bash
flask db init
flask db migrate -m "initial"
flask db upgrade
```

### 5. Run development server

```bash
python run.py
```

### 6. Start Celery worker (separate terminal)

```bash
celery -A app.workers.celery_app.celery_app worker --loglevel=info
```

### 7. Start Celery Beat (separate terminal)

```bash
celery -A app.workers.celery_app.celery_app beat --loglevel=info
```

---

## Production Deployment (Docker)

```bash
cp .env.example .env
# Fill in all values in .env

docker compose up -d --build
```

### Set up Telegram webhook

```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://yourdomain.com/telegram/webhook"
```

### SSL (Let's Encrypt)

```bash
docker compose run --rm certbot certonly --webroot \
  --webroot-path=/var/www/certbot \
  -d yourdomain.com -d www.yourdomain.com \
  --email your@email.com --agree-tos --no-eff-email
```

---

## Algorithms

### MA Crossover (MA7 × MA25)
- **BUY**: Fast MA crosses above Slow MA (golden cross)
- **SELL**: Fast MA crosses below (death cross) OR optional SL/TP/Trailing TP
- SL/TP: Optional

### RSI
- **BUY**: RSI drops below oversold threshold (30 or 20)
- **SELL**: RSI rises above overbought threshold (70 or 80) OR Stop-Loss (required)
- SL: **Required**; TP/Trailing TP: Optional

### Adding New Algorithms
1. Create `app/algorithms/my_algo.py` extending `BaseStrategy`
2. Register in `app/algorithms/base.py` → `_build_registry()`
3. Add parameter form section in `templates/bots/create.html`

---

## Security

- API keys encrypted with Fernet (AES-128-CBC). Master key stored ONLY in `.env`
- API Secret never displayed in UI after saving
- CSRF tokens on all forms (Flask-WTF)
- Rate limiting on auth endpoints (Flask-Limiter)
- HTTPS enforced via Nginx + Let's Encrypt
- GDPR: account deletion anonymizes all personal data (Art. 17)
- Binance API: instructions to whitelist bot IP and enable Spot only

---

## Legal

All legal documents located at:
- `/legal/terms` — Terms of Service
- `/legal/privacy` — Privacy Policy
- `/legal/disclaimer` — Risk Disclaimer
- `/legal/impressum` — Impressum (German legal requirement)

---

## License

Proprietary. All rights reserved. © 2026 BOTTEU.
