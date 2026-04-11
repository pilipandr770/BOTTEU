"""
Unit tests for app/services/risk_manager.py

Tests check_before_buy() and emergency_stop() with mocked DB.
No real database or Binance connection needed.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────

def _make_rc(
    enabled=True,
    max_open_positions=None,
    max_daily_loss_pct=None,
    max_drawdown_pct=None,
):
    rc = MagicMock()
    rc.enabled             = enabled
    rc.max_open_positions  = max_open_positions
    rc.max_daily_loss_pct  = max_daily_loss_pct
    rc.max_drawdown_pct    = max_drawdown_pct
    return rc


def _make_bot(user_id=1, bot_id=1, has_position=False):
    bot = MagicMock()
    bot.id      = bot_id
    bot.user_id = user_id
    bot.state   = {"has_position": has_position}
    return bot


# ── check_before_buy ──────────────────────────────────────────────────────

class TestCheckBeforeBuy:
    def test_no_risk_config_allows_buy(self):
        with (
            patch("app.services.risk_manager.RiskConfig") as MockRC,
            patch("app.services.risk_manager.Bot") as MockBot,
        ):
            MockRC.query.filter_by.return_value.first.return_value = None
            MockBot.query.filter_by.return_value.all.return_value  = []

            from app.services.risk_manager import check_before_buy
            allowed, reason = check_before_buy(1, 1)

        assert allowed is True
        assert reason == ""

    def test_disabled_risk_config_allows_buy(self):
        with (
            patch("app.services.risk_manager.RiskConfig") as MockRC,
            patch("app.services.risk_manager.Bot") as MockBot,
        ):
            MockRC.query.filter_by.return_value.first.return_value = _make_rc(enabled=False)
            MockBot.query.filter_by.return_value.all.return_value  = []

            from app.services.risk_manager import check_before_buy
            allowed, reason = check_before_buy(1, 1)

        assert allowed is True

    def test_max_open_positions_blocks_buy(self):
        bot = _make_bot(has_position=True)
        with (
            patch("app.services.risk_manager.RiskConfig") as MockRC,
            patch("app.services.risk_manager.Bot") as MockBot,
        ):
            MockRC.query.filter_by.return_value.first.return_value = _make_rc(max_open_positions=1)
            MockBot.query.filter_by.return_value.all.return_value  = [bot]

            from app.services.risk_manager import check_before_buy
            allowed, reason = check_before_buy(1, 1)

        assert allowed is False
        assert "max open positions" in reason.lower()

    def test_max_open_positions_allows_buy_when_under_limit(self):
        bot_no_pos = _make_bot(has_position=False)
        with (
            patch("app.services.risk_manager.RiskConfig") as MockRC,
            patch("app.services.risk_manager.Bot") as MockBot,
        ):
            MockRC.query.filter_by.return_value.first.return_value = _make_rc(max_open_positions=3)
            MockBot.query.filter_by.return_value.all.return_value  = [bot_no_pos]

            from app.services.risk_manager import check_before_buy
            allowed, reason = check_before_buy(1, 1)

        assert allowed is True

    def test_max_drawdown_blocks_buy(self):
        """Simulate cumulative PnL that exceeds drawdown threshold."""
        from app.models.order import OrderSide  # noqa: F401 — needed for mock target

        pnl_rows = [(Decimal("-100"),), (Decimal("-50"),)]  # lost 150 from 1000 = 15%

        with (
            patch("app.services.risk_manager.RiskConfig") as MockRC,
            patch("app.services.risk_manager.Bot") as MockBot,
            patch("app.services.risk_manager.db") as MockDB,
            patch("app.services.risk_manager.Order") as MockOrder,
        ):
            MockRC.query.filter_by.return_value.first.return_value = _make_rc(max_drawdown_pct=10.0)
            MockBot.query.filter_by.return_value.all.return_value  = [_make_bot(bot_id=1)]
            MockDB.session.query.return_value.filter.return_value.order_by.return_value.with_entities.return_value.all.return_value = pnl_rows
            MockOrder.query.filter.return_value.order_by.return_value.with_entities.return_value.all.return_value = pnl_rows

            from app.services.risk_manager import check_before_buy
            allowed, reason = check_before_buy(1, 1)

        assert allowed is False
        assert "drawdown" in reason.lower()


# ── emergency_stop ────────────────────────────────────────────────────────

class TestEmergencyStop:
    def test_stops_running_bots(self):
        from app.models.bot import BotStatus  # noqa — used for MagicMock spec

        bot1 = MagicMock()
        bot2 = MagicMock()

        with (
            patch("app.services.risk_manager.Bot") as MockBot,
            patch("app.services.risk_manager.BotLog"),
            patch("app.services.risk_manager.db") as MockDB,
        ):
            MockBot.query.filter_by.return_value.all.return_value = [bot1, bot2]
            MockBot.BotStatus = BotStatus  # expose the enum
            MockDB.session.commit = MagicMock()
            MockDB.session.add    = MagicMock()

            from app.services.risk_manager import emergency_stop
            count = emergency_stop(1, "drawdown limit exceeded")

        assert count == 2
