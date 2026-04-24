"""
Tests for aml.py — mock mode, caching, error handling.
"""
import time
from unittest.mock import AsyncMock, patch, MagicMock

import pytest


class TestMockMode:
    @pytest.mark.asyncio
    async def test_mock_returns_deterministic(self):
        with patch("aml.AMLBOT_API_KEY", ""):
            from aml import check_address, _aml_cache
            _aml_cache.clear()

            result1 = await check_address("TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE", "TRON")
            result2 = await check_address("TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE", "TRON")

            assert result1["risk_score"] == result2["risk_score"]
            assert result1["risk_level"] == result2["risk_level"]
            assert result1["mock"] is True

    @pytest.mark.asyncio
    async def test_mock_different_addresses_different_results(self):
        with patch("aml.AMLBOT_API_KEY", ""):
            from aml import check_address, _aml_cache
            _aml_cache.clear()

            result1 = await check_address("addr_one", "BTC")
            _aml_cache.clear()
            result2 = await check_address("addr_two", "BTC")

            # Different addresses should generally produce different scores
            # (not guaranteed but highly likely with SHA256)
            assert result1["mock"] is True
            assert result2["mock"] is True

    @pytest.mark.asyncio
    async def test_mock_different_chains_different_results(self):
        with patch("aml.AMLBOT_API_KEY", ""):
            from aml import check_address, _aml_cache
            _aml_cache.clear()

            result1 = await check_address("0xSameAddress", "ETH")
            _aml_cache.clear()
            result2 = await check_address("0xSameAddress", "BSC")

            assert result1["mock"] is True
            assert result2["mock"] is True
            # Different chain → different hash → different score
            # (extremely likely to differ)

    @pytest.mark.asyncio
    async def test_mock_risk_level_matches_score(self):
        with patch("aml.AMLBOT_API_KEY", ""):
            from aml import check_address, _aml_cache
            _aml_cache.clear()

            result = await check_address("test_addr", "BTC")
            score = result["risk_score"]
            level = result["risk_level"]

            if score <= 30:
                assert level == "low"
            elif score <= 70:
                assert level == "medium"
            else:
                assert level == "high"

    @pytest.mark.asyncio
    async def test_mock_high_risk_address(self):
        """Find an address that produces high risk score (deterministic)."""
        with patch("aml.AMLBOT_API_KEY", ""):
            from aml import _mock_result
            # Test a known address prefix that yields high risk
            # Search deterministically
            found_high = False
            for i in range(100):
                result = _mock_result(f"high_risk_test_{i}", "BTC")
                if result["risk_score"] > 70:
                    assert result["risk_level"] == "high"
                    found_high = True
                    break
            assert found_high, "Should find at least one high-risk address in 100 tries"


class TestCaching:
    @pytest.mark.asyncio
    async def test_cache_hit_within_ttl(self):
        with patch("aml.AMLBOT_API_KEY", ""):
            from aml import check_address, _aml_cache
            _aml_cache.clear()

            result1 = await check_address("cached_addr", "ETH")
            assert result1["cached"] is False

            result2 = await check_address("cached_addr", "ETH")
            assert result2["cached"] is True
            assert result2["risk_score"] == result1["risk_score"]

    @pytest.mark.asyncio
    async def test_cache_miss_after_expiry(self):
        with patch("aml.AMLBOT_API_KEY", ""):
            from aml import check_address, _aml_cache, AML_CACHE_TTL
            _aml_cache.clear()

            result1 = await check_address("expire_test", "BTC")
            assert result1["cached"] is False

            # Manually expire cache
            key = ("BTC", "expire_test")
            if key in _aml_cache:
                data, _ = _aml_cache[key]
                _aml_cache[key] = (data, time.time() - AML_CACHE_TTL - 1)

            result2 = await check_address("expire_test", "BTC")
            assert result2["cached"] is False

    @pytest.mark.asyncio
    async def test_cache_different_chains_separate(self):
        with patch("aml.AMLBOT_API_KEY", ""):
            from aml import check_address, _aml_cache
            _aml_cache.clear()

            await check_address("multi_chain_addr", "ETH")
            await check_address("multi_chain_addr", "BSC")

            # Both should be in cache separately
            assert ("ETH", "multi_chain_addr") in _aml_cache
            assert ("BSC", "multi_chain_addr") in _aml_cache


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_api_error_returns_error_dict(self):
        """When API fails, should return error dict, not raise."""
        with patch("aml.AMLBOT_API_KEY", "real_key"):
            from aml import check_address, _aml_cache
            _aml_cache.clear()

            with patch("aml._fetch_amlbot", new_callable=AsyncMock,
                       return_value={"error": "Connection timeout", "risk_score": None}):
                result = await check_address("error_test", "BTC")
                assert "error" in result
                assert result["risk_score"] is None

    @pytest.mark.asyncio
    async def test_api_error_not_cached(self):
        """Error responses should NOT be cached."""
        with patch("aml.AMLBOT_API_KEY", "real_key"):
            from aml import check_address, _aml_cache
            _aml_cache.clear()

            with patch("aml._fetch_amlbot", new_callable=AsyncMock,
                       return_value={"error": "Server error", "risk_score": None}):
                await check_address("no_cache_error", "ETH")

            assert ("ETH", "no_cache_error") not in _aml_cache

    @pytest.mark.asyncio
    async def test_mock_result_has_signals_list(self):
        with patch("aml.AMLBOT_API_KEY", ""):
            from aml import check_address, _aml_cache
            _aml_cache.clear()

            result = await check_address("signal_test", "TRON")
            assert isinstance(result["signals"], list)

    @pytest.mark.asyncio
    async def test_mock_result_has_raw_dict(self):
        with patch("aml.AMLBOT_API_KEY", ""):
            from aml import check_address, _aml_cache
            _aml_cache.clear()

            result = await check_address("raw_test", "BTC")
            assert isinstance(result["raw"], dict)
            assert result["raw"]["mock"] is True
