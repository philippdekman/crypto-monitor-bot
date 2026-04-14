"""
Tests for database.py mock layer — CRUD addresses, subscriptions, payments, HD indices.
These test the MockDB which mirrors real database.py behavior.
"""
import time
from unittest.mock import patch

import pytest
import pytest_asyncio

from config import FREE_ADDRESS_LIMIT, PRICING


# ══════════════════════════════════════════════════════════════
#  User operations
# ══════════════════════════════════════════════════════════════

class TestUsers:
    @pytest.mark.asyncio
    async def test_upsert_and_get_user(self, patch_db):
        db = patch_db
        await db.upsert_user(100, "alice", "Alice", "ru")
        user = await db.get_user(100)
        assert user["user_id"] == 100
        assert user["username"] == "alice"
        assert user["first_name"] == "Alice"
        assert user["language_code"] == "ru"

    @pytest.mark.asyncio
    async def test_upsert_updates_existing(self, patch_db):
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        await db.upsert_user(100, "alice_new", "Alice Updated")
        user = await db.get_user(100)
        assert user["username"] == "alice_new"
        assert user["first_name"] == "Alice Updated"

    @pytest.mark.asyncio
    async def test_get_nonexistent_user(self, patch_db):
        db = patch_db
        user = await db.get_user(999)
        assert user is None


# ══════════════════════════════════════════════════════════════
#  Subscriptions
# ══════════════════════════════════════════════════════════════

class TestSubscriptions:
    @pytest.mark.asyncio
    async def test_create_free_subscription(self, patch_db):
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        await db.create_free_subscription(100)
        sub = await db.get_active_subscription(100)
        assert sub["plan"] == "free"
        assert sub["is_active"] == 1
        assert sub["expires_at"] is None

    @pytest.mark.asyncio
    async def test_free_subscription_idempotent(self, patch_db):
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        await db.create_free_subscription(100)
        await db.create_free_subscription(100)  # second call
        subs = [s for s in db.subscriptions if s["user_id"] == 100 and s["plan"] == "free"]
        assert len(subs) == 1

    @pytest.mark.asyncio
    async def test_paid_subscription_deactivates_old(self, patch_db):
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        await db.create_free_subscription(100)
        await db.create_subscription(100, "basic", "monthly", 30)

        active = await db.get_active_subscription(100)
        assert active["plan"] == "basic"

        # Free sub should be deactivated
        free_subs = [s for s in db.subscriptions
                     if s["user_id"] == 100 and s["plan"] == "free" and s["is_active"] == 1]
        assert len(free_subs) == 0

    @pytest.mark.asyncio
    async def test_upgrade_basic_to_premium(self, patch_db):
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        await db.create_subscription(100, "basic", "monthly", 30)
        await db.create_subscription(100, "premium", "yearly", 365)

        active = await db.get_active_subscription(100)
        assert active["plan"] == "premium"
        assert active["period"] == "yearly"

    @pytest.mark.asyncio
    async def test_subscription_expiry(self, patch_db):
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        # Create already-expired subscription
        db._sub_id += 1
        db.subscriptions.append({
            "id": db._sub_id, "user_id": 100, "plan": "basic",
            "period": "monthly", "started_at": time.time() - 86400 * 31,
            "expires_at": time.time() - 1, "is_active": 1,
            "reminder_30d": 0, "reminder_7d": 0, "reminder_3d": 0, "reminder_1d": 0,
        })

        await db.expire_subscriptions()
        sub = await db.get_active_subscription(100)
        assert sub is None  # expired and deactivated


# ══════════════════════════════════════════════════════════════
#  Monitored addresses
# ══════════════════════════════════════════════════════════════

class TestMonitoredAddresses:
    @pytest.mark.asyncio
    async def test_add_and_list(self, patch_db):
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        addr = await db.add_monitored_address(100, "0xAddr1", "ETH", label="My ETH")
        assert addr["address"] == "0xAddr1"
        assert addr["chain"] == "ETH"

        addrs = await db.get_user_addresses(100)
        assert len(addrs) == 1

    @pytest.mark.asyncio
    async def test_add_duplicate_returns_existing(self, patch_db):
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        addr1 = await db.add_monitored_address(100, "0xAddr1", "ETH")
        addr2 = await db.add_monitored_address(100, "0xAddr1", "ETH")
        assert addr1["id"] == addr2["id"]
        assert await db.count_user_addresses(100) == 1

    @pytest.mark.asyncio
    async def test_same_address_different_tokens(self, patch_db):
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        a1 = await db.add_monitored_address(100, "TAddr", "TRON")  # native TRX
        a2 = await db.add_monitored_address(100, "TAddr", "TRON",
                                             token_contract="TR7N", token_symbol="USDT")
        assert a1["id"] != a2["id"]
        assert await db.count_user_addresses(100) == 2

    @pytest.mark.asyncio
    async def test_remove_address(self, patch_db):
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        addr = await db.add_monitored_address(100, "0xAddr1", "ETH")
        await db.remove_monitored_address(addr["id"], 100)

        addrs = await db.get_user_addresses(100)
        assert len(addrs) == 0
        assert await db.count_user_addresses(100) == 0

    @pytest.mark.asyncio
    async def test_remove_wrong_user_noop(self, patch_db):
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        await db.upsert_user(200, "bob", "Bob")
        addr = await db.add_monitored_address(100, "0xAddr1", "ETH")

        await db.remove_monitored_address(addr["id"], 200)  # wrong user
        assert await db.count_user_addresses(100) == 1

    @pytest.mark.asyncio
    async def test_update_monitor_state(self, patch_db):
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        addr = await db.add_monitored_address(100, "0xAddr1", "ETH")

        await db.update_monitor_state(addr["id"], balance="5.5", last_tx_hash="0xhash")

        addrs = await db.get_user_addresses(100)
        assert addrs[0]["last_balance"] == "5.5"
        assert addrs[0]["last_tx_hash"] == "0xhash"

    @pytest.mark.asyncio
    async def test_get_all_active_monitors(self, patch_db):
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        await db.upsert_user(200, "bob", "Bob")
        await db.create_free_subscription(100)
        await db.create_free_subscription(200)
        await db.add_monitored_address(100, "0xAlice", "ETH")
        await db.add_monitored_address(200, "TBob", "TRON")

        monitors = await db.get_all_active_monitors()
        assert len(monitors) == 2


# ══════════════════════════════════════════════════════════════
#  Transaction log
# ══════════════════════════════════════════════════════════════

class TestTransactionLog:
    @pytest.mark.asyncio
    async def test_log_new_transaction(self, patch_db):
        db = patch_db
        was_new = await db.log_transaction(
            monitor_id=1, tx_hash="0xabc", direction="in",
            value="1.0", value_usd=3000.0, token_symbol="ETH",
            from_addr="0xSender", to_addr="0xRecv",
        )
        assert was_new is True

    @pytest.mark.asyncio
    async def test_duplicate_transaction(self, patch_db):
        db = patch_db
        await db.log_transaction(1, "0xabc", "in", "1.0", 3000.0, "ETH", "0xS", "0xR")
        was_new = await db.log_transaction(1, "0xabc", "in", "1.0", 3000.0, "ETH", "0xS", "0xR")
        assert was_new is False

    @pytest.mark.asyncio
    async def test_same_hash_different_direction(self, patch_db):
        """Same tx_hash but different direction should be separate entries."""
        db = patch_db
        r1 = await db.log_transaction(1, "0xabc", "in", "1.0", 3000.0, "ETH", "0xS", "0xR")
        r2 = await db.log_transaction(1, "0xabc", "out", "0.5", 1500.0, "ETH", "0xR", "0xS")
        assert r1 is True
        assert r2 is True


# ══════════════════════════════════════════════════════════════
#  Payments & HD indices
# ══════════════════════════════════════════════════════════════

class TestPaymentsAndHD:
    @pytest.mark.asyncio
    async def test_hd_index_auto_increment(self, patch_db):
        db = patch_db
        idx0 = await db.get_next_hd_index("BTC")
        idx1 = await db.get_next_hd_index("BTC")
        idx2 = await db.get_next_hd_index("BTC")
        assert idx0 == 0
        assert idx1 == 1
        assert idx2 == 2

    @pytest.mark.asyncio
    async def test_hd_index_per_chain(self, patch_db):
        db = patch_db
        btc0 = await db.get_next_hd_index("BTC")
        eth0 = await db.get_next_hd_index("ETH")
        tron0 = await db.get_next_hd_index("TRON")
        assert btc0 == 0
        assert eth0 == 0
        assert tron0 == 0

    @pytest.mark.asyncio
    async def test_create_and_get_pending_payment(self, patch_db):
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        pay = await db.create_payment(100, "basic", "monthly", 5.0,
                                       "BTC", "1Addr", "0.0001", 0)
        assert pay["status"] == "pending"
        assert pay["user_id"] == 100

        pending = await db.get_pending_payments()
        assert len(pending) == 1
        assert pending[0]["id"] == pay["id"]

    @pytest.mark.asyncio
    async def test_confirm_payment(self, patch_db):
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        pay = await db.create_payment(100, "basic", "monthly", 5.0,
                                       "BTC", "1Addr", "0.0001", 0)
        await db.confirm_payment(pay["id"], "txhash_123")

        # Should no longer be in pending
        pending = await db.get_pending_payments()
        assert len(pending) == 0

        # Check payment was updated
        payment = next(p for p in db.payments if p["id"] == pay["id"])
        assert payment["status"] == "confirmed"
        assert payment["tx_hash"] == "txhash_123"

    @pytest.mark.asyncio
    async def test_expire_old_payments(self, patch_db):
        db = patch_db
        await db.upsert_user(100, "alice", "Alice")
        await db.create_payment(100, "basic", "monthly", 5.0,
                                 "BTC", "1Addr", "0.0001", 0, expires_in=-100)

        await db.expire_old_payments()
        assert db.payments[0]["status"] == "expired"
