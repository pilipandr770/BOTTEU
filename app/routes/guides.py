"""Guides & FAQ blueprint."""
import urllib.request
from flask import Blueprint, render_template, jsonify

guides_bp = Blueprint("guides", __name__, url_prefix="/guides")


@guides_bp.route("/")
def index():
    return render_template("guides/index.html")


@guides_bp.route("/myip")
def myip():
    """Return the server's outbound IP — used once to configure Binance whitelist."""
    with urllib.request.urlopen("https://api.ipify.org?format=json", timeout=5) as r:
        data = r.read().decode()
    return data, 200, {"Content-Type": "application/json"}
