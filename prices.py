"""
Crypto price fetcher with fallback: CoinGecko → Binance → Coinbase.
Caches prices for 60 seconds to stay within rate limits.
"""
import time
import logging
import aiohttp

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
#  API endpoints
# ══════════════════════════════════════════════════════════════

COINGECKO_URL = "https://api.coingecko.com/api/v3"
BINANCE_URL = "https://api.binance.com/api/v3"
COINBASE_URL = "https://api.coinbase.com/v2"

# Map symbols → CoinGecko IDs
COINGECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "TRX": "tron",
    "BNB": "binancecoin",
    "USDT": "tether",
    "USDC": "usd-coin",
}

# Map symbols → Binance ticker pairs
BINANCE_PAIRS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "TRX": "TRXUSDT",
    "BNB": "BNBUSDT",
}

# Map symbols → Coinbase currency codes
COINBASE_CODES = {
    "BTC": "BTC",
    "ETH": "ETH",
    "TRX": "TRX",
    "BNB": "BNB",
}

STABLECOINS = {"USDT", "USDC", "DAI", "BUSD", "TUSD"}

_price_cache: dict[str, tuple[float, float]] = {}  # symbol -> (price, timestamp)
CACHE_TTL = 60  # seconds
_timeout = aiohttp.ClientTimeout(total=8)


# ══════════════════════════════════════════════════════════════
#  Individual price sources
# ══════════════════════════════════════════════════════════════

async def _fetch_coingecko(symbol: str) -> float | None:
    """Fetch price from CoinGecko free API."""
    cg_id = COINGECKO_IDS.get(symbol)
    if not cg_id:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            params = {"ids": cg_id, "vs_currencies": "usd"}
            async with session.get(
                f"{COINGECKO_URL}/simple/price",
                params=params, timeout=_timeout
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    price = data.get(cg_id, {}).get("usd")
                    if price:
                        return float(price)
                else:
                    logger.warning(f"CoinGecko {resp.status} for {symbol}")
    except Exception as e:
        logger.warning(f"CoinGecko error for {symbol}: {e}")
    return None


async def _fetch_binance(symbol: str) -> float | None:
    """Fetch price from Binance public API (no key needed)."""
    pair = BINANCE_PAIRS.get(symbol)
    if not pair:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            params = {"symbol": pair}
            async with session.get(
                f"{BINANCE_URL}/ticker/price",
                params=params, timeout=_timeout
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    price = data.get("price")
                    if price:
                        return float(price)
                else:
                    logger.warning(f"Binance {resp.status} for {symbol}")
    except Exception as e:
        logger.warning(f"Binance error for {symbol}: {e}")
    return None


async def _fetch_coinbase(symbol: str) -> float | None:
    """Fetch price from Coinbase public API (no key needed)."""
    code = COINBASE_CODES.get(symbol)
    if not code:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{COINBASE_URL}/prices/{code}-USD/spot",
                timeout=_timeout
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    price = data.get("data", {}).get("amount")
                    if price:
                        return float(price)
                else:
                    logger.warning(f"Coinbase {resp.status} for {symbol}")
    except Exception as e:
        logger.warning(f"Coinbase error for {symbol}: {e}")
    return None


# ══════════════════════════════════════════════════════════════
#  Main price getter with fallback chain
# ══════════════════════════════════════════════════════════════

async def get_price_usd(symbol: str) -> float | None:
    """
    Get current USD price with fallback: CoinGecko → Binance → Coinbase.
    Results are cached for 60 seconds.
    """
    symbol = symbol.upper()

    # Stablecoins — always $1
    if symbol in STABLECOINS:
        return 1.0

    # Check cache
    if symbol in _price_cache:
        price, ts = _price_cache[symbol]
        if time.time() - ts < CACHE_TTL:
            return price

    # Try sources in order
    for name, fetcher in [
        ("CoinGecko", _fetch_coingecko),
        ("Binance", _fetch_binance),
        ("Coinbase", _fetch_coinbase),
    ]:
        price = await fetcher(symbol)
        if price is not None:
            _price_cache[symbol] = (price, time.time())
            logger.debug(f"Price {symbol}: ${price:.4f} (from {name})")
            return price

    # All sources failed — return stale cache if available
    if symbol in _price_cache:
        logger.warning(f"All price sources failed for {symbol}, using stale cache")
        return _price_cache[symbol][0]

    logger.error(f"All price sources failed for {symbol}, no cache available")
    return None


async def get_prices_bulk(symbols: list[str]) -> dict[str, float]:
    """
    Get prices for multiple symbols.
    Tries CoinGecko bulk first, then falls back per-symbol.
    """
    result = {}
    to_fetch = []

    for s in symbols:
        s = s.upper()
        if s in STABLECOINS:
            result[s] = 1.0
            continue
        if s in _price_cache:
            price, ts = _price_cache[s]
            if time.time() - ts < CACHE_TTL:
                result[s] = price
                continue
        to_fetch.append(s)

    if not to_fetch:
        return result

    # Try CoinGecko bulk first
    cg_ids = {s: COINGECKO_IDS[s] for s in to_fetch if s in COINGECKO_IDS}
    if cg_ids:
        try:
            ids_str = ",".join(cg_ids.values())
            async with aiohttp.ClientSession() as session:
                params = {"ids": ids_str, "vs_currencies": "usd"}
                async with session.get(
                    f"{COINGECKO_URL}/simple/price",
                    params=params, timeout=_timeout
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for sym, cg_id in cg_ids.items():
                            price = data.get(cg_id, {}).get("usd")
                            if price is not None:
                                _price_cache[sym] = (float(price), time.time())
                                result[sym] = float(price)
        except Exception as e:
            logger.warning(f"CoinGecko bulk fetch error: {e}")

    # Fetch remaining symbols individually (with fallback)
    still_missing = [s for s in to_fetch if s not in result]
    for s in still_missing:
        price = await get_price_usd(s)
        if price is not None:
            result[s] = price

    return result


# ══════════════════════════════════════════════════════════════
#  Conversion helpers
# ══════════════════════════════════════════════════════════════

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
