"""
Unit tests for app/services/order_manager.py — Binance order placement.

This module places real money orders in production, so every branch (filter
rounding, MIN_NOTIONAL checks, LIMIT->MARKET fallback, OCO/stop-loss) is
covered here with a mocked python-binance Client. No network calls are made.
"""
from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from binance.exceptions import BinanceAPIException

from app.services.order_manager import (
    _round_step,
    get_symbol_filters,
    place_market_order,
    place_limit_order,
    wait_for_fill,
    cancel_order,
    place_smart_order,
    place_stop_loss_order,
    place_oco_sell_order,
    cancel_open_orders,
)


# ── Helpers ───────────────────────────────────────────────────────────────

def _binance_exc(code: int, msg: str = "error") -> BinanceAPIException:
    response = MagicMock()
    response.text = json.dumps({"code": code, "msg": msg})
    return BinanceAPIException(response=response, status_code=400, text=response.text)


def _symbol_info(step_size="0.00001", tick_size="0.01", min_notional="10"):
    return {
        "filters": [
            {"filterType": "LOT_SIZE", "stepSize": step_size, "maxQty": "9000000"},
            {"filterType": "PRICE_FILTER", "tickSize": tick_size},
            {"filterType": "NOTIONAL", "minNotional": min_notional},
        ]
    }


def _make_client(step_size="0.00001", tick_size="0.01", min_notional="10"):
    client = MagicMock()
    client.get_symbol_info.return_value = _symbol_info(step_size, tick_size, min_notional)
    return client


# ── _round_step ───────────────────────────────────────────────────────────

class TestRoundStep:
    def test_floors_to_step(self):
        assert _round_step(Decimal("1.23456"), Decimal("0.001")) == Decimal("1.234")

    def test_exact_multiple_unchanged(self):
        assert _round_step(Decimal("2.500"), Decimal("0.5")) == Decimal("2.500")

    def test_zero_step_returns_value_unchanged(self):
        assert _round_step(Decimal("1.23456"), Decimal("0")) == Decimal("1.23456")


# ── get_symbol_filters ────────────────────────────────────────────────────

class TestGetSymbolFilters:
    def test_parses_filters(self):
        client = _make_client(step_size="0.001", tick_size="0.1", min_notional="5")
        filters = get_symbol_filters(client, "BTCUSDT")
        assert filters["step_size"] == Decimal("0.001")
        assert filters["tick_size"] == Decimal("0.1")
        assert filters["min_notional"] == Decimal("5")

    def test_falls_back_to_min_notional_filter_type(self):
        client = MagicMock()
        client.get_symbol_info.return_value = {
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                {"filterType": "MIN_NOTIONAL", "minNotional": "7"},
            ]
        }
        filters = get_symbol_filters(client, "BTCUSDT")
        assert filters["min_notional"] == Decimal("7")

    def test_unknown_symbol_raises(self):
        client = MagicMock()
        client.get_symbol_info.return_value = None
        with pytest.raises(ValueError, match="not found"):
            get_symbol_filters(client, "FOOBAR")


# ── place_market_order ────────────────────────────────────────────────────

class TestPlaceMarketOrder:
    def test_buy_below_min_notional_raises(self):
        client = _make_client(min_notional="10")
        with pytest.raises(ValueError, match="MIN_NOTIONAL"):
            place_market_order(client, "BTCUSDT", "BUY", quote_amount=Decimal("5"))
        client.order_market_buy.assert_not_called()

    def test_buy_happy_path(self):
        client = _make_client(min_notional="10")
        client.order_market_buy.return_value = {"orderId": 1, "status": "FILLED"}
        resp = place_market_order(client, "BTCUSDT", "BUY", quote_amount=Decimal("50.005"))
        client.order_market_buy.assert_called_once_with(symbol="BTCUSDT", quoteOrderQty="50.00")
        assert resp["orderId"] == 1

    def test_buy_without_quote_amount_raises(self):
        client = _make_client()
        with pytest.raises(ValueError, match="quote_amount required"):
            place_market_order(client, "BTCUSDT", "BUY")

    def test_sell_rounds_quantity_to_step(self):
        client = _make_client(step_size="0.001")
        client.order_market_sell.return_value = {"orderId": 2, "status": "FILLED"}
        place_market_order(client, "BTCUSDT", "SELL", quantity=Decimal("1.23456"))
        client.order_market_sell.assert_called_once_with(symbol="BTCUSDT", quantity="1.234")

    def test_sell_rounded_to_zero_raises(self):
        client = _make_client(step_size="0.001")
        with pytest.raises(ValueError, match="zero"):
            place_market_order(client, "BTCUSDT", "SELL", quantity=Decimal("0.0001"))
        client.order_market_sell.assert_not_called()

    def test_sell_without_quantity_raises(self):
        client = _make_client()
        with pytest.raises(ValueError, match="quantity required"):
            place_market_order(client, "BTCUSDT", "SELL")

    def test_invalid_side_raises(self):
        client = _make_client()
        with pytest.raises(ValueError, match="Invalid side"):
            place_market_order(client, "BTCUSDT", "HOLD", quantity=Decimal("1"))


# ── place_limit_order ─────────────────────────────────────────────────────

class TestPlaceLimitOrder:
    def test_happy_path_rounds_and_calls_create_order(self):
        client = _make_client(step_size="0.001", tick_size="0.5", min_notional="10")
        client.create_order.return_value = {"orderId": 3, "status": "NEW"}
        resp = place_limit_order(client, "BTCUSDT", "BUY", Decimal("1.23456"), Decimal("100.24"))
        client.create_order.assert_called_once_with(
            symbol="BTCUSDT", side="BUY", type="LIMIT", timeInForce="GTC",
            quantity="1.234", price="100.0",
        )
        assert resp["orderId"] == 3

    def test_zero_quantity_raises(self):
        client = _make_client(step_size="1")
        with pytest.raises(ValueError, match="zero"):
            place_limit_order(client, "BTCUSDT", "BUY", Decimal("0.4"), Decimal("100"))

    def test_below_min_notional_raises(self):
        client = _make_client(step_size="0.001", tick_size="0.01", min_notional="1000")
        with pytest.raises(ValueError, match="MIN_NOTIONAL"):
            place_limit_order(client, "BTCUSDT", "BUY", Decimal("1"), Decimal("10"))


# ── wait_for_fill ──────────────────────────────────────────────────────────

class TestWaitForFill:
    def test_returns_immediately_when_filled(self, monkeypatch):
        client = MagicMock()
        client.get_order.return_value = {"status": "FILLED"}
        sleep_calls = []
        monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))
        result = wait_for_fill(client, "BTCUSDT", 123, timeout_sec=10)
        assert result["status"] == "FILLED"
        assert sleep_calls == []

    def test_polls_until_terminal_status(self, monkeypatch):
        client = MagicMock()
        client.get_order.side_effect = [
            {"status": "NEW"},
            {"status": "PARTIALLY_FILLED"},
            {"status": "FILLED"},
        ]
        monkeypatch.setattr("time.sleep", lambda s: None)
        result = wait_for_fill(client, "BTCUSDT", 123, timeout_sec=10, poll_interval=0.01)
        assert result["status"] == "FILLED"
        assert client.get_order.call_count == 3

    def test_returns_last_status_on_timeout(self, monkeypatch):
        client = MagicMock()
        client.get_order.return_value = {"status": "NEW"}
        # Simulate the deadline elapsing immediately without a real sleep.
        times = iter([0, 0, 100])
        monkeypatch.setattr("time.monotonic", lambda: next(times))
        monkeypatch.setattr("time.sleep", lambda s: None)
        result = wait_for_fill(client, "BTCUSDT", 123, timeout_sec=1, poll_interval=0.01)
        assert result["status"] == "NEW"


# ── cancel_order ───────────────────────────────────────────────────────────

class TestCancelOrder:
    def test_cancels_successfully(self):
        client = MagicMock()
        client.cancel_order.return_value = {"status": "CANCELED"}
        result = cancel_order(client, "BTCUSDT", 1)
        assert result["status"] == "CANCELED"

    def test_swallows_unknown_order_error(self):
        client = MagicMock()
        client.cancel_order.side_effect = _binance_exc(-2011, "Unknown order sent.")
        result = cancel_order(client, "BTCUSDT", 1)
        assert result == {}

    def test_reraises_other_binance_errors(self):
        client = MagicMock()
        client.cancel_order.side_effect = _binance_exc(-1013, "Invalid quantity.")
        with pytest.raises(BinanceAPIException):
            cancel_order(client, "BTCUSDT", 1)


# ── place_smart_order ─────────────────────────────────────────────────────

class TestPlaceSmartOrder:
    def test_use_limit_false_goes_straight_to_market(self):
        client = _make_client()
        client.order_market_buy.return_value = {"orderId": 1, "status": "FILLED"}
        resp = place_smart_order(client, "BTCUSDT", "BUY", quote_amount=Decimal("50"), use_limit=False)
        assert resp["order_type"] == "MARKET"
        client.get_orderbook_ticker.assert_not_called()

    def test_broken_orderbook_falls_back_to_market(self):
        client = _make_client()
        client.get_orderbook_ticker.return_value = {"bidPrice": "0", "askPrice": "0"}
        client.order_market_buy.return_value = {"orderId": 1, "status": "FILLED"}
        resp = place_smart_order(client, "BTCUSDT", "BUY", quote_amount=Decimal("50"))
        assert resp["order_type"] == "MARKET_FALLBACK"

    def test_limit_fully_filled(self, monkeypatch):
        client = _make_client(step_size="0.001", tick_size="0.01", min_notional="10")
        client.get_orderbook_ticker.return_value = {"bidPrice": "100.00", "askPrice": "100.10"}
        client.create_order.return_value = {"orderId": 5, "status": "NEW"}
        client.get_order.return_value = {
            "status": "FILLED", "executedQty": "0.5", "cummulativeQuoteQty": "50",
        }
        resp = place_smart_order(client, "BTCUSDT", "BUY", quote_amount=Decimal("50"))
        assert resp["order_type"] == "LIMIT"
        assert resp["status"] == "FILLED"

    def test_limit_order_raises_falls_back_to_market(self, monkeypatch):
        client = _make_client(step_size="0.001", tick_size="0.01", min_notional="10")
        client.get_orderbook_ticker.return_value = {"bidPrice": "100.00", "askPrice": "100.10"}
        # Exchange rejects the LIMIT order itself (e.g. insufficient balance).
        client.create_order.side_effect = _binance_exc(-2010, "Account has insufficient balance.")
        client.order_market_buy.return_value = {"orderId": 9, "status": "FILLED"}
        resp = place_smart_order(client, "BTCUSDT", "BUY", quote_amount=Decimal("50"))
        assert resp["order_type"] == "MARKET_FALLBACK"

    def test_partial_fill_completes_remainder_via_market(self, monkeypatch):
        client = _make_client(step_size="0.001", tick_size="0.01", min_notional="10")
        client.get_orderbook_ticker.return_value = {"bidPrice": "100.00", "askPrice": "100.10"}
        client.create_order.return_value = {"orderId": 5, "status": "NEW"}
        # Only half filled — well below the 99% "essentially filled" threshold.
        client.get_order.return_value = {
            "status": "PARTIALLY_FILLED", "executedQty": "0.25", "cummulativeQuoteQty": "25",
        }
        client.cancel_order.return_value = {"status": "CANCELED"}
        client.order_market_buy.return_value = {"orderId": 6, "executedQty": "0.25", "cummulativeQuoteQty": "25"}

        resp = place_smart_order(client, "BTCUSDT", "BUY", quote_amount=Decimal("50"), limit_timeout_sec=0)

        assert resp["order_type"] == "LIMIT+MARKET"
        assert resp["status"] == "FILLED"
        assert Decimal(resp["executedQty"]) == Decimal("0.5")
        assert Decimal(resp["cummulativeQuoteQty"]) == Decimal("50")

    def test_essentially_full_fill_within_tolerance_treated_as_limit(self):
        client = _make_client(step_size="0.001", tick_size="0.01", min_notional="10")
        client.get_orderbook_ticker.return_value = {"bidPrice": "100.00", "askPrice": "100.10"}
        client.create_order.return_value = {"orderId": 5, "status": "NEW"}
        # 99.9% filled — within the 99% tolerance band, should not trigger a market remainder order.
        client.get_order.return_value = {
            "status": "PARTIALLY_FILLED", "executedQty": "0.4995", "cummulativeQuoteQty": "49.95",
        }
        resp = place_smart_order(client, "BTCUSDT", "BUY", quote_amount=Decimal("50"), limit_timeout_sec=0)
        assert resp["order_type"] == "LIMIT"
        client.order_market_buy.assert_not_called()

    def test_market_remainder_failure_returns_limit_partial(self):
        client = _make_client(step_size="0.001", tick_size="0.01", min_notional="10")
        client.get_orderbook_ticker.return_value = {"bidPrice": "100.00", "askPrice": "100.10"}
        client.create_order.return_value = {"orderId": 5, "status": "NEW"}
        client.get_order.return_value = {
            "status": "PARTIALLY_FILLED", "executedQty": "0.25", "cummulativeQuoteQty": "25",
        }
        client.cancel_order.return_value = {"status": "CANCELED"}
        client.order_market_buy.side_effect = _binance_exc(-1021, "Timestamp outside recv window.")

        resp = place_smart_order(client, "BTCUSDT", "BUY", quote_amount=Decimal("50"), limit_timeout_sec=0)
        assert resp["order_type"] == "LIMIT_PARTIAL"

    def test_sell_side_uses_ask_price_and_quantity(self):
        client = _make_client(step_size="0.001", tick_size="0.01", min_notional="10")
        client.get_orderbook_ticker.return_value = {"bidPrice": "99.90", "askPrice": "100.10"}
        client.create_order.return_value = {"orderId": 7, "status": "NEW"}
        client.get_order.return_value = {
            "status": "FILLED", "executedQty": "0.5", "cummulativeQuoteQty": "50.05",
        }
        resp = place_smart_order(client, "BTCUSDT", "SELL", quantity=Decimal("0.5"))
        assert resp["order_type"] == "LIMIT"
        call_kwargs = client.create_order.call_args.kwargs
        assert Decimal(call_kwargs["price"]) == Decimal("100.10")


# ── place_stop_loss_order ─────────────────────────────────────────────────

class TestPlaceStopLossOrder:
    def test_happy_path(self):
        client = _make_client(step_size="0.001", tick_size="0.01")
        client.create_order.return_value = {"orderId": 8}
        place_stop_loss_order(client, "BTCUSDT", Decimal("1.23456"), Decimal("100"))
        kwargs = client.create_order.call_args.kwargs
        assert kwargs["side"] == "SELL"
        assert kwargs["type"] == "STOP_LOSS_LIMIT"
        assert kwargs["quantity"] == "1.234"
        assert kwargs["stopPrice"] == "100.00"
        assert Decimal(kwargs["price"]) < Decimal(kwargs["stopPrice"])

    def test_zero_quantity_raises(self):
        client = _make_client(step_size="1")
        with pytest.raises(ValueError, match="zero"):
            place_stop_loss_order(client, "BTCUSDT", Decimal("0.4"), Decimal("100"))


# ── place_oco_sell_order ───────────────────────────────────────────────────

class TestPlaceOcoSellOrder:
    def test_happy_path(self):
        client = _make_client(step_size="0.001", tick_size="0.01")
        client.order_oco_sell.return_value = {"orderListId": 42}
        place_oco_sell_order(
            client, "BTCUSDT", Decimal("1.23456"), Decimal("90"), Decimal("110"),
        )
        kwargs = client.order_oco_sell.call_args.kwargs
        assert kwargs["quantity"] == "1.234"
        assert kwargs["price"] == "110.00"
        assert kwargs["stopPrice"] == "90.00"
        assert Decimal(kwargs["stopLimitPrice"]) < Decimal(kwargs["stopPrice"])

    def test_zero_quantity_raises(self):
        client = _make_client(step_size="1")
        with pytest.raises(ValueError, match="zero"):
            place_oco_sell_order(client, "BTCUSDT", Decimal("0.4"), Decimal("90"), Decimal("110"))


# ── cancel_open_orders ─────────────────────────────────────────────────────

class TestCancelOpenOrders:
    def test_returns_cancelled_list(self):
        client = MagicMock()
        client.cancel_open_orders.return_value = [{"orderId": 1}, {"orderId": 2}]
        result = cancel_open_orders(client, "BTCUSDT")
        assert len(result) == 2

    def test_swallows_exception_returns_empty_list(self):
        client = MagicMock()
        client.cancel_open_orders.side_effect = RuntimeError("network error")
        result = cancel_open_orders(client, "BTCUSDT")
        assert result == []
