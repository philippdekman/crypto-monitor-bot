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


def get_user_plan_info(user_id: int) -> dict:
    """Get current plan details for a user."""
    sub = db.get_active_subscription(user_id)
    if not sub or sub["plan"] == "free":
        return {
            "plan": "free",
            "max_addresses": FREE_ADDRESS_LIMIT,
            "current_addresses": db.count_user_addresses(user_id),
            "expires_at": None,
            "is_active": True,
        }

    plan_config = PRICING.get(sub["plan"], {})
    return {
        "plan": sub["plan"],
        "period": sub["period"],
        "max_addresses": plan_config.get("max_addresses", FREE_ADDRESS_LIMIT),
        "current_addresses": db.count_user_addresses(user_id),
        "expires_at": sub["expires_at"],
        "started_at": sub["started_at"],
        "is_active": sub["is_active"] == 1,
    }


def can_add_address(user_id: int) -> tuple[bool, str]:
    """Check if user can add another address."""
    info = get_user_plan_info(user_id)
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
        return None

    amount_usd = plan_config.get(period)
    if not amount_usd:
        return None

    # Get next HD wallet index and generate address
    idx = db.get_next_hd_index(pay_chain)
    addr_info = generate_address(pay_chain, idx)
    if not addr_info:
        return None

    # Convert USD to crypto amount
    native_symbols = {"BTC": "BTC", "ETH": "ETH", "BSC": "BNB", "TRON": "TRX"}
    symbol = native_symbols.get(pay_chain, "ETH")
    crypto_amount = await convert_from_usd(amount_usd, symbol)
    if crypto_amount is None:
        return None

    # Create payment record
    payment = db.create_payment(
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
    """Check all pending payments for incoming transactions. Returns confirmed ones."""
    from chains import get_transactions

    confirmed = []
    pending = db.get_pending_payments()

    for payment in pending:
        pay_chain = payment["pay_chain"]
        pay_address = payment["pay_address"]
        expected_amount = float(payment["pay_amount"])

        txs = await get_transactions(pay_chain, pay_address)
        for tx in txs:
            if tx["direction"] == "in":
                received = float(tx["value"])
                # Allow 1% tolerance for network fees
                if received >= expected_amount * 0.99:
                    db.confirm_payment(payment["id"], tx["tx_hash"])

                    # Activate subscription
                    duration = 365 if payment["period"] == "yearly" else 30
                    db.create_subscription(
                        payment["user_id"], payment["plan"],
                        payment["period"], duration
                    )

                    confirmed.append({
                        "user_id": payment["user_id"],
                        "plan": payment["plan"],
                        "period": payment["period"],
                        "tx_hash": tx["tx_hash"],
                        "amount": tx["value"],
                        "symbol": tx["symbol"],
                    })
                    break

    # Expire old payments
    db.expire_old_payments()

    return confirmed


def get_expiring_subscriptions_to_notify() -> list[dict]:
    """Get subscriptions that need reminder notifications."""
    notifications = []

    for days in REMINDER_DAYS:
        subs = db.get_expiring_subscriptions(days)
        for sub in subs:
            db.mark_reminder_sent(sub["id"], days)
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
