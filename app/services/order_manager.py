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


def place_stop_loss_order(
    client: Client,
    symbol: str,
    quantity: Decimal,
    stop_price: Decimal,
) -> dict:
    """
    Place a STOP_LOSS_LIMIT SELL order on Binance so the exchange enforces the
    stop-loss even if the server goes offline (P1 fix).

    Uses stopLimitPrice = stopPrice * 0.999 (0.1% below trigger) to avoid
    immediate fill rejection in fast-moving markets.
    """
    filters = get_symbol_filters(client, symbol)
    step_size  = filters["step_size"]
    tick_size  = filters["tick_size"]

    quantity    = _round_step(quantity, step_size)
    stop_price  = _round_step(stop_price, tick_size)
    limit_price = _round_step(stop_price * Decimal("0.999"), tick_size)

    if quantity <= 0:
        raise ValueError("Rounded quantity is zero — cannot place stop-loss order.")

    response = client.create_order(
        symbol=symbol,
        side="SELL",
        type="STOP_LOSS_LIMIT",
        timeInForce="GTC",
        quantity=str(quantity),
        stopPrice=str(stop_price),
        price=str(limit_price),
    )
    logger.info("Stop-loss order placed: %s stopPrice=%s response=%s",
                symbol, stop_price, response.get("orderId"))
    return response


def place_oco_sell_order(
    client: Client,
    symbol: str,
    quantity: Decimal,
    stop_price: Decimal,
    take_profit_price: Decimal,
) -> dict:
    """
    Place an OCO (One-Cancels-the-Other) SELL order:
      - LIMIT_MAKER leg at take_profit_price (TP)
      - STOP_LOSS_LIMIT leg at stop_price (SL)

    Whichever triggers first cancels the other.
    Requires BOTH stop_price < current_price < take_profit_price.
    """
    filters = get_symbol_filters(client, symbol)
    step_size  = filters["step_size"]
    tick_size  = filters["tick_size"]

    quantity         = _round_step(quantity, step_size)
    stop_price       = _round_step(stop_price, tick_size)
    stop_limit_price = _round_step(stop_price * Decimal("0.999"), tick_size)
    take_profit_price = _round_step(take_profit_price, tick_size)

    if quantity <= 0:
        raise ValueError("Rounded quantity is zero — cannot place OCO order.")

    response = client.order_oco_sell(
        symbol=symbol,
        quantity=str(quantity),
        price=str(take_profit_price),        # TP limit price
        stopPrice=str(stop_price),           # SL trigger
        stopLimitPrice=str(stop_limit_price),
        stopLimitTimeInForce="GTC",
    )
    logger.info("OCO sell order placed: %s tp=%s sl=%s listId=%s",
                symbol, take_profit_price, stop_price, response.get("orderListId"))
    return response


def cancel_open_orders(client: Client, symbol: str) -> list[dict]:
    """Cancel all open orders for a symbol. Called before a market SELL to avoid
    'would reduce position' conflicts with existing stop/OCO orders."""
    try:
        result = client.cancel_open_orders(symbol=symbol)
        logger.info("Cancelled open orders for %s: %d order(s)", symbol, len(result))
        return result
    except Exception:
        logger.exception("Failed to cancel open orders for %s", symbol)
        return []
