"""
Order Manager — handles Binance exchange filters and places MARKET orders.

Responsibilities:
  1. Fetch LOT_SIZE (stepSize), PRICE_FILTER (tickSize), MIN_NOTIONAL for a symbol.
  2. Round quantity to stepSize, price to tickSize (Decimal-precise).
  3. Verify MIN_NOTIONAL before order submission.
  4. Place a MARKET order and return the full response.
"""
import logging
from decimal import Decimal, ROUND_DOWN

from binance.client import Client
from binance.exceptions import BinanceAPIException

logger = logging.getLogger(__name__)


def _round_step(value: Decimal, step: Decimal) -> Decimal:
    """Floor-round `value` to the nearest multiple of `step`."""
    if step == 0:
        return value
    return (value // step) * step


def get_symbol_filters(client: Client, symbol: str) -> dict:
    """
    Return a dict with keys:
        step_size   — minimum quantity increment (Decimal)
        tick_size   — minimum price increment (Decimal)
        min_notional — minimum order value in quote currency (Decimal)
    """
    info = client.get_symbol_info(symbol)
    if info is None:
        raise ValueError(f"Symbol {symbol} not found on Binance.")

    filters = {f["filterType"]: f for f in info["filters"]}
    step_size = Decimal(filters["LOT_SIZE"]["stepSize"])
    tick_size = Decimal(filters["PRICE_FILTER"]["tickSize"])
    min_notional = Decimal(filters.get("NOTIONAL", {}).get("minNotional") or
                           filters.get("MIN_NOTIONAL", {}).get("minNotional", "1"))
    return {"step_size": step_size, "tick_size": tick_size, "min_notional": min_notional}


def place_market_order(
    client: Client,
    symbol: str,
    side: str,           # "BUY" or "SELL"
    quote_amount: Decimal | None = None,   # USDT to spend (BUY)
    quantity: Decimal | None = None,       # base asset quantity (SELL)
) -> dict:
    """
    Place a MARKET BUY (by quoteOrderQty) or MARKET SELL (by quantity).
    Validates filters before submission.
    Returns full Binance order response dict.
    """
    filters = get_symbol_filters(client, symbol)
    step_size = filters["step_size"]
    min_notional = filters["min_notional"]

    if side == "BUY":
        if quote_amount is None:
            raise ValueError("quote_amount required for BUY.")
        if quote_amount < min_notional:
            raise ValueError(
                f"Order value {quote_amount} USDT is below MIN_NOTIONAL {min_notional}."
            )
        response = client.order_market_buy(
            symbol=symbol,
            quoteOrderQty=str(quote_amount.quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
        )
    elif side == "SELL":
        if quantity is None:
            raise ValueError("quantity required for SELL.")
        quantity = _round_step(quantity, step_size)
        if quantity <= 0:
            raise ValueError("Rounded quantity is zero — cannot place SELL order.")
        response = client.order_market_sell(
            symbol=symbol,
            quantity=str(quantity),
        )
    else:
        raise ValueError(f"Invalid side: {side}")

    logger.info("Order placed: %s %s response=%s", side, symbol, response.get("orderId"))
    return response
