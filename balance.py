"""
Баланс пользователя: пополнение, списание, AML-проверки.
Высокоуровневый API над database.py.
"""
import logging
from config import (
    MIN_TOPUP_USD, AML_CHECK_PRICE_CENTS, NAME_CHECK_PRICE_CENTS, FREE_AML_CHECKS
)
import database as db
from hd_wallet import generate_address
from prices import convert_from_usd

logger = logging.getLogger(__name__)


async def get_user_balance_info(user_id: int) -> dict:
    """Возвращает баланс, оставшиеся бесплатные AML-проверки, план."""
    balance_info = await db.get_balance_info(user_id)
    sub = await db.get_active_subscription(user_id)
    plan = sub["plan"] if sub else "free"

    free_total = FREE_AML_CHECKS.get(plan, 0)
    used = balance_info["aml_checks_used_this_month"]
    free_remaining = max(0, free_total - used)

    return {
        "balance_cents": balance_info["balance_cents"],
        "balance_usd": balance_info["balance_cents"] / 100,
        "plan": plan,
        "free_aml_total": free_total,
        "free_aml_remaining": free_remaining,
        "aml_checks_used": used,
    }


async def create_topup_invoice(user_id: int, amount_usd: float, pay_chain: str) -> dict | None:
    """Создаёт HD-адрес и платёжную запись для пополнения баланса."""
    if amount_usd < MIN_TOPUP_USD:
        logger.warning(f"Topup below minimum: {amount_usd} < {MIN_TOPUP_USD}")
        return None

    try:
        idx = await db.get_next_hd_index(pay_chain)
    except Exception as e:
        logger.error(f"Failed to get HD index for {pay_chain}: {e}")
        return None

    addr_info = generate_address(pay_chain, idx)
    if not addr_info:
        logger.error(f"HD wallet generation failed for {pay_chain} index {idx}")
        return None

    native_symbols = {"BTC": "BTC", "ETH": "ETH", "BSC": "BNB", "TRON": "TRX"}
    symbol = native_symbols.get(pay_chain, "ETH")

    # Retry price fetch up to 3 times
    import asyncio
    crypto_amount = None
    for attempt in range(3):
        crypto_amount = await convert_from_usd(amount_usd, symbol)
        if crypto_amount is not None:
            break
        logger.warning(f"Price fetch failed for {symbol}, attempt {attempt + 1}/3")
        await asyncio.sleep(2)

    if crypto_amount is None:
        logger.error(f"Could not get price for {symbol} after 3 attempts")
        return None

    try:
        payment = await db.create_topup_payment(
            user_id=user_id,
            amount_usd=amount_usd,
            pay_chain=pay_chain,
            pay_address=addr_info["address"],
            pay_amount=f"{crypto_amount:.8f}",
            derivation_idx=idx,
            expires_in=3600,
        )
    except Exception as e:
        logger.error(f"Failed to create topup payment: {e}")
        return None

    logger.info(f"Topup invoice: user={user_id}, ${amount_usd}, "
                f"{crypto_amount:.8f} {symbol}, addr={addr_info['address']}")

    return {
        "payment_id": payment["id"],
        "address": addr_info["address"],
        "amount_crypto": f"{crypto_amount:.8f}",
        "symbol": symbol,
        "amount_usd": amount_usd,
        "expires_in_minutes": 60,
    }


async def credit_topup(user_id: int, payment_id: int, actual_amount_usd: float) -> dict:
    """Вызывается монитором при подтверждении оплаты. Зачисляет на баланс."""
    cents = int(round(actual_amount_usd * 100))
    result = await db.credit_balance(
        user_id=user_id,
        cents=cents,
        description=f"Пополнение баланса ${actual_amount_usd:.2f}",
        reference_id=str(payment_id),
        tx_type="topup",
    )

    logger.info(f"Topup credited: user={user_id}, "
                f"${actual_amount_usd:.2f}, new balance={result['balance_cents']} cents")
    return result


async def can_use_free_aml_check(user_id: int) -> tuple[bool, int]:
    """Возвращает (можно_ли_бесплатно, оставшихся_бесплатных)."""
    info = await get_user_balance_info(user_id)
    remaining = info["free_aml_remaining"]
    return remaining > 0, remaining


async def charge_aml_check(user_id: int, address: str) -> tuple[bool, str]:
    """Списывает за AML-проверку: сначала бесплатные, потом с баланса.
    Возвращает (успех, сообщение)."""
    can_free, remaining = await can_use_free_aml_check(user_id)

    if can_free:
        await db.increment_aml_checks_used(user_id)
        return True, f"Использована бесплатная проверка (осталось: {remaining - 1})"

    # Try paid check
    result = await db.debit_balance(
        user_id=user_id,
        cents=AML_CHECK_PRICE_CENTS,
        tx_type="aml_check",
        description=f"AML-проверка {address[:16]}...",
        reference_id=address,
    )

    if result is None:
        balance_cents = await db.get_balance_cents(user_id)
        return False, (
            f"Недостаточно средств. Баланс: ${balance_cents / 100:.2f}, "
            f"стоимость проверки: ${AML_CHECK_PRICE_CENTS / 100:.2f}"
        )

    await db.increment_aml_checks_used(user_id)
    return True, f"Списано ${AML_CHECK_PRICE_CENTS / 100:.2f} за AML-проверку"


async def charge_name_check(user_id: int, query_ref: str) -> tuple[bool, str]:
    """Списывает за проверку имени: сначала бесплатные (общая квота с AML), потом с баланса $1.00.
    Возвращает (успех, сообщение)."""
    can_free, remaining = await can_use_free_aml_check(user_id)

    if can_free:
        await db.increment_aml_checks_used(user_id)
        return True, f"Использована бесплатная проверка (осталось: {remaining - 1})"

    # Try paid check
    result = await db.debit_balance(
        user_id=user_id,
        cents=NAME_CHECK_PRICE_CENTS,
        tx_type="name_check",
        description=f"Проверка имени: {query_ref[:30]}",
        reference_id=query_ref,
    )

    if result is None:
        balance_cents = await db.get_balance_cents(user_id)
        return False, (
            f"Недостаточно средств. Баланс: ${balance_cents / 100:.2f}, "
            f"стоимость проверки: ${NAME_CHECK_PRICE_CENTS / 100:.2f}"
        )

    await db.increment_aml_checks_used(user_id)
    return True, f"Списано ${NAME_CHECK_PRICE_CENTS / 100:.2f} за проверку имени"
