"""Legal pages blueprint — Terms, Privacy, Disclaimer, Impressum."""
from flask import Blueprint, render_template

legal_bp = Blueprint("legal", __name__, url_prefix="/legal")


@legal_bp.route("/terms")
def terms():
    return render_template("legal/terms.html")


@legal_bp.route("/privacy")
def privacy():
    return render_template("legal/privacy.html")


@legal_bp.route("/disclaimer")
def disclaimer():
    return render_template("legal/disclaimer.html")


@legal_bp.route("/impressum")
def impressum():
    return render_template("legal/impressum.html")
