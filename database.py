"""
PostgreSQL database layer (asyncpg) — users, wallets, subscriptions, payments, monitored addresses.
Designed for Railway PostgreSQL add-on (DATABASE_URL env var).
"""
import asyncio
import logging
import time
from config import DATABASE_URL

import asyncpg

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id         BIGINT PRIMARY KEY,
            username        TEXT,
            first_name      TEXT,
            language_code   TEXT DEFAULT 'en',
            created_at      DOUBLE PRECISION DEFAULT extract(epoch from now()),
            is_blocked      INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            id              SERIAL PRIMARY KEY,
            user_id         BIGINT NOT NULL REFERENCES users(user_id),
            plan            TEXT NOT NULL DEFAULT 'free',
            period          TEXT,
            started_at      DOUBLE PRECISION,
            expires_at      DOUBLE PRECISION,
            is_active       INTEGER DEFAULT 1,
            reminder_30d    INTEGER DEFAULT 0,
            reminder_7d     INTEGER DEFAULT 0,
            reminder_3d     INTEGER DEFAULT 0,
            reminder_1d     INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS monitored_addresses (
            id              SERIAL PRIMARY KEY,
            user_id         BIGINT NOT NULL REFERENCES users(user_id),
            address         TEXT NOT NULL,
            chain           TEXT NOT NULL,
            token_contract  TEXT,
            token_symbol    TEXT,
            label           TEXT,
            last_balance    TEXT,
            last_checked_at DOUBLE PRECISION,
            last_tx_hash    TEXT,
            created_at      DOUBLE PRECISION DEFAULT extract(epoch from now()),
            is_active       INTEGER DEFAULT 1,
            UNIQUE(user_id, address, chain, token_contract)
        );

        CREATE TABLE IF NOT EXISTS transactions_log (
            id              SERIAL PRIMARY KEY,
            monitor_id      INTEGER NOT NULL REFERENCES monitored_addresses(id),
            tx_hash         TEXT NOT NULL,
            direction       TEXT NOT NULL,
            value           TEXT NOT NULL,
            value_usd       DOUBLE PRECISION,
            token_symbol    TEXT,
            from_addr       TEXT,
            to_addr         TEXT,
            block_number    BIGINT,
            timestamp       DOUBLE PRECISION,
            notified        INTEGER DEFAULT 0,
            UNIQUE(monitor_id, tx_hash, direction)
        );

        CREATE TABLE IF NOT EXISTS payments (
            id              SERIAL PRIMARY KEY,
            user_id         BIGINT NOT NULL REFERENCES users(user_id),
            plan            TEXT NOT NULL,
            period          TEXT NOT NULL,
            amount_usd      DOUBLE PRECISION NOT NULL,
            pay_chain       TEXT NOT NULL,
            pay_address     TEXT NOT NULL,
            pay_amount      TEXT,
            derivation_idx  INTEGER,
            status          TEXT DEFAULT 'pending',
            tx_hash         TEXT,
            created_at      DOUBLE PRECISION DEFAULT extract(epoch from now()),
            confirmed_at    DOUBLE PRECISION,
            expires_at      DOUBLE PRECISION
        );

        CREATE TABLE IF NOT EXISTS hd_wallet_index (
            chain           TEXT PRIMARY KEY,
            next_index      INTEGER DEFAULT 0
        );
        """)

        # Create indexes if not exist
        await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_mon_user ON monitored_addresses(user_id, is_active);
        CREATE INDEX IF NOT EXISTS idx_mon_active ON monitored_addresses(is_active);
        CREATE INDEX IF NOT EXISTS idx_sub_user ON subscriptions(user_id, is_active);
        CREATE INDEX IF NOT EXISTS idx_pay_status ON payments(status);
        CREATE INDEX IF NOT EXISTS idx_txlog_monitor ON transactions_log(monitor_id);
        """)

    logger.info("Database initialized (PostgreSQL)")


# ── Helper to convert asyncpg.Record to dict ────────────────

def _rec(record) -> dict | None:
    if record is None:
        return None
    return dict(record)


def _recs(records) -> list[dict]:
    return [dict(r) for r in records]


# ── User operations ──────────────────────────────────────────

async def upsert_user(user_id: int, username: str = None, first_name: str = None, language_code: str = "en"):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, username, first_name, language_code)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT(user_id) DO UPDATE SET
                username=EXCLUDED.username,
                first_name=EXCLUDED.first_name,
                language_code=EXCLUDED.language_code
        """, user_id, username, first_name, language_code)


async def get_user(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
        return _rec(row)


# ── Subscription operations ──────────────────────────────────

async def get_active_subscription(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT * FROM subscriptions
            WHERE user_id=$1 AND is_active=1
            ORDER BY expires_at DESC NULLS LAST LIMIT 1
        """, user_id)
        return _rec(row)


async def create_subscription(user_id: int, plan: str, period: str, duration_days: int):
    now = time.time()
    expires = now + duration_days * 86400
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE subscriptions SET is_active=0 WHERE user_id=$1 AND is_active=1", user_id)
        await conn.execute("""
            INSERT INTO subscriptions (user_id, plan, period, started_at, expires_at, is_active)
            VALUES ($1, $2, $3, $4, $5, 1)
        """, user_id, plan, period, now, expires)


async def create_free_subscription(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id FROM subscriptions WHERE user_id=$1 AND plan='free'", user_id
        )
        if not existing:
            await conn.execute("""
                INSERT INTO subscriptions (user_id, plan, period, started_at, expires_at, is_active)
                VALUES ($1, 'free', NULL, $2, NULL, 1)
            """, user_id, time.time())


async def get_expiring_subscriptions(days_before: int):
    now = time.time()
    target = now + days_before * 86400
    reminder_col = f"reminder_{days_before}d"
    valid_cols = ["reminder_30d", "reminder_7d", "reminder_3d", "reminder_1d"]
    if reminder_col not in valid_cols:
        return []
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT s.*, u.first_name, u.username FROM subscriptions s
            JOIN users u ON s.user_id = u.user_id
            WHERE s.is_active=1 AND s.plan != 'free'
              AND s.expires_at <= $1
              AND s.expires_at > $2
              AND s.{reminder_col} = 0
        """, target, now)
        return _recs(rows)


async def mark_reminder_sent(sub_id: int, days_before: int):
    reminder_col = f"reminder_{days_before}d"
    valid_cols = ["reminder_30d", "reminder_7d", "reminder_3d", "reminder_1d"]
    if reminder_col not in valid_cols:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(f"UPDATE subscriptions SET {reminder_col}=1 WHERE id=$1", sub_id)


async def expire_subscriptions():
    now = time.time()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE subscriptions SET is_active=0
            WHERE is_active=1 AND plan != 'free' AND expires_at <= $1
        """, now)


# ── Monitored address operations ─────────────────────────────

async def count_user_addresses(user_id: int) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) as cnt FROM monitored_addresses WHERE user_id=$1 AND is_active=1",
            user_id
        )
        return row["cnt"]


async def get_address_limit(user_id: int) -> int:
    from config import FREE_ADDRESS_LIMIT, PRICING
    sub = await get_active_subscription(user_id)
    if not sub or sub["plan"] == "free":
        return FREE_ADDRESS_LIMIT
    return PRICING.get(sub["plan"], {}).get("max_addresses", FREE_ADDRESS_LIMIT)


async def add_monitored_address(user_id: int, address: str, chain: str,
                                token_contract: str = None, token_symbol: str = None,
                                label: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO monitored_addresses
            (user_id, address, chain, token_contract, token_symbol, label)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT(user_id, address, chain, token_contract) DO NOTHING
        """, user_id, address, chain, token_contract, token_symbol, label)
        if token_contract is None:
            row = await conn.fetchrow(
                "SELECT * FROM monitored_addresses WHERE user_id=$1 AND address=$2 AND chain=$3 AND token_contract IS NULL",
                user_id, address, chain
            )
        else:
            row = await conn.fetchrow(
                "SELECT * FROM monitored_addresses WHERE user_id=$1 AND address=$2 AND chain=$3 AND token_contract=$4",
                user_id, address, chain, token_contract
            )
        return _rec(row)


async def get_user_addresses(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM monitored_addresses WHERE user_id=$1 AND is_active=1 ORDER BY created_at",
            user_id
        )
        return _recs(rows)


async def remove_monitored_address(monitor_id: int, user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE monitored_addresses SET is_active=0 WHERE id=$1 AND user_id=$2",
            monitor_id, user_id
        )


async def get_all_active_monitors():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT ma.*, s.plan FROM monitored_addresses ma
            JOIN subscriptions s ON ma.user_id = s.user_id AND s.is_active=1
            WHERE ma.is_active=1
        """)
        return _recs(rows)


async def update_monitor_state(monitor_id: int, balance: str, last_tx_hash: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE monitored_addresses
            SET last_balance=$1, last_checked_at=$2, last_tx_hash=COALESCE($3, last_tx_hash)
            WHERE id=$4
        """, balance, time.time(), last_tx_hash, monitor_id)


# ── Transaction log ──────────────────────────────────────────

async def log_transaction(monitor_id: int, tx_hash: str, direction: str, value: str,
                          value_usd: float, token_symbol: str, from_addr: str, to_addr: str,
                          block_number: int = None, timestamp: float = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute("""
                INSERT INTO transactions_log
                (monitor_id, tx_hash, direction, value, value_usd, token_symbol,
                 from_addr, to_addr, block_number, timestamp)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """, monitor_id, tx_hash, direction, value, value_usd, token_symbol,
                from_addr, to_addr, block_number, timestamp)
            return True
        except asyncpg.UniqueViolationError:
            return False  # duplicate


async def get_unnotified_transactions(monitor_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM transactions_log
            WHERE monitor_id=$1 AND notified=0
            ORDER BY timestamp
        """, monitor_id)
        return _recs(rows)


async def mark_notified(tx_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE transactions_log SET notified=1 WHERE id=$1", tx_id)


# ── Payment operations ───────────────────────────────────────

async def get_next_hd_index(chain: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT next_index FROM hd_wallet_index WHERE chain=$1", chain)
        if row:
            idx = row["next_index"]
            await conn.execute("UPDATE hd_wallet_index SET next_index=$1 WHERE chain=$2", idx + 1, chain)
        else:
            idx = 0
            await conn.execute("INSERT INTO hd_wallet_index (chain, next_index) VALUES ($1, $2)", chain, 1)
        return idx


async def create_payment(user_id: int, plan: str, period: str, amount_usd: float,
                         pay_chain: str, pay_address: str, pay_amount: str,
                         derivation_idx: int, expires_in: int = 3600):
    now = time.time()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO payments
            (user_id, plan, period, amount_usd, pay_chain, pay_address,
             pay_amount, derivation_idx, status, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending', $9)
            RETURNING *
        """, user_id, plan, period, amount_usd, pay_chain, pay_address,
            pay_amount, derivation_idx, now + expires_in)
        return _rec(row)


async def get_pending_payments():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM payments WHERE status='pending' AND expires_at > $1
        """, time.time())
        return _recs(rows)


async def confirm_payment(payment_id: int, tx_hash: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE payments SET status='confirmed', tx_hash=$1, confirmed_at=$2
            WHERE id=$3
        """, tx_hash, time.time(), payment_id)


async def expire_old_payments():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE payments SET status='expired'
            WHERE status='pending' AND expires_at <= $1
        """, time.time())
