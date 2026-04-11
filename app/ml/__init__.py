"""
ML Ensemble module for BOTTEU.

Three scikit-learn classifiers vote independently on BUY/SELL/HOLD
using features derived from OHLCV + indicator data.

Architecture:
    features.py  — feature extraction from DataFrame
    ensemble.py  — 3-model ensemble with majority vote
    trainer.py   — train/load/save helpers

Entry point for consensus engine:
    from app.ml.trainer import get_ml_votes
"""
from app.ml.ensemble import MLEnsemble, MODEL_TAGS, CLASSES
from app.ml.features import (
    extract_features,
    extract_labels,
    get_tf_label_params,
    FEATURE_NAMES,
    N_FEATURES,
)
from app.ml.trainer import (
    get_ensemble,
    get_ml_votes,
    make_key,
    streaming_update,
    train_from_csv,
    train_from_df,
    ML_MODELS_DIR,
)

__all__ = [
    # Submodules
    "ensemble",
    "trainer",
    "features",
    # ensemble symbols
    "MLEnsemble",
    "MODEL_TAGS",
    "CLASSES",
    # features symbols
    "extract_features",
    "extract_labels",
    "get_tf_label_params",
    "FEATURE_NAMES",
    "N_FEATURES",
    # trainer symbols
    "get_ensemble",
    "get_ml_votes",
    "make_key",
    "streaming_update",
    "train_from_csv",
    "train_from_df",
    "ML_MODELS_DIR",
]
