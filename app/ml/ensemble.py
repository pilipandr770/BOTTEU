"""
ML Ensemble — 3 scikit-learn classifiers vote on BUY/SELL/HOLD.

Models
------
  0  sgd_logreg    SGDClassifier(loss='log_loss')       online-capable
  1  sgd_huber     SGDClassifier(loss='modified_huber') online-capable, robust
  2  rf_batch      RandomForestClassifier               batch, retrained each time

Majority vote
-------------
  2 or 3 models agree  → use that label
  All 3 differ          → HOLD (0)
  Majority is 0         → HOLD

Persistence
-----------
  joblib dump to  {store_dir}/{key}_ensemble.pkl
"""
from __future__ import annotations

import logging
import os
from collections import Counter

import numpy as np

logger = logging.getLogger(__name__)

CLASSES = np.array([-1, 0, 1], dtype=np.int8)
MODEL_TAGS = ["sgd_logreg", "sgd_huber", "rf_batch"]


def _build_models():
    from sklearn.linear_model import SGDClassifier
    from sklearn.ensemble import RandomForestClassifier
    return [
        SGDClassifier(
            loss="log_loss", max_iter=1000, tol=1e-3,
            class_weight="balanced", random_state=42,
        ),
        SGDClassifier(
            loss="modified_huber", max_iter=1000, tol=1e-3,
            class_weight="balanced", random_state=43,
        ),
        RandomForestClassifier(
            n_estimators=50, max_depth=5, class_weight="balanced",
            random_state=44, n_jobs=1,
        ),
    ]


class MLEnsemble:
    def __init__(self, store_dir: str = "instance/ml_models", key: str = "default"):
        self.store_dir = store_dir
        self.key = key
        self.models = None
        self.scalers = None
        self.fitted: list[bool] = [False, False, False]
        self._train_stats: dict = {}

    # ── Internal helpers ───────────────────────────────────────────────────

    def _ensure_init(self) -> None:
        if self.models is None:
            from sklearn.preprocessing import StandardScaler
            self.models = _build_models()
            self.scalers = [StandardScaler() for _ in range(len(self.models))]

    def _store_path(self) -> str:
        return os.path.join(self.store_dir, f"{self.key}_ensemble.pkl")

    # ── Training ───────────────────────────────────────────────────────────

    def fit(self, X: np.ndarray, y: np.ndarray) -> dict:
        """
        Batch-train all 3 models. Returns dict with accuracy per model.
        Raises ValueError if fewer than 50 samples.
        """
        self._ensure_init()

        n = len(X)
        if n < 50:
            raise ValueError(f"Need ≥ 50 samples, got {n}")

        unique, counts = np.unique(y, return_counts=True)
        stats: dict = {
            "n_samples": int(n),
            "label_dist": {int(k): int(v) for k, v in zip(unique, counts)},
        }

        for i, (model, scaler) in enumerate(zip(self.models, self.scalers)):
            tag = MODEL_TAGS[i]
            try:
                X_scaled = scaler.fit_transform(X)
                if hasattr(model, "partial_fit"):
                    model.partial_fit(X_scaled, y, classes=CLASSES)
                else:
                    model.fit(X_scaled, y)
                self.fitted[i] = True
                preds = model.predict(X_scaled)
                acc = float(np.mean(preds == y))
                stats[f"{tag}_acc"] = round(acc, 3)
            except Exception as exc:
                logger.warning("MLEnsemble: model %d (%s) training failed: %s", i, tag, exc)
                stats[f"{tag}_error"] = str(exc)

        self._train_stats = stats
        return stats

    # ── Prediction ─────────────────────────────────────────────────────────

    def predict_one(self, x: np.ndarray) -> tuple[int, list[int], float]:
        """
        Predict for a single feature vector.

        Returns
        -------
        majority : int
            BUY=1, SELL=-1, HOLD=0
        individual : list[int]
            Raw prediction of each model
        confidence : float
            Fraction of models that agree with majority (0.67 or 1.0)
        """
        if not any(self.fitted):
            return 0, [0, 0, 0], 0.0

        x2d = x.reshape(1, -1)
        preds: list[int] = []

        for i, (model, scaler) in enumerate(zip(self.models, self.scalers)):
            if not self.fitted[i]:
                preds.append(0)
                continue
            try:
                xs = scaler.transform(x2d)
                pred = int(model.predict(xs)[0])
                preds.append(pred)
            except Exception as exc:
                logger.debug("Model %d predict failed: %s", i, exc)
                preds.append(0)

        cnt = Counter(preds)
        majority, count = cnt.most_common(1)[0]
        # Require at least 2/3 agreement, and majority must not be HOLD
        if count < 2 or majority == 0:
            return 0, preds, 0.0

        confidence = count / len(preds)
        return majority, preds, confidence

    # ── Persistence ────────────────────────────────────────────────────────

    def save(self) -> None:
        import joblib
        os.makedirs(self.store_dir, exist_ok=True)
        self._ensure_init()
        joblib.dump(
            {
                "models": self.models,
                "scalers": self.scalers,
                "fitted": self.fitted,
                "train_stats": self._train_stats,
            },
            self._store_path(),
        )
        logger.info("MLEnsemble saved → %s", self._store_path())

    def load(self) -> bool:
        """Load from disk. Returns True on success."""
        import joblib
        path = self._store_path()
        if not os.path.exists(path):
            return False
        try:
            data = joblib.load(path)
            self.models = data["models"]
            self.scalers = data["scalers"]
            self.fitted = data["fitted"]
            self._train_stats = data.get("train_stats", {})
            logger.debug("MLEnsemble loaded ← %s", path)
            return True
        except Exception as exc:
            logger.warning("MLEnsemble: load failed: %s", exc)
            return False

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def is_trained(self) -> bool:
        return any(self.fitted)

    @property
    def train_stats(self) -> dict:
        return dict(self._train_stats)

    @property
    def models_trained(self) -> list[bool]:
        return list(self.fitted)
