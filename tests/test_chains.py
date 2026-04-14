"""
Tests for chains.py — address detection, balance parsing, transaction parsing.
"""
import pytest
from unittest.mock import AsyncMock, patch

from chains import (
    detect_chains, CHAIN_HANDLERS, get_balance, get_transactions, get_token_list,
    _tron_hex_to_base58,
)


# ══════════════════════════════════════════════════════════════
#  Address detection
# ══════════════════════════════════════════════════════════════

class TestDetectChains:
    def test_btc_legacy(self):
        chains = detect_chains("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")
        assert "BTC" in chains

    def test_btc_segwit(self):
        chains = detect_chains("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4")
        assert "BTC" in chains

    def test_btc_p2sh(self):
        chains = detect_chains("3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy")
        assert "BTC" in chains

    def test_eth_address(self):
        chains = detect_chains("0x742d35Cc6634C0532925a3b844Bc9e7595f2bD28")
        assert "ETH" in chains

    def test_eth_also_matches_bsc(self):
        """0x addresses are valid on both ETH and BSC."""
        chains = detect_chains("0x742d35Cc6634C0532925a3b844Bc9e7595f2bD28")
        assert "ETH" in chains
        assert "BSC" in chains

    def test_tron_address(self):
        chains = detect_chains("TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE")
        assert "TRON" in chains

    def test_tron_not_eth(self):
        """TRON addresses must NOT match ETH/BSC."""
        chains = detect_chains("TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE")
        assert "ETH" not in chains
        assert "BSC" not in chains

    def test_invalid_address(self):
        chains = detect_chains("hello_world")
        assert chains == []

    def test_empty_string(self):
        chains = detect_chains("")
        assert chains == []

    def test_short_address(self):
        chains = detect_chains("0x123")
        assert chains == []

    def test_tron_hex_to_base58_valid(self):
        result = _tron_hex_to_base58("4158ba2c53839e84effc798e4166dc90cdf5c31647")
        assert result.startswith("T")
        assert len(result) == 34

    def test_tron_hex_to_base58_invalid(self):
        result = _tron_hex_to_base58("not_a_hex")
        assert result == "not_a_hex"

    def test_tron_hex_to_base58_short(self):
        result = _tron_hex_to_base58("41abcd")
        assert result == "41abcd"  # too short, returns unchanged


# ══════════════════════════════════════════════════════════════
#  CHAIN_HANDLERS structure
# ══════════════════════════════════════════════════════════════

class TestChainHandlers:
    def test_all_chains_present(self):
        assert "BTC" in CHAIN_HANDLERS
        assert "ETH" in CHAIN_HANDLERS
        assert "TRON" in CHAIN_HANDLERS
        assert "BSC" in CHAIN_HANDLERS

    def test_handlers_have_required_keys(self):
        required = ["get_balance", "get_transactions", "native_symbol", "name", "explorer"]
        for chain, handler in CHAIN_HANDLERS.items():
            for key in required:
                assert key in handler, f"{chain} missing key '{key}'"

    def test_native_symbols(self):
        assert CHAIN_HANDLERS["BTC"]["native_symbol"] == "BTC"
        assert CHAIN_HANDLERS["ETH"]["native_symbol"] == "ETH"
        assert CHAIN_HANDLERS["TRON"]["native_symbol"] == "TRX"
        assert CHAIN_HANDLERS["BSC"]["native_symbol"] == "BNB"

    def test_token_list_support(self):
        """ETH, TRON, BSC support token lists; BTC does not."""
        assert CHAIN_HANDLERS["BTC"]["get_token_list"] is None
        assert CHAIN_HANDLERS["ETH"]["get_token_list"] is not None
        assert CHAIN_HANDLERS["TRON"]["get_token_list"] is not None
        assert CHAIN_HANDLERS["BSC"]["get_token_list"] is not None


# ══════════════════════════════════════════════════════════════
#  Balance parsing (mocked HTTP)
# ══════════════════════════════════════════════════════════════

class TestBalanceParsing:
    @pytest.mark.asyncio
    async def test_tron_native_balance(self):
        """Verify TRX balance is converted from SUN correctly."""
        mock_response = {
            "data": [{
                "balance": 1920749266087,  # in SUN
                "trc20": [],
            }]
        }
        with patch("chains._trongrid_get", new_callable=AsyncMock, return_value=mock_response):
            result = await CHAIN_HANDLERS["TRON"]["get_balance"](
                "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE"
            )
            assert result["symbol"] == "TRX"
            balance = float(result["balance"])
            assert abs(balance - 1920749.266087) < 0.001

    @pytest.mark.asyncio
    async def test_tron_usdt_balance(self):
        """Verify TRC-20 USDT balance with 6 decimals."""
        mock_response = {
            "data": [{
                "balance": 0,
                "trc20": [
                    {"TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t": "1500000000"}  # 1500 USDT
                ],
            }]
        }
        with patch("chains._trongrid_get", new_callable=AsyncMock, return_value=mock_response):
            result = await CHAIN_HANDLERS["TRON"]["get_balance"](
                "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE",
                token_contract="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
                token_symbol="USDT"
            )
            assert result["symbol"] == "USDT"
            assert float(result["balance"]) == 1500.0

    @pytest.mark.asyncio
    async def test_tron_empty_account(self):
        with patch("chains._trongrid_get", new_callable=AsyncMock, return_value={"data": []}):
            result = await CHAIN_HANDLERS["TRON"]["get_balance"]("TFakeAddr")
            assert float(result["balance"]) == 0

    @pytest.mark.asyncio
    async def test_tron_api_failure(self):
        with patch("chains._trongrid_get", new_callable=AsyncMock, return_value=None):
            result = await CHAIN_HANDLERS["TRON"]["get_balance"]("TFakeAddr")
            assert float(result["balance"]) == 0

    @pytest.mark.asyncio
    async def test_btc_balance(self):
        mock_data = {"chain_stats": {"funded_txo_sum": 50000000, "spent_txo_sum": 10000000}}
        with patch("chains._http_get", new_callable=AsyncMock, return_value=mock_data):
            result = await CHAIN_HANDLERS["BTC"]["get_balance"]("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")
            assert result["symbol"] == "BTC"
            balance = float(result["balance"])
            assert abs(balance - 0.4) < 0.001  # (50M - 10M) / 1e8 = 0.4 BTC


# ══════════════════════════════════════════════════════════════
#  Transaction parsing (TRON)
# ══════════════════════════════════════════════════════════════

class TestTronTransactions:
    @pytest.mark.asyncio
    async def test_trc20_incoming_tx(self):
        """TRC-20 token incoming transaction parsed correctly."""
        mock_data = {
            "data": [{
                "transaction_id": "abc123def",
                "from": "TSenderAddress1234",
                "to": "TMyAddress12345678",
                "value": "100000000",  # 100 USDT (6 decimals)
                "token_info": {"symbol": "USDT", "decimals": "6"},
                "block_timestamp": 1700000000000,
            }],
            "success": True,
        }
        with patch("chains._trongrid_get", new_callable=AsyncMock, return_value=mock_data):
            txs = await CHAIN_HANDLERS["TRON"]["get_transactions"](
                "TMyAddress12345678",
                token_contract="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
            )
            assert len(txs) == 1
            assert txs[0]["direction"] == "in"
            assert txs[0]["symbol"] == "USDT"
            assert float(txs[0]["value"]) == 100.0

    @pytest.mark.asyncio
    async def test_trc20_outgoing_tx(self):
        mock_data = {
            "data": [{
                "transaction_id": "out123",
                "from": "TMyAddress12345678",
                "to": "TReceiverAddr567890",
                "value": "50000000",
                "token_info": {"symbol": "USDT", "decimals": "6"},
                "block_timestamp": 1700000000000,
            }],
            "success": True,
        }
        with patch("chains._trongrid_get", new_callable=AsyncMock, return_value=mock_data):
            txs = await CHAIN_HANDLERS["TRON"]["get_transactions"](
                "TMyAddress12345678",
                token_contract="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
            )
            assert len(txs) == 1
            assert txs[0]["direction"] == "out"
            assert float(txs[0]["value"]) == 50.0

    @pytest.mark.asyncio
    async def test_native_trx_transfer(self):
        """Native TRX TransferContract transaction."""
        mock_data = {
            "data": [{
                "txID": "nativetx456",
                "raw_data": {
                    "contract": [{
                        "type": "TransferContract",
                        "parameter": {
                            "value": {
                                "owner_address": "TSenderHex",
                                "to_address": "TMyAddress12345678",
                                "amount": 5000000,  # 5 TRX
                            }
                        }
                    }],
                    "timestamp": 1700000000000,
                },
                "blockNumber": 12345,
            }],
            "success": True,
        }
        with patch("chains._trongrid_get", new_callable=AsyncMock, return_value=mock_data):
            txs = await CHAIN_HANDLERS["TRON"]["get_transactions"]("TMyAddress12345678")
            assert len(txs) == 1
            assert txs[0]["symbol"] == "TRX"
            assert float(txs[0]["value"]) == 5.0
            assert txs[0]["direction"] == "in"

    @pytest.mark.asyncio
    async def test_last_seen_txid_stops_parsing(self):
        """Transactions before last_seen_txid should not be returned."""
        mock_data = {
            "data": [
                {
                    "transaction_id": "new_tx",
                    "from": "TSender", "to": "TMyAddr",
                    "value": "100000000",
                    "token_info": {"symbol": "USDT", "decimals": "6"},
                    "block_timestamp": 1700000002000,
                },
                {
                    "transaction_id": "old_tx",
                    "from": "TSender", "to": "TMyAddr",
                    "value": "200000000",
                    "token_info": {"symbol": "USDT", "decimals": "6"},
                    "block_timestamp": 1700000001000,
                },
            ],
            "success": True,
        }
        with patch("chains._trongrid_get", new_callable=AsyncMock, return_value=mock_data):
            txs = await CHAIN_HANDLERS["TRON"]["get_transactions"](
                "TMyAddr",
                token_contract="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
                last_seen_txid="old_tx"
            )
            assert len(txs) == 1
            assert txs[0]["tx_hash"] == "new_tx"

    @pytest.mark.asyncio
    async def test_empty_response(self):
        with patch("chains._trongrid_get", new_callable=AsyncMock, return_value={"data": []}):
            txs = await CHAIN_HANDLERS["TRON"]["get_transactions"]("TAddr")
            assert txs == []

    @pytest.mark.asyncio
    async def test_api_returns_none(self):
        with patch("chains._trongrid_get", new_callable=AsyncMock, return_value=None):
            txs = await CHAIN_HANDLERS["TRON"]["get_transactions"]("TAddr")
            assert txs == []


# ══════════════════════════════════════════════════════════════
#  Unified interface
# ══════════════════════════════════════════════════════════════

class TestUnifiedInterface:
    @pytest.mark.asyncio
    async def test_get_balance_unknown_chain(self):
        result = await get_balance("SOLANA", "SomeAddress")
        assert result["balance"] == "0"

    @pytest.mark.asyncio
    async def test_get_transactions_unknown_chain(self):
        result = await get_transactions("SOLANA", "SomeAddress")
        assert result == []

    @pytest.mark.asyncio
    async def test_get_token_list_btc_returns_empty(self):
        result = await get_token_list("BTC", "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")
        assert result == []
