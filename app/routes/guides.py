"""Guides & FAQ blueprint."""
from flask import Blueprint, render_template, jsonify

guides_bp = Blueprint("guides", __name__, url_prefix="/guides")


@guides_bp.route("/")
def index():
    return render_template("guides/index.html")


@guides_bp.route("/status")
def status():
    """Diagnostic: check if APScheduler is running."""
    from app.workers.scheduler import _scheduler
    sched_running = _scheduler is not None and _scheduler.running
    jobs = []
    if sched_running:
        jobs = [{"id": j.id, "next_run": str(j.next_run_time)} for j in _scheduler.get_jobs()]
    return jsonify({"scheduler_running": sched_running, "jobs": jobs})
