"""Guides & FAQ blueprint."""
import threading
from functools import wraps

from flask import Blueprint, render_template, jsonify, abort
from flask_login import current_user, login_required

guides_bp = Blueprint("guides", __name__, url_prefix="/guides")


def _admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


@guides_bp.route("/")
def index():
    return render_template("guides/index.html")


@guides_bp.route("/status")
@login_required
@_admin_required
def status():
    """Diagnostic: check if tick thread is running. Admin-only."""
    tick_thread = next((t for t in threading.enumerate() if t.name == "bot-tick"), None)
    return jsonify({
        "tick_thread_running": tick_thread is not None and tick_thread.is_alive(),
        "all_threads": [t.name for t in threading.enumerate()],
    })
