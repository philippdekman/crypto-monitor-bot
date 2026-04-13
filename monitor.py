"""
Background monitoring engine.
Periodically checks monitored addresses for new transactions,
filters by $10 minimum, and queues notifications.
"""
import asyncio
import logging
import time

import database as db
from chains import get_balance, get_transactions, CHAIN_HANDLERS
from prices import get_price_usd, convert_to_usd
from config import MONITOR_INTERVAL_SECONDS, MIN_TX_VALUE_USD

logger = logging.getLogger(__name__)


async def check_address(monitor: dict) -> list[dict]:
    """
    Check a single monitored address for new transactions.
    Returns list of new transactions above $10 threshold.
    """
    chain = monitor["chain"]
    address = monitor["address"]
    token_contract = monitor["token_contract"]
    token_symbol = monitor["token_symbol"]
    last_tx = monitor["last_tx_hash"]
    monitor_id = monitor["id"]

    handler = CHAIN_HANDLERS.get(chain)
    if not handler:
        return []

    native_symbol = handler["native_symbol"]
    symbol = token_symbol or native_symbol

    # Get new transactions
    try:
        txs = await get_transactions(chain, address, token_contract=token_contract,
                                     last_seen_txid=last_tx)
    except Exception as e:
        logger.error(f"Error checking {chain}:{address}: {e}")
        return []

    if not txs:
        return []

    # Get current price for USD conversion
    price = await get_price_usd(symbol)

    new_transactions = []
    latest_tx_hash = None

    for tx in txs:
        tx_value = float(tx["value"])

        # Calculate USD value
        if price:
            value_usd = tx_value * price
        else:
            value_usd = 0

        # Filter by minimum USD value
        if value_usd < MIN_TX_VALUE_USD:
            continue

        # Log to database
        was_new = await db.log_transaction(
            monitor_id=monitor_id,
            tx_hash=tx["tx_hash"],
            direction=tx["direction"],
            value=tx["value"],
            value_usd=value_usd,
            token_symbol=symbol,
            from_addr=tx.get("from_addr", ""),
            to_addr=tx.get("to_addr", ""),
            block_number=tx.get("block_number"),
            timestamp=tx.get("timestamp"),
        )

        if was_new:
            tx["value_usd"] = value_usd
            tx["monitor_id"] = monitor_id
            tx["user_id"] = monitor["user_id"]
            tx["chain"] = chain
            tx["explorer_url"] = handler["explorer"] + tx["tx_hash"]
            new_transactions.append(tx)

        if latest_tx_hash is None:
            latest_tx_hash = tx["tx_hash"]

    # Update monitor state
    if latest_tx_hash or txs:
        balance_data = await get_balance(chain, address,
                                          token_contract=token_contract,
                                          token_symbol=token_symbol)
        await db.update_monitor_state(
            monitor_id,
            balance=balance_data.get("balance", "0"),
            last_tx_hash=latest_tx_hash,
        )

    return new_transactions


async def run_monitoring_cycle() -> list[dict]:
    """
    Run one full monitoring cycle across all active monitors.
    Returns list of all new transactions to notify about.
    """
    monitors = await db.get_all_active_monitors()
    all_new_txs = []

    # Process in batches to respect API rate limits
    batch_size = 5
    for i in range(0, len(monitors), batch_size):
        batch = monitors[i:i + batch_size]
        tasks = [check_address(dict(m)) for m in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Monitor error: {result}")
            elif result:
                all_new_txs.extend(result)

        # Rate limit pause between batches
        if i + batch_size < len(monitors):
            await asyncio.sleep(1)

    return all_new_txs


async def get_initial_balance(chain: str, address: str, token_contract: str = None,
                              token_symbol: str = None) -> dict:
    """
    Get balance when user adds an address or checks /balance.
    If token_contract is set, returns the TOKEN balance (not native).
    """
    handler = CHAIN_HANDLERS.get(chain)
    if not handler:
        return {"error": "Неподдерживаемая сеть"}

    native_symbol = handler["native_symbol"]

    if token_contract and token_symbol:
        # Get token balance directly from chain
        balance_data = await get_balance(chain, address,
                                         token_contract=token_contract,
                                         token_symbol=token_symbol)
        balance_str = balance_data.get("balance", "0")
        symbol = balance_data.get("symbol", token_symbol)
        price = await get_price_usd(symbol)
        balance_usd = float(balance_str) * price if price else 0

        return {
            "chain": chain,
            "chain_name": handler["name"],
            "address": address,
            "symbol": symbol,
            "balance": balance_str,
            "balance_usd": balance_usd,
            "is_token": True,
        }
    else:
        # Native balance
        balance_data = await get_balance(chain, address)
        balance_str = balance_data.get("balance", "0")
        price = await get_price_usd(native_symbol)
        balance_usd = float(balance_str) * price if price else 0

        return {
            "chain": chain,
            "chain_name": handler["name"],
            "address": address,
            "symbol": native_symbol,
            "balance": balance_str,
            "balance_usd": balance_usd,
            "is_token": False,
        }


def format_transaction_notification(tx: dict, label: str = None) -> str:
    """Format a transaction for Telegram notification."""
    direction_emoji = "📥" if tx["direction"] == "in" else "📤"
    direction_text = "Входящая" if tx["direction"] == "in" else "Исходящая"

    chain_name = CHAIN_HANDLERS.get(tx.get("chain"), {}).get("name", tx.get("chain", ""))

    text = f"{direction_emoji} <b>{direction_text} транзакция</b>\n\n"

    if label:
        text += f"📋 {label}\n"

    text += (
        f"⛓ Сеть: {chain_name}\n"
        f"💰 Сумма: {tx['value']} {tx['symbol']}\n"
        f"💵 ≈ ${tx.get('value_usd', 0):,.2f}\n"
    )

    if tx["direction"] == "in":
        text += f"📤 От: <code>{tx.get('from_addr', '?')[:16]}...</code>\n"
    else:
        text += f"📥 Кому: <code>{tx.get('to_addr', '?')[:16]}...</code>\n"

    explorer_url = tx.get("explorer_url", "")
    if explorer_url:
        text += f"\n🔗 <a href='{explorer_url}'>Открыть в Explorer</a>"

    return text


def format_balance_message(balance_info: dict, label: str = None) -> str:
    """Format balance info for display."""
    text = f"💰 <b>Баланс</b>"
    if label:
        text += f" — {label}"
    text += "\n\n"

    text += (
        f"⛓ {balance_info['chain_name']}\n"
        f"📍 <code>{balance_info['address'][:20]}...</code>\n\n"
    )

    symbol = balance_info.get("symbol", "?")
    balance = balance_info.get("balance", "0")
    balance_usd = balance_info.get("balance_usd", 0)

    text += f"🪙 {balance} {symbol} (${balance_usd:,.2f})\n"

    return text
