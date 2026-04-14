"""
Tests for subscription.py — plan info, address limits, invoice creation, payment verification.
"""
import time
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import pytest_asyncio

from config import PRICING, FREE_ADDRESS_LIMIT


# ══════════════════════════════════════════════════════════════
#  Plan info & limits
# ══════════════════════════════════════════════════════════════

class TestPlanInfo:
    @pytest.mark.asyncio
    async def test_free_user_plan_info(self, patch_db):
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")
        await db.create_free_subscription(111)

        from subscription import get_user_plan_info
        info = await get_user_plan_info(111)
        assert info["plan"] == "free"
        assert info["max_addresses"] == FREE_ADDRESS_LIMIT
        assert info["current_addresses"] == 0
        assert info["expires_at"] is None

    @pytest.mark.asyncio
    async def test_basic_user_plan_info(self, patch_db):
        db = patch_db
        await db.upsert_user(222, "bob", "Bob")
        await db.create_subscription(222, "basic", "monthly", 30)

        from subscription import get_user_plan_info
        info = await get_user_plan_info(222)
        assert info["plan"] == "basic"
        assert info["max_addresses"] == PRICING["basic"]["max_addresses"]
        assert info["expires_at"] is not None
        assert info["expires_at"] > time.time()

    @pytest.mark.asyncio
    async def test_premium_user_plan_info(self, patch_db):
        db = patch_db
        await db.upsert_user(333, "carol", "Carol")
        await db.create_subscription(333, "premium", "yearly", 365)

        from subscription import get_user_plan_info
        info = await get_user_plan_info(333)
        assert info["plan"] == "premium"
        assert info["max_addresses"] == PRICING["premium"]["max_addresses"]


class TestCanAddAddress:
    @pytest.mark.asyncio
    async def test_free_user_can_add_first(self, patch_db):
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")
        await db.create_free_subscription(111)

        from subscription import can_add_address
        can, reason = await can_add_address(111)
        assert can is True
        assert reason == ""

    @pytest.mark.asyncio
    async def test_free_user_at_limit(self, patch_db):
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")
        await db.create_free_subscription(111)
        for i in range(FREE_ADDRESS_LIMIT):
            await db.add_monitored_address(111, f"addr{i}", "BTC")

        from subscription import can_add_address
        can, reason = await can_add_address(111)
        assert can is False
        assert "лимит" in reason.lower() or "Бесплатный" in reason

    @pytest.mark.asyncio
    async def test_basic_user_can_add_many(self, patch_db):
        db = patch_db
        await db.upsert_user(222, "bob", "Bob")
        await db.create_subscription(222, "basic", "monthly", 30)
        for i in range(10):
            await db.add_monitored_address(222, f"addr{i}", "ETH")

        from subscription import can_add_address
        can, reason = await can_add_address(222)
        assert can is True

    @pytest.mark.asyncio
    async def test_basic_user_at_limit(self, patch_db):
        db = patch_db
        await db.upsert_user(222, "bob", "Bob")
        await db.create_subscription(222, "basic", "monthly", 30)
        for i in range(PRICING["basic"]["max_addresses"]):
            await db.add_monitored_address(222, f"0x{i:040x}", "ETH")

        from subscription import can_add_address
        can, reason = await can_add_address(222)
        assert can is False
        assert "Basic" in reason

    @pytest.mark.asyncio
    async def test_no_subscription_gets_free(self, patch_db):
        db = patch_db
        await db.upsert_user(444, "dave", "Dave")
        # No subscription at all

        from subscription import can_add_address
        can, reason = await can_add_address(444)
        assert can is True  # 0 < FREE_ADDRESS_LIMIT


# ══════════════════════════════════════════════════════════════
#  Invoice creation
# ══════════════════════════════════════════════════════════════

class TestCreateInvoice:
    @pytest.mark.asyncio
    async def test_successful_invoice_btc(self, patch_db):
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")

        with patch("subscription.generate_address", return_value={
            "address": "1TestBTCAddr", "path": "m/44'/0'/0'/0/0",
            "chain": "BTC", "index": 0,
        }), patch("subscription.convert_from_usd", new_callable=AsyncMock, return_value=0.0001):
            from subscription import create_payment_invoice
            invoice = await create_payment_invoice(111, "basic", "monthly", "BTC")

            assert invoice is not None
            assert invoice["symbol"] == "BTC"
            assert invoice["amount_usd"] == 5.0
            assert invoice["plan"] == "basic"
            assert invoice["period"] == "monthly"
            assert invoice["address"] == "1TestBTCAddr"
            assert float(invoice["amount_crypto"]) > 0

    @pytest.mark.asyncio
    async def test_successful_invoice_tron(self, patch_db):
        db = patch_db
        await db.upsert_user(222, "bob", "Bob")

        with patch("subscription.generate_address", return_value={
            "address": "TTestTronAddr", "path": "m/44'/195'/0'/0/0",
            "chain": "TRON", "index": 0,
        }), patch("subscription.convert_from_usd", new_callable=AsyncMock, return_value=50.0):
            from subscription import create_payment_invoice
            invoice = await create_payment_invoice(222, "basic", "monthly", "TRON")

            assert invoice["symbol"] == "TRX"
            assert invoice["address"].startswith("TTest")

    @pytest.mark.asyncio
    async def test_invoice_unknown_plan(self, patch_db):
        await patch_db.upsert_user(111, "alice", "Alice")
        from subscription import create_payment_invoice
        result = await create_payment_invoice(111, "nonexistent", "monthly", "BTC")
        assert result is None

    @pytest.mark.asyncio
    async def test_invoice_unknown_period(self, patch_db):
        await patch_db.upsert_user(111, "alice", "Alice")
        from subscription import create_payment_invoice
        result = await create_payment_invoice(111, "basic", "weekly", "BTC")
        assert result is None

    @pytest.mark.asyncio
    async def test_invoice_hd_wallet_fails(self, patch_db):
        await patch_db.upsert_user(111, "alice", "Alice")
        with patch("subscription.generate_address", return_value=None):
            from subscription import create_payment_invoice
            result = await create_payment_invoice(111, "basic", "monthly", "BTC")
            assert result is None

    @pytest.mark.asyncio
    async def test_invoice_price_fetch_fails_all_retries(self, patch_db):
        await patch_db.upsert_user(111, "alice", "Alice")
        with patch("subscription.generate_address", return_value={
            "address": "1Test", "path": "m/44'/0'/0'/0/0", "chain": "BTC", "index": 0,
        }), patch("subscription.convert_from_usd", new_callable=AsyncMock, return_value=None), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            from subscription import create_payment_invoice
            result = await create_payment_invoice(111, "basic", "monthly", "BTC")
            assert result is None

    @pytest.mark.asyncio
    async def test_invoice_price_succeeds_on_retry(self, patch_db):
        """Price fetch fails first time, succeeds on second."""
        await patch_db.upsert_user(111, "alice", "Alice")
        call_count = 0

        async def flaky_convert(usd, sym):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return None
            return 0.0001

        with patch("subscription.generate_address", return_value={
            "address": "1Test", "path": "m/44'/0'/0'/0/0", "chain": "BTC", "index": 0,
        }), patch("subscription.convert_from_usd", side_effect=flaky_convert), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            from subscription import create_payment_invoice
            result = await create_payment_invoice(111, "basic", "monthly", "BTC")
            assert result is not None
            assert call_count == 2

    @pytest.mark.asyncio
    async def test_hd_index_increments(self, patch_db):
        """Each invoice should use a new HD index."""
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")

        indices = []

        def capture_generate(chain, idx):
            indices.append(idx)
            return {"address": f"addr_{idx}", "path": f"m/44'/0'/0'/0/{idx}",
                    "chain": chain, "index": idx}

        with patch("subscription.generate_address", side_effect=capture_generate), \
             patch("subscription.convert_from_usd", new_callable=AsyncMock, return_value=0.001):
            from subscription import create_payment_invoice
            await create_payment_invoice(111, "basic", "monthly", "BTC")
            await create_payment_invoice(111, "basic", "monthly", "BTC")
            await create_payment_invoice(111, "premium", "yearly", "BTC")

        assert indices == [0, 1, 2]


# ══════════════════════════════════════════════════════════════
#  Payment verification
# ══════════════════════════════════════════════════════════════

class TestPaymentVerification:
    @pytest.mark.asyncio
    async def test_payment_confirmed_exact_amount(self, patch_db):
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")
        await db.create_free_subscription(111)

        await db.create_payment(
            user_id=111, plan="basic", period="monthly",
            amount_usd=5.0, pay_chain="TRON", pay_address="TPayAddr123",
            pay_amount="50.00000000", derivation_idx=0,
        )

        mock_txs = [{
            "tx_hash": "txhash_confirmed",
            "direction": "in",
            "value": "50.00000000",
            "symbol": "TRX",
            "from_addr": "TSender",
            "to_addr": "TPayAddr123",
        }]

        with patch("chains.get_transactions", new_callable=AsyncMock, return_value=mock_txs):
            from subscription import check_pending_payments
            confirmed = await check_pending_payments()

        assert len(confirmed) == 1
        assert confirmed[0]["user_id"] == 111
        assert confirmed[0]["plan"] == "basic"
        assert confirmed[0]["tx_hash"] == "txhash_confirmed"

        # Check subscription was activated
        sub = await db.get_active_subscription(111)
        assert sub["plan"] == "basic"

    @pytest.mark.asyncio
    async def test_payment_confirmed_with_tolerance(self, patch_db):
        """1% tolerance — 49.5 TRX for 50.0 expected should be accepted."""
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")
        await db.create_free_subscription(111)

        await db.create_payment(
            user_id=111, plan="basic", period="monthly",
            amount_usd=5.0, pay_chain="TRON", pay_address="TPayAddr",
            pay_amount="50.00000000", derivation_idx=0,
        )

        mock_txs = [{
            "tx_hash": "tx_tolerance",
            "direction": "in",
            "value": "49.60000000",  # 99.2% of expected — within 1%
            "symbol": "TRX",
            "from_addr": "TSender",
            "to_addr": "TPayAddr",
        }]

        with patch("chains.get_transactions", new_callable=AsyncMock, return_value=mock_txs):
            from subscription import check_pending_payments
            confirmed = await check_pending_payments()

        assert len(confirmed) == 1

    @pytest.mark.asyncio
    async def test_payment_rejected_insufficient(self, patch_db):
        """Payment below 99% threshold should NOT be confirmed."""
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")
        await db.create_free_subscription(111)

        await db.create_payment(
            user_id=111, plan="basic", period="monthly",
            amount_usd=5.0, pay_chain="TRON", pay_address="TPayAddr",
            pay_amount="50.00000000", derivation_idx=0,
        )

        mock_txs = [{
            "tx_hash": "tx_insufficient",
            "direction": "in",
            "value": "25.00000000",  # only 50% of expected
            "symbol": "TRX",
            "from_addr": "TSender",
            "to_addr": "TPayAddr",
        }]

        with patch("chains.get_transactions", new_callable=AsyncMock, return_value=mock_txs):
            from subscription import check_pending_payments
            confirmed = await check_pending_payments()

        assert len(confirmed) == 0

    @pytest.mark.asyncio
    async def test_outgoing_tx_ignored(self, patch_db):
        """Outgoing transactions should not count as payment."""
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")
        await db.create_free_subscription(111)

        await db.create_payment(
            user_id=111, plan="basic", period="monthly",
            amount_usd=5.0, pay_chain="TRON", pay_address="TPayAddr",
            pay_amount="50.00000000", derivation_idx=0,
        )

        mock_txs = [{
            "tx_hash": "tx_out",
            "direction": "out",
            "value": "50.00000000",
            "symbol": "TRX",
            "from_addr": "TPayAddr",
            "to_addr": "TSomeone",
        }]

        with patch("chains.get_transactions", new_callable=AsyncMock, return_value=mock_txs):
            from subscription import check_pending_payments
            confirmed = await check_pending_payments()

        assert len(confirmed) == 0

    @pytest.mark.asyncio
    async def test_expired_payment_not_checked(self, patch_db):
        """Expired payments should not appear in pending list."""
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")

        pay = await db.create_payment(
            user_id=111, plan="basic", period="monthly",
            amount_usd=5.0, pay_chain="BTC", pay_address="1PayAddr",
            pay_amount="0.0001", derivation_idx=0, expires_in=-100,
        )

        pending = await db.get_pending_payments()
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_payment_api_error_continues(self, patch_db):
        """API error for one payment should not block others."""
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")
        await db.upsert_user(222, "bob", "Bob")
        await db.create_free_subscription(111)
        await db.create_free_subscription(222)

        await db.create_payment(111, "basic", "monthly", 5.0, "BTC", "1Fail", "0.0001", 0)
        await db.create_payment(222, "basic", "monthly", 5.0, "TRON", "TSucc", "50.0", 1)

        call_count = 0

        async def mock_get_txs(chain, addr, **kw):
            nonlocal call_count
            call_count += 1
            if addr == "1Fail":
                raise ConnectionError("API down")
            return [{
                "tx_hash": "tx_bob", "direction": "in",
                "value": "50.00000000", "symbol": "TRX",
                "from_addr": "TSender", "to_addr": "TSucc",
            }]

        with patch("chains.get_transactions", side_effect=mock_get_txs):
            from subscription import check_pending_payments
            confirmed = await check_pending_payments()

        assert call_count == 2  # Both were attempted
        assert len(confirmed) == 1
        assert confirmed[0]["user_id"] == 222

    @pytest.mark.asyncio
    async def test_yearly_subscription_duration(self, patch_db):
        """Yearly payment should create 365-day subscription."""
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")
        await db.create_free_subscription(111)

        await db.create_payment(111, "premium", "yearly", 150.0, "ETH", "0xPayAddr",
                                 "0.05", 0)

        mock_txs = [{
            "tx_hash": "tx_yearly", "direction": "in",
            "value": "0.05000000", "symbol": "ETH",
            "from_addr": "0xSender", "to_addr": "0xPayAddr",
        }]

        with patch("chains.get_transactions", new_callable=AsyncMock, return_value=mock_txs):
            from subscription import check_pending_payments
            await check_pending_payments()

        sub = await db.get_active_subscription(111)
        assert sub["plan"] == "premium"
        assert sub["period"] == "yearly"
        duration = sub["expires_at"] - sub["started_at"]
        assert abs(duration - 365 * 86400) < 10

    @pytest.mark.asyncio
    async def test_monthly_subscription_duration(self, patch_db):
        """Monthly payment should create 30-day subscription."""
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")
        await db.create_free_subscription(111)

        await db.create_payment(111, "basic", "monthly", 5.0, "BTC", "1BtcAddr",
                                 "0.0001", 0)

        mock_txs = [{
            "tx_hash": "tx_monthly", "direction": "in",
            "value": "0.00010000", "symbol": "BTC",
            "from_addr": "1Sender", "to_addr": "1BtcAddr",
        }]

        with patch("chains.get_transactions", new_callable=AsyncMock, return_value=mock_txs):
            from subscription import check_pending_payments
            await check_pending_payments()

        sub = await db.get_active_subscription(111)
        assert sub["plan"] == "basic"
        duration = sub["expires_at"] - sub["started_at"]
        assert abs(duration - 30 * 86400) < 10


# ══════════════════════════════════════════════════════════════
#  Pricing text
# ══════════════════════════════════════════════════════════════

class TestPricingText:
    def test_format_pricing_text_contains_plans(self):
        from subscription import format_pricing_text
        text = format_pricing_text()
        assert "Free" in text
        assert "Basic" in text
        assert "Premium" in text
        assert "$5" in text
        assert "$20" in text
