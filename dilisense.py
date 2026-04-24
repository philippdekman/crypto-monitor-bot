"""
Dilisense API — проверка физических лиц и организаций по санкциям, PEP и уголовным спискам.
Работает в mock-режиме когда DILISENSE_API_KEY не задан.
"""
import hashlib
import logging
import time

import aiohttp

from config import DILISENSE_API_KEY, DILISENSE_BASE_URL

logger = logging.getLogger(__name__)

# In-memory cache: (name_lower, check_type) -> (result_dict, timestamp)
_cache: dict[tuple[str, str], tuple[dict, float]] = {}
CACHE_TTL = 3600  # 1 hour


def _compute_risk_level(found_records: list[dict]) -> str:
    """Determine risk level from found records."""
    if not found_records:
        return "clean"

    for rec in found_records:
        src = rec.get("source_type", "").upper()
        if src in ("SANCTION", "CRIMINAL"):
            return "high"

    # Check PEP types
    for rec in found_records:
        src = rec.get("source_type", "").upper()
        if src == "PEP":
            pep_type = rec.get("pep_type", "").upper()
            if pep_type in ("POLITICIAN", "FAMILY"):
                return "medium"

    return "low"


def _extract_source_types(found_records: list[dict]) -> list[str]:
    """Extract unique source types."""
    types = set()
    for rec in found_records:
        st = rec.get("source_type", "")
        if st:
            types.add(st.upper())
    return sorted(types)


def _simplify_hit(rec: dict) -> dict:
    """Simplify a found_record into a concise hit dict."""
    return {
        "id": rec.get("id", ""),
        "entity_type": rec.get("entity_type", ""),
        "name": rec.get("name", ""),
        "alias_names": rec.get("alias_names", []),
        "date_of_birth": rec.get("date_of_birth", []),
        "citizenship": rec.get("citizenship", []),
        "source_id": rec.get("source_id", ""),
        "source_type": rec.get("source_type", ""),
        "pep_type": rec.get("pep_type", ""),
        "positions": rec.get("positions", []),
        "description": rec.get("description", []),
    }


def _mock_result_individual(name: str) -> dict:
    """Deterministic mock result for individual name checks."""
    name_lower = name.strip().lower()

    # Known mock names for demo
    if name_lower == "john smith":
        return {
            "clean": True,
            "total_hits": 0,
            "hits": [],
            "source_types": [],
            "risk_level": "clean",
            "raw": {"timestamp": "2024-09-24T19:16:00Z", "total_hits": 0, "found_records": []},
            "cached": False,
            "mock": True,
        }

    if name_lower == "boris johnson":
        raw = {
            "timestamp": "2024-09-24T19:16:00Z",
            "total_hits": 1,
            "found_records": [
                {
                    "id": "c438b18a93cd3c13",
                    "entity_type": "INDIVIDUAL",
                    "name": "Boris Johnson",
                    "alias_names": ["BoJo", "Alexander Boris de Pfeffel Johnson"],
                    "date_of_birth": ["19/06/1964"],
                    "citizenship": ["GB", "US"],
                    "source_id": "dilisense_pep",
                    "source_type": "PEP",
                    "pep_type": "POLITICIAN",
                    "positions": ["UK Prime Minister (2019-2022)", "MP for Uxbridge"],
                    "description": ["Former Prime Minister of the United Kingdom"],
                }
            ],
        }
        hits = [_simplify_hit(r) for r in raw["found_records"]]
        return {
            "clean": False,
            "total_hits": 1,
            "hits": hits,
            "source_types": ["PEP"],
            "risk_level": "medium",
            "raw": raw,
            "cached": False,
            "mock": True,
        }

    if name_lower == "vladimir putin":
        raw = {
            "timestamp": "2024-09-24T19:16:00Z",
            "total_hits": 2,
            "found_records": [
                {
                    "id": "a1b2c3d4e5f60001",
                    "entity_type": "INDIVIDUAL",
                    "name": "Vladimir Putin",
                    "alias_names": ["Vladimir Vladimirovich Putin"],
                    "date_of_birth": ["07/10/1952"],
                    "citizenship": ["RU"],
                    "source_id": "ofac_sdn",
                    "source_type": "SANCTION",
                    "pep_type": "",
                    "positions": [],
                    "description": ["President of the Russian Federation"],
                },
                {
                    "id": "a1b2c3d4e5f60002",
                    "entity_type": "INDIVIDUAL",
                    "name": "Vladimir Putin",
                    "alias_names": [],
                    "date_of_birth": ["07/10/1952"],
                    "citizenship": ["RU"],
                    "source_id": "dilisense_pep",
                    "source_type": "PEP",
                    "pep_type": "POLITICIAN",
                    "positions": ["President of Russia"],
                    "description": [],
                },
            ],
        }
        hits = [_simplify_hit(r) for r in raw["found_records"]]
        return {
            "clean": False,
            "total_hits": 2,
            "hits": hits,
            "source_types": ["PEP", "SANCTION"],
            "risk_level": "high",
            "raw": raw,
            "cached": False,
            "mock": True,
        }

    # Generic deterministic result based on name hash
    h = hashlib.sha256(f"individual:{name_lower}".encode()).hexdigest()
    score_byte = int(h[:2], 16)

    if score_byte < 180:
        # ~70% clean
        return {
            "clean": True,
            "total_hits": 0,
            "hits": [],
            "source_types": [],
            "risk_level": "clean",
            "raw": {"timestamp": "2024-09-24T19:16:00Z", "total_hits": 0, "found_records": []},
            "cached": False,
            "mock": True,
        }

    # Generate a hit
    if score_byte >= 240:
        source_type = "SANCTION"
        source_id = "ofac_sdn"
        pep_type = ""
    elif score_byte >= 220:
        source_type = "CRIMINAL"
        source_id = "interpol_red"
        pep_type = ""
    elif score_byte >= 200:
        source_type = "PEP"
        source_id = "dilisense_pep"
        pep_type = "POLITICIAN"
    else:
        source_type = "PEP"
        source_id = "dilisense_pep"
        pep_type = "ASSOCIATE"

    rec = {
        "id": h[:16],
        "entity_type": "INDIVIDUAL",
        "name": name,
        "alias_names": [],
        "date_of_birth": [],
        "citizenship": [],
        "source_id": source_id,
        "source_type": source_type,
        "pep_type": pep_type,
        "positions": [],
        "description": [f"Mock hit for {name}"],
    }
    raw = {
        "timestamp": "2024-09-24T19:16:00Z",
        "total_hits": 1,
        "found_records": [rec],
    }
    hits = [_simplify_hit(rec)]
    risk = _compute_risk_level(raw["found_records"])
    return {
        "clean": False,
        "total_hits": 1,
        "hits": hits,
        "source_types": _extract_source_types(raw["found_records"]),
        "risk_level": risk,
        "raw": raw,
        "cached": False,
        "mock": True,
    }


def _mock_result_entity(name: str) -> dict:
    """Deterministic mock result for entity name checks."""
    name_lower = name.strip().lower()
    h = hashlib.sha256(f"entity:{name_lower}".encode()).hexdigest()
    score_byte = int(h[:2], 16)

    if score_byte < 180:
        return {
            "clean": True,
            "total_hits": 0,
            "hits": [],
            "source_types": [],
            "risk_level": "clean",
            "raw": {"timestamp": "2024-09-24T19:16:00Z", "total_hits": 0, "found_records": []},
            "cached": False,
            "mock": True,
        }

    source_type = "SANCTION" if score_byte >= 220 else "PEP"
    source_id = "ofac_sdn" if source_type == "SANCTION" else "dilisense_pep"

    rec = {
        "id": h[:16],
        "entity_type": "ENTITY",
        "name": name,
        "alias_names": [],
        "date_of_birth": [],
        "citizenship": [],
        "source_id": source_id,
        "source_type": source_type,
        "pep_type": "",
        "positions": [],
        "description": [f"Mock entity hit for {name}"],
    }
    raw = {
        "timestamp": "2024-09-24T19:16:00Z",
        "total_hits": 1,
        "found_records": [rec],
    }
    hits = [_simplify_hit(rec)]
    risk = _compute_risk_level(raw["found_records"])
    return {
        "clean": False,
        "total_hits": 1,
        "hits": hits,
        "source_types": _extract_source_types(raw["found_records"]),
        "risk_level": risk,
        "raw": raw,
        "cached": False,
        "mock": True,
    }


async def _fetch_individual(name: str, dob: str | None = None, gender: str | None = None) -> dict:
    """Call Dilisense checkIndividual API."""
    params = {"names": name}
    if dob:
        params["dob"] = dob
    if gender:
        params["gender"] = gender

    timeout = aiohttp.ClientTimeout(total=15)
    headers = {"x-api-key": DILISENSE_API_KEY}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{DILISENSE_BASE_URL}/checkIndividual",
                params=params,
                headers=headers,
                timeout=timeout,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    found = data.get("found_records", [])
                    hits = [_simplify_hit(r) for r in found]
                    total = data.get("total_hits", len(found))
                    return {
                        "clean": total == 0,
                        "total_hits": total,
                        "hits": hits,
                        "source_types": _extract_source_types(found),
                        "risk_level": _compute_risk_level(found),
                        "raw": data,
                        "cached": False,
                        "mock": False,
                    }
                else:
                    body = await resp.text()
                    logger.error(f"Dilisense checkIndividual {resp.status}: {body}")
                    return {"error": f"Dilisense API error: {resp.status}", "total_hits": None}
    except Exception as e:
        logger.error(f"Dilisense checkIndividual request failed: {e}")
        return {"error": str(e), "total_hits": None}


async def _fetch_entity(name: str) -> dict:
    """Call Dilisense checkEntity API."""
    params = {"names": name}
    timeout = aiohttp.ClientTimeout(total=15)
    headers = {"x-api-key": DILISENSE_API_KEY}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{DILISENSE_BASE_URL}/checkEntity",
                params=params,
                headers=headers,
                timeout=timeout,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    found = data.get("found_records", [])
                    hits = [_simplify_hit(r) for r in found]
                    total = data.get("total_hits", len(found))
                    return {
                        "clean": total == 0,
                        "total_hits": total,
                        "hits": hits,
                        "source_types": _extract_source_types(found),
                        "risk_level": _compute_risk_level(found),
                        "raw": data,
                        "cached": False,
                        "mock": False,
                    }
                else:
                    body = await resp.text()
                    logger.error(f"Dilisense checkEntity {resp.status}: {body}")
                    return {"error": f"Dilisense API error: {resp.status}", "total_hits": None}
    except Exception as e:
        logger.error(f"Dilisense checkEntity request failed: {e}")
        return {"error": str(e), "total_hits": None}


async def check_individual(name: str, dob: str | None = None, gender: str | None = None) -> dict:
    """
    Check an individual name against sanctions, PEP, and criminal lists.

    Returns:
    {
        'clean': bool,          # True if total_hits == 0
        'total_hits': int,
        'hits': list[dict],     # simplified hit records
        'source_types': list[str],  # unique: ['SANCTION', 'PEP', 'CRIMINAL']
        'risk_level': 'clean' | 'low' | 'medium' | 'high',
        'raw': dict,            # full API response
        'cached': bool,
        'mock': bool,
    }
    """
    cache_key = (name.strip().lower(), "individual")

    # Check cache
    if cache_key in _cache:
        result, cached_at = _cache[cache_key]
        if time.time() - cached_at < CACHE_TTL:
            cached_result = dict(result)
            cached_result["cached"] = True
            return cached_result

    # Mock mode
    if not DILISENSE_API_KEY:
        result = _mock_result_individual(name)
        _cache[cache_key] = (result, time.time())
        return result

    # Real API call
    result = await _fetch_individual(name, dob, gender)

    # Only cache successful results
    if "error" not in result:
        _cache[cache_key] = (result, time.time())

    return result


async def check_entity(name: str) -> dict:
    """
    Check a company/organization name against sanctions and other lists.
    Same return shape as check_individual.
    """
    cache_key = (name.strip().lower(), "entity")

    # Check cache
    if cache_key in _cache:
        result, cached_at = _cache[cache_key]
        if time.time() - cached_at < CACHE_TTL:
            cached_result = dict(result)
            cached_result["cached"] = True
            return cached_result

    # Mock mode
    if not DILISENSE_API_KEY:
        result = _mock_result_entity(name)
        _cache[cache_key] = (result, time.time())
        return result

    # Real API call
    result = await _fetch_entity(name)

    # Only cache successful results
    if "error" not in result:
        _cache[cache_key] = (result, time.time())

    return result
