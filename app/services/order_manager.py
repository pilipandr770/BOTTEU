"""Order Manager - handles Binance exchange filters and places LIMIT / MARKET orders.

Responsibilities:
  1. Fetch LOT_SIZE (stepSize), PRICE_FILTER (tickSize), MIN_NOTIONAL for a symbol.
  2. Round quantity to stepSize, price to tickSize (Decimal-precise).
  3. Verify MIN_NOTIONAL before order submission.
  4. Place a LIMIT order (maker fee, single fill, no slippage).
  5. Fallback to MARKET if LIMIT is not filled within timeout.
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
        step_size   - minimum quantity increment (Decimal)
        tick_size   - minimum price increment (Decimal)
        min_notional - minimum order value in quote currency (Decimal)
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
            raise ValueError("Rounded quantity is zero - cannot place SELL order.")
        response = client.order_market_sell(
            symbol=symbol,
            quantity=str(quantity),
        )
    else:
        raise ValueError(f"Invalid side: {side}")

    logger.info("Order placed: %s %s response=%s", side, symbol, response.get("orderId"))
    return response


def place_limit_order(
    client: Client,
    symbol: str,
    side: str,
    quantity: Decimal,
    price: Decimal,
) -> dict:
    """
    Place a LIMIT GTC order.
    Single fill at one price -- no partial fill fee multiplication,
    lower maker fee (0.075% vs 0.1% with BNB discount).
    """
    filters    = get_symbol_filters(client, symbol)
    step_size  = filters["step_size"]
    tick_size  = filters["tick_size"]
    min_not    = filters["min_notional"]

    quantity = _round_step(quantity, step_size)
    price    = _round_step(price, tick_size)

    if quantity <= 0:
        raise ValueError("Rounded quantity is zero - cannot place LIMIT order.")
    if price * quantity < min_not:
        raise ValueError(
            f"LIMIT order notional {price * quantity} < MIN_NOTIONAL {min_not}."
        )

    response = client.create_order(
        symbol=symbol,
        side=side.upper(),
        type="LIMIT",
        timeInForce="GTC",
        quantity=str(quantity),
        price=str(price),
    )
    logger.info("LIMIT order placed: %s %s qty=%s price=%s id=%s",
                side, symbol, quantity, price, response.get("orderId"))
    return response


def wait_for_fill(
    client: Client,
    symbol: str,
    order_id: int,
    timeout_sec: int = 30,
    poll_interval: float = 2.0,
) -> dict:
    """
    Poll until a LIMIT order is filled or timeout expires.
    Returns the order status dict.  Status values:
      NEW, PARTIALLY_FILLED, FILLED, CANCELED, EXPIRED.
    """
    import time
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        order = client.get_order(symbol=symbol, orderId=order_id)
        if order["status"] in ("FILLED", "CANCELED", "EXPIRED", "REJECTED"):
            return order
        time.sleep(poll_interval)
    return client.get_order(symbol=symbol, orderId=order_id)


def cancel_order(client: Client, symbol: str, order_id: int) -> dict:
    """Cancel a single order by ID. Returns Binance response."""
    try:
        return client.cancel_order(symbol=symbol, orderId=order_id)
    except BinanceAPIException as exc:
        if exc.code == -2011:  # "Unknown order" - already cancelled or filled
            logger.debug("Order %s already cancelled/filled", order_id)
            return {}
        raise


def place_smart_order(
    client: Client,
    symbol: str,
    side: str,
    quote_amount: Decimal | None = None,
    quantity: Decimal | None = None,
    use_limit: bool = True,
    limit_timeout_sec: int = 30,
) -> dict:
    """
    Smart order placement:
      1. Try LIMIT at current best bid (BUY) or best ask (SELL) -- maker fee, single fill
      2. If not fully filled within timeout -- cancel remainder -- MARKET for the rest
      3. Fallback to pure MARKET if use_limit=False

    Returns a unified dict with keys:
        orderId, executedQty, cummulativeQuoteQty, status, order_type
    """
    if not use_limit:
        resp = place_market_order(client, symbol, side, quote_amount=quote_amount, quantity=quantity)
        resp["order_type"] = "MARKET"
        return resp

    filters   = get_symbol_filters(client, symbol)
    step_size = filters["step_size"]
    tick_size = filters["tick_size"]

    # Get current best bid/ask to place limit order AT the book
    ticker = client.get_orderbook_ticker(symbol=symbol)
    if side.upper() == "BUY":
        # Place LIMIT BUY at best bid price
        limit_price = Decimal(ticker["bidPrice"])
        if quote_amount is None:
            raise ValueError("quote_amount required for BUY.")
        limit_qty = _round_step(quote_amount / limit_price, step_size)
    else:
        # Place LIMIT SELL at best ask price
        limit_price = Decimal(ticker["askPrice"])
        if quantity is None:
            raise ValueError("quantity required for SELL.")
        limit_qty = _round_step(quantity, step_size)

    if limit_qty <= 0 or limit_price <= 0:
        # Fallback to MARKET if book price is broken
        resp = place_market_order(client, symbol, side, quote_amount=quote_amount, quantity=quantity)
        resp["order_type"] = "MARKET_FALLBACK"
        return resp

    try:
        limit_resp = place_limit_order(client, symbol, side, limit_qty, limit_price)
    except (BinanceAPIException, ValueError) as exc:
        logger.warning("LIMIT order failed (%s), falling back to MARKET: %s", symbol, exc)
        resp = place_market_order(client, symbol, side, quote_amount=quote_amount, quantity=quantity)
        resp["order_type"] = "MARKET_FALLBACK"
        return resp

    order_id = int(limit_resp["orderId"])
    status   = wait_for_fill(client, symbol, order_id, timeout_sec=limit_timeout_sec)

    if status["status"] == "FILLED":
        status["order_type"] = "LIMIT"
        return status

    # Partially filled or not filled - cancel the remainder
    cancel_order(client, symbol, order_id)
    filled_qty  = Decimal(status.get("executedQty", "0"))
    filled_quote = Decimal(status.get("cummulativeQuoteQty", "0"))

    if filled_qty >= limit_qty * Decimal("0.99"):
        # Essentially fully filled (rounding)
        status["order_type"] = "LIMIT"
        return status

    # Execute remaining via MARKET
    remaining_qty   = limit_qty - filled_qty
    remaining_quote = (quote_amount or Decimal("0")) - filled_quote

    try:
        if side.upper() == "BUY" and remaining_quote > 0:
            market_resp = place_market_order(client, symbol, side, quote_amount=remaining_quote)
        elif side.upper() == "SELL" and remaining_qty > 0:
            market_resp = place_market_order(client, symbol, side, quantity=remaining_qty)
        else:
            status["order_type"] = "LIMIT_PARTIAL"
            return status
    except Exception as exc:
        logger.warning("MARKET fallback failed for remainder on %s: %s", symbol, exc)
        status["order_type"] = "LIMIT_PARTIAL"
        return status

    # Merge the two fills into one response
    total_qty   = filled_qty + Decimal(market_resp.get("executedQty", "0"))
    total_quote = filled_quote + Decimal(market_resp.get("cummulativeQuoteQty", "0"))
    return {
        "orderId":              market_resp.get("orderId"),
        "executedQty":          str(total_qty),
        "cummulativeQuoteQty":  str(total_quote),
        "status":               "FILLED",
        "order_type":           "LIMIT+MARKET",
    }


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
        raise ValueError("Rounded quantity is zero - cannot place stop-loss order.")

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
        raise ValueError("Rounded quantity is zero - cannot place OCO order.")

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
