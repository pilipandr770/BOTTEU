"""
Binance client factory — decrypts API credentials on-the-fly,
never stores plaintext in memory beyond the scope of a single task.
"""
import logging
from datetime import datetime, timezone

from binance.client import Client
from binance.exceptions import BinanceAPIException

from app.extensions import db
from app.models.api_key import ApiKey
from app.services.encryption import decrypt

logger = logging.getLogger(__name__)

# Quote currencies we support (order = priority in UI)
SUPPORTED_QUOTES = ("USDT", "USDC", "BTC", "ETH", "BNB")


def get_client_for_user(user_id: int) -> Client:
    """
    Return an authenticated python-binance Client for the given user.
    Credentials are decrypted in-memory and not persisted anywhere.
    Raises ValueError if no API key is configured or it is invalid.
    """
    api_key_record: ApiKey | None = ApiKey.query.filter_by(user_id=user_id).first()
    if not api_key_record:
        raise ValueError("No API key found. Please add your Binance API key in the cabinet.")

    api_key = decrypt(api_key_record.encrypted_api_key)
    api_secret = decrypt(api_key_record.encrypted_api_secret)

    client = Client(api_key, api_secret)
    api_key = api_secret = None  # noqa: F841
    return client


def _fetch_spot_symbols(client: Client) -> list[dict]:
    """
    Fetch all active SPOT trading pairs from Binance exchange info.
    Returns list of {symbol, base, quote} dicts filtered to SUPPORTED_QUOTES.
    Uses the public endpoint — no credentials needed, but we reuse the client.
    """
    try:
        info = client.get_exchange_info()
        result = []
        for s in info.get("symbols", []):
            if (
                s.get("status") == "TRADING"
                and s.get("isSpotTradingAllowed")
                and s.get("quoteAsset") in SUPPORTED_QUOTES
            ):
                result.append({
                    "symbol": s["symbol"],
                    "base": s["baseAsset"],
                    "quote": s["quoteAsset"],
                })
        # Sort: USDT first, then USDC, then others; alphabetically within group
        quote_order = {q: i for i, q in enumerate(SUPPORTED_QUOTES)}
        result.sort(key=lambda x: (quote_order.get(x["quote"], 99), x["symbol"]))
        return result
    except Exception as exc:
        logger.warning("Could not fetch exchange symbols: %s", exc)
        return []


def validate_api_key(user_id: int) -> tuple[bool, str]:
    """
    Test API credentials by calling get_account().
    On success also syncs available spot symbols from exchange info.
    Returns (success: bool, message: str).
    Updates ApiKey.is_valid in the DB.
    """
    try:
        client = get_client_for_user(user_id)
        client.get_account()  # raises BinanceAPIException on bad creds

        api_key_record = ApiKey.query.filter_by(user_id=user_id).first()
        api_key_record.is_valid = True
        api_key_record.last_checked_at = datetime.now(timezone.utc)

        # Sync available trading pairs
        symbols = _fetch_spot_symbols(client)
        if symbols:
            api_key_record.cached_symbols = symbols
            logger.info("Synced %d spot symbols for user %d", len(symbols), user_id)

        db.session.commit()
        symbol_count = len(symbols) if symbols else 0
        return True, f"Connection successful. Synced {symbol_count} trading pairs."

    except BinanceAPIException as exc:
        api_key_record = ApiKey.query.filter_by(user_id=user_id).first()
        if api_key_record:
            api_key_record.is_valid = False
            db.session.commit()
        return False, f"Binance error: {exc.message}"
    except ValueError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"Unexpected error: {exc}"


def get_cached_symbols(user_id: int) -> list[dict]:
    """Return cached symbol list for user, or empty list if not synced yet."""
    record = ApiKey.query.filter_by(user_id=user_id).first()
    if record and record.cached_symbols:
        return record.cached_symbols
    return []


def get_spot_balance(user_id: int) -> dict:
    """
    Fetch non-zero spot balances for the user.
    Returns dict with keys:
        balances: [{asset, free, locked}]  — non-zero assets sorted by free desc
        error: str | None
    """
    try:
        client = get_client_for_user(user_id)
        account = client.get_account()
        balances = [
            {
                "asset":  b["asset"],
                "free":   float(b["free"]),
                "locked": float(b["locked"]),
            }
            for b in account.get("balances", [])
            if float(b["free"]) > 0 or float(b["locked"]) > 0
        ]
        # Sort: USDT/USDC first, then by free balance descending
        priority = {"USDT": 0, "USDC": 1}
        balances.sort(key=lambda x: (priority.get(x["asset"], 2), -x["free"]))
        return {"balances": balances, "error": None}
    except ValueError as exc:
        return {"balances": [], "error": str(exc)}
    except Exception as exc:
        logger.warning("get_spot_balance failed for user %d: %s", user_id, exc)
        return {"balances": [], "error": "Failed to fetch balance. Check your API key."}


def get_quote_free_balance(client, symbol: str) -> float:
    """Return free balance of the quote asset for the given symbol (e.g. USDT for BTCUSDT)."""
    quote = None
    for q in SUPPORTED_QUOTES:
        if symbol.upper().endswith(q):
            quote = q
            break
    if not quote:
        return 0.0
    try:
        account = client.get_account()
        for b in account.get("balances", []):
            if b["asset"] == quote:
                return float(b["free"])
    except Exception:
        pass
    return 0.0
