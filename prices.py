"""
Crypto price fetcher using CoinGecko free (Demo) API.
Caches prices for 60 seconds to stay within rate limits (30 calls/min).
"""
import time
import logging
import aiohttp

logger = logging.getLogger(__name__)

COINGECKO_URL = "https://api.coingecko.com/api/v3"

# Map our chain/token symbols to CoinGecko IDs
COIN_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "TRX": "tron",
    "BNB": "binancecoin",
    "USDT": "tether",
    "USDC": "usd-coin",
}

_price_cache: dict[str, tuple[float, float]] = {}  # symbol -> (price, timestamp)
CACHE_TTL = 60  # seconds


async def get_price_usd(symbol: str) -> float | None:
    """Get current USD price for a crypto symbol."""
    symbol = symbol.upper()

    # Stablecoins — shortcut
    if symbol in ("USDT", "USDC", "DAI", "BUSD", "TUSD"):
        return 1.0

    # Check cache
    if symbol in _price_cache:
        price, ts = _price_cache[symbol]
        if time.time() - ts < CACHE_TTL:
            return price

    cg_id = COIN_IDS.get(symbol)
    if not cg_id:
        return None

    try:
        async with aiohttp.ClientSession() as session:
            url = f"{COINGECKO_URL}/simple/price"
            params = {"ids": cg_id, "vs_currencies": "usd"}
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    price = data.get(cg_id, {}).get("usd")
                    if price is not None:
                        _price_cache[symbol] = (price, time.time())
                        return price
                else:
                    logger.warning(f"CoinGecko returned {resp.status} for {symbol}")
    except Exception as e:
        logger.error(f"Price fetch error for {symbol}: {e}")

    # Return cached even if stale
    if symbol in _price_cache:
        return _price_cache[symbol][0]
    return None


async def get_prices_bulk(symbols: list[str]) -> dict[str, float]:
    """Get prices for multiple symbols in one request."""
    result = {}
    to_fetch = []

    for s in symbols:
        s = s.upper()
        if s in ("USDT", "USDC", "DAI", "BUSD", "TUSD"):
            result[s] = 1.0
            continue
        if s in _price_cache:
            price, ts = _price_cache[s]
            if time.time() - ts < CACHE_TTL:
                result[s] = price
                continue
        cg_id = COIN_IDS.get(s)
        if cg_id:
            to_fetch.append((s, cg_id))

    if to_fetch:
        ids_str = ",".join(cg_id for _, cg_id in to_fetch)
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{COINGECKO_URL}/simple/price"
                params = {"ids": ids_str, "vs_currencies": "usd"}
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for sym, cg_id in to_fetch:
                            price = data.get(cg_id, {}).get("usd")
                            if price is not None:
                                _price_cache[sym] = (price, time.time())
                                result[sym] = price
        except Exception as e:
            logger.error(f"Bulk price fetch error: {e}")

    return result


async def convert_to_usd(amount: float, symbol: str) -> float | None:
    """Convert crypto amount to USD."""
    price = await get_price_usd(symbol)
    if price is None:
        return None
    return amount * price


async def convert_from_usd(usd_amount: float, symbol: str) -> float | None:
    """Convert USD amount to crypto."""
    price = await get_price_usd(symbol)
    if price is None or price == 0:
        return None
    return usd_amount / price
