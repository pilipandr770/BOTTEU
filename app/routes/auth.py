"""Auth blueprint — register, login, logout, email verification, account deletion."""
import secrets
from datetime import datetime, timezone
from functools import wraps

import bcrypt
from flask import Blueprint, render_template, redirect, url_for, flash, request, abort, session
from flask_login import login_user, logout_user, current_user, login_required
from flask_babel import gettext as _

from app.extensions import db, limiter
from app.models.user import User
from app.models.subscription import Subscription, Plan
from app.models.telegram_account import TelegramAccount

auth_bp = Blueprint("auth", __name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _send_verification_email(user: User) -> None:
    """Send email verification link."""
    from flask_mail import Message
    from app.extensions import mail
    from flask import current_app

    token = user.generate_verify_token()
    db.session.commit()

    link = url_for("auth.verify_email", token=token, _external=True)
    msg = Message(
        subject="Verify your BOTTEU account",
        recipients=[user.email],
        html=render_template("email/verify.html", user=user, link=link),
    )
    try:
        mail.send(msg)
    except Exception:
        pass  # Don't block registration on mail failure


# ── Routes ────────────────────────────────────────────────────────────────────

@auth_bp.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        disclaimer = request.form.get("risk_disclaimer") == "on"

        # Validation
        if not email or "@" not in email:
            flash(_("Please enter a valid email address."), "danger")
            return render_template("auth/register.html")
        if len(password) < 8:
            flash(_("Password must be at least 8 characters."), "danger")
            return render_template("auth/register.html")
        if password != confirm:
            flash(_("Passwords do not match."), "danger")
            return render_template("auth/register.html")
        if not disclaimer:
            flash(_("You must accept the risk disclaimer to continue."), "danger")
            return render_template("auth/register.html")
        if User.query.filter_by(email=email).first():
            flash(_("An account with this email already exists."), "danger")
            return render_template("auth/register.html")

        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        user = User(
            email=email,
            password_hash=pw_hash,
            risk_disclaimer_accepted=True,
        )
        db.session.add(user)
        db.session.flush()  # get user.id

        # Default Free subscription
        db.session.add(Subscription(user_id=user.id, plan=Plan.FREE))
        # Telegram account placeholder
        db.session.add(TelegramAccount(user_id=user.id))

        # Auto-verify when mail is not configured (local development)
        from flask import current_app
        if not current_app.config.get("MAIL_USERNAME"):
            user.is_verified = True

        db.session.commit()

        if user.is_verified:
            flash(_("Registration successful! You can now log in."), "success")
        else:
            _send_verification_email(user)
            flash(_("Registration successful! Check your email to verify your account."), "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/register.html")


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = User.query.filter_by(email=email, is_deleted=False).first()
        if not user or not bcrypt.checkpw(password.encode(), user.password_hash.encode()):
            flash(_("Invalid email or password."), "danger")
            return render_template("auth/login.html")

        if not user.is_verified:
            flash(_("Please verify your email before logging in."), "warning")
            return render_template("auth/login.html")

        login_user(user, remember=request.form.get("remember") == "on")
        session["lang"] = user.preferred_lang
        next_page = request.args.get("next")
        return redirect(next_page or url_for("dashboard.index"))

    return render_template("auth/login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


@auth_bp.route("/verify/<token>")
def verify_email(token: str):
    user = User.query.filter_by(verify_token=token, is_deleted=False).first()
    if not user:
        flash(_("Invalid or expired verification link."), "danger")
        return redirect(url_for("auth.login"))
    user.is_verified = True
    user.verify_token = None
    db.session.commit()
    flash(_("Email verified! You can now log in."), "success")
    return redirect(url_for("auth.login"))


@auth_bp.route("/delete-account", methods=["GET", "POST"])
@login_required
def delete_account():
    if request.method == "POST":
        password = request.form.get("password", "")
        if not bcrypt.checkpw(password.encode(), current_user.password_hash.encode()):
            flash(_("Incorrect password."), "danger")
            return render_template("auth/delete_account.html")

        current_user.anonymize()
        db.session.commit()
        logout_user()
        flash(_("Your account has been permanently deleted."), "info")
        return redirect(url_for("auth.login"))

    return render_template("auth/delete_account.html")


@auth_bp.route("/set-lang/<lang>")
def set_lang(lang: str):
    if lang in ("en", "de"):
        session["lang"] = lang
        if current_user.is_authenticated:
            current_user.preferred_lang = lang
            db.session.commit()
    return redirect(request.referrer or url_for("dashboard.index"))
