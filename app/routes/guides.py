"""Guides & FAQ blueprint."""
from flask import Blueprint, render_template, jsonify

guides_bp = Blueprint("guides", __name__, url_prefix="/guides")


@guides_bp.route("/")
def index():
    return render_template("guides/index.html")


@guides_bp.route("/status")
def status():
    """Diagnostic: check if tick thread is running."""
    import threading
    tick_thread = next((t for t in threading.enumerate() if t.name == "bot-tick"), None)
    return jsonify({
        "tick_thread_running": tick_thread is not None and tick_thread.is_alive(),
        "all_threads": [t.name for t in threading.enumerate()],
    })
