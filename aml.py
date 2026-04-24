"""
AML-проверка адресов через AMLBot API.
Работает в mock-режиме когда AMLBOT_API_KEY не задан.
"""
import hashlib
import logging
import time

import aiohttp

from config import AMLBOT_API_KEY, AMLBOT_BASE_URL

logger = logging.getLogger(__name__)

# In-memory cache: (chain, address) -> (result_dict, timestamp)
_aml_cache: dict[tuple[str, str], tuple[dict, float]] = {}
AML_CACHE_TTL = 3600  # 1 hour


def _mock_result(address: str, chain: str) -> dict:
    """Детерминированный фейковый результат на основе хеша адреса."""
    h = hashlib.sha256(f"{chain}:{address}".encode()).hexdigest()
    score_byte = int(h[:2], 16)  # 0–255
    risk_score = int(score_byte / 255 * 100)

    if risk_score <= 30:
        risk_level = "low"
    elif risk_score <= 70:
        risk_level = "medium"
    else:
        risk_level = "high"

    signals = []
    # Deterministic signal generation based on hash
    if int(h[2:4], 16) % 3 == 0:
        signals.append("exchange:Binance")
    if int(h[4:6], 16) % 5 == 0:
        signals.append("mixer:TornadoCash")
    if int(h[6:8], 16) % 4 == 0:
        signals.append("gambling:unknown")
    if int(h[8:10], 16) % 7 == 0:
        signals.append("darknet:unknown")
    if risk_score > 50 and not signals:
        signals.append("suspicious:high_risk_pattern")

    return {
        "risk_score": risk_score,
        "risk_level": risk_level,
        "signals": signals,
        "raw": {"mock": True, "hash_prefix": h[:16]},
        "cached": False,
        "mock": True,
    }


async def _fetch_amlbot(address: str, chain: str) -> dict:
    """
    Запрос к AMLBot API.
    TODO: уточнить формат запроса/ответа по реальной документации AMLBot.
    Текущий формат — плейсхолдер на основе типичной структуры API.
    """
    # Map chain names to AMLBot chain identifiers
    chain_map = {
        "BTC": "bitcoin",
        "ETH": "ethereum",
        "TRON": "tron",
        "BSC": "bsc",
    }
    aml_chain = chain_map.get(chain, chain.lower())

    timeout = aiohttp.ClientTimeout(total=15)
    headers = {
        "Authorization": f"Bearer {AMLBOT_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "address": address,
        "chain": aml_chain,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{AMLBOT_BASE_URL}/aml/check",
                json=payload,
                headers=headers,
                timeout=timeout,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Parse response — adjust field names per real AMLBot docs
                    risk_score = data.get("risk_score", data.get("riskscore", 0))
                    risk_score = int(risk_score) if risk_score is not None else 0

                    if risk_score <= 30:
                        risk_level = "low"
                    elif risk_score <= 70:
                        risk_level = "medium"
                    else:
                        risk_level = "high"

                    signals = data.get("signals", data.get("tags", []))
                    if isinstance(signals, dict):
                        signals = [f"{k}:{v}" for k, v in signals.items()]

                    return {
                        "risk_score": risk_score,
                        "risk_level": risk_level,
                        "signals": signals,
                        "raw": data,
                        "cached": False,
                        "mock": False,
                    }
                else:
                    body = await resp.text()
                    logger.error(f"AMLBot API {resp.status}: {body}")
                    return {"error": f"AMLBot API error: {resp.status}", "risk_score": None}
    except Exception as e:
        logger.error(f"AMLBot API request failed: {e}")
        return {"error": str(e), "risk_score": None}


async def check_address(address: str, chain: str) -> dict:
    """
    Проверяет адрес через AML. В mock-режиме при пустом AMLBOT_API_KEY
    возвращает детерминированный фейковый результат.

    Returns:
    {
        'risk_score': int (0-100) | None on error,
        'risk_level': 'low' | 'medium' | 'high',
        'signals': list[str],
        'raw': dict,
        'cached': bool,
        'mock': bool,
    }
    """
    cache_key = (chain, address)

    # Check cache
    if cache_key in _aml_cache:
        result, cached_at = _aml_cache[cache_key]
        if time.time() - cached_at < AML_CACHE_TTL:
            cached_result = dict(result)
            cached_result["cached"] = True
            return cached_result

    # Mock mode
    if not AMLBOT_API_KEY:
        result = _mock_result(address, chain)
        _aml_cache[cache_key] = (result, time.time())
        return result

    # Real API call
    result = await _fetch_amlbot(address, chain)

    # Only cache successful results
    if "error" not in result:
        _aml_cache[cache_key] = (result, time.time())

    return result
