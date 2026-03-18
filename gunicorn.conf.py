# Bind — Render/Docker expose port 5000
bind = "0.0.0.0:5000"

# IMPORTANT: keep workers=1 so only one APScheduler instance runs.
# Use threads for concurrency instead.
workers = 1
threads = 4
worker_class = "gthread"

# Timeouts
timeout = 120
keepalive = 5

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"


def on_starting(server):
    """Run DB migrations before workers start."""
    import subprocess, sys, os
    subprocess.run(
        [sys.executable, "-m", "flask", "db", "upgrade"],
        env=os.environ | {"FLASK_APP": "run.py"},
        check=True,
    )
