"""
Configuration for Crypto Monitor Bot.
All settings are loaded from environment variables.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_USER_IDS = [int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()]

# API Keys (free tiers)
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")        # etherscan.io — ETH + ERC-20
BSCTRACE_API_KEY = os.getenv("BSCTRACE_API_KEY", "")          # BSCTrace via MegaNode — BSC + BEP-20
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "")           # trongrid.io — TRON + TRC-20

# Blockstream (BTC) — no API key needed

# HD Wallet master seed (BIP39 mnemonic) — KEEP SECRET
HD_WALLET_MNEMONIC = os.getenv("HD_WALLET_MNEMONIC", "")

# Payment receiving addresses (for networks where HD derivation isn't used)
PAYMENT_ADDRESSES = {
    "BTC": os.getenv("PAYMENT_ADDR_BTC", ""),
    "ETH": os.getenv("PAYMENT_ADDR_ETH", ""),
    "TRON": os.getenv("PAYMENT_ADDR_TRON", ""),
    "BSC": os.getenv("PAYMENT_ADDR_BSC", ""),
}

# Subscription pricing (USD)
PRICING = {
    "basic": {
        "monthly": 5.0,
        "yearly": 20.0,
        "max_addresses": 100,
    },
    "premium": {
        "monthly": 20.0,
        "yearly": 150.0,
        "max_addresses": 10000,
    },
}

FREE_ADDRESS_LIMIT = 2

# Monitoring
MONITOR_INTERVAL_SECONDS = int(os.getenv("MONITOR_INTERVAL_SECONDS", "120"))  # default 2 min
MIN_TX_VALUE_USD = 10.0

# Subscription reminders (days before expiry)
REMINDER_DAYS = [30, 7, 3, 1]

# Database (PostgreSQL on Railway — uses DATABASE_URL)
DATABASE_URL = os.getenv("DATABASE_URL", "")
