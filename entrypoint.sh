#!/bin/sh
# Entrypoint for the BOTTEU web container.
# 1. Runs DB migrations (safe to re-run; idempotent).
# 2. Starts Gunicorn with 1 worker + 4 threads.
#
# WHY --workers 1?
#   APScheduler runs inside the Flask process. Multiple Gunicorn workers would
#   each start their own scheduler → multiple bots ticking simultaneously →
#   duplicate orders. Using threads keeps concurrency without spawning extra
#   scheduler instances.

set -e

echo "==> Running DB migrations…"
flask db upgrade

echo "==> Starting Gunicorn…"
exec gunicorn \
    --bind 0.0.0.0:5000 \
    --workers 1 \
    --threads 4 \
    --worker-class gthread \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    run:app
