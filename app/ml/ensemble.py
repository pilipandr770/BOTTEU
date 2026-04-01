"""
ML Ensemble — 3 streaming classifiers vote on BUY/SELL/HOLD.

All three models use partial_fit() → true online / streaming learning.
Predictions start after MIN_WARM_SAMPLES ticks (default 20).
No pre-training or collector CSV needed — improves every tick.

Models
------
  0  sgd_logreg      SGDClassifier(loss='log_loss')       online logistic regression
  1  pa_classifier   PassiveAggressiveClassifier           online, updates on errors only
  2  sgd_huber       SGDClassifier(loss='modified_huber') online, robust to price spikes

Majority vote
-------------
  2 or 3 models agree on non-HOLD → use that label
  Otherwise → HOLD

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
MODEL_TAGS = ["sgd_logreg", "pa_classifier", "sgd_huber"]
MIN_WARM_SAMPLES = 20   # start predicting after this many samples seen


def _build_models():
    from sklearn.linear_model import SGDClassifier
    return [
        SGDClassifier(
            loss="log_loss", max_iter=1, warm_start=True, random_state=42,
        ),
        # Passive-Aggressive (PA-I) via SGD — PAC deprecated in sklearn ≥ 1.8
        SGDClassifier(
            loss="hinge", penalty=None, learning_rate="pa1", eta0=1.0,
            max_iter=1, warm_start=True, random_state=43,
        ),
        SGDClassifier(
            loss="modified_huber", max_iter=1, warm_start=True, random_state=44,
        ),
    ]


class MLEnsemble:
    def __init__(self, store_dir: str = "instance/ml_models", key: str = "default"):
        self.store_dir = store_dir
        self.key = key
        self.models = None
        self.scalers = None
        self.fitted: list[bool] = [False, False, False]
        self.n_seen: int = 0        # total samples seen via partial_fit
        self._train_stats: dict = {}

    # ── Internal helpers ───────────────────────────────────────────────────

    def _ensure_init(self) -> None:
        if self.models is None:
            from sklearn.preprocessing import StandardScaler
            self.models = _build_models()
            self.scalers = [StandardScaler() for _ in range(len(self.models))]

    def _store_path(self) -> str:
        return os.path.join(self.store_dir, f"{self.key}_ensemble.pkl")

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def is_warm(self) -> bool:
        """True when models have seen enough samples to give meaningful signals."""
        return self.n_seen >= MIN_WARM_SAMPLES

    @property
    def is_trained(self) -> bool:
        return any(self.fitted)

    @property
    def train_stats(self) -> dict:
        return dict(self._train_stats)

    @property
    def models_trained(self) -> list[bool]:
        return list(self.fitted)

    def _compute_sample_weight(self, y: np.ndarray) -> np.ndarray:
        """Compute per-sample weights to balance BUY/HOLD/SELL classes."""
        from sklearn.utils.class_weight import compute_class_weight
        present = np.unique(y)
        if len(present) < 2:
            return np.ones(len(y), dtype=float)
        try:
            cw = compute_class_weight("balanced", classes=present, y=y)
            weight_map = dict(zip(present, cw))
            return np.array([weight_map[yi] for yi in y], dtype=float)
        except Exception:
            return np.ones(len(y), dtype=float)

    # ── Streaming update (called every tick) ──────────────────────────────

    def partial_update(self, X: np.ndarray, y: np.ndarray) -> dict:
        """
        Incremental online update with a mini-batch (e.g. last 50 candles).
        Called every tick — no waiting for collected data.
        All 3 models update via partial_fit().

        Returns per-model accuracy on the current batch.
        """
        self._ensure_init()

        n = len(X)
        if n < 2:
            return {"skipped": "too few samples in batch"}

        stats: dict = {"n_batch": int(n), "n_seen_after": int(self.n_seen + n)}
        sw = self._compute_sample_weight(y)

        for i, (model, scaler) in enumerate(zip(self.models, self.scalers)):
            tag = MODEL_TAGS[i]
            try:
                if not self.fitted[i]:
                    X_scaled = scaler.fit_transform(X)
                else:
                    scaler.partial_fit(X)
                    X_scaled = scaler.transform(X)

                model.partial_fit(X_scaled, y, classes=CLASSES, sample_weight=sw)
                self.fitted[i] = True

                preds = model.predict(X_scaled)
                acc = float(np.mean(preds == y))
                stats[f"{tag}_acc"] = round(acc, 3)
            except Exception as exc:
                logger.warning(
                    "MLEnsemble partial_update: model %d (%s) failed: %s", i, tag, exc
                )
                stats[f"{tag}_error"] = str(exc)

        self.n_seen += n
        self._train_stats = stats
        return stats

    # ── Batch seed (optional initial training or manual retrain) ──────────

    def fit(self, X: np.ndarray, y: np.ndarray) -> dict:
        """
        Seed all 3 models with a larger batch (uses partial_fit internally).
        Optional — streaming_update() is enough for live operation.
        Requires ≥ MIN_WARM_SAMPLES rows.
        """
        self._ensure_init()

        n = len(X)
        if n < MIN_WARM_SAMPLES:
            raise ValueError(f"Need ≥ {MIN_WARM_SAMPLES} samples, got {n}")

        unique, counts = np.unique(y, return_counts=True)
        stats: dict = {
            "n_samples": int(n),
            "label_dist": {int(k): int(v) for k, v in zip(unique, counts)},
        }

        chunk = max(50, n // 4)
        for i, (model, scaler) in enumerate(zip(self.models, self.scalers)):
            tag = MODEL_TAGS[i]
            try:
                X_scaled = scaler.fit_transform(X)
                sw = self._compute_sample_weight(y)
                for start in range(0, n, chunk):
                    sl = slice(start, start + chunk)
                    model.partial_fit(
                        X_scaled[sl], y[sl],
                        classes=CLASSES, sample_weight=sw[sl],
                    )
                self.fitted[i] = True
                preds = model.predict(X_scaled)
                acc = float(np.mean(preds == y))
                stats[f"{tag}_acc"] = round(acc, 3)
            except Exception as exc:
                logger.warning("MLEnsemble.fit: model %d (%s) failed: %s", i, tag, exc)
                stats[f"{tag}_error"] = str(exc)

        self.n_seen = max(self.n_seen, n)
        self._train_stats = stats
        return stats

    # ── Prediction ─────────────────────────────────────────────────────────

    def predict_one(self, x: np.ndarray) -> tuple[int, list[int], float]:
        """
        Predict for a single feature vector.
        Returns HOLD silently if not yet warm (n_seen < MIN_WARM_SAMPLES).

        Returns
        -------
        majority : int
            BUY=1, SELL=-1, HOLD=0
        individual : list[int]
            Raw prediction of each model
        confidence : float
            Fraction of models that agree with majority (0.67 or 1.0)
        """
        if not self.is_warm:
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
                "n_seen": self.n_seen,
                "train_stats": self._train_stats,
            },
            self._store_path(),
        )
        logger.debug("MLEnsemble saved → %s  (n_seen=%d)", self._store_path(), self.n_seen)

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
            self.n_seen = data.get("n_seen", 0)
            self._train_stats = data.get("train_stats", {})
            logger.debug("MLEnsemble loaded ← %s  (n_seen=%d)", path, self.n_seen)
            return True
        except Exception as exc:
            logger.warning("MLEnsemble: load failed: %s", exc)
            return False
