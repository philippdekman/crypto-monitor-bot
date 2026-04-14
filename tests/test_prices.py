"""
Tests for prices.py — fallback chain, caching, stablecoins, conversions.
"""
import time
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import pytest_asyncio

from prices import (
    get_price_usd, get_prices_bulk, convert_to_usd, convert_from_usd,
    _price_cache, CACHE_TTL, _fetch_coingecko, _fetch_binance, _fetch_coinbase,
)


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear price cache before each test."""
    _price_cache.clear()
    yield
    _price_cache.clear()


# ══════════════════════════════════════════════════════════════
#  Stablecoins (no API call needed)
# ══════════════════════════════════════════════════════════════

class TestStablecoins:
    @pytest.mark.asyncio
    async def test_usdt_returns_one(self):
        assert await get_price_usd("USDT") == 1.0

    @pytest.mark.asyncio
    async def test_usdc_returns_one(self):
        assert await get_price_usd("USDC") == 1.0

    @pytest.mark.asyncio
    async def test_dai_returns_one(self):
        assert await get_price_usd("DAI") == 1.0

    @pytest.mark.asyncio
    async def test_stablecoin_case_insensitive(self):
        assert await get_price_usd("usdt") == 1.0
        assert await get_price_usd("Usdc") == 1.0

    @pytest.mark.asyncio
    async def test_stablecoins_in_bulk(self):
        result = await get_prices_bulk(["USDT", "USDC", "DAI"])
        assert result == {"USDT": 1.0, "USDC": 1.0, "DAI": 1.0}


# ══════════════════════════════════════════════════════════════
#  Caching
# ══════════════════════════════════════════════════════════════

class TestCaching:
    @pytest.mark.asyncio
    async def test_fresh_cache_prevents_api_call(self):
        _price_cache["BTC"] = (50000.0, time.time())
        with patch("prices._fetch_coingecko") as mock_cg:
            price = await get_price_usd("BTC")
            assert price == 50000.0
            mock_cg.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_cache_triggers_fetch(self):
        _price_cache["BTC"] = (50000.0, time.time() - CACHE_TTL - 10)
        with patch("prices._fetch_coingecko", new_callable=AsyncMock, return_value=55000.0):
            price = await get_price_usd("BTC")
            assert price == 55000.0

    @pytest.mark.asyncio
    async def test_stale_cache_used_when_all_fail(self):
        _price_cache["BTC"] = (48000.0, time.time() - 999)
        with patch("prices._fetch_coingecko", new_callable=AsyncMock, return_value=None), \
             patch("prices._fetch_binance", new_callable=AsyncMock, return_value=None), \
             patch("prices._fetch_coinbase", new_callable=AsyncMock, return_value=None):
            price = await get_price_usd("BTC")
            assert price == 48000.0

    @pytest.mark.asyncio
    async def test_successful_fetch_updates_cache(self):
        with patch("prices._fetch_coingecko", new_callable=AsyncMock, return_value=60000.0):
            await get_price_usd("BTC")
        assert "BTC" in _price_cache
        assert _price_cache["BTC"][0] == 60000.0


# ══════════════════════════════════════════════════════════════
#  Fallback chain: CoinGecko → Binance → Coinbase
# ══════════════════════════════════════════════════════════════

class TestFallbackChain:
    @pytest.mark.asyncio
    async def test_coingecko_success_no_fallback(self):
        with patch("prices._fetch_coingecko", new_callable=AsyncMock, return_value=50000.0) as cg, \
             patch("prices._fetch_binance", new_callable=AsyncMock) as bn, \
             patch("prices._fetch_coinbase", new_callable=AsyncMock) as cb:
            price = await get_price_usd("BTC")
            assert price == 50000.0
            cg.assert_called_once_with("BTC")
            bn.assert_not_called()
            cb.assert_not_called()

    @pytest.mark.asyncio
    async def test_coingecko_fails_binance_succeeds(self):
        with patch("prices._fetch_coingecko", new_callable=AsyncMock, return_value=None) as cg, \
             patch("prices._fetch_binance", new_callable=AsyncMock, return_value=50100.0) as bn, \
             patch("prices._fetch_coinbase", new_callable=AsyncMock) as cb:
            price = await get_price_usd("BTC")
            assert price == 50100.0
            cg.assert_called_once()
            bn.assert_called_once_with("BTC")
            cb.assert_not_called()

    @pytest.mark.asyncio
    async def test_coingecko_and_binance_fail_coinbase_succeeds(self):
        with patch("prices._fetch_coingecko", new_callable=AsyncMock, return_value=None), \
             patch("prices._fetch_binance", new_callable=AsyncMock, return_value=None), \
             patch("prices._fetch_coinbase", new_callable=AsyncMock, return_value=49900.0):
            price = await get_price_usd("BTC")
            assert price == 49900.0

    @pytest.mark.asyncio
    async def test_all_sources_fail_no_cache(self):
        with patch("prices._fetch_coingecko", new_callable=AsyncMock, return_value=None), \
             patch("prices._fetch_binance", new_callable=AsyncMock, return_value=None), \
             patch("prices._fetch_coinbase", new_callable=AsyncMock, return_value=None):
            price = await get_price_usd("BTC")
            assert price is None

    @pytest.mark.asyncio
    async def test_unknown_symbol_returns_none(self):
        price = await get_price_usd("FAKECOIN")
        assert price is None

    @pytest.mark.asyncio
    async def test_all_supported_symbols(self):
        """Ensure all symbols we use have mappings."""
        for sym in ["BTC", "ETH", "TRX", "BNB"]:
            with patch("prices._fetch_coingecko", new_callable=AsyncMock, return_value=100.0):
                price = await get_price_usd(sym)
                assert price == 100.0
                _price_cache.clear()


# ══════════════════════════════════════════════════════════════
#  Bulk fetch
# ══════════════════════════════════════════════════════════════

class TestBulkFetch:
    @pytest.mark.asyncio
    async def test_bulk_mixed_stablecoins_and_crypto(self):
        _price_cache.clear()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"ethereum": {"usd": 3000.0}})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("prices.aiohttp.ClientSession", return_value=mock_session):
            result = await get_prices_bulk(["USDT", "ETH"])
            assert result["USDT"] == 1.0
            assert result["ETH"] == 3000.0

    @pytest.mark.asyncio
    async def test_bulk_uses_cache(self):
        _price_cache["BTC"] = (55000.0, time.time())
        result = await get_prices_bulk(["BTC", "USDT"])
        assert result["BTC"] == 55000.0
        assert result["USDT"] == 1.0

    @pytest.mark.asyncio
    async def test_bulk_empty_list(self):
        result = await get_prices_bulk([])
        assert result == {}


# ══════════════════════════════════════════════════════════════
#  Conversion helpers
# ══════════════════════════════════════════════════════════════

class TestConversions:
    @pytest.mark.asyncio
    async def test_convert_to_usd(self):
        with patch("prices.get_price_usd", new_callable=AsyncMock, return_value=50000.0):
            result = await convert_to_usd(0.5, "BTC")
            assert result == 25000.0

    @pytest.mark.asyncio
    async def test_convert_from_usd(self):
        with patch("prices.get_price_usd", new_callable=AsyncMock, return_value=50000.0):
            result = await convert_from_usd(25000.0, "BTC")
            assert result == 0.5

    @pytest.mark.asyncio
    async def test_convert_to_usd_no_price(self):
        with patch("prices.get_price_usd", new_callable=AsyncMock, return_value=None):
            result = await convert_to_usd(1.0, "FAKECOIN")
            assert result is None

    @pytest.mark.asyncio
    async def test_convert_from_usd_no_price(self):
        with patch("prices.get_price_usd", new_callable=AsyncMock, return_value=None):
            result = await convert_from_usd(100.0, "FAKECOIN")
            assert result is None

    @pytest.mark.asyncio
    async def test_convert_from_usd_zero_price(self):
        with patch("prices.get_price_usd", new_callable=AsyncMock, return_value=0):
            result = await convert_from_usd(100.0, "BTC")
            assert result is None

    @pytest.mark.asyncio
    async def test_convert_stablecoin(self):
        result = await convert_to_usd(100.0, "USDT")
        assert result == 100.0

    @pytest.mark.asyncio
    async def test_convert_precision(self):
        """Ensure no floating-point drift on typical amounts."""
        with patch("prices.get_price_usd", new_callable=AsyncMock, return_value=0.10):
            result = await convert_from_usd(5.0, "TRX")
            assert abs(result - 50.0) < 0.001
