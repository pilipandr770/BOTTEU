"""
Base strategy interface + algorithm registry.

To add a new algorithm:
  1. Create app/algorithms/my_algo.py with a class that extends BaseStrategy.
  2. Register it in ALGORITHMS below.
  3. The frontend form params are driven by ALGORITHM_PARAMS dict.
"""
from __future__ import annotations

import abc
import logging
from typing import Literal

import pandas as pd

logger = logging.getLogger(__name__)

Signal = Literal["BUY", "SELL", "HOLD"]


class BaseStrategy(abc.ABC):
    """
    All algorithms must implement `generate_signal`.
    They receive a OHLCV DataFrame and current bot state,
    and return a Signal + updated state dict.
    """

    #: Human-readable name shown in the UI
    display_name: str = "Base Strategy"

    #: Whether a stop-loss is mandatory for this strategy
    stop_loss_required: bool = False

    #: Whether take-profit fields should be shown
    take_profit_available: bool = True

    @abc.abstractmethod
    def generate_signal(
        self,
        df: pd.DataFrame,
        state: dict,
        params: dict,
    ) -> tuple[Signal, dict]:
        """
        Args:
            df:     OHLCV DataFrame with columns [open, high, low, close, volume].
                    Sorted ascending by time, last row = latest candle.
            state:  Mutable bot state dict persisted between runs (e.g. has_position).
            params: Bot configuration dict (timeframe, indicators, SL/TP values, etc.)

        Returns:
            (signal, updated_state)
        """
        ...


# ── Registry ────────────────────────────────────────────────────────────────
# Populated lazily to avoid circular imports at module level.

def _build_registry() -> dict[str, type[BaseStrategy]]:
    from app.algorithms.ma_crossover import MACrossoverStrategy
    from app.algorithms.rsi import RSIStrategy
    from app.algorithms.combined import CombinedStrategy
    return {
        "ma_crossover": MACrossoverStrategy,
        "rsi": RSIStrategy,
        "combined": CombinedStrategy,
    }


def get_algorithm(name: str) -> BaseStrategy:
    """Return an *instance* of the requested algorithm."""
    registry = _build_registry()
    cls = registry.get(name)
    if cls is None:
        raise ValueError(f"Unknown algorithm: '{name}'. Available: {list(registry)}")
    return cls()


def list_algorithms() -> list[dict]:
    """Return metadata list used to populate the UI dropdown."""
    registry = _build_registry()
    return [
        {
            "key": key,
            "label": cls.display_name,
            "stop_loss_required": cls.stop_loss_required,
            "take_profit_available": cls.take_profit_available,
        }
        for key, cls in registry.items()
    ]
