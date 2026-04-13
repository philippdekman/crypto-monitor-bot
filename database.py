"""
SQLite database layer — users, wallets, subscriptions, payments, monitored addresses.
"""
import sqlite3
import os
import time
from contextlib import contextmanager
from config import DATABASE_PATH


def _ensure_dir():
    os.makedirs(os.path.dirname(DATABASE_PATH) or ".", exist_ok=True)


@contextmanager
def get_db():
    _ensure_dir()
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id         INTEGER PRIMARY KEY,
            username        TEXT,
            first_name      TEXT,
            language_code   TEXT DEFAULT 'en',
            created_at      REAL DEFAULT (strftime('%s','now')),
            is_blocked      INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL REFERENCES users(user_id),
            plan            TEXT NOT NULL DEFAULT 'free',   -- free / basic / premium
            period          TEXT,                            -- monthly / yearly
            started_at      REAL,
            expires_at      REAL,
            is_active       INTEGER DEFAULT 1,
            reminder_30d    INTEGER DEFAULT 0,
            reminder_7d     INTEGER DEFAULT 0,
            reminder_3d     INTEGER DEFAULT 0,
            reminder_1d     INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS monitored_addresses (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL REFERENCES users(user_id),
            address         TEXT NOT NULL,
            chain           TEXT NOT NULL,       -- BTC / ETH / TRON / BSC
            token_contract  TEXT,                 -- NULL for native, contract addr for tokens
            token_symbol    TEXT,                 -- e.g. USDT, USDC
            label           TEXT,                 -- user-given label
            last_balance    TEXT,
            last_checked_at REAL,
            last_tx_hash    TEXT,                 -- last known transaction hash
            created_at      REAL DEFAULT (strftime('%s','now')),
            is_active       INTEGER DEFAULT 1,
            UNIQUE(user_id, address, chain, token_contract)
        );

        CREATE TABLE IF NOT EXISTS transactions_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            monitor_id      INTEGER NOT NULL REFERENCES monitored_addresses(id),
            tx_hash         TEXT NOT NULL,
            direction       TEXT NOT NULL,        -- in / out
            value           TEXT NOT NULL,
            value_usd       REAL,
            token_symbol    TEXT,
            from_addr       TEXT,
            to_addr         TEXT,
            block_number    INTEGER,
            timestamp       REAL,
            notified        INTEGER DEFAULT 0,
            UNIQUE(monitor_id, tx_hash, direction)
        );

        CREATE TABLE IF NOT EXISTS payments (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL REFERENCES users(user_id),
            plan            TEXT NOT NULL,
            period          TEXT NOT NULL,
            amount_usd      REAL NOT NULL,
            pay_chain       TEXT NOT NULL,        -- which crypto chain
            pay_address     TEXT NOT NULL,         -- generated HD address
            pay_amount      TEXT,                  -- expected crypto amount
            derivation_idx  INTEGER,               -- BIP44 index
            status          TEXT DEFAULT 'pending', -- pending / confirmed / expired
            tx_hash         TEXT,
            created_at      REAL DEFAULT (strftime('%s','now')),
            confirmed_at    REAL,
            expires_at      REAL                   -- payment window (e.g. 1 hour)
        );

        CREATE TABLE IF NOT EXISTS hd_wallet_index (
            chain           TEXT PRIMARY KEY,
            next_index      INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_mon_user ON monitored_addresses(user_id, is_active);
        CREATE INDEX IF NOT EXISTS idx_mon_active ON monitored_addresses(is_active);
        CREATE INDEX IF NOT EXISTS idx_sub_user ON subscriptions(user_id, is_active);
        CREATE INDEX IF NOT EXISTS idx_pay_status ON payments(status);
        CREATE INDEX IF NOT EXISTS idx_txlog_monitor ON transactions_log(monitor_id);
        """)


# ── User operations ──────────────────────────────────────────

def upsert_user(user_id: int, username: str = None, first_name: str = None, language_code: str = "en"):
    with get_db() as db:
        db.execute("""
            INSERT INTO users (user_id, username, first_name, language_code)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                language_code=excluded.language_code
        """, (user_id, username, first_name, language_code))


def get_user(user_id: int):
    with get_db() as db:
        return db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


# ── Subscription operations ──────────────────────────────────

def get_active_subscription(user_id: int):
    with get_db() as db:
        return db.execute("""
            SELECT * FROM subscriptions
            WHERE user_id=? AND is_active=1
            ORDER BY expires_at DESC LIMIT 1
        """, (user_id,)).fetchone()


def create_subscription(user_id: int, plan: str, period: str, duration_days: int):
    now = time.time()
    expires = now + duration_days * 86400
    with get_db() as db:
        # deactivate old subscriptions
        db.execute("UPDATE subscriptions SET is_active=0 WHERE user_id=? AND is_active=1", (user_id,))
        db.execute("""
            INSERT INTO subscriptions (user_id, plan, period, started_at, expires_at, is_active)
            VALUES (?, ?, ?, ?, ?, 1)
        """, (user_id, plan, period, now, expires))


def create_free_subscription(user_id: int):
    with get_db() as db:
        existing = db.execute(
            "SELECT id FROM subscriptions WHERE user_id=? AND plan='free'", (user_id,)
        ).fetchone()
        if not existing:
            db.execute("""
                INSERT INTO subscriptions (user_id, plan, period, started_at, expires_at, is_active)
                VALUES (?, 'free', NULL, ?, NULL, 1)
            """, (user_id, time.time()))


def get_expiring_subscriptions(days_before: int):
    """Get subscriptions expiring within `days_before` days."""
    now = time.time()
    target = now + days_before * 86400
    reminder_col = f"reminder_{days_before}d"
    valid_cols = ["reminder_30d", "reminder_7d", "reminder_3d", "reminder_1d"]
    if reminder_col not in valid_cols:
        return []
    with get_db() as db:
        rows = db.execute(f"""
            SELECT s.*, u.first_name, u.username FROM subscriptions s
            JOIN users u ON s.user_id = u.user_id
            WHERE s.is_active=1 AND s.plan != 'free'
              AND s.expires_at <= ?
              AND s.expires_at > ?
              AND s.{reminder_col} = 0
        """, (target, now)).fetchall()
        return rows


def mark_reminder_sent(sub_id: int, days_before: int):
    reminder_col = f"reminder_{days_before}d"
    valid_cols = ["reminder_30d", "reminder_7d", "reminder_3d", "reminder_1d"]
    if reminder_col not in valid_cols:
        return
    with get_db() as db:
        db.execute(f"UPDATE subscriptions SET {reminder_col}=1 WHERE id=?", (sub_id,))


def expire_subscriptions():
    """Deactivate expired subscriptions."""
    now = time.time()
    with get_db() as db:
        db.execute("""
            UPDATE subscriptions SET is_active=0
            WHERE is_active=1 AND plan != 'free' AND expires_at <= ?
        """, (now,))


# ── Monitored address operations ─────────────────────────────

def count_user_addresses(user_id: int) -> int:
    with get_db() as db:
        row = db.execute(
            "SELECT COUNT(*) as cnt FROM monitored_addresses WHERE user_id=? AND is_active=1",
            (user_id,)
        ).fetchone()
        return row["cnt"]


def get_address_limit(user_id: int) -> int:
    from config import FREE_ADDRESS_LIMIT, PRICING
    sub = get_active_subscription(user_id)
    if not sub or sub["plan"] == "free":
        return FREE_ADDRESS_LIMIT
    return PRICING.get(sub["plan"], {}).get("max_addresses", FREE_ADDRESS_LIMIT)


def add_monitored_address(user_id: int, address: str, chain: str,
                          token_contract: str = None, token_symbol: str = None,
                          label: str = None):
    with get_db() as db:
        db.execute("""
            INSERT OR IGNORE INTO monitored_addresses
            (user_id, address, chain, token_contract, token_symbol, label)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, address, chain, token_contract, token_symbol, label))
        return db.execute(
            "SELECT * FROM monitored_addresses WHERE user_id=? AND address=? AND chain=? AND token_contract IS ?",
            (user_id, address, chain, token_contract)
        ).fetchone()


def get_user_addresses(user_id: int):
    with get_db() as db:
        return db.execute(
            "SELECT * FROM monitored_addresses WHERE user_id=? AND is_active=1 ORDER BY created_at",
            (user_id,)
        ).fetchall()


def remove_monitored_address(monitor_id: int, user_id: int):
    with get_db() as db:
        db.execute(
            "UPDATE monitored_addresses SET is_active=0 WHERE id=? AND user_id=?",
            (monitor_id, user_id)
        )


def get_all_active_monitors():
    with get_db() as db:
        return db.execute("""
            SELECT ma.*, s.plan FROM monitored_addresses ma
            JOIN subscriptions s ON ma.user_id = s.user_id AND s.is_active=1
            WHERE ma.is_active=1
        """).fetchall()


def update_monitor_state(monitor_id: int, balance: str, last_tx_hash: str = None):
    with get_db() as db:
        db.execute("""
            UPDATE monitored_addresses
            SET last_balance=?, last_checked_at=?, last_tx_hash=COALESCE(?, last_tx_hash)
            WHERE id=?
        """, (balance, time.time(), last_tx_hash, monitor_id))


# ── Transaction log ──────────────────────────────────────────

def log_transaction(monitor_id: int, tx_hash: str, direction: str, value: str,
                    value_usd: float, token_symbol: str, from_addr: str, to_addr: str,
                    block_number: int = None, timestamp: float = None):
    with get_db() as db:
        try:
            db.execute("""
                INSERT INTO transactions_log
                (monitor_id, tx_hash, direction, value, value_usd, token_symbol,
                 from_addr, to_addr, block_number, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (monitor_id, tx_hash, direction, value, value_usd, token_symbol,
                  from_addr, to_addr, block_number, timestamp))
            return True
        except sqlite3.IntegrityError:
            return False  # duplicate


def get_unnotified_transactions(monitor_id: int):
    with get_db() as db:
        return db.execute("""
            SELECT * FROM transactions_log
            WHERE monitor_id=? AND notified=0
            ORDER BY timestamp
        """, (monitor_id,)).fetchall()


def mark_notified(tx_id: int):
    with get_db() as db:
        db.execute("UPDATE transactions_log SET notified=1 WHERE id=?", (tx_id,))


# ── Payment operations ───────────────────────────────────────

def get_next_hd_index(chain: str) -> int:
    with get_db() as db:
        row = db.execute("SELECT next_index FROM hd_wallet_index WHERE chain=?", (chain,)).fetchone()
        if row:
            idx = row["next_index"]
            db.execute("UPDATE hd_wallet_index SET next_index=? WHERE chain=?", (idx + 1, chain))
        else:
            idx = 0
            db.execute("INSERT INTO hd_wallet_index (chain, next_index) VALUES (?, ?)", (chain, 1))
        return idx


def create_payment(user_id: int, plan: str, period: str, amount_usd: float,
                   pay_chain: str, pay_address: str, pay_amount: str,
                   derivation_idx: int, expires_in: int = 3600):
    now = time.time()
    with get_db() as db:
        db.execute("""
            INSERT INTO payments
            (user_id, plan, period, amount_usd, pay_chain, pay_address,
             pay_amount, derivation_idx, status, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """, (user_id, plan, period, amount_usd, pay_chain, pay_address,
              pay_amount, derivation_idx, now + expires_in))
        return db.execute("SELECT * FROM payments WHERE rowid=last_insert_rowid()").fetchone()


def get_pending_payments():
    with get_db() as db:
        return db.execute("""
            SELECT * FROM payments WHERE status='pending' AND expires_at > ?
        """, (time.time(),)).fetchall()


def confirm_payment(payment_id: int, tx_hash: str):
    with get_db() as db:
        db.execute("""
            UPDATE payments SET status='confirmed', tx_hash=?, confirmed_at=?
            WHERE id=?
        """, (tx_hash, time.time(), payment_id))


def expire_old_payments():
    with get_db() as db:
        db.execute("""
            UPDATE payments SET status='expired'
            WHERE status='pending' AND expires_at <= ?
        """, (time.time(),))
