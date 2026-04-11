"""
Unit tests for app/ml/ensemble.py

Tests partial_update(), predict_one(), fit(), save/load cycle.
No real database or Binance connection needed.
"""
from __future__ import annotations

import os
import tempfile

import numpy as np
import pytest


def _make_xy(n: int = 50, n_features: int = 10):
    """Generate simple synthetic feature matrix and label vector."""
    rng = np.random.default_rng(42)
    X = rng.standard_normal((n, n_features)).astype(np.float32)
    y = rng.choice([-1, 0, 1], size=n).astype(np.int8)
    return X, y


class TestMLEnsemble:
    def test_initial_state(self):
        from app.ml.ensemble import MLEnsemble
        ens = MLEnsemble(key="test")
        assert ens.is_warm is False
        assert ens.is_trained is False
        assert ens.n_seen == 0
        assert ens.models_trained == [False, False, False]

    def test_partial_update_trains_models(self):
        from app.ml.ensemble import MLEnsemble, MIN_WARM_SAMPLES
        X, y = _make_xy(MIN_WARM_SAMPLES + 5)
        ens = MLEnsemble(key="test")
        stats = ens.partial_update(X, y)

        assert ens.is_trained is True
        assert ens.n_seen == len(X)
        assert isinstance(stats, dict)

    def test_is_warm_after_threshold(self):
        from app.ml.ensemble import MLEnsemble, MIN_WARM_SAMPLES
        X, y = _make_xy(MIN_WARM_SAMPLES)
        ens = MLEnsemble(key="test")
        ens.partial_update(X, y)
        assert ens.is_warm is True

    def test_predict_one_returns_hold_before_warm(self):
        from app.ml.ensemble import MLEnsemble
        X, y = _make_xy(5)  # below MIN_WARM_SAMPLES
        ens = MLEnsemble(key="test")
        ens.partial_update(X, y)

        majority, individual, confidence = ens.predict_one(X[0])
        assert majority == 0
        assert individual == [0, 0, 0]
        assert confidence == 0.0

    def test_predict_one_returns_valid_output_after_training(self):
        from app.ml.ensemble import MLEnsemble, MIN_WARM_SAMPLES
        X, y = _make_xy(MIN_WARM_SAMPLES + 20)
        ens = MLEnsemble(key="test")
        ens.partial_update(X, y)

        majority, individual, confidence = ens.predict_one(X[-1])
        assert majority in (-1, 0, 1)
        assert len(individual) == 3
        assert all(p in (-1, 0, 1) for p in individual)
        assert 0.0 <= confidence <= 1.0

    def test_fit_trains_all_models(self):
        from app.ml.ensemble import MLEnsemble, MIN_WARM_SAMPLES
        X, y = _make_xy(MIN_WARM_SAMPLES * 2)
        ens = MLEnsemble(key="test")
        stats = ens.fit(X, y)

        assert ens.is_trained is True
        assert "n_samples" in stats
        assert stats["n_samples"] == len(X)

    def test_fit_raises_on_too_few_samples(self):
        from app.ml.ensemble import MLEnsemble, MIN_WARM_SAMPLES
        X, y = _make_xy(5)
        ens = MLEnsemble(key="test")
        with pytest.raises(ValueError, match="samples"):
            ens.fit(X, y)

    def test_save_load_roundtrip(self):
        from app.ml.ensemble import MLEnsemble, MIN_WARM_SAMPLES
        X, y = _make_xy(MIN_WARM_SAMPLES + 10)

        with tempfile.TemporaryDirectory() as tmpdir:
            ens = MLEnsemble(store_dir=tmpdir, key="roundtrip_test")
            ens.fit(X, y)
            n_before = ens.n_seen
            ens.save()

            pkl_path = os.path.join(tmpdir, "roundtrip_test_ensemble.pkl")
            assert os.path.exists(pkl_path)

            ens2 = MLEnsemble(store_dir=tmpdir, key="roundtrip_test")
            loaded = ens2.load()
            assert loaded is True
            assert ens2.n_seen == n_before
            assert ens2.is_trained is True
            assert ens2.models_trained == [True, True, True]

    def test_load_returns_false_when_no_file(self):
        from app.ml.ensemble import MLEnsemble
        with tempfile.TemporaryDirectory() as tmpdir:
            ens = MLEnsemble(store_dir=tmpdir, key="nonexistent")
            assert ens.load() is False

    def test_train_stats_populated_after_fit(self):
        from app.ml.ensemble import MLEnsemble, MIN_WARM_SAMPLES
        X, y = _make_xy(MIN_WARM_SAMPLES + 5)
        ens = MLEnsemble(key="test")
        ens.fit(X, y)
        stats = ens.train_stats
        assert "n_samples" in stats
        assert "label_dist" in stats
