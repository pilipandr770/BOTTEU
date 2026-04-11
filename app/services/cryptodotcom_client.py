"""
Crypto.com Exchange public API client — price cross-check utility.

No authentication required. Used to validate Binance prices before
executing real orders (deviation > 1.5% may indicate stale/bad data).

Public API docs: https://exchange-docs.crypto.com/exchange/v1/rest-ws/index.html
"""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

CRYPTODOTCOM_API = "https://api.crypto.com/exchange/v1"
_REQUEST_TIMEOUT = 5  # seconds


def get_price(symbol: str) -> float | None:
    """
    Get the ask price from Crypto.com public ticker.
    Returns None on any network/parsing error.

    symbol: Binance-style e.g. "BTCUSDT" → converted to "BTC_USDT" for Crypto.com.
    """
    try:
        instrument = symbol.replace("USDT", "_USDT").upper()
        r = requests.get(
            f"{CRYPTODOTCOM_API}/public/get-ticker",
            params={"instrument_name": instrument},
            timeout=_REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        return float(data["result"]["data"]["a"])  # ask price
    except Exception as exc:
        logger.debug("Crypto.com price fetch failed for %s: %s", symbol, exc)
        return None


def price_deviation_pct(binance_price: float, symbol: str) -> float | None:
    """
    Return the absolute % deviation between the Binance price and Crypto.com
    ask price for the same symbol.  Returns None when the Crypto.com price
    cannot be fetched (network error, unsupported pair, etc.).
    """
    cdc_price = get_price(symbol)
    if cdc_price and binance_price:
        return abs(binance_price - cdc_price) / binance_price * 100
    return None
