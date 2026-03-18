"""Guides & FAQ blueprint."""
from flask import Blueprint, render_template

guides_bp = Blueprint("guides", __name__, url_prefix="/guides")


@guides_bp.route("/")
def index():
    return render_template("guides/index.html")
