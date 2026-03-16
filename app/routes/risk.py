from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app.extensions import db

risk_bp = Blueprint("risk", __name__, url_prefix="/risk")


@risk_bp.route("/")
@login_required
def index():
    from app.models.risk_config import RiskConfig
    rc = RiskConfig.query.filter_by(user_id=current_user.id).first()
    return render_template("risk/index.html", rc=rc)


@risk_bp.route("/save", methods=["POST"])
@login_required
def save():
    from app.models.risk_config import RiskConfig

    rc = RiskConfig.query.filter_by(user_id=current_user.id).first()
    if not rc:
        rc = RiskConfig(user_id=current_user.id)
        db.session.add(rc)

    def _float_or_none(key: str):
        v = request.form.get(key, "").strip()
        try:
            return float(v) if v else None
        except ValueError:
            return None

    def _int_or_none(key: str):
        v = request.form.get(key, "").strip()
        try:
            return int(v) if v else None
        except ValueError:
            return None

    rc.enabled             = bool(request.form.get("enabled"))
    rc.max_daily_loss_pct  = _float_or_none("max_daily_loss_pct")
    rc.max_drawdown_pct    = _float_or_none("max_drawdown_pct")
    rc.max_open_positions  = _int_or_none("max_open_positions")
    db.session.commit()
    flash("Risk Manager settings saved.", "success")
    return redirect(url_for("risk.index"))


@risk_bp.route("/emergency-stop", methods=["POST"])
@login_required
def do_emergency_stop():
    from app.services.risk_manager import emergency_stop
    n = emergency_stop(current_user.id, "Manual emergency stop by user")
    flash(f"Emergency stop: {n} bot(s) stopped.", "warning")
    return redirect(url_for("risk.index"))
