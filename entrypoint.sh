#!/bin/sh
# Entrypoint for the BOTTEU web container.
# Gunicorn is configured via gunicorn.conf.py:
#   - 1 worker + 4 threads (gthread) — keeps a single in-process tick thread
#     (see app/__init__.py _ensure_tick_thread); more workers would duplicate it
#   - on_starting hook: flask db upgrade

set -e

exec gunicorn -c gunicorn.conf.py run:app
