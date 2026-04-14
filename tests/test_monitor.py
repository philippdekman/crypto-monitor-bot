"""
Tests for monitor.py — check_address, run_monitoring_cycle, balance display, $10 filter.
"""
import time
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import pytest_asyncio

from monitor import (
    check_address, run_monitoring_cycle, get_initial_balance,
    format_transaction_notification, format_balance_message,
)


# ══════════════════════════════════════════════════════════════
#  check_address
# ══════════════════════════════════════════════════════════════

class TestCheckAddress:
    def _make_monitor(self, **overrides):
        base = {
            "id": 1, "user_id": 100, "chain": "ETH",
            "address": "0xTestAddr", "token_contract": None,
            "token_symbol": None, "last_tx_hash": None,
            "plan": "free",
        }
        base.update(overrides)
        return base

    @pytest.mark.asyncio
    async def test_new_large_tx_detected(self, patch_db):
        """A $500 ETH transaction should be returned."""
        monitor = self._make_monitor()

        mock_txs = [{
            "tx_hash": "0xabc", "direction": "in",
            "value": "0.5", "symbol": "ETH",
            "from_addr": "0xSender", "to_addr": "0xTestAddr",
            "block_number": 999, "timestamp": time.time(),
        }]
        mock_balance = {"balance": "1.5", "symbol": "ETH"}

        with patch("monitor.get_transactions", new_callable=AsyncMock, return_value=mock_txs), \
             patch("monitor.get_price_usd", new_callable=AsyncMock, return_value=1000.0), \
             patch("monitor.get_balance", new_callable=AsyncMock, return_value=mock_balance):
            result = await check_address(monitor)

        assert len(result) == 1
        assert result[0]["tx_hash"] == "0xabc"
        assert result[0]["value_usd"] == 500.0
        assert result[0]["user_id"] == 100
        assert "explorer_url" in result[0]

    @pytest.mark.asyncio
    async def test_small_tx_filtered_out(self, patch_db):
        """A $5 transaction (below $10 threshold) should be filtered."""
        monitor = self._make_monitor()

        mock_txs = [{
            "tx_hash": "0xsmall", "direction": "in",
            "value": "0.005", "symbol": "ETH",
            "from_addr": "0xSender", "to_addr": "0xTestAddr",
        }]
        mock_balance = {"balance": "1.0", "symbol": "ETH"}

        with patch("monitor.get_transactions", new_callable=AsyncMock, return_value=mock_txs), \
             patch("monitor.get_price_usd", new_callable=AsyncMock, return_value=1000.0), \
             patch("monitor.get_balance", new_callable=AsyncMock, return_value=mock_balance):
            result = await check_address(monitor)

        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_nine_dollars_filtered(self, patch_db):
        """$9 should be filtered (below $10 threshold)."""
        monitor = self._make_monitor()

        mock_txs = [{
            "tx_hash": "0xedge", "direction": "in",
            "value": "0.009", "symbol": "ETH",
            "from_addr": "0xSender", "to_addr": "0xTestAddr",
        }]
        mock_balance = {"balance": "1.0", "symbol": "ETH"}

        with patch("monitor.get_transactions", new_callable=AsyncMock, return_value=mock_txs), \
             patch("monitor.get_price_usd", new_callable=AsyncMock, return_value=1000.0), \
             patch("monitor.get_balance", new_callable=AsyncMock, return_value=mock_balance):
            result = await check_address(monitor)

        # $9.0 < MIN_TX_VALUE_USD ($10.0) → filtered
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_no_new_transactions(self, patch_db):
        monitor = self._make_monitor()

        with patch("monitor.get_transactions", new_callable=AsyncMock, return_value=[]):
            result = await check_address(monitor)

        assert result == []

    @pytest.mark.asyncio
    async def test_api_error_returns_empty(self, patch_db):
        monitor = self._make_monitor()

        with patch("monitor.get_transactions", new_callable=AsyncMock,
                    side_effect=ConnectionError("API down")):
            result = await check_address(monitor)

        assert result == []

    @pytest.mark.asyncio
    async def test_unknown_chain_returns_empty(self, patch_db):
        monitor = self._make_monitor(chain="SOLANA")
        result = await check_address(monitor)
        assert result == []

    @pytest.mark.asyncio
    async def test_no_price_available_value_usd_zero(self, patch_db):
        """When price API fails, value_usd=0 and all txs are filtered (< $10)."""
        monitor = self._make_monitor()

        mock_txs = [{
            "tx_hash": "0xnoprice", "direction": "in",
            "value": "100.0", "symbol": "ETH",
            "from_addr": "0xSender", "to_addr": "0xTestAddr",
        }]
        mock_balance = {"balance": "200.0", "symbol": "ETH"}

        with patch("monitor.get_transactions", new_callable=AsyncMock, return_value=mock_txs), \
             patch("monitor.get_price_usd", new_callable=AsyncMock, return_value=None), \
             patch("monitor.get_balance", new_callable=AsyncMock, return_value=mock_balance):
            result = await check_address(monitor)

        # value_usd = 0 → all filtered
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_duplicate_tx_not_notified_twice(self, patch_db):
        """Same tx_hash logged again should return was_new=False."""
        db = patch_db
        await db.upsert_user(100, "test", "Test")
        await db.create_free_subscription(100)

        monitor = self._make_monitor()

        mock_txs = [{
            "tx_hash": "0xdupe", "direction": "in",
            "value": "1.0", "symbol": "ETH",
            "from_addr": "0xSender", "to_addr": "0xTestAddr",
            "block_number": 999, "timestamp": time.time(),
        }]
        mock_balance = {"balance": "5.0", "symbol": "ETH"}

        with patch("monitor.get_transactions", new_callable=AsyncMock, return_value=mock_txs), \
             patch("monitor.get_price_usd", new_callable=AsyncMock, return_value=5000.0), \
             patch("monitor.get_balance", new_callable=AsyncMock, return_value=mock_balance):
            result1 = await check_address(monitor)
            result2 = await check_address(monitor)

        assert len(result1) == 1
        assert len(result2) == 0  # duplicate not notified

    @pytest.mark.asyncio
    async def test_token_monitor_uses_token_symbol(self, patch_db):
        """When monitoring a token, uses token_symbol for price lookup."""
        monitor = self._make_monitor(
            token_contract="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            token_symbol="USDT",
            chain="TRON"
        )

        mock_txs = [{
            "tx_hash": "txtoken", "direction": "in",
            "value": "100.0", "symbol": "USDT",
            "from_addr": "TSender", "to_addr": "TMyAddr",
        }]
        mock_balance = {"balance": "500.0", "symbol": "USDT"}

        with patch("monitor.get_transactions", new_callable=AsyncMock, return_value=mock_txs), \
             patch("monitor.get_price_usd", new_callable=AsyncMock, return_value=1.0) as price_mock, \
             patch("monitor.get_balance", new_callable=AsyncMock, return_value=mock_balance):
            result = await check_address(monitor)

        # Price was looked up for USDT, not TRX
        price_mock.assert_called_once_with("USDT")
        assert len(result) == 1
        assert result[0]["value_usd"] == 100.0


# ══════════════════════════════════════════════════════════════
#  run_monitoring_cycle
# ══════════════════════════════════════════════════════════════

class TestMonitoringCycle:
    @pytest.mark.asyncio
    async def test_empty_monitors(self, patch_db):
        result = await run_monitoring_cycle()
        assert result == []

    @pytest.mark.asyncio
    async def test_batch_processing(self, patch_db):
        """Multiple monitors processed in parallel batches."""
        db = patch_db
        await db.upsert_user(100, "test", "Test")
        await db.create_free_subscription(100)

        for i in range(7):
            await db.add_monitored_address(100, f"0x{i:040x}", "ETH")

        with patch("monitor.check_address", new_callable=AsyncMock, return_value=[]) as mock_check, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            await run_monitoring_cycle()

        assert mock_check.call_count == 7

    @pytest.mark.asyncio
    async def test_one_error_doesnt_stop_others(self, patch_db):
        db = patch_db
        await db.upsert_user(100, "test", "Test")
        await db.create_free_subscription(100)
        await db.add_monitored_address(100, "0xFail", "ETH")
        await db.add_monitored_address(100, "0xOK", "ETH")

        call_count = 0

        async def mock_check(monitor):
            nonlocal call_count
            call_count += 1
            if monitor["address"] == "0xFail":
                raise RuntimeError("boom")
            return [{"tx_hash": "good"}]

        with patch("monitor.check_address", side_effect=mock_check), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await run_monitoring_cycle()

        assert call_count == 2
        assert len(result) == 1


# ══════════════════════════════════════════════════════════════
#  get_initial_balance
# ══════════════════════════════════════════════════════════════

class TestInitialBalance:
    @pytest.mark.asyncio
    async def test_native_balance(self):
        with patch("monitor.get_balance", new_callable=AsyncMock,
                    return_value={"balance": "1.5", "symbol": "BTC"}), \
             patch("monitor.get_price_usd", new_callable=AsyncMock, return_value=50000.0):
            result = await get_initial_balance("BTC", "1TestAddr")

        assert result["symbol"] == "BTC"
        assert result["balance"] == "1.5"
        assert result["balance_usd"] == 75000.0
        assert result["is_token"] is False
        assert result["chain_name"] == "Bitcoin"

    @pytest.mark.asyncio
    async def test_token_balance(self):
        with patch("monitor.get_balance", new_callable=AsyncMock,
                    return_value={"balance": "1000.0", "symbol": "USDT"}), \
             patch("monitor.get_price_usd", new_callable=AsyncMock, return_value=1.0):
            result = await get_initial_balance(
                "TRON", "TAddr", token_contract="TR7N", token_symbol="USDT"
            )

        assert result["symbol"] == "USDT"
        assert result["balance_usd"] == 1000.0
        assert result["is_token"] is True

    @pytest.mark.asyncio
    async def test_unknown_chain(self):
        result = await get_initial_balance("SOLANA", "SomeAddr")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_balance_no_price(self):
        with patch("monitor.get_balance", new_callable=AsyncMock,
                    return_value={"balance": "100.0", "symbol": "ETH"}), \
             patch("monitor.get_price_usd", new_callable=AsyncMock, return_value=None):
            result = await get_initial_balance("ETH", "0xAddr")

        assert result["balance_usd"] == 0


# ══════════════════════════════════════════════════════════════
#  Notification formatting
# ══════════════════════════════════════════════════════════════

class TestFormatting:
    def test_incoming_tx_format(self):
        tx = {
            "direction": "in", "chain": "ETH",
            "value": "0.5", "symbol": "ETH",
            "value_usd": 1500.0,
            "from_addr": "0xSenderAddr1234567890",
            "to_addr": "0xRecvAddr",
            "explorer_url": "https://etherscan.io/tx/0xabc",
        }
        text = format_transaction_notification(tx)
        assert "Входящая" in text
        assert "0.5" in text
        assert "ETH" in text
        assert "$1,500.00" in text
        assert "etherscan.io" in text

    def test_outgoing_tx_format(self):
        tx = {
            "direction": "out", "chain": "BTC",
            "value": "0.01", "symbol": "BTC",
            "value_usd": 500.0,
            "from_addr": "1MyAddr",
            "to_addr": "1ReceiverAddr12345678",
            "explorer_url": "https://blockstream.info/tx/abc",
        }
        text = format_transaction_notification(tx)
        assert "Исходящая" in text
        assert "Кому" in text

    def test_balance_message_format(self):
        info = {
            "chain_name": "TRON",
            "address": "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE",
            "symbol": "TRX",
            "balance": "1920749.266087",
            "balance_usd": 192074.93,
        }
        text = format_balance_message(info, label="TRX on TRON")
        assert "TRON" in text
        assert "TRX" in text
        assert "TQn9Y2khEsLJW1ChVWFM..." in text
        assert "TRX on TRON" in text

    def test_notification_with_label(self):
        tx = {
            "direction": "in", "chain": "TRON",
            "value": "100", "symbol": "USDT",
            "value_usd": 100.0,
            "from_addr": "TSender",
            "to_addr": "TRecv",
        }
        text = format_transaction_notification(tx, label="Main Wallet")
        assert "Main Wallet" in text
