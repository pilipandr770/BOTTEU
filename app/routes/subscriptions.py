"""Subscriptions blueprint — Stripe checkout, portal, webhooks."""
import stripe
from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app, abort
from flask_login import login_required, current_user
from flask_babel import gettext as _

from app.extensions import db, csrf
from app.models.subscription import Subscription, Plan

subscriptions_bp = Blueprint("subscriptions", __name__, url_prefix="/subscriptions")

# Map plan key → (Plan enum, config key, display name)
_PLAN_MAP = {
    "basic": (Plan.BASIC, "STRIPE_PRICE_ID_BASIC", "Basic"),
    "pro":   (Plan.PRO,   "STRIPE_PRICE_ID_PRO",   "Pro"),
    "elite": (Plan.ELITE, "STRIPE_PRICE_ID_ELITE",  "Elite"),
}


@subscriptions_bp.route("/plans")
@login_required
def plans():
    return render_template("subscriptions/plans.html")


@subscriptions_bp.route("/checkout/<plan_key>", methods=["POST"])
@login_required
def checkout(plan_key: str):
    if plan_key not in _PLAN_MAP:
        abort(404)
    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    plan_enum, config_key, plan_name = _PLAN_MAP[plan_key]
    price_id = current_app.config.get(config_key, "")
    if not price_id:
        flash(_("Payment is not configured yet. Please contact support."), "warning")
        return redirect(url_for("subscriptions.plans"))

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            customer_email=current_user.email,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=url_for("subscriptions.success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=url_for("subscriptions.plans", _external=True),
            metadata={"user_id": str(current_user.id), "plan": plan_key},
        )
        return redirect(session.url, code=303)
    except stripe.error.StripeError:
        flash(_("Payment error. Please try again or contact support."), "danger")
        return redirect(url_for("subscriptions.plans"))


@subscriptions_bp.route("/checkout/consultation", methods=["POST"])
@login_required
def checkout_consultation():
    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    price_id = current_app.config.get("STRIPE_PRICE_ID_CONSULTATION", "")
    if not price_id:
        flash(_("Consultation booking is not configured yet. Please contact support."), "warning")
        return redirect(url_for("subscriptions.plans"))

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            customer_email=current_user.email,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=url_for("subscriptions.consultation_success", _external=True),
            cancel_url=url_for("subscriptions.plans", _external=True),
            metadata={"user_id": str(current_user.id), "type": "consultation"},
        )
        return redirect(session.url, code=303)
    except stripe.error.StripeError:
        flash(_("Payment error. Please try again or contact support."), "danger")
        return redirect(url_for("subscriptions.plans"))


@subscriptions_bp.route("/success")
@login_required
def success():
    flash(_("🎉 Subscription activated! Your plan is now live."), "success")
    return redirect(url_for("dashboard.index"))


@subscriptions_bp.route("/consultation-success")
@login_required
def consultation_success():
    flash(_("✅ Consultation booked! We will contact you within 24 hours."), "success")
    return redirect(url_for("dashboard.index"))


@subscriptions_bp.route("/portal")
@login_required
def portal():
    """Redirect to Stripe Customer Portal for subscription management."""
    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    sub = current_user.subscription
    if not sub or not sub.stripe_customer_id:
        flash(_("No active subscription found."), "warning")
        return redirect(url_for("subscriptions.plans"))
    try:
        session = stripe.billing_portal.Session.create(
            customer=sub.stripe_customer_id,
            return_url=url_for("subscriptions.plans", _external=True),
        )
        return redirect(session.url, code=303)
    except stripe.error.StripeError:
        flash(_("Payment error. Please try again or contact support."), "danger")
        return redirect(url_for("subscriptions.plans"))


@subscriptions_bp.route("/webhook", methods=["POST"])
@csrf.exempt
def webhook():
    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    webhook_secret = current_app.config.get("STRIPE_WEBHOOK_SECRET", "")
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        abort(400)

    if event["type"] == "checkout.session.completed":
        session_obj = event["data"]["object"]
        user_id = int(session_obj.get("metadata", {}).get("user_id", 0))
        plan_key = session_obj.get("metadata", {}).get("plan", "")
        if user_id and plan_key in _PLAN_MAP:
            plan_enum, _, _ = _PLAN_MAP[plan_key]
            sub = Subscription.query.filter_by(user_id=user_id).first()
            if sub:
                sub.plan = plan_enum
                sub.stripe_customer_id = session_obj.get("customer")
                sub.stripe_subscription_id = session_obj.get("subscription")
                db.session.commit()

    elif event["type"] == "customer.subscription.deleted":
        stripe_sub_id = event["data"]["object"]["id"]
        sub = Subscription.query.filter_by(stripe_subscription_id=stripe_sub_id).first()
        if sub:
            sub.plan = Plan.FREE
            db.session.commit()

    return "", 200
