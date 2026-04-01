"""
ML Trainer — high-level functions to train ensembles and retrieve Vote objects.

Public API
----------
    train_from_df(df, key, ...)      → stats dict
    train_from_csv(path, key, ...)   → stats dict
    get_ml_votes(symbol, mtf_data, primary_tf, ml_weight)  → list[Vote]
    get_ensemble(key)                → MLEnsemble (loaded or empty)
    make_key(symbol, timeframe)      → "btcusdt_1h"

Storage directory: <workspace>/instance/ml_models/
"""
from __future__ import annotations

import logging
import os

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── Directory ─────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_WORKSPACE = os.path.dirname(os.path.dirname(_HERE))       # project root
ML_MODELS_DIR = os.environ.get(
    "ML_MODELS_DIR",
    os.path.join(_WORKSPACE, "instance", "ml_models"),
)


def make_key(symbol: str, timeframe: str) -> str:
    """Canonical ensemble key: 'btcusdt_1h'."""
    return f"{symbol.strip().lower()}_{timeframe}"


def get_ensemble(key: str):
    """Return a MLEnsemble instance, loading from disk if available."""
    from app.ml.ensemble import MLEnsemble
    e = MLEnsemble(store_dir=ML_MODELS_DIR, key=key)
    e.load()  # no-op if not found
    return e


# ── Training helpers ──────────────────────────────────────────────────────

def train_from_df(
    df: pd.DataFrame,
    key: str,
    forward_n: int = 5,
    threshold_pct: float = 0.5,
) -> dict:
    """
    Train the ensemble on an in-memory DataFrame.
    Returns stats dict (keys: n_samples, label_dist, <tag>_acc, ...).
    """
    from app.ml.features import extract_features, extract_labels
    from app.ml.ensemble import MLEnsemble

    n_rows = len(df)
    if n_rows < 100:
        return {"error": f"Not enough rows: {n_rows} (need ≥ 100)"}

    X = extract_features(df)
    y = extract_labels(df, forward_n=forward_n, threshold_pct=threshold_pct)

    # Drop last forward_n rows — labels are NaN there
    X = X[:-forward_n]
    y = y[:-forward_n]

    # Keep only rows with all-finite features
    mask = np.isfinite(X).all(axis=1)
    X = X[mask]
    y = y[mask]

    ensemble = MLEnsemble(store_dir=ML_MODELS_DIR, key=key)
    try:
        stats = ensemble.fit(X, y)
    except ValueError as exc:
        return {"error": str(exc)}

    ensemble.save()
    return stats


def train_from_csv(
    csv_path: str,
    key: str,
    forward_n: int = 5,
    threshold_pct: float = 0.5,
) -> dict:
    """Load a collector CSV and train. Returns stats dict."""
    if not os.path.exists(csv_path):
        return {"error": f"File not found: {csv_path}"}
    try:
        df = pd.read_csv(csv_path, parse_dates=["timestamp"])
    except Exception as exc:
        return {"error": f"CSV read error: {exc}"}
    return train_from_df(df, key=key, forward_n=forward_n, threshold_pct=threshold_pct)


# ── Vote generation ───────────────────────────────────────────────────────

def get_ml_votes(
    symbol: str,
    mtf_data: dict,
    primary_tf: str,
    ml_weight: float = 3.0,
) -> list:
    """
    Load ensemble for (symbol, primary_tf), predict on latest candle,
    return a list of Vote objects (one per model that gave a non-neutral signal).

    If the ensemble is not trained or data is insufficient → empty list.
    """
    from app.algorithms.consensus.engine import Vote
    from app.ml.ensemble import MLEnsemble, MODEL_TAGS
    from app.ml.features import extract_features

    key = make_key(symbol, primary_tf)
    ensemble = MLEnsemble(store_dir=ML_MODELS_DIR, key=key)
    if not ensemble.load():
        logger.debug("ML ensemble not trained yet for key=%s", key)
        return []

    df = mtf_data.get(primary_tf)
    if df is None or df.empty or len(df) < 30:
        logger.debug("ML: insufficient data for primary_tf=%s", primary_tf)
        return []

    X = extract_features(df)
    if len(X) == 0:
        return []

    majority, individual, confidence = ensemble.predict_one(X[-1])

    votes: list[Vote] = []
    for i, pred in enumerate(individual):
        if pred == 0:
            continue  # neutral votes dilute the score — skip
        tag = MODEL_TAGS[i] if i < len(MODEL_TAGS) else f"ml_{i}"
        votes.append(Vote(
            voter=f"ml_{tag}",
            timeframe=primary_tf,
            signal=float(pred),
            weight=ml_weight,
            raw_value=float(pred),
            confidence=float(confidence),
        ))

    if votes:
        logger.debug(
            "ML votes for %s %s — majority=%d  individual=%s  confidence=%.2f",
            symbol, primary_tf, majority, individual, confidence,
        )
    return votes


def auto_train_if_needed(
    symbol: str,
    primary_tf: str,
    mtf_data: dict,
    collector_data_dir: str | None = None,
) -> bool:
    """
    Train ensemble from collector CSV if not already trained.
    Called automatically on first tick with use_ml_signals=True.
    Returns True if already trained or training succeeded.
    """
    from app.ml.ensemble import MLEnsemble
    from app.algorithms.consensus.data import COLLECTOR_DATA_DIR

    key = make_key(symbol, primary_tf)
    ensemble = MLEnsemble(store_dir=ML_MODELS_DIR, key=key)
    if ensemble.load() and ensemble.is_trained:
        return True  # already trained

    data_dir = collector_data_dir or COLLECTOR_DATA_DIR

    # Try collector CSV for primary TF
    csv_path = os.path.join(data_dir, f"{symbol.lower()}_{primary_tf}_clean.csv")
    if os.path.exists(csv_path):
        stats = train_from_csv(csv_path, key=key)
        if "error" not in stats:
            logger.info("ML auto-trained from collector CSV: %s  stats=%s", csv_path, stats)
            return True

    # Fallback: use the in-memory DataFrame if long enough
    df = mtf_data.get(primary_tf)
    if df is not None and len(df) >= 100:
        stats = train_from_df(df, key=key)
        if "error" not in stats:
            logger.info("ML auto-trained from mtf_data[%s]  stats=%s", primary_tf, stats)
            return True

    logger.warning(
        "ML auto-train skipped for %s %s — no CSV and not enough live data",
        symbol, primary_tf,
    )
    return False
