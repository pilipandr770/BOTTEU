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
