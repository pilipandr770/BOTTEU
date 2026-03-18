#!/bin/sh
# Entrypoint for the BOTTEU web container.
# Gunicorn is configured via gunicorn.conf.py:
#   - 1 worker + 4 threads (gthread) — keeps a single APScheduler instance
#   - on_starting hook: flask db upgrade
#   - post_fork hook: restarts APScheduler in the worker process

set -e

exec gunicorn -c gunicorn.conf.py run:app
