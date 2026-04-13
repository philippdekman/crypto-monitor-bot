"""
Blockchain interaction layer.
Supports: BTC (Blockstream), ETH (Blockscout + Etherscan fallback), TRON (TronGrid), BSC (Etherscan V2 if key available).

Each chain provides:
  - detect(address) -> bool
  - get_balance(address) -> dict
  - get_transactions(address, token_contract=None) -> list[dict]
  - get_token_list(address) -> list[dict]

API Strategy (no registration required):
  BTC  → Blockstream (no key)
  ETH  → Blockscout Etherscan-compatible API (no key), Etherscan V2 fallback
  BSC  → Etherscan V2 (key required), PublicNode RPC for balance
  TRON → TronGrid (works without key, key optional for higher limits)
"""
import re
import asyncio
import logging
import aiohttp
from config import ETHERSCAN_API_KEY, BSCTRACE_API_KEY, TRONGRID_API_KEY

logger = logging.getLogger(__name__)

# ── Address detection ─────────────────────────────────────────

BTC_REGEX = re.compile(r"^(1[1-9A-HJ-NP-Za-km-z]{25,34}|3[1-9A-HJ-NP-Za-km-z]{25,34}|bc1[a-z0-9]{39,59})$")
ETH_REGEX = re.compile(r"^0x[0-9a-fA-F]{40}$")
TRON_REGEX = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")


def detect_chains(address: str) -> list[str]:
    address = address.strip()
    chains = []
    if BTC_REGEX.match(address):
        chains.append("BTC")
    if ETH_REGEX.match(address):
        chains.extend(["ETH", "BSC"])
    if TRON_REGEX.match(address):
        chains.append("TRON")
    return chains


# ── HTTP helper ───────────────────────────────────────────────

async def _http_get(url: str, params: dict = None, headers: dict = None) -> dict | list | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning(f"HTTP {resp.status} from {url}")
    except Exception as e:
        logger.error(f"HTTP error {url}: {e}")
    return None


async def _http_post(url: str, json_data: dict = None, headers: dict = None) -> dict | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=json_data, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning(f"HTTP POST {resp.status} from {url}")
    except Exception as e:
        logger.error(f"HTTP POST error {url}: {e}")
    return None


# ══════════════════════════════════════════════════════════════
#  BITCOIN  (Blockstream API — no key needed)
# ══════════════════════════════════════════════════════════════

BLOCKSTREAM_URL = "https://blockstream.info/api"


async def btc_get_balance(address: str, token_contract: str = None, token_symbol: str = None) -> dict:
    data = await _http_get(f"{BLOCKSTREAM_URL}/address/{address}")
    if not data:
        return {"balance": "0", "balance_sat": 0, "symbol": "BTC"}
    funded = data.get("chain_stats", {}).get("funded_txo_sum", 0)
    spent = data.get("chain_stats", {}).get("spent_txo_sum", 0)
    balance_sat = funded - spent
    return {
        "balance": f"{balance_sat / 1e8:.8f}",
        "balance_sat": balance_sat,
        "symbol": "BTC",
    }


async def btc_get_transactions(address: str, last_seen_txid: str = None, **kw) -> list[dict]:
    data = await _http_get(f"{BLOCKSTREAM_URL}/address/{address}/txs")
    if not data:
        return []
    txs = []
    for tx in data[:50]:
        txid = tx.get("txid", "")
        if last_seen_txid and txid == last_seen_txid:
            break
        is_input = any(
            vin.get("prevout", {}).get("scriptpubkey_address") == address
            for vin in tx.get("vin", [])
        )
        is_output = any(
            vout.get("scriptpubkey_address") == address
            for vout in tx.get("vout", [])
        )
        if is_output and not is_input:
            direction = "in"
            value_sat = sum(
                vout.get("value", 0) for vout in tx.get("vout", [])
                if vout.get("scriptpubkey_address") == address
            )
        elif is_input:
            direction = "out"
            value_sat = sum(
                vin.get("prevout", {}).get("value", 0) for vin in tx.get("vin", [])
                if vin.get("prevout", {}).get("scriptpubkey_address") == address
            )
        else:
            continue
        txs.append({
            "tx_hash": txid,
            "direction": direction,
            "value": f"{value_sat / 1e8:.8f}",
            "value_raw": value_sat,
            "symbol": "BTC",
            "from_addr": tx.get("vin", [{}])[0].get("prevout", {}).get("scriptpubkey_address", ""),
            "to_addr": tx.get("vout", [{}])[0].get("scriptpubkey_address", "") if direction == "out" else address,
            "block_number": tx.get("status", {}).get("block_height"),
            "timestamp": tx.get("status", {}).get("block_time"),
        })
    return txs


# ══════════════════════════════════════════════════════════════
#  ETHEREUM  (Blockscout Etherscan-compatible API — no key!)
# ══════════════════════════════════════════════════════════════

BLOCKSCOUT_ETH_URL = "https://eth.blockscout.com/api"
ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"


async def _blockscout_call(base_url: str, params: dict) -> dict | list | None:
    """Call Blockscout Etherscan-compatible API (no key needed)."""
    data = await _http_get(base_url, params=params)
    if data and data.get("status") == "1":
        return data.get("result")
    if data:
        logger.warning(f"Blockscout error: {data.get('message')} — {data.get('result')}")
    return None


async def _etherscan_v2_call(params: dict, chain_id: int = 1) -> dict | list | None:
    """Call Etherscan V2 API (requires key)."""
    if not ETHERSCAN_API_KEY:
        return None
    params["apikey"] = ETHERSCAN_API_KEY
    params["chainid"] = chain_id
    data = await _http_get(ETHERSCAN_V2_URL, params=params)
    if data and data.get("status") == "1":
        return data.get("result")
    if data:
        logger.warning(f"Etherscan V2 error: {data.get('message')} — {data.get('result')}")
    return None


async def eth_get_balance(address: str, token_contract: str = None, token_symbol: str = None) -> dict:
    # If a specific ERC-20 token, get balance via Blockscout v2
    if token_contract:
        data = await _http_get(
            f"https://eth.blockscout.com/api/v2/addresses/{address}/tokens",
            params={"type": "ERC-20"}
        )
        if data and data.get("items"):
            for item in data["items"]:
                tok = item.get("token", {})
                if tok.get("address", "").lower() == token_contract.lower():
                    raw = item.get("value", "0")
                    decimals = int(tok.get("decimals", "18") or "18")
                    balance = int(raw) / (10 ** decimals)
                    return {"balance": f"{balance:.6f}", "symbol": token_symbol or tok.get("symbol", "TOKEN")}
        return {"balance": "0", "symbol": token_symbol or "TOKEN"}

    # Native ETH balance — primary: Blockscout
    result = await _blockscout_call(BLOCKSCOUT_ETH_URL, {
        "module": "account", "action": "balance", "address": address, "tag": "latest"
    })
    if result:
        balance_wei = int(result)
        return {"balance": f"{balance_wei / 1e18:.8f}", "balance_wei": balance_wei, "symbol": "ETH"}

    # Fallback: Etherscan V2
    result = await _etherscan_v2_call({
        "module": "account", "action": "balance", "address": address, "tag": "latest"
    }, chain_id=1)
    if result:
        balance_wei = int(result)
        return {"balance": f"{balance_wei / 1e18:.8f}", "balance_wei": balance_wei, "symbol": "ETH"}

    return {"balance": "0", "balance_wei": 0, "symbol": "ETH"}


async def eth_get_token_balances(address: str) -> list[dict]:
    """Get ERC-20 tokens held by address. Uses Blockscout v2 API (no key)."""
    # Primary: Blockscout v2 — returns actual token balances
    data = await _http_get(
        f"https://eth.blockscout.com/api/v2/addresses/{address}/tokens",
        params={"type": "ERC-20"}
    )
    if data and data.get("items"):
        tokens = []
        for item in data["items"][:20]:
            tok = item.get("token", {})
            contract = tok.get("address", "")
            symbol = tok.get("symbol", "TOKEN")
            decimals = int(tok.get("decimals", "18") or "18")
            if contract:
                tokens.append({"contract": contract, "symbol": symbol, "decimals": decimals})
        return tokens

    # Fallback: Etherscan-compatible API (token tx history)
    result = await _blockscout_call(BLOCKSCOUT_ETH_URL, {
        "module": "account", "action": "tokentx",
        "address": address, "page": "1", "offset": "100", "sort": "desc"
    })
    if not result:
        result = await _etherscan_v2_call({
            "module": "account", "action": "tokentx",
            "address": address, "page": 1, "offset": 100, "sort": "desc"
        }, chain_id=1)
    if not result or not isinstance(result, list):
        return []
    tokens_map = {}
    for tx in result:
        contract = tx.get("contractAddress", "")
        symbol = tx.get("tokenSymbol", "")
        decimals = int(tx.get("tokenDecimal", 18))
        if contract and contract not in tokens_map:
            tokens_map[contract] = {"contract": contract, "symbol": symbol, "decimals": decimals}
    return list(tokens_map.values())


async def _evm_parse_txlist(result: list, address: str, token_contract: str = None,
                            last_seen_txid: str = None, native_symbol: str = "ETH") -> list[dict]:
    """Parse Etherscan/Blockscout transaction list into normalized format."""
    txs = []
    for tx in result:
        tx_hash = tx.get("hash", "")
        if last_seen_txid and tx_hash == last_seen_txid:
            break
        from_addr = tx.get("from", "").lower()
        to_addr = tx.get("to", "").lower()
        addr_lower = address.lower()
        if to_addr == addr_lower:
            direction = "in"
        elif from_addr == addr_lower:
            direction = "out"
        else:
            continue
        if token_contract:
            decimals = int(tx.get("tokenDecimal", 18))
            value_raw = int(tx.get("value", 0))
            value = value_raw / (10 ** decimals)
            symbol = tx.get("tokenSymbol", "TOKEN")
        else:
            value_raw = int(tx.get("value", 0))
            value = value_raw / 1e18
            symbol = native_symbol
        txs.append({
            "tx_hash": tx_hash,
            "direction": direction,
            "value": f"{value:.8f}",
            "value_raw": value_raw,
            "symbol": symbol,
            "from_addr": from_addr,
            "to_addr": to_addr,
            "block_number": int(tx.get("blockNumber", 0)),
            "timestamp": int(tx.get("timeStamp", 0)),
        })
    return txs


async def eth_get_transactions(address: str, token_contract: str = None,
                               last_seen_txid: str = None) -> list[dict]:
    if token_contract:
        params = {
            "module": "account", "action": "tokentx",
            "contractaddress": token_contract,
            "address": address, "page": "1", "offset": "50", "sort": "desc"
        }
    else:
        params = {
            "module": "account", "action": "txlist",
            "address": address, "page": "1", "offset": "50", "sort": "desc"
        }

    # Primary: Blockscout
    result = await _blockscout_call(BLOCKSCOUT_ETH_URL, params)

    # Fallback: Etherscan V2
    if not result:
        params_v2 = {k: v for k, v in params.items()}
        result = await _etherscan_v2_call(params_v2, chain_id=1)

    if not result or not isinstance(result, list):
        return []

    return await _evm_parse_txlist(result, address, token_contract, last_seen_txid, "ETH")


# ══════════════════════════════════════════════════════════════
#  BSC  (Etherscan V2 with key, or PublicNode RPC for balance)
# ══════════════════════════════════════════════════════════════

PUBLICNODE_BSC_URL = "https://bsc-rpc.publicnode.com"
BSC_CHAIN_ID = 56


async def bsc_get_balance(address: str, token_contract: str = None, token_symbol: str = None) -> dict:
    # BEP-20 token balance via Etherscan V2 (if key available)
    if token_contract and ETHERSCAN_API_KEY:
        result = await _etherscan_v2_call({
            "module": "account", "action": "tokenbalance",
            "contractaddress": token_contract,
            "address": address, "tag": "latest"
        }, chain_id=BSC_CHAIN_ID)
        if result:
            raw = int(result)
            balance = raw / 1e18  # default, may vary
            return {"balance": f"{balance:.6f}", "symbol": token_symbol or "TOKEN"}
        return {"balance": "0", "symbol": token_symbol or "TOKEN"}
    elif token_contract:
        return {"balance": "0", "symbol": token_symbol or "TOKEN"}

    # Native BNB — primary: Etherscan V2
    result = await _etherscan_v2_call({
        "module": "account", "action": "balance", "address": address, "tag": "latest"
    }, chain_id=BSC_CHAIN_ID)
    if result:
        balance_wei = int(result)
        return {"balance": f"{balance_wei / 1e18:.8f}", "balance_wei": balance_wei, "symbol": "BNB"}

    # Fallback: PublicNode JSON-RPC
    rpc_result = await _http_post(PUBLICNODE_BSC_URL, json_data={
        "jsonrpc": "2.0",
        "method": "eth_getBalance",
        "params": [address, "latest"],
        "id": 1,
    })
    if rpc_result and rpc_result.get("result"):
        balance_wei = int(rpc_result["result"], 16)
        return {"balance": f"{balance_wei / 1e18:.8f}", "balance_wei": balance_wei, "symbol": "BNB"}

    return {"balance": "0", "balance_wei": 0, "symbol": "BNB"}


async def bsc_get_token_balances(address: str) -> list[dict]:
    result = await _etherscan_v2_call({
        "module": "account", "action": "tokentx",
        "address": address, "page": 1, "offset": 100, "sort": "desc"
    }, chain_id=BSC_CHAIN_ID)
    if not result or not isinstance(result, list):
        return []
    tokens = {}
    for tx in result:
        contract = tx.get("contractAddress", "")
        symbol = tx.get("tokenSymbol", "")
        decimals = int(tx.get("tokenDecimal", 18))
        if contract and contract not in tokens:
            tokens[contract] = {"contract": contract, "symbol": symbol, "decimals": decimals}
    return list(tokens.values())


async def bsc_get_transactions(address: str, token_contract: str = None,
                               last_seen_txid: str = None) -> list[dict]:
    if token_contract:
        params = {
            "module": "account", "action": "tokentx",
            "contractaddress": token_contract,
            "address": address, "page": 1, "offset": 50, "sort": "desc"
        }
    else:
        params = {
            "module": "account", "action": "txlist",
            "address": address, "page": 1, "offset": 50, "sort": "desc"
        }

    result = await _etherscan_v2_call(params, chain_id=BSC_CHAIN_ID)
    if not result or not isinstance(result, list):
        if not ETHERSCAN_API_KEY:
            logger.warning("BSC tx history requires ETHERSCAN_API_KEY. Balance-only mode.")
        return []

    return await _evm_parse_txlist(result, address, token_contract, last_seen_txid, "BNB")


# ══════════════════════════════════════════════════════════════
#  TRON  (TronGrid — works without key!)
# ══════════════════════════════════════════════════════════════

TRONGRID_URL = "https://api.trongrid.io"


async def _trongrid_get(path: str, params: dict = None) -> dict | list | None:
    headers = {}
    if TRONGRID_API_KEY:
        headers["TRON-PRO-API-KEY"] = TRONGRID_API_KEY
    return await _http_get(f"{TRONGRID_URL}{path}", params=params, headers=headers)


async def tron_get_balance(address: str, token_contract: str = None, token_symbol: str = None) -> dict:
    data = await _trongrid_get(f"/v1/accounts/{address}")
    if not data or not data.get("data"):
        sym = token_symbol or "TRX"
        return {"balance": "0", "symbol": sym}
    account = data["data"][0]

    # If a specific TRC-20 token is requested, return its balance
    if token_contract:
        trc20 = account.get("trc20", [])
        for t in trc20:
            for contract, raw_balance in t.items():
                if contract == token_contract:
                    # Look up decimals from known tokens or default to 6
                    KNOWN_DECIMALS = {
                        "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t": 6,   # USDT
                        "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8": 6,   # USDC
                        "TUpMhErZL2fhh4sVNULAbNKLokS4GjC1F4": 18,  # TUSD
                    }
                    decimals = KNOWN_DECIMALS.get(token_contract, 6)
                    balance = int(raw_balance) / (10 ** decimals)
                    return {
                        "balance": f"{balance:.6f}",
                        "symbol": token_symbol or "TOKEN",
                    }
        return {"balance": "0", "symbol": token_symbol or "TOKEN"}

    # Native TRX balance
    balance_sun = account.get("balance", 0)
    return {
        "balance": f"{balance_sun / 1e6:.6f}",
        "balance_sun": balance_sun,
        "symbol": "TRX",
    }


async def tron_get_token_balances(address: str) -> list[dict]:
    data = await _trongrid_get(f"/v1/accounts/{address}")
    if not data or not data.get("data"):
        return []
    account = data["data"][0]
    trc20 = account.get("trc20", [])
    tokens = []
    for token_dict in trc20:
        for contract, balance in token_dict.items():
            tokens.append({"contract": contract, "balance_raw": balance})

    # Enrich with token info (limit to avoid rate limits)
    enriched = []
    # Well-known TRC-20 tokens
    KNOWN_TOKENS = {
        "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t": ("USDT", 6),
        "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8": ("USDC", 6),
        "TUpMhErZL2fhh4sVNULAbNKLokS4GjC1F4": ("TUSD", 18),
        "TN3W4H6rK2ce4vX9YnFQHwKENnHjoxb3m9": ("BTC", 8),
        "THb4CqiFdwNHsWsQCs4JhzwjMWys4aqCbF": ("ETH", 18),
        "TCFLL5dx5ZJdKnWuesXxi1VPwjLVmWZZy9": ("JST", 18),
        "TSSMHYeV2uE9qYH95DqyoCuNCzEL1NvU3S": ("SUN", 18),
        "TLa2f6VPqDgRE67v1736s7bJ8Ray5wYjU7": ("WIN", 6),
    }

    unknown_count = 0
    for t in tokens:
        contract = t["contract"]
        if contract in KNOWN_TOKENS:
            sym, dec = KNOWN_TOKENS[contract]
            enriched.append({"contract": contract, "symbol": sym, "decimals": dec})
        else:
            unknown_count += 1
            if unknown_count > 5:
                # Skip unknown tokens beyond 5 to avoid rate limits
                enriched.append({"contract": contract, "symbol": "TOKEN", "decimals": 6})
                continue
            # Rate limit pause between requests
            if unknown_count > 1:
                await asyncio.sleep(0.5)
            info = await _trongrid_get(f"/v1/contracts/{contract}")
            symbol = "TOKEN"
            decimals = 6
            if info and info.get("data"):
                d = info["data"][0] if isinstance(info["data"], list) else info["data"]
                symbol = d.get("symbol", "TOKEN")
                decimals = d.get("decimals", 6)
            enriched.append({"contract": contract, "symbol": symbol, "decimals": decimals})

    return enriched


async def tron_get_transactions(address: str, token_contract: str = None,
                                last_seen_txid: str = None, **kw) -> list[dict]:
    if token_contract:
        data = await _trongrid_get(f"/v1/accounts/{address}/transactions/trc20", params={
            "contract_address": token_contract,
            "limit": 50, "order_by": "block_timestamp,desc"
        })
    else:
        data = await _trongrid_get(f"/v1/accounts/{address}/transactions", params={
            "limit": 50, "order_by": "block_timestamp,desc"
        })

    if not data or not data.get("data"):
        return []

    txs = []
    for tx in data["data"]:
        if token_contract:
            tx_hash = tx.get("transaction_id", "")
            if last_seen_txid and tx_hash == last_seen_txid:
                break
            from_addr = tx.get("from", "")
            to_addr = tx.get("to", "")
            value_raw = int(tx.get("value", 0))
            decimals = int(tx.get("token_info", {}).get("decimals", 6))
            value = value_raw / (10 ** decimals)
            symbol = tx.get("token_info", {}).get("symbol", "TOKEN")
            if to_addr == address:
                direction = "in"
            elif from_addr == address:
                direction = "out"
            else:
                continue
            txs.append({
                "tx_hash": tx_hash,
                "direction": direction,
                "value": f"{value:.6f}",
                "value_raw": value_raw,
                "symbol": symbol,
                "from_addr": from_addr,
                "to_addr": to_addr,
                "block_number": tx.get("block_timestamp"),
                "timestamp": tx.get("block_timestamp", 0) / 1000 if tx.get("block_timestamp") else None,
            })
        else:
            tx_hash = tx.get("txID", "")
            if last_seen_txid and tx_hash == last_seen_txid:
                break
            raw_data = tx.get("raw_data", {})
            contracts = raw_data.get("contract", [])
            if not contracts:
                continue
            contract_data = contracts[0]
            if contract_data.get("type") != "TransferContract":
                continue
            params = contract_data.get("parameter", {}).get("value", {})
            from_addr = params.get("owner_address", "")
            to_addr = params.get("to_address", "")
            value_sun = params.get("amount", 0)
            value = value_sun / 1e6
            if to_addr == address or _tron_hex_to_base58(to_addr) == address:
                direction = "in"
            elif from_addr == address or _tron_hex_to_base58(from_addr) == address:
                direction = "out"
            else:
                continue
            txs.append({
                "tx_hash": tx_hash,
                "direction": direction,
                "value": f"{value:.6f}",
                "value_raw": value_sun,
                "symbol": "TRX",
                "from_addr": from_addr,
                "to_addr": to_addr,
                "block_number": tx.get("blockNumber"),
                "timestamp": raw_data.get("timestamp", 0) / 1000 if raw_data.get("timestamp") else None,
            })
    return txs


def _tron_hex_to_base58(hex_addr: str) -> str:
    if not hex_addr.startswith("41") or len(hex_addr) != 42:
        return hex_addr
    try:
        import base58
        import hashlib
        addr_bytes = bytes.fromhex(hex_addr)
        h1 = hashlib.sha256(addr_bytes).digest()
        h2 = hashlib.sha256(h1).digest()
        return base58.b58encode(addr_bytes + h2[:4]).decode()
    except Exception:
        return hex_addr


# ══════════════════════════════════════════════════════════════
#  Unified interface
# ══════════════════════════════════════════════════════════════

CHAIN_HANDLERS = {
    "BTC": {
        "get_balance": btc_get_balance,
        "get_transactions": btc_get_transactions,
        "get_token_list": None,
        "native_symbol": "BTC",
        "name": "Bitcoin",
        "explorer": "https://blockstream.info/tx/",
    },
    "ETH": {
        "get_balance": eth_get_balance,
        "get_transactions": eth_get_transactions,
        "get_token_list": eth_get_token_balances,
        "native_symbol": "ETH",
        "name": "Ethereum",
        "explorer": "https://etherscan.io/tx/",
    },
    "BSC": {
        "get_balance": bsc_get_balance,
        "get_transactions": bsc_get_transactions,
        "get_token_list": bsc_get_token_balances,
        "native_symbol": "BNB",
        "name": "BNB Smart Chain",
        "explorer": "https://bscscan.com/tx/",
        "note": "Full tx history requires ETHERSCAN_API_KEY",
    },
    "TRON": {
        "get_balance": tron_get_balance,
        "get_transactions": tron_get_transactions,
        "get_token_list": tron_get_token_balances,
        "native_symbol": "TRX",
        "name": "TRON",
        "explorer": "https://tronscan.org/#/transaction/",
    },
}


async def get_balance(chain: str, address: str, token_contract: str = None,
                      token_symbol: str = None) -> dict:
    handler = CHAIN_HANDLERS.get(chain)
    if not handler:
        return {"balance": "0", "symbol": "?"}
    return await handler["get_balance"](address, token_contract=token_contract,
                                        token_symbol=token_symbol)


async def get_transactions(chain: str, address: str, token_contract: str = None,
                           last_seen_txid: str = None) -> list[dict]:
    handler = CHAIN_HANDLERS.get(chain)
    if not handler:
        return []
    func = handler["get_transactions"]
    return await func(address, token_contract=token_contract, last_seen_txid=last_seen_txid)


async def get_token_list(chain: str, address: str) -> list[dict]:
    handler = CHAIN_HANDLERS.get(chain)
    if not handler or not handler["get_token_list"]:
        return []
    return await handler["get_token_list"](address)
