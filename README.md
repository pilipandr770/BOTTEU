# BOTTEU — Automated Binance Spot Trading Bot

[![Tests](https://github.com/pilipandr770/BOTTEU/actions/workflows/tests.yml/badge.svg)](https://github.com/pilipandr770/BOTTEU/actions/workflows/tests.yml)

> **Not financial advice. All trading involves significant risk of loss.**

## Overview

BOTTEU is a web application that automates Binance Spot trading for individual users. Users register, add their encrypted Binance API key, select a trading algorithm, and let bots run 24/7 with Telegram notifications.

**Spot only. No Futures. No leverage.**

Key features:
- 📊 **Real-time bot logs** — every tick produces human-readable log entries (MA values, RSI, SL/TP hits)
- 🧪 **Automatic simulation mode** — if the spot balance is below the order threshold, the bot runs in demo mode (no real orders placed, all trades logged as `🧪 DEMO`)
- 💰 **Live spot balance widget** — shows free balance per asset and whether real or demo trading is active
- 🤖 **In-process scheduler** — a background thread ticks all running bots every 60 seconds inside Flask (no Celery / Redis / separate worker process required)

---

## Tech Stack

| Layer          | Technology |
|----------------|------------|
| Backend        | Flask 3 + Gunicorn (gthread worker) |
| Database       | PostgreSQL (prod) / SQLite (dev) · SQLAlchemy · Flask-Migrate |
| Bot Scheduler  | Python `threading.Thread` (in-process, 60s interval) |
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
│   ├── __init__.py          # Flask app factory + in-process tick/collector threads
│   ├── config.py            # Config classes (dev / prod / test)
│   ├── extensions.py        # SQLAlchemy, Login, Babel, etc.
│   ├── models/              # User, ApiKey, Bot, BotLog, Order, Subscription, TelegramAccount
│   ├── routes/              # auth, dashboard, bots, backtest, subscriptions, legal, guides
│   ├── services/            # encryption, binance_client, order_manager, risk_manager, telegram_notifier
│   ├── algorithms/          # base (registry), ma_crossover, rsi, combined, consensus
│   ├── ml/                  # sklearn ensemble (features, trainer)
│   ├── ai/                  # Claude-based advisor, autopilot, scanner
│   ├── workers/
│   │   └── core/tick.py     # Core bot tick logic (signal → order), single source of truth
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
# The tick thread starts on the first incoming HTTP request (health checks count)
# No separate Celery / Redis / worker process needed
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
| `redis` | SSE pub/sub (live bot logs) + rate-limiter storage |
| `web` | Flask app + Gunicorn + in-process tick/collector threads |
| `nginx` | Reverse proxy (port 80/443), self-signed cert on first boot |
| `certbot` | Auto-renews TLS certificates (see "First-time SSL" for initial issuance) |

> **Why only 1 Gunicorn worker?**  
> The tick thread runs inside the Flask process. Multiple workers would each start their own tick thread → duplicate bot ticks → double orders. `gthread` workers provide concurrency via threads instead.

### First-time SSL (Let's Encrypt)

Before the first deploy, edit `nginx/nginx.conf` and replace `yourdomain.com` /
`www.yourdomain.com` with your real domain (both must already point at this
server). Until a real certificate exists, nginx boots with a temporary
self-signed one so `docker compose up` never crash-loops on a fresh host —
browsers will show a certificate warning until you run the real issuance below.

```bash
# Issue the real certificate (run once, after DNS points here and the stack is up):
docker compose run --rm --entrypoint \
  "certbot certonly --webroot -w /var/www/certbot -d yourdomain.com -d www.yourdomain.com --email you@example.com --agree-tos --no-eff-email" \
  certbot

# Reload nginx to pick up the real certificate:
docker compose exec nginx nginx -s reload
```

The `certbot` service itself only *renews* certificates on a loop — the command
above is a one-off override that performs the initial issuance.

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

## Testing

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

Runs on every push/PR via GitHub Actions (`.github/workflows/tests.yml`) on
Python 3.10 — the same version as the production Docker image. Covers
algorithms, the consensus/ML ensemble, risk management, and Binance order
placement (`order_manager.py`) with a mocked exchange client — no network
calls or real API keys needed.

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

