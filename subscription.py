"""
Subscription management: check limits, create invoices, verify payments, send reminders.
"""
import logging
import time
from config import PRICING, FREE_ADDRESS_LIMIT, REMINDER_DAYS
import database as db
from hd_wallet import generate_address
from prices import convert_from_usd

logger = logging.getLogger(__name__)


async def get_user_plan_info(user_id: int) -> dict:
    """Get current plan details for a user."""
    sub = await db.get_active_subscription(user_id)
    if not sub or sub["plan"] == "free":
        return {
            "plan": "free",
            "max_addresses": FREE_ADDRESS_LIMIT,
            "current_addresses": await db.count_user_addresses(user_id),
            "expires_at": None,
            "is_active": True,
        }

    plan_config = PRICING.get(sub["plan"], {})
    return {
        "plan": sub["plan"],
        "period": sub["period"],
        "max_addresses": plan_config.get("max_addresses", FREE_ADDRESS_LIMIT),
        "current_addresses": await db.count_user_addresses(user_id),
        "expires_at": sub["expires_at"],
        "started_at": sub["started_at"],
        "is_active": sub["is_active"] == 1,
    }


async def can_add_address(user_id: int) -> tuple[bool, str]:
    """Check if user can add another address."""
    info = await get_user_plan_info(user_id)
    if info["current_addresses"] >= info["max_addresses"]:
        if info["plan"] == "free":
            return False, (
                f"🔒 Бесплатный лимит — {FREE_ADDRESS_LIMIT} адреса.\n"
                f"Оформите подписку для мониторинга до {PRICING['basic']['max_addresses']} адресов."
            )
        elif info["plan"] == "basic":
            return False, (
                f"🔒 На плане Basic лимит — {PRICING['basic']['max_addresses']} адресов.\n"
                f"Перейдите на Premium для мониторинга до {PRICING['premium']['max_addresses']} адресов."
            )
        else:
            return False, "🔒 Достигнут лимит адресов на вашем плане."
    return True, ""


async def create_payment_invoice(user_id: int, plan: str, period: str, pay_chain: str) -> dict | None:
    """Create a payment invoice with a unique HD wallet address."""
    plan_config = PRICING.get(plan)
    if not plan_config:
        logger.error(f"Unknown plan: {plan}")
        return None

    amount_usd = plan_config.get(period)
    if not amount_usd:
        logger.error(f"Unknown period '{period}' for plan '{plan}'")
        return None

    # Get next HD wallet index and generate address
    try:
        idx = await db.get_next_hd_index(pay_chain)
    except Exception as e:
        logger.error(f"Failed to get HD index for {pay_chain}: {e}")
        return None

    addr_info = generate_address(pay_chain, idx)
    if not addr_info:
        logger.error(f"HD wallet address generation failed for {pay_chain} index {idx}")
        return None

    # Convert USD to crypto amount
    native_symbols = {"BTC": "BTC", "ETH": "ETH", "BSC": "BNB", "TRON": "TRX"}
    symbol = native_symbols.get(pay_chain, "ETH")

    # Retry price fetch up to 3 times (CoinGecko rate limits)
    crypto_amount = None
    for attempt in range(3):
        crypto_amount = await convert_from_usd(amount_usd, symbol)
        if crypto_amount is not None:
            break
        logger.warning(f"Price fetch failed for {symbol}, attempt {attempt + 1}/3")
        import asyncio
        await asyncio.sleep(2)

    if crypto_amount is None:
        logger.error(f"Could not get price for {symbol} after 3 attempts")
        return None

    # Create payment record
    try:
        payment = await db.create_payment(
            user_id=user_id,
            plan=plan,
            period=period,
            amount_usd=amount_usd,
            pay_chain=pay_chain,
            pay_address=addr_info["address"],
            pay_amount=f"{crypto_amount:.8f}",
            derivation_idx=idx,
            expires_in=3600,  # 1 hour to pay
        )
    except Exception as e:
        logger.error(f"Failed to create payment record: {e}")
        return None

    logger.info(f"Invoice created: user={user_id}, plan={plan}/{period}, "
                f"{amount_usd} USD = {crypto_amount:.8f} {symbol}, addr={addr_info['address']}")

    return {
        "payment_id": payment["id"],
        "address": addr_info["address"],
        "amount_crypto": f"{crypto_amount:.8f}",
        "symbol": symbol,
        "amount_usd": amount_usd,
        "plan": plan,
        "period": period,
        "expires_in_minutes": 60,
    }


async def check_pending_payments() -> list[dict]:
    """Check all pending payments for incoming transactions. Returns confirmed ones.
    Routes by payment_kind: 'subscription' activates plan, 'balance_topup' credits balance."""
    from chains import get_transactions

    confirmed = []
    pending = await db.get_pending_payments()

    for payment in pending:
        pay_chain = payment["pay_chain"]
        pay_address = payment["pay_address"]
        expected_amount = float(payment["pay_amount"])
        payment_kind = payment.get("payment_kind") or "subscription"

        try:
            txs = await get_transactions(pay_chain, pay_address)
        except Exception as e:
            logger.error(f"Payment check failed for {pay_chain}:{pay_address}: {e}")
            continue

        for tx in txs:
            if tx["direction"] == "in":
                received = float(tx["value"])
                logger.info(f"Payment tx found: {pay_chain}:{pay_address} received {received}, "
                            f"expected {expected_amount}, tx={tx['tx_hash'][:16]}...")
                # Allow 1% tolerance for network fees
                if received >= expected_amount * 0.99:
                    await db.confirm_payment(payment["id"], tx["tx_hash"])

                    if payment_kind == "balance_topup":
                        # Credit user balance
                        from balance import credit_topup
                        await credit_topup(payment["user_id"], payment["id"], payment["amount_usd"])
                        confirmed.append({
                            "user_id": payment["user_id"],
                            "payment_id": payment["id"],
                            "payment_kind": "balance_topup",
                            "amount_usd": payment["amount_usd"],
                            "tx_hash": tx["tx_hash"],
                            "amount": tx["value"],
                            "symbol": tx["symbol"],
                        })
                        logger.info(f"Topup confirmed: user={payment['user_id']}, "
                                    f"${payment['amount_usd']}, tx={tx['tx_hash'][:16]}...")
                    else:
                        # Activate subscription (original behavior)
                        duration = 365 if payment["period"] == "yearly" else 30
                        await db.create_subscription(
                            payment["user_id"], payment["plan"],
                            payment["period"], duration
                        )
                        confirmed.append({
                            "user_id": payment["user_id"],
                            "payment_id": payment["id"],
                            "payment_kind": "subscription",
                            "plan": payment["plan"],
                            "period": payment["period"],
                            "tx_hash": tx["tx_hash"],
                            "amount": tx["value"],
                            "symbol": tx["symbol"],
                        })
                        logger.info(f"Payment confirmed: user={payment['user_id']}, "
                                    f"plan={payment['plan']}, tx={tx['tx_hash'][:16]}...")
                    break

    # Expire old payments
    await db.expire_old_payments()

    return confirmed


async def get_expiring_subscriptions_to_notify() -> list[dict]:
    """Get subscriptions that need reminder notifications."""
    notifications = []

    for days in REMINDER_DAYS:
        subs = await db.get_expiring_subscriptions(days)
        for sub in subs:
            await db.mark_reminder_sent(sub["id"], days)
            remaining = sub["expires_at"] - time.time()
            days_left = max(1, int(remaining / 86400))

            notifications.append({
                "user_id": sub["user_id"],
                "first_name": sub["first_name"],
                "plan": sub["plan"],
                "days_left": days_left,
                "expires_at": sub["expires_at"],
            })

    return notifications


def format_pricing_text() -> str:
    """Format pricing plans for display."""
    b = PRICING["basic"]
    p = PRICING["premium"]
    return (
        "💎 <b>Тарифные планы</b>\n\n"
        f"🆓 <b>Free</b> — до {FREE_ADDRESS_LIMIT} адресов, бесплатно\n\n"
        f"⭐ <b>Basic</b> — до {b['max_addresses']} адресов\n"
        f"   • ${b['monthly']:.0f}/месяц\n"
        f"   • ${b['yearly']:.0f}/год (экономия {100 - b['yearly'] / (b['monthly'] * 12) * 100:.0f}%)\n\n"
        f"👑 <b>Premium</b> — до {p['max_addresses']} адресов\n"
        f"   • ${p['monthly']:.0f}/месяц\n"
        f"   • ${p['yearly']:.0f}/год (экономия {100 - p['yearly'] / (p['monthly'] * 12) * 100:.0f}%)\n"
    )
