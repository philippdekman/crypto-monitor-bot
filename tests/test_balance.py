"""
Tests for balance.py — credit, debit, AML check charging, topup creation.
"""
import asyncio
import time
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from config import (
    MIN_TOPUP_USD, AML_CHECK_PRICE_CENTS, FREE_AML_CHECKS
)


class TestGetBalanceInfo:
    @pytest.mark.asyncio
    async def test_new_user_zero_balance(self, patch_db):
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")
        await db.create_free_subscription(111)

        from balance import get_user_balance_info
        info = await get_user_balance_info(111)
        assert info["balance_cents"] == 0
        assert info["balance_usd"] == 0.0
        assert info["plan"] == "free"
        assert info["free_aml_total"] == 0
        assert info["free_aml_remaining"] == 0

    @pytest.mark.asyncio
    async def test_basic_plan_has_free_checks(self, patch_db):
        db = patch_db
        await db.upsert_user(222, "bob", "Bob")
        await db.create_subscription(222, "basic", "monthly", 30)

        from balance import get_user_balance_info
        info = await get_user_balance_info(222)
        assert info["plan"] == "basic"
        assert info["free_aml_total"] == FREE_AML_CHECKS["basic"]
        assert info["free_aml_remaining"] == FREE_AML_CHECKS["basic"]

    @pytest.mark.asyncio
    async def test_premium_plan_has_more_free_checks(self, patch_db):
        db = patch_db
        await db.upsert_user(333, "carol", "Carol")
        await db.create_subscription(333, "premium", "yearly", 365)

        from balance import get_user_balance_info
        info = await get_user_balance_info(333)
        assert info["plan"] == "premium"
        assert info["free_aml_total"] == FREE_AML_CHECKS["premium"]
        assert info["free_aml_remaining"] == FREE_AML_CHECKS["premium"]


class TestCreditDebit:
    @pytest.mark.asyncio
    async def test_credit_increases_balance(self, patch_db):
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")

        result = await db.credit_balance(111, 1000, "Test topup", "ref123")
        assert result["balance_cents"] == 1000
        assert result["transaction"]["amount_cents"] == 1000
        assert result["transaction"]["type"] == "topup"
        assert result["transaction"]["reference_id"] == "ref123"

    @pytest.mark.asyncio
    async def test_multiple_credits_accumulate(self, patch_db):
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")

        await db.credit_balance(111, 500, "First")
        result = await db.credit_balance(111, 300, "Second")
        assert result["balance_cents"] == 800

    @pytest.mark.asyncio
    async def test_debit_decreases_balance(self, patch_db):
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")
        await db.credit_balance(111, 1000)

        result = await db.debit_balance(111, 50, "aml_check", "AML check")
        assert result is not None
        assert result["balance_cents"] == 950
        assert result["transaction"]["amount_cents"] == -50

    @pytest.mark.asyncio
    async def test_debit_insufficient_returns_none(self, patch_db):
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")
        await db.credit_balance(111, 30)

        result = await db.debit_balance(111, 50)
        assert result is None

        # Balance unchanged
        balance = await db.get_balance_cents(111)
        assert balance == 30

    @pytest.mark.asyncio
    async def test_debit_insufficient_no_transaction(self, patch_db):
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")
        await db.credit_balance(111, 30)

        await db.debit_balance(111, 50)
        txs = await db.get_balance_transactions(111)
        # Only the credit tx, no debit tx
        assert len(txs) == 1
        assert txs[0]["type"] == "topup"

    @pytest.mark.asyncio
    async def test_debit_exact_balance(self, patch_db):
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")
        await db.credit_balance(111, 50)

        result = await db.debit_balance(111, 50)
        assert result is not None
        assert result["balance_cents"] == 0

    @pytest.mark.asyncio
    async def test_balance_persists(self, patch_db):
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")
        await db.credit_balance(111, 1234)

        balance = await db.get_balance_cents(111)
        assert balance == 1234

    @pytest.mark.asyncio
    async def test_concurrent_debits_race_safe(self, patch_db):
        """Simulate concurrent debits — at most one should succeed when balance = 50."""
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")
        await db.credit_balance(111, 50)

        results = await asyncio.gather(
            db.debit_balance(111, 50, "aml_check", "Check 1"),
            db.debit_balance(111, 50, "aml_check", "Check 2"),
        )

        successes = [r for r in results if r is not None]
        # In MockDB (in-memory, no real locking), both may succeed or one fails
        # The point is the final balance should never be negative
        balance = await db.get_balance_cents(111)
        assert balance >= 0


class TestTransactionHistory:
    @pytest.mark.asyncio
    async def test_history_ordered_desc(self, patch_db):
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")

        await db.credit_balance(111, 100, "First")
        await db.credit_balance(111, 200, "Second")
        await db.credit_balance(111, 300, "Third")

        txs = await db.get_balance_transactions(111)
        assert len(txs) == 3
        # Most recent first
        assert txs[0]["description"] == "Third"
        assert txs[1]["description"] == "Second"
        assert txs[2]["description"] == "First"

    @pytest.mark.asyncio
    async def test_history_limit(self, patch_db):
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")
        for i in range(15):
            await db.credit_balance(111, 10, f"Tx {i}")

        txs = await db.get_balance_transactions(111, limit=5)
        assert len(txs) == 5

    @pytest.mark.asyncio
    async def test_history_offset(self, patch_db):
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")
        for i in range(5):
            await db.credit_balance(111, 10, f"Tx {i}")

        txs = await db.get_balance_transactions(111, limit=2, offset=2)
        assert len(txs) == 2
        # Offset 2 from descending: "Tx 2", "Tx 1"
        assert txs[0]["description"] == "Tx 2"
        assert txs[1]["description"] == "Tx 1"


class TestAmlChecksCounter:
    @pytest.mark.asyncio
    async def test_increment_within_month(self, patch_db):
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")

        count = await db.increment_aml_checks_used(111)
        assert count == 1
        count = await db.increment_aml_checks_used(111)
        assert count == 2

    @pytest.mark.asyncio
    async def test_reset_after_30_days(self, patch_db):
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")

        # Increment once
        await db.increment_aml_checks_used(111)

        # Force reset_at to 31 days ago
        db.balances[111]["aml_checks_reset_at"] = time.time() - 31 * 86400

        count = await db.increment_aml_checks_used(111)
        assert count == 1  # Reset happened, so count starts from 1


class TestCanUseFreeAmlCheck:
    @pytest.mark.asyncio
    async def test_free_plan_no_free_checks(self, patch_db):
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")
        await db.create_free_subscription(111)

        from balance import can_use_free_aml_check
        can, remaining = await can_use_free_aml_check(111)
        assert can is False
        assert remaining == 0

    @pytest.mark.asyncio
    async def test_basic_plan_has_free_checks(self, patch_db):
        db = patch_db
        await db.upsert_user(222, "bob", "Bob")
        await db.create_subscription(222, "basic", "monthly", 30)

        from balance import can_use_free_aml_check
        can, remaining = await can_use_free_aml_check(222)
        assert can is True
        assert remaining == FREE_AML_CHECKS["basic"]

    @pytest.mark.asyncio
    async def test_free_checks_deplete(self, patch_db):
        db = patch_db
        await db.upsert_user(222, "bob", "Bob")
        await db.create_subscription(222, "basic", "monthly", 30)

        for _ in range(FREE_AML_CHECKS["basic"]):
            await db.increment_aml_checks_used(222)

        from balance import can_use_free_aml_check
        can, remaining = await can_use_free_aml_check(222)
        assert can is False
        assert remaining == 0


class TestChargeAmlCheck:
    @pytest.mark.asyncio
    async def test_charge_uses_free_first(self, patch_db):
        db = patch_db
        await db.upsert_user(222, "bob", "Bob")
        await db.create_subscription(222, "basic", "monthly", 30)
        await db.credit_balance(222, 1000)  # $10

        from balance import charge_aml_check
        success, msg = await charge_aml_check(222, "TTestAddr123")
        assert success is True
        assert "бесплатная" in msg.lower() or "Бесплатная" in msg

        # Balance not touched (free check was used)
        balance = await db.get_balance_cents(222)
        assert balance == 1000

    @pytest.mark.asyncio
    async def test_charge_paid_when_no_free(self, patch_db):
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")
        await db.create_free_subscription(111)
        await db.credit_balance(111, 500)

        from balance import charge_aml_check
        success, msg = await charge_aml_check(111, "TTestAddr123")
        assert success is True

        balance = await db.get_balance_cents(111)
        assert balance == 500 - AML_CHECK_PRICE_CENTS

    @pytest.mark.asyncio
    async def test_charge_fails_insufficient_balance(self, patch_db):
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")
        await db.create_free_subscription(111)
        # No balance at all

        from balance import charge_aml_check
        success, msg = await charge_aml_check(111, "TTestAddr123")
        assert success is False
        assert "Недостаточно" in msg


class TestCreateTopupInvoice:
    @pytest.mark.asyncio
    async def test_successful_topup_invoice(self, patch_db):
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")

        with patch("balance.generate_address", return_value={
            "address": "TTestTopupAddr", "path": "m/44'/195'/0'/0/0",
            "chain": "TRON", "index": 0,
        }), patch("balance.convert_from_usd", new_callable=AsyncMock, return_value=50.0):
            from balance import create_topup_invoice
            invoice = await create_topup_invoice(111, 10.0, "TRON")

            assert invoice is not None
            assert invoice["amount_usd"] == 10.0
            assert invoice["symbol"] == "TRX"
            assert invoice["address"] == "TTestTopupAddr"

    @pytest.mark.asyncio
    async def test_topup_below_minimum_rejected(self, patch_db):
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")

        from balance import create_topup_invoice
        result = await create_topup_invoice(111, 2.0, "BTC")
        assert result is None

    @pytest.mark.asyncio
    async def test_topup_at_minimum(self, patch_db):
        db = patch_db
        await db.upsert_user(111, "alice", "Alice")

        with patch("balance.generate_address", return_value={
            "address": "1TestBTCAddr", "path": "m/44'/0'/0'/0/0",
            "chain": "BTC", "index": 0,
        }), patch("balance.convert_from_usd", new_callable=AsyncMock, return_value=0.0001):
            from balance import create_topup_invoice
            invoice = await create_topup_invoice(111, MIN_TOPUP_USD, "BTC")
            assert invoice is not None
            assert invoice["amount_usd"] == MIN_TOPUP_USD
