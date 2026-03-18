import multiprocessing

# Bind
bind = "0.0.0.0:10000"              # Render injects PORT=10000 by default

# Workers: 2-4 x CPU cores  (free tier: 1 CPU → 2 workers)
workers = multiprocessing.cpu_count() * 2 + 1

# Worker class — sync is fine for Flask (no async views)
worker_class = "sync"

# Timeouts
timeout = 120         # Binance API calls can be slow
keepalive = 5

# Logging
accesslog = "-"       # stdout → Render log viewer
errorlog = "-"
loglevel = "info"

# Run DB migrations at startup (safe with Alembic's lock mechanism)
def on_starting(server):
    import subprocess, sys
    subprocess.run(
        [sys.executable, "-m", "flask", "db", "upgrade"],
        env=__import__("os").environ | {"FLASK_APP": "run.py"},
        check=True,
    )
