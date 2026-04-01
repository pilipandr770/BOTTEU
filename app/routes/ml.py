"""
ML Blueprint — endpoints to train and inspect ML ensembles per bot.

Routes
------
  POST /ml/train/<bot_id>      Train ensemble using collector CSV or Binance API data
  GET  /ml/status/<bot_id>     Return training status and accuracy stats
"""
from __future__ import annotations

import os

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from flask_wtf.csrf import validate_csrf
from wtforms.validators import ValidationError

from app.models.bot import Bot

ml_bp = Blueprint("ml", __name__, url_prefix="/ml")


@ml_bp.route("/train/<int:bot_id>", methods=["POST"])
@login_required
def train(bot_id: int):
    """
    Trigger ML ensemble training for a bot.
    Accepts JSON or form POST. CSRF token must be in X-CSRFToken header or form field.
    """
    # Validate CSRF from header (fetch sends it there)
    token = request.headers.get("X-CSRFToken") or request.form.get("csrf_token", "")
    try:
        validate_csrf(token)
    except ValidationError:
        return jsonify({"success": False, "error": "CSRF validation failed"}), 400
    bot = Bot.query.filter_by(id=bot_id, user_id=current_user.id).first_or_404()

    req = request.get_json(silent=True) or {}
    forward_n  = int(req.get("forward_n",  5))
    threshold  = float(req.get("threshold", 0.5))

    consensus  = bot.params.get("consensus", {})
    primary_tf = bot.params.get("timeframe", "1h")
    bot_tfs    = consensus.get("timeframes") or [primary_tf]
    timeframes_to_train = req.get("timeframes", bot_tfs) or [primary_tf]

    from app.ml.trainer import train_from_csv, train_from_df, make_key
    from app.algorithms.consensus.data import COLLECTOR_DATA_DIR

    results: dict = {}

    for tf in timeframes_to_train:
        key = make_key(bot.symbol, tf)
        csv_path = os.path.join(
            COLLECTOR_DATA_DIR, f"{bot.symbol.lower()}_{tf}_clean.csv"
        )

        if os.path.exists(csv_path):
            stats = train_from_csv(
                csv_path, key=key,
                forward_n=forward_n, threshold_pct=threshold,
            )
        else:
            # Fallback: fetch from Binance
            stats = _train_from_binance(bot.symbol, tf, key, current_user.id,
                                        forward_n, threshold)
        results[tf] = stats

    any_ok = any("error" not in v for v in results.values())
    return jsonify({"success": any_ok, "symbol": bot.symbol, "results": results})


@ml_bp.route("/status/<int:bot_id>")
@login_required
def status(bot_id: int):
    """Return training status for all TFs of a bot."""
    bot = Bot.query.filter_by(id=bot_id, user_id=current_user.id).first_or_404()

    consensus  = bot.params.get("consensus", {})
    primary_tf = bot.params.get("timeframe", "1h")
    bot_tfs    = consensus.get("timeframes") or [primary_tf]

    from app.ml.ensemble import MLEnsemble, MODEL_TAGS
    from app.ml.trainer import make_key, ML_MODELS_DIR

    statuses: dict = {}
    for tf in bot_tfs:
        key = make_key(bot.symbol, tf)
        ens = MLEnsemble(store_dir=ML_MODELS_DIR, key=key)
        loaded = ens.load()
        statuses[tf] = {
            "trained": loaded and ens.is_trained,
            "models": {
                tag: {"trained": loaded and ens.fitted[i]}
                for i, tag in enumerate(MODEL_TAGS)
            },
            "stats": ens.train_stats if loaded else {},
        }

    return jsonify({
        "bot_id": bot.id,
        "symbol": bot.symbol,
        "use_ml": bool(
            bot.params.get("consensus", {}).get("use_ml_signals")
        ),
        "timeframes": statuses,
    })


# ── Internal helper ───────────────────────────────────────────────────────

def _train_from_binance(
    symbol: str,
    tf: str,
    key: str,
    user_id: int,
    forward_n: int,
    threshold: float,
) -> dict:
    """Fetch historical candles from Binance and train."""
    try:
        from app.services.binance_client import get_client_for_user
        from app.algorithms.consensus.data import fetch_multi_tf_binance
        from app.ml.trainer import train_from_df

        client = get_client_for_user(user_id)
        tf_data = fetch_multi_tf_binance(client, symbol, [tf])
        df = tf_data.get(tf)
        if df is None or len(df) < 100:
            return {"error": f"Binance returned only {len(df) if df is not None else 0} rows"}
        return train_from_df(df, key=key, forward_n=forward_n, threshold_pct=threshold)

    except Exception as exc:
        return {"error": str(exc)}
