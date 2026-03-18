import multiprocessing

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


def post_fork(server, worker):
    """Restart APScheduler inside the worker process.

    Gunicorn forks worker processes after loading the app.
    Background threads (APScheduler) do NOT survive fork, so we
    must restart the scheduler in every worker after forking.
    With workers=1 this runs exactly once.
    """
    import app.workers.scheduler as sched_module
    import run
    sched_module._scheduler = None   # reset so start_scheduler doesn't bail early
    sched_module.start_scheduler(run.app)
