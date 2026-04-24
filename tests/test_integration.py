"""
Integration tests — full end-to-end flows combining multiple modules.
"""
import time
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from config import FREE_ADDRESS_LIMIT, PRICING, AML_CHECK_PRICE_CENTS, NAME_CHECK_PRICE_CENTS, FREE_AML_CHECKS


# ══════════════════════════════════════════════════════════════
#  Full payment flow: create invoice → pay → confirm → subscribe
# ══════════════════════════════════════════════════════════════

class TestFullPaymentFlow:
    @pytest.mark.asyncio
    async def test_btc_monthly_payment_flow(self, patch_db):
        """User creates invoice, pays BTC, subscription is activated."""
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        await db.create_free_subscription(100)

        # Step 1: Create invoice
        with patch("subscription.generate_address", return_value={
            "address": "1InvoiceBTC", "path": "m/44'/0'/0'/0/0",
            "chain": "BTC", "index": 0,
        }), patch("subscription.convert_from_usd", new_callable=AsyncMock, return_value=0.0001):
            from subscription import create_payment_invoice
            invoice = await create_payment_invoice(100, "basic", "monthly", "BTC")

        assert invoice is not None
        assert invoice["symbol"] == "BTC"
        assert invoice["amount_usd"] == 5.0

        # Step 2: Simulate payment on blockchain
        mock_txs = [{
            "tx_hash": "btc_payment_hash_123",
            "direction": "in",
            "value": invoice["amount_crypto"],
            "symbol": "BTC",
            "from_addr": "1UserWallet",
            "to_addr": invoice["address"],
        }]

        # Step 3: Check pending payments → confirm → activate subscription
        with patch("chains.get_transactions", new_callable=AsyncMock, return_value=mock_txs):
            from subscription import check_pending_payments
            confirmed = await check_pending_payments()

        assert len(confirmed) == 1
        assert confirmed[0]["plan"] == "basic"
        assert confirmed[0]["tx_hash"] == "btc_payment_hash_123"

        # Step 4: Verify subscription is active
        sub = await db.get_active_subscription(100)
        assert sub["plan"] == "basic"
        assert sub["period"] == "monthly"

        # Step 5: User can now add more addresses
        from subscription import can_add_address
        for i in range(5):
            await db.add_monitored_address(100, f"0x{i:040x}", "ETH")
        can, _ = await can_add_address(100)
        assert can is True  # basic allows 100 addresses

    @pytest.mark.asyncio
    async def test_tron_yearly_premium_flow(self, patch_db):
        """Full flow with TRON payment for Premium yearly."""
        db = patch_db
        await db.upsert_user(200, "bob", "Bob")
        await db.create_free_subscription(200)

        with patch("subscription.generate_address", return_value={
            "address": "TPremiumPayAddr", "path": "m/44'/195'/0'/0/0",
            "chain": "TRON", "index": 0,
        }), patch("subscription.convert_from_usd", new_callable=AsyncMock, return_value=1500.0):
            from subscription import create_payment_invoice
            invoice = await create_payment_invoice(200, "premium", "yearly", "TRON")

        assert invoice["symbol"] == "TRX"
        assert invoice["amount_usd"] == 150.0

        mock_txs = [{
            "tx_hash": "trx_premium_tx",
            "direction": "in",
            "value": "1500.00000000",
            "symbol": "TRX",
            "from_addr": "TUserWallet",
            "to_addr": "TPremiumPayAddr",
        }]

        with patch("chains.get_transactions", new_callable=AsyncMock, return_value=mock_txs):
            from subscription import check_pending_payments
            confirmed = await check_pending_payments()

        assert len(confirmed) == 1
        sub = await db.get_active_subscription(200)
        assert sub["plan"] == "premium"
        assert sub["period"] == "yearly"
        # Premium yearly = 365 days
        duration = sub["expires_at"] - sub["started_at"]
        assert abs(duration - 365 * 86400) < 10


# ══════════════════════════════════════════════════════════════
#  Address replace flow
# ══════════════════════════════════════════════════════════════

class TestAddressReplaceFlow:
    @pytest.mark.asyncio
    async def test_replace_stays_within_free_limit(self, patch_db):
        """Replace: delete old + add new should not exceed free limit."""
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        await db.create_free_subscription(100)

        # Add 2 addresses (at free limit)
        a1 = await db.add_monitored_address(100, "0xOld1", "ETH")
        a2 = await db.add_monitored_address(100, "0xKeep", "ETH")
        assert await db.count_user_addresses(100) == 2

        # User can't add more
        from subscription import can_add_address
        can, _ = await can_add_address(100)
        assert can is False

        # Step 1: Delete old address
        await db.remove_monitored_address(a1["id"], 100)
        assert await db.count_user_addresses(100) == 1

        # Step 2: Now user CAN add (slot freed)
        can, _ = await can_add_address(100)
        assert can is True

        # Step 3: Add replacement
        a3 = await db.add_monitored_address(100, "0xNew1", "ETH")
        assert await db.count_user_addresses(100) == 2

        # Still at limit
        can, _ = await can_add_address(100)
        assert can is False


# ══════════════════════════════════════════════════════════════
#  Monitoring + notification flow
# ══════════════════════════════════════════════════════════════

class TestMonitoringFlow:
    @pytest.mark.asyncio
    async def test_new_user_full_monitoring_flow(self, patch_db):
        """User adds address → initial balance → monitoring detects new tx."""
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        await db.create_free_subscription(100)

        # Step 1: Add address
        addr = await db.add_monitored_address(100, "TAliceAddr", "TRON", label="My TRON")

        # Step 2: Get initial balance + snapshot existing txs
        with patch("monitor.get_balance", new_callable=AsyncMock,
                    return_value={"balance": "500.0", "symbol": "TRX"}), \
             patch("monitor.get_price_usd", new_callable=AsyncMock, return_value=0.10):
            from monitor import get_initial_balance
            balance = await get_initial_balance("TRON", "TAliceAddr")
            assert balance["balance"] == "500.0"
            assert balance["balance_usd"] == 50.0

        # Set last_tx_hash as "already seen"
        await db.update_monitor_state(addr["id"], balance="500.0", last_tx_hash="old_tx_hash")

        # Step 3: Monitoring cycle finds a new $200 tx
        new_txs = [{
            "tx_hash": "new_big_tx",
            "direction": "in",
            "value": "2000.0",  # 2000 TRX at $0.10 = $200
            "symbol": "TRX",
            "from_addr": "TSender",
            "to_addr": "TAliceAddr",
            "block_number": 12345,
            "timestamp": time.time(),
        }]
        balance_after = {"balance": "2500.0", "symbol": "TRX"}

        monitor_data = dict(addr)
        monitor_data["plan"] = "free"

        with patch("monitor.get_transactions", new_callable=AsyncMock, return_value=new_txs), \
             patch("monitor.get_price_usd", new_callable=AsyncMock, return_value=0.10), \
             patch("monitor.get_balance", new_callable=AsyncMock, return_value=balance_after):
            from monitor import check_address
            notifications = await check_address(monitor_data)

        assert len(notifications) == 1
        assert notifications[0]["value_usd"] == 200.0
        assert notifications[0]["user_id"] == 100

    @pytest.mark.asyncio
    async def test_small_txs_not_notified(self, patch_db):
        """Transactions under $10 should not generate notifications."""
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        await db.create_free_subscription(100)
        addr = await db.add_monitored_address(100, "0xAddr", "ETH")

        small_txs = [{
            "tx_hash": f"small_{i}",
            "direction": "in",
            "value": "0.001",  # 0.001 ETH at $3000 = $3
            "symbol": "ETH",
            "from_addr": "0xS", "to_addr": "0xAddr",
        } for i in range(5)]

        monitor_data = dict(addr)
        monitor_data["plan"] = "free"

        with patch("monitor.get_transactions", new_callable=AsyncMock, return_value=small_txs), \
             patch("monitor.get_price_usd", new_callable=AsyncMock, return_value=3000.0), \
             patch("monitor.get_balance", new_callable=AsyncMock,
                   return_value={"balance": "1.0", "symbol": "ETH"}):
            from monitor import check_address
            notifications = await check_address(monitor_data)

        assert len(notifications) == 0


# ══════════════════════════════════════════════════════════════
#  Edge cases
# ══════════════════════════════════════════════════════════════

class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_multiple_payments_different_users(self, patch_db):
        """Two users with pending payments — both should be checked."""
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        await db.upsert_user(200, "bob", "Bob")
        await db.create_free_subscription(100)
        await db.create_free_subscription(200)

        await db.create_payment(100, "basic", "monthly", 5.0,
                                 "ETH", "0xAlicePay", "0.002", 0)
        await db.create_payment(200, "premium", "yearly", 150.0,
                                 "ETH", "0xBobPay", "0.06", 1)

        async def mock_txs(chain, addr, **kw):
            if addr == "0xAlicePay":
                return [{
                    "tx_hash": "alice_tx", "direction": "in",
                    "value": "0.00200000", "symbol": "ETH",
                    "from_addr": "0xAlice", "to_addr": "0xAlicePay",
                }]
            elif addr == "0xBobPay":
                return [{
                    "tx_hash": "bob_tx", "direction": "in",
                    "value": "0.06000000", "symbol": "ETH",
                    "from_addr": "0xBob", "to_addr": "0xBobPay",
                }]
            return []

        with patch("chains.get_transactions", side_effect=mock_txs):
            from subscription import check_pending_payments
            confirmed = await check_pending_payments()

        assert len(confirmed) == 2
        plans = {c["plan"] for c in confirmed}
        assert plans == {"basic", "premium"}

    @pytest.mark.asyncio
    async def test_subscription_upgrade_replaces_old(self, patch_db):
        """Upgrading from basic to premium deactivates basic."""
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        await db.create_subscription(100, "basic", "monthly", 30)

        # Simulate premium payment confirmation
        await db.create_subscription(100, "premium", "yearly", 365)

        active = await db.get_active_subscription(100)
        assert active["plan"] == "premium"

        # Only one active sub
        active_subs = [s for s in db.subscriptions
                       if s["user_id"] == 100 and s["is_active"] == 1]
        assert len(active_subs) == 1

    @pytest.mark.asyncio
    async def test_concurrent_address_add_same_user(self, patch_db):
        """Adding the same address twice should not create duplicates."""
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        await db.create_free_subscription(100)

        a1 = await db.add_monitored_address(100, "TAddr1", "TRON")
        a2 = await db.add_monitored_address(100, "TAddr1", "TRON")
        assert a1["id"] == a2["id"]
        assert await db.count_user_addresses(100) == 1


# ══════════════════════════════════════════════════════════════
#  Balance topup + AML check integration
# ══════════════════════════════════════════════════════════════

class TestTopupAndAmlFlow:
    @pytest.mark.asyncio
    async def test_full_topup_confirm_aml_debit(self, patch_db):
        """Full flow: create topup → confirm payment → balance credited → AML check → balance debited."""
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        await db.create_free_subscription(100)

        # 1. Create topup invoice
        with patch("balance.generate_address", return_value={
            "address": "TTopupAddr", "path": "m/44'/195'/0'/0/0",
            "chain": "TRON", "index": 0,
        }), patch("balance.convert_from_usd", new_callable=AsyncMock, return_value=100.0):
            from balance import create_topup_invoice
            invoice = await create_topup_invoice(100, 10.0, "TRON")
            assert invoice is not None

        # 2. Confirm payment
        mock_txs = [{
            "tx_hash": "topup_tx_abc",
            "direction": "in",
            "value": "100.00000000",
            "symbol": "TRX",
            "from_addr": "TSender",
            "to_addr": "TTopupAddr",
        }]

        with patch("chains.get_transactions", new_callable=AsyncMock, return_value=mock_txs):
            from subscription import check_pending_payments
            confirmed = await check_pending_payments()

        assert len(confirmed) == 1
        assert confirmed[0]["payment_kind"] == "balance_topup"

        # 3. Verify balance
        balance = await db.get_balance_cents(100)
        assert balance == 1000  # $10

        # 4. AML check (paid — free plan has 0 free)
        from balance import charge_aml_check
        ok, msg = await charge_aml_check(100, "TTestAddr")
        assert ok is True

        # 5. Balance deducted
        balance = await db.get_balance_cents(100)
        assert balance == 1000 - AML_CHECK_PRICE_CENTS

    @pytest.mark.asyncio
    async def test_plan_upgrade_gives_free_aml_checks(self, patch_db):
        """Free plan → Basic plan gives 3 free AML checks, then paid kicks in."""
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        await db.create_free_subscription(100)
        await db.credit_balance(100, 5000)  # $50

        # Free plan: no free checks
        from balance import can_use_free_aml_check, charge_aml_check
        can, remaining = await can_use_free_aml_check(100)
        assert can is False

        # Upgrade to basic
        await db.create_subscription(100, "basic", "monthly", 30)

        # Now has 3 free checks
        can, remaining = await can_use_free_aml_check(100)
        assert can is True
        assert remaining == FREE_AML_CHECKS["basic"]

        # Use all free checks
        for i in range(FREE_AML_CHECKS["basic"]):
            ok, msg = await charge_aml_check(100, f"addr_{i}")
            assert ok is True

        # Balance should still be $50 (free checks don't cost money)
        balance = await db.get_balance_cents(100)
        assert balance == 5000

        # Next check should be paid
        ok, msg = await charge_aml_check(100, "addr_paid")
        assert ok is True
        balance = await db.get_balance_cents(100)
        assert balance == 5000 - AML_CHECK_PRICE_CENTS


# ══════════════════════════════════════════════════════════════
#  Name check (Dilisense) integration
# ══════════════════════════════════════════════════════════════

class TestNameCheckIntegration:
    @pytest.mark.asyncio
    async def test_topup_basic_plan_mixed_free_then_paid(self, patch_db):
        """Full flow: topup → Basic plan → 3 free checks (mix of AML + name) → 4th paid."""
        db = patch_db
        await db.upsert_user(500, "testuser", "TestUser")
        await db.create_subscription(500, "basic", "monthly", 30)
        await db.credit_balance(500, 2000)  # $20

        from balance import charge_aml_check, charge_name_check, get_user_balance_info

        # 1st free: AML address check
        success, msg = await charge_aml_check(500, "0xABC123")
        assert success is True

        # 2nd free: Name check
        success, msg = await charge_name_check(500, "Boris Johnson")
        assert success is True

        # 3rd free: Another name check
        success, msg = await charge_name_check(500, "John Smith")
        assert success is True

        # Verify no balance consumed yet
        info = await get_user_balance_info(500)
        assert info["balance_cents"] == 2000
        assert info["free_aml_remaining"] == 0

        # 4th check: paid name check ($1.00)
        success, msg = await charge_name_check(500, "Vladimir Putin")
        assert success is True
        assert "Списано" in msg

        balance = await db.get_balance_cents(500)
        assert balance == 2000 - NAME_CHECK_PRICE_CENTS

    @pytest.mark.asyncio
    async def test_name_check_with_dilisense_mock(self, patch_db):
        """Integration: charge + dilisense mock check end-to-end."""
        db = patch_db
        await db.upsert_user(600, "checker", "Checker")
        await db.create_subscription(600, "basic", "monthly", 30)

        from balance import charge_name_check
        with patch("dilisense.DILISENSE_API_KEY", ""):
            import dilisense
            dilisense._cache.clear()

            # Free check + dilisense mock
            success, charge_msg = await charge_name_check(600, "Vladimir Putin")
            assert success is True

            result = await dilisense.check_individual("Vladimir Putin")
            assert result["mock"] is True
            assert result["risk_level"] == "high"
            assert "SANCTION" in result["source_types"]
            assert result["total_hits"] == 2
