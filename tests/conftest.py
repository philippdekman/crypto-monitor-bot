"""
Shared fixtures for all tests.
Uses real asyncpg against a PostgreSQL instance when DATABASE_URL is set,
otherwise uses a mock DB layer.
"""
import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# Ensure project root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Async event loop ─────────────────────────────────────────

@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ── Mock database layer ──────────────────────────────────────

class MockDB:
    """In-memory mock of database.py for unit tests (no PostgreSQL needed)."""

    def __init__(self):
        self.users = {}
        self.subscriptions = []
        self.addresses = []
        self.payments = []
        self.transactions = []
        self.hd_indices = {}
        self.balances = {}  # user_id -> {balance_cents, aml_checks_used_this_month, aml_checks_reset_at}
        self.balance_transactions = []
        self._sub_id = 0
        self._addr_id = 0
        self._pay_id = 0
        self._tx_id = 0
        self._btx_id = 0

    async def upsert_user(self, user_id, username=None, first_name=None, language_code="en"):
        self.users[user_id] = {
            "user_id": user_id, "username": username,
            "first_name": first_name, "language_code": language_code,
            "created_at": time.time(), "is_blocked": 0,
        }

    async def get_user(self, user_id):
        return self.users.get(user_id)

    async def get_active_subscription(self, user_id):
        for s in reversed(self.subscriptions):
            if s["user_id"] == user_id and s["is_active"] == 1:
                return s
        return None

    async def create_subscription(self, user_id, plan, period, duration_days):
        now = time.time()
        # Deactivate old subs
        for s in self.subscriptions:
            if s["user_id"] == user_id and s["is_active"] == 1:
                s["is_active"] = 0
        self._sub_id += 1
        sub = {
            "id": self._sub_id, "user_id": user_id, "plan": plan,
            "period": period, "started_at": now,
            "expires_at": now + duration_days * 86400, "is_active": 1,
            "reminder_30d": 0, "reminder_7d": 0, "reminder_3d": 0, "reminder_1d": 0,
        }
        self.subscriptions.append(sub)
        return sub

    async def create_free_subscription(self, user_id):
        for s in self.subscriptions:
            if s["user_id"] == user_id and s["plan"] == "free":
                return
        self._sub_id += 1
        self.subscriptions.append({
            "id": self._sub_id, "user_id": user_id, "plan": "free",
            "period": None, "started_at": time.time(),
            "expires_at": None, "is_active": 1,
            "reminder_30d": 0, "reminder_7d": 0, "reminder_3d": 0, "reminder_1d": 0,
        })

    async def count_user_addresses(self, user_id):
        return sum(1 for a in self.addresses
                   if a["user_id"] == user_id and a["is_active"] == 1)

    async def add_monitored_address(self, user_id, address, chain,
                                     token_contract=None, token_symbol=None, label=None):
        # Check uniqueness
        for a in self.addresses:
            if (a["user_id"] == user_id and a["address"] == address
                    and a["chain"] == chain and a["token_contract"] == token_contract
                    and a["is_active"] == 1):
                return a
        self._addr_id += 1
        addr = {
            "id": self._addr_id, "user_id": user_id, "address": address,
            "chain": chain, "token_contract": token_contract,
            "token_symbol": token_symbol, "label": label,
            "last_balance": None, "last_checked_at": None,
            "last_tx_hash": None, "created_at": time.time(), "is_active": 1,
        }
        self.addresses.append(addr)
        return addr

    async def get_user_addresses(self, user_id):
        return [a for a in self.addresses
                if a["user_id"] == user_id and a["is_active"] == 1]

    async def remove_monitored_address(self, monitor_id, user_id):
        for a in self.addresses:
            if a["id"] == monitor_id and a["user_id"] == user_id:
                a["is_active"] = 0

    async def get_all_active_monitors(self):
        result = []
        for a in self.addresses:
            if a["is_active"] == 1:
                sub = await self.get_active_subscription(a["user_id"])
                if sub:
                    a_copy = dict(a)
                    a_copy["plan"] = sub["plan"]
                    result.append(a_copy)
        return result

    async def update_monitor_state(self, monitor_id, balance, last_tx_hash=None):
        for a in self.addresses:
            if a["id"] == monitor_id:
                a["last_balance"] = balance
                a["last_checked_at"] = time.time()
                if last_tx_hash:
                    a["last_tx_hash"] = last_tx_hash

    async def log_transaction(self, monitor_id, tx_hash, direction, value,
                               value_usd, token_symbol, from_addr, to_addr,
                               block_number=None, timestamp=None):
        # Check duplicate
        for t in self.transactions:
            if t["monitor_id"] == monitor_id and t["tx_hash"] == tx_hash and t["direction"] == direction:
                return False
        self._tx_id += 1
        self.transactions.append({
            "id": self._tx_id, "monitor_id": monitor_id, "tx_hash": tx_hash,
            "direction": direction, "value": value, "value_usd": value_usd,
            "token_symbol": token_symbol, "from_addr": from_addr,
            "to_addr": to_addr, "block_number": block_number,
            "timestamp": timestamp, "notified": 0,
        })
        return True

    async def get_next_hd_index(self, chain):
        idx = self.hd_indices.get(chain, 0)
        self.hd_indices[chain] = idx + 1
        return idx

    async def create_payment(self, user_id, plan, period, amount_usd,
                              pay_chain, pay_address, pay_amount,
                              derivation_idx, expires_in=3600):
        now = time.time()
        self._pay_id += 1
        pay = {
            "id": self._pay_id, "user_id": user_id, "plan": plan,
            "period": period, "amount_usd": amount_usd,
            "pay_chain": pay_chain, "pay_address": pay_address,
            "pay_amount": pay_amount, "derivation_idx": derivation_idx,
            "status": "pending", "tx_hash": None,
            "created_at": now, "confirmed_at": None,
            "expires_at": now + expires_in,
            "payment_kind": "subscription",
        }
        self.payments.append(pay)
        return pay

    async def get_pending_payments(self):
        now = time.time()
        return [p for p in self.payments
                if p["status"] == "pending" and p["expires_at"] > now]

    async def confirm_payment(self, payment_id, tx_hash):
        for p in self.payments:
            if p["id"] == payment_id:
                p["status"] = "confirmed"
                p["tx_hash"] = tx_hash
                p["confirmed_at"] = time.time()

    async def expire_old_payments(self):
        now = time.time()
        for p in self.payments:
            if p["status"] == "pending" and p["expires_at"] <= now:
                p["status"] = "expired"

    async def get_expiring_subscriptions(self, days_before):
        return []

    async def mark_reminder_sent(self, sub_id, days_before):
        pass

    async def expire_subscriptions(self):
        now = time.time()
        for s in self.subscriptions:
            if s["is_active"] == 1 and s["plan"] != "free" and s["expires_at"] and s["expires_at"] <= now:
                s["is_active"] = 0

    async def get_address_limit(self, user_id):
        from config import FREE_ADDRESS_LIMIT, PRICING
        sub = await self.get_active_subscription(user_id)
        if not sub or sub["plan"] == "free":
            return FREE_ADDRESS_LIMIT
        return PRICING.get(sub["plan"], {}).get("max_addresses", FREE_ADDRESS_LIMIT)

    def _ensure_balance(self, user_id):
        if user_id not in self.balances:
            self.balances[user_id] = {
                "user_id": user_id,
                "balance_cents": 0,
                "aml_checks_used_this_month": 0,
                "aml_checks_reset_at": time.time(),
                "updated_at": time.time(),
            }
        return self.balances[user_id]

    async def get_balance_cents(self, user_id):
        b = self._ensure_balance(user_id)
        return b["balance_cents"]

    async def get_balance_info(self, user_id):
        return dict(self._ensure_balance(user_id))

    async def credit_balance(self, user_id, cents, description=None,
                             reference_id=None, tx_type="topup"):
        b = self._ensure_balance(user_id)
        b["balance_cents"] += cents
        b["updated_at"] = time.time()
        self._btx_id += 1
        tx = {
            "id": self._btx_id, "user_id": user_id, "type": tx_type,
            "amount_cents": cents, "balance_after_cents": b["balance_cents"],
            "description": description, "reference_id": reference_id,
            "created_at": time.time(),
        }
        self.balance_transactions.append(tx)
        return {"balance_cents": b["balance_cents"], "transaction": tx}

    async def debit_balance(self, user_id, cents, tx_type="aml_check",
                            description=None, reference_id=None):
        b = self._ensure_balance(user_id)
        if b["balance_cents"] < cents:
            return None
        b["balance_cents"] -= cents
        b["updated_at"] = time.time()
        self._btx_id += 1
        tx = {
            "id": self._btx_id, "user_id": user_id, "type": tx_type,
            "amount_cents": -cents, "balance_after_cents": b["balance_cents"],
            "description": description, "reference_id": reference_id,
            "created_at": time.time(),
        }
        self.balance_transactions.append(tx)
        return {"balance_cents": b["balance_cents"], "transaction": tx}

    async def increment_aml_checks_used(self, user_id):
        b = self._ensure_balance(user_id)
        now = time.time()
        if (now - b["aml_checks_reset_at"]) > 30 * 86400:
            b["aml_checks_used_this_month"] = 1
            b["aml_checks_reset_at"] = now
        else:
            b["aml_checks_used_this_month"] += 1
        b["updated_at"] = now
        return b["aml_checks_used_this_month"]

    async def get_balance_transactions(self, user_id, limit=10, offset=0):
        user_txs = [t for t in self.balance_transactions if t["user_id"] == user_id]
        user_txs.sort(key=lambda x: x["created_at"], reverse=True)
        return user_txs[offset:offset + limit]

    async def create_topup_payment(self, user_id, amount_usd, pay_chain,
                                    pay_address, pay_amount, derivation_idx,
                                    expires_in=3600):
        now = time.time()
        self._pay_id += 1
        pay = {
            "id": self._pay_id, "user_id": user_id, "plan": "topup",
            "period": "one-time", "amount_usd": amount_usd,
            "pay_chain": pay_chain, "pay_address": pay_address,
            "pay_amount": pay_amount, "derivation_idx": derivation_idx,
            "status": "pending", "tx_hash": None,
            "created_at": now, "confirmed_at": None,
            "expires_at": now + expires_in,
            "payment_kind": "balance_topup",
        }
        self.payments.append(pay)
        return pay

    async def get_pool(self):
        return MagicMock()


@pytest.fixture(autouse=True)
def clear_price_cache():
    """Clear the prices module cache before each test to prevent pollution."""
    from prices import _price_cache
    _price_cache.clear()
    yield
    _price_cache.clear()


@pytest.fixture
def mock_db():
    """Return a fresh MockDB instance and patch the database module."""
    return MockDB()


@pytest.fixture
def patch_db(mock_db):
    """Patch 'database' module globally with MockDB."""
    import database as db_module
    original_attrs = {}

    for attr_name in dir(mock_db):
        if not attr_name.startswith("_") and hasattr(db_module, attr_name):
            original_attrs[attr_name] = getattr(db_module, attr_name)
            setattr(db_module, attr_name, getattr(mock_db, attr_name))

    yield mock_db

    for attr_name, original in original_attrs.items():
        setattr(db_module, attr_name, original)
