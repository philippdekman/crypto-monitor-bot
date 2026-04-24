"""
Tests for dilisense.py — mock mode, caching, risk heuristic, error handling.
"""
import time
from unittest.mock import AsyncMock, patch

import pytest


class TestMockModeIndividual:
    @pytest.mark.asyncio
    async def test_clean_name_returns_clean(self):
        with patch("dilisense.DILISENSE_API_KEY", ""):
            from dilisense import check_individual, _cache
            _cache.clear()

            result = await check_individual("John Smith")
            assert result["clean"] is True
            assert result["total_hits"] == 0
            assert result["risk_level"] == "clean"
            assert result["hits"] == []
            assert result["source_types"] == []
            assert result["mock"] is True

    @pytest.mark.asyncio
    async def test_pep_name_returns_pep_hit(self):
        with patch("dilisense.DILISENSE_API_KEY", ""):
            from dilisense import check_individual, _cache
            _cache.clear()

            result = await check_individual("Boris Johnson")
            assert result["clean"] is False
            assert result["total_hits"] == 1
            assert result["risk_level"] == "medium"
            assert "PEP" in result["source_types"]
            assert result["mock"] is True
            assert len(result["hits"]) == 1
            assert result["hits"][0]["pep_type"] == "POLITICIAN"

    @pytest.mark.asyncio
    async def test_sanctioned_name_returns_high(self):
        with patch("dilisense.DILISENSE_API_KEY", ""):
            from dilisense import check_individual, _cache
            _cache.clear()

            result = await check_individual("Vladimir Putin")
            assert result["clean"] is False
            assert result["total_hits"] == 2
            assert result["risk_level"] == "high"
            assert "SANCTION" in result["source_types"]
            assert result["mock"] is True

    @pytest.mark.asyncio
    async def test_mock_has_raw_dict(self):
        with patch("dilisense.DILISENSE_API_KEY", ""):
            from dilisense import check_individual, _cache
            _cache.clear()

            result = await check_individual("Test Person")
            assert isinstance(result["raw"], dict)
            assert "found_records" in result["raw"]


class TestMockModeEntity:
    @pytest.mark.asyncio
    async def test_entity_check_returns_proper_shape(self):
        with patch("dilisense.DILISENSE_API_KEY", ""):
            from dilisense import check_entity, _cache
            _cache.clear()

            result = await check_entity("Test Company LLC")
            assert "clean" in result
            assert "total_hits" in result
            assert "hits" in result
            assert "source_types" in result
            assert "risk_level" in result
            assert "raw" in result
            assert "cached" in result
            assert "mock" in result
            assert result["mock"] is True

    @pytest.mark.asyncio
    async def test_entity_has_entity_type_in_hits(self):
        """Entity hits should have entity_type ENTITY when they have hits."""
        with patch("dilisense.DILISENSE_API_KEY", ""):
            from dilisense import check_entity, _cache
            _cache.clear()

            # Try multiple names to find one with hits
            for i in range(50):
                _cache.clear()
                result = await check_entity(f"TestCorp Entity {i}")
                if result["hits"]:
                    assert result["hits"][0]["entity_type"] == "ENTITY"
                    return

            # If all clean, that's fine — test the shape of a clean result
            assert result["clean"] is True


class TestCaching:
    @pytest.mark.asyncio
    async def test_cache_hit_within_ttl(self):
        with patch("dilisense.DILISENSE_API_KEY", ""):
            from dilisense import check_individual, _cache
            _cache.clear()

            result1 = await check_individual("Cache Test Name")
            assert result1["cached"] is False

            result2 = await check_individual("Cache Test Name")
            assert result2["cached"] is True
            assert result2["risk_level"] == result1["risk_level"]

    @pytest.mark.asyncio
    async def test_cache_miss_after_expiry(self):
        with patch("dilisense.DILISENSE_API_KEY", ""):
            from dilisense import check_individual, _cache, CACHE_TTL
            _cache.clear()

            result1 = await check_individual("Expire Test Name")
            assert result1["cached"] is False

            # Manually expire cache
            key = ("expire test name", "individual")
            if key in _cache:
                data, _ = _cache[key]
                _cache[key] = (data, time.time() - CACHE_TTL - 1)

            result2 = await check_individual("Expire Test Name")
            assert result2["cached"] is False


class TestRiskLevelHeuristic:
    def test_sanction_is_high(self):
        from dilisense import _compute_risk_level
        records = [{"source_type": "SANCTION", "pep_type": ""}]
        assert _compute_risk_level(records) == "high"

    def test_criminal_is_high(self):
        from dilisense import _compute_risk_level
        records = [{"source_type": "CRIMINAL", "pep_type": ""}]
        assert _compute_risk_level(records) == "high"

    def test_pep_politician_is_medium(self):
        from dilisense import _compute_risk_level
        records = [{"source_type": "PEP", "pep_type": "POLITICIAN"}]
        assert _compute_risk_level(records) == "medium"

    def test_pep_family_is_medium(self):
        from dilisense import _compute_risk_level
        records = [{"source_type": "PEP", "pep_type": "FAMILY"}]
        assert _compute_risk_level(records) == "medium"

    def test_retired_pep_is_low(self):
        from dilisense import _compute_risk_level
        records = [{"source_type": "PEP", "pep_type": "RETIRED"}]
        assert _compute_risk_level(records) == "low"

    def test_associate_pep_is_low(self):
        from dilisense import _compute_risk_level
        records = [{"source_type": "PEP", "pep_type": "ASSOCIATE"}]
        assert _compute_risk_level(records) == "low"

    def test_empty_records_is_clean(self):
        from dilisense import _compute_risk_level
        assert _compute_risk_level([]) == "clean"


class TestDobParam:
    @pytest.mark.asyncio
    async def test_dob_passed_to_api(self):
        """check_individual with DOB passes dob param correctly."""
        with patch("dilisense.DILISENSE_API_KEY", "test_key"):
            from dilisense import check_individual, _cache
            _cache.clear()

            mock_response = {
                "timestamp": "2024-09-24T19:16:00Z",
                "total_hits": 0,
                "found_records": [],
            }

            with patch("dilisense._fetch_individual", new_callable=AsyncMock,
                       return_value={
                           "clean": True, "total_hits": 0, "hits": [],
                           "source_types": [], "risk_level": "clean",
                           "raw": mock_response, "cached": False, "mock": False,
                       }) as mock_fetch:
                result = await check_individual("Test Name", dob="19/06/1964", gender="male")
                mock_fetch.assert_called_once_with("Test Name", "19/06/1964", "male")
                assert result["clean"] is True


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_api_error_returns_error_dict(self):
        """When API fails, should return error dict, not raise."""
        with patch("dilisense.DILISENSE_API_KEY", "real_key"):
            from dilisense import check_individual, _cache
            _cache.clear()

            with patch("dilisense._fetch_individual", new_callable=AsyncMock,
                       return_value={"error": "Connection timeout", "total_hits": None}):
                result = await check_individual("Error Test")
                assert "error" in result
                assert result["total_hits"] is None

    @pytest.mark.asyncio
    async def test_error_not_cached(self):
        """Error responses should NOT be cached."""
        with patch("dilisense.DILISENSE_API_KEY", "real_key"):
            from dilisense import check_individual, _cache
            _cache.clear()

            with patch("dilisense._fetch_individual", new_callable=AsyncMock,
                       return_value={"error": "Server error", "total_hits": None}):
                await check_individual("No Cache Error")

            assert ("no cache error", "individual") not in _cache
