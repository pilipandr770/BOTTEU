"""Admin blueprint — only accessible to users with is_admin=True."""
from functools import wraps
from flask import Blueprint, render_template, redirect, url_for, flash, abort, request
from flask_login import login_required, current_user

from app.extensions import db
from app.models.user import User
from app.models.bot import Bot
from app.models.order import Order
from app.models.subscription import Subscription, Plan

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


@admin_bp.route("/")
@login_required
@admin_required
def index():
    total_users = User.query.filter_by(is_deleted=False).count()
    total_bots = Bot.query.count()
    running_bots = Bot.query.filter_by(status="running").count()
    pro_users = Subscription.query.filter_by(plan=Plan.PRO).count()
    recent_users = (
        User.query.filter_by(is_deleted=False)
        .order_by(User.created_at.desc())
        .limit(20)
        .all()
    )
    recent_orders = (
        Order.query.order_by(Order.created_at.desc()).limit(30).all()
    )
    return render_template(
        "admin/index.html",
        total_users=total_users,
        total_bots=total_bots,
        running_bots=running_bots,
        pro_users=pro_users,
        recent_users=recent_users,
        recent_orders=recent_orders,
    )


@admin_bp.route("/users")
@login_required
@admin_required
def users():
    all_users = (
        User.query.filter_by(is_deleted=False)
        .order_by(User.created_at.desc())
        .all()
    )
    return render_template("admin/users.html", users=all_users)


@admin_bp.route("/users/<int:user_id>/toggle-admin", methods=["POST"])
@login_required
@admin_required
def toggle_admin(user_id: int):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("You cannot change your own admin status.", "warning")
        return redirect(url_for("admin.users"))
    user.is_admin = not user.is_admin
    db.session.commit()
    flash(f"{'Admin granted to' if user.is_admin else 'Admin revoked from'} {user.email}", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/set-plan", methods=["POST"])
@login_required
@admin_required
def set_plan(user_id: int):
    user = User.query.get_or_404(user_id)
    plan_value = request.form.get("plan", "")
    valid_values = {p.value for p in Plan}
    if plan_value not in valid_values:
        flash("Invalid plan.", "danger")
        return redirect(url_for("admin.users"))
    sub = user.subscription
    if not sub:
        sub = Subscription(user_id=user.id)
        db.session.add(sub)
        db.session.flush()  # get sub.id
    # Use raw SQL to avoid SQLAlchemy sending enum NAME instead of VALUE
    db.session.execute(
        db.text(
            "UPDATE subscriptions SET plan = CAST(:plan AS plan), "
            "expires_at = NULL, updated_at = NOW() "
            "WHERE user_id = :uid"
        ),
        {"plan": plan_value, "uid": user_id},
    )
    db.session.commit()
    flash(f"Plan set to '{plan_value}' for {user.email}", "success")
    return redirect(url_for("admin.users"))
