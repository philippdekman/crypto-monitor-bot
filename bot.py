"""
Crypto Monitor Telegram Bot.

Commands:
  /start          — Welcome + registration
  /add <address>  — Add address to monitor
  /list           — List monitored addresses
  /remove         — Remove an address
  /balance        — Check balance of monitored addresses
  /plan           — View current plan
  /subscribe      — Subscribe to a paid plan
  /pay            — Check payment status
  /help           — Help
"""
import asyncio
import logging
import time
from datetime import datetime

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from telegram.constants import ParseMode

from config import (
    TELEGRAM_BOT_TOKEN, ADMIN_USER_IDS, MONITOR_INTERVAL_SECONDS,
    FREE_ADDRESS_LIMIT, PRICING
)
import database as db
from chains import detect_chains, CHAIN_HANDLERS, get_token_list
from monitor import (
    run_monitoring_cycle, get_initial_balance,
    format_transaction_notification, format_balance_message
)
from subscription import (
    get_user_plan_info, can_add_address, create_payment_invoice,
    check_pending_payments, get_expiring_subscriptions_to_notify,
    format_pricing_text
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
#  Command handlers
# ══════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id, user.username, user.first_name, user.language_code or "en")
    db.create_free_subscription(user.id)

    text = (
        f"👋 Привет, {user.first_name}!\n\n"
        f"Я мониторю крипто-адреса и сообщаю о входящих/исходящих транзакциях.\n\n"
        f"<b>Поддерживаемые сети:</b>\n"
        f"• Bitcoin (BTC)\n"
        f"• Ethereum (ETH + ERC-20 токены)\n"
        f"• BNB Smart Chain (BNB + BEP-20 токены)\n"
        f"• TRON (TRX + TRC-20 токены)\n\n"
        f"🆓 Бесплатно — до {FREE_ADDRESS_LIMIT} адресов\n"
        f"⭐ Подписка — до {PRICING['basic']['max_addresses']}+ адресов\n\n"
        f"<b>Как начать:</b>\n"
        f"Отправьте мне крипто-адрес или используйте /add &lt;адрес&gt;"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>Команды</b>\n\n"
        "/add &lt;адрес&gt; — добавить адрес для мониторинга\n"
        "/list — список отслеживаемых адресов\n"
        "/remove — удалить адрес\n"
        "/balance — проверить балансы\n"
        "/plan — текущий план\n"
        "/subscribe — оформить подписку\n"
        "/help — эта справка\n\n"
        "Или просто отправьте крипто-адрес — я определю сеть автоматически."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ── Add address flow ─────────────────────────────────────────

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.upsert_user(user_id, update.effective_user.username,
                   update.effective_user.first_name)

    if not context.args:
        await update.message.reply_text(
            "Отправьте адрес для мониторинга:\n<code>/add TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE</code>",
            parse_mode=ParseMode.HTML
        )
        return

    address = context.args[0].strip()
    await process_address(update, context, address)


async def process_address(update: Update, context: ContextTypes.DEFAULT_TYPE, address: str):
    """Process a submitted crypto address — detect chain and offer options."""
    user_id = update.effective_user.id

    # Check if can add
    can, reason = can_add_address(user_id)
    if not can:
        kb = [[InlineKeyboardButton("⭐ Оформить подписку", callback_data="subscribe")]]
        await update.message.reply_text(reason, reply_markup=InlineKeyboardMarkup(kb),
                                        parse_mode=ParseMode.HTML)
        return

    # Detect chain
    chains = detect_chains(address)
    if not chains:
        await update.message.reply_text(
            "❌ Не удалось определить блокчейн для этого адреса.\n"
            "Поддерживаемые форматы:\n"
            "• BTC: 1..., 3..., bc1...\n"
            "• ETH/BSC: 0x...\n"
            "• TRON: T..."
        )
        return

    # Store address in context for callback
    context.user_data["pending_address"] = address
    context.user_data["pending_chains"] = chains

    if len(chains) == 1:
        # Single chain detected — ask what to monitor
        chain = chains[0]
        await offer_monitoring_options(update, context, address, chain)
    else:
        # Multiple possible chains (e.g., 0x address → ETH or BSC)
        buttons = []
        for chain in chains:
            name = CHAIN_HANDLERS[chain]["name"]
            buttons.append([InlineKeyboardButton(
                f"⛓ {name} ({chain})",
                callback_data=f"select_chain:{chain}"
            )])
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

        await update.message.reply_text(
            f"🔍 Адрес <code>{address[:16]}...</code> совместим с несколькими сетями.\n"
            f"Выберите сеть:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML
        )


async def offer_monitoring_options(update_or_query, context, address: str, chain: str):
    """Show options: monitor native crypto or specific tokens."""
    handler = CHAIN_HANDLERS[chain]
    native = handler["native_symbol"]

    buttons = [
        [InlineKeyboardButton(
            f"🪙 {native} (нативная валюта)",
            callback_data=f"monitor:{chain}:native"
        )]
    ]

    # Check for tokens on this address
    if handler.get("get_token_list"):
        msg = update_or_query.message if hasattr(update_or_query, "message") and update_or_query.message else None
        if msg:
            status_msg = await msg.reply_text("🔍 Ищу токены на этом адресе...")
        else:
            status_msg = None

        tokens = await get_token_list(chain, address)

        if tokens:
            for token in tokens[:10]:  # Max 10 tokens
                sym = token.get("symbol", "???")
                contract = token.get("contract", "")
                buttons.append([InlineKeyboardButton(
                    f"🔷 {sym} (токен)",
                    callback_data=f"monitor:{chain}:{contract}:{sym}"
                )])

        buttons.append([InlineKeyboardButton(
            f"📦 Всё ({native} + все токены)",
            callback_data=f"monitor:{chain}:all"
        )])

        if status_msg:
            await status_msg.delete()

    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    text = (
        f"⛓ <b>{handler['name']}</b>\n"
        f"📍 <code>{address}</code>\n\n"
        f"Что мониторить?"
    )

    if hasattr(update_or_query, "message") and update_or_query.message:
        await update_or_query.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML
        )
    elif hasattr(update_or_query, "edit_message_text"):
        await update_or_query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML
        )


# ── Callback handlers ────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data == "cancel":
        await query.edit_message_text("❌ Отменено.")
        return

    if data.startswith("select_chain:"):
        chain = data.split(":")[1]
        address = context.user_data.get("pending_address", "")
        if address:
            await offer_monitoring_options(query, context, address, chain)
        return

    if data.startswith("monitor:"):
        parts = data.split(":")
        chain = parts[1]
        monitor_type = parts[2]  # native / all / contract_address
        address = context.user_data.get("pending_address", "")

        if not address:
            await query.edit_message_text("❌ Адрес не найден. Отправьте заново.")
            return

        await query.edit_message_text("⏳ Добавляю адрес и получаю баланс...")

        if monitor_type == "native":
            await add_and_show_balance(query, user_id, address, chain)
        elif monitor_type == "all":
            # Add native + all tokens
            await add_and_show_balance(query, user_id, address, chain)
            handler = CHAIN_HANDLERS[chain]
            if handler.get("get_token_list"):
                tokens = await get_token_list(chain, address)
                added_count = 0
                for token in tokens[:10]:
                    contract = token.get("contract", "")
                    sym = token.get("symbol", "TOKEN")
                    mon = db.add_monitored_address(
                        user_id, address, chain,
                        token_contract=contract, token_symbol=sym,
                        label=f"{sym} on {chain}"
                    )
                    # Snapshot last tx so we only alert on NEW transactions
                    if mon:
                        from chains import get_transactions as _gt
                        try:
                            etxs = await _gt(chain, address, token_contract=contract)
                            if etxs:
                                db.update_monitor_state(mon["id"], balance="0", last_tx_hash=etxs[0]["tx_hash"])
                        except Exception:
                            pass
                    added_count += 1
                if added_count:
                    await query.message.reply_text(
                        f"✅ Также добавлено {added_count} токенов для мониторинга.",
                        parse_mode=ParseMode.HTML
                    )
        else:
            # Specific token
            token_contract = parts[2]
            token_symbol = parts[3] if len(parts) > 3 else "TOKEN"
            await add_and_show_balance(
                query, user_id, address, chain,
                token_contract=token_contract, token_symbol=token_symbol
            )
        return

    # ── Subscription flow ─────────────────────────────────

    if data == "subscribe":
        await show_subscription_menu(query, user_id)
        return

    if data.startswith("sub_plan:"):
        plan = data.split(":")[1]
        await show_period_selection(query, plan)
        return

    if data.startswith("sub_period:"):
        parts = data.split(":")
        plan = parts[1]
        period = parts[2]
        await show_payment_chain_selection(query, plan, period)
        return

    if data.startswith("sub_pay:"):
        parts = data.split(":")
        plan = parts[1]
        period = parts[2]
        pay_chain = parts[3]
        await create_and_show_invoice(query, context, user_id, plan, period, pay_chain)
        return

    if data.startswith("check_payment:"):
        payment_id = int(data.split(":")[1])
        await check_payment_status(query, user_id, payment_id)
        return

    # ── Remove address ────────────────────────────────────

    if data.startswith("rm:"):
        monitor_id = int(data.split(":")[1])
        db.remove_monitored_address(monitor_id, user_id)
        await query.edit_message_text("✅ Адрес удалён из мониторинга.")
        return


async def add_and_show_balance(query, user_id, address, chain,
                                token_contract=None, token_symbol=None):
    """Add address, snapshot current state, and show balance.
    Records the latest existing tx hash so monitoring only alerts on NEW transactions."""
    label = f"{token_symbol or CHAIN_HANDLERS[chain]['native_symbol']} on {CHAIN_HANDLERS[chain]['name']}"

    monitor = db.add_monitored_address(
        user_id, address, chain,
        token_contract=token_contract,
        token_symbol=token_symbol,
        label=label,
    )

    balance_info = await get_initial_balance(chain, address, token_contract, token_symbol)

    if "error" in balance_info:
        await query.edit_message_text(f"❌ {balance_info['error']}")
        return

    # Snapshot: fetch existing transactions and mark the latest as "already seen"
    # so the monitoring loop only notifies about genuinely NEW transactions
    from chains import get_transactions as chain_get_txs
    try:
        existing_txs = await chain_get_txs(chain, address, token_contract=token_contract)
        if existing_txs and monitor:
            latest_hash = existing_txs[0]["tx_hash"]
            db.update_monitor_state(
                monitor["id"],
                balance=balance_info.get("balance", "0"),
                last_tx_hash=latest_hash,
            )
    except Exception as e:
        logger.warning(f"Could not snapshot txs for {chain}:{address}: {e}")

    text = (
        "✅ <b>Адрес добавлен в мониторинг!</b>\n\n"
        + format_balance_message(balance_info, label)
        + "\n\n🔔 Буду сообщать о новых транзакциях свыше $10."
    )
    await query.edit_message_text(text, parse_mode=ParseMode.HTML)


# ── Subscription menu ────────────────────────────────────────

async def show_subscription_menu(query, user_id):
    text = format_pricing_text()

    info = get_user_plan_info(user_id)
    text += f"\n\n📍 Ваш текущий план: <b>{info['plan'].upper()}</b>"
    if info.get("expires_at"):
        exp = datetime.fromtimestamp(info["expires_at"]).strftime("%d.%m.%Y")
        text += f"\n📅 Действует до: {exp}"
    text += f"\n📊 Адресов: {info['current_addresses']} / {info['max_addresses']}"

    buttons = [
        [InlineKeyboardButton("⭐ Basic", callback_data="sub_plan:basic")],
        [InlineKeyboardButton("👑 Premium", callback_data="sub_plan:premium")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons),
                                  parse_mode=ParseMode.HTML)


async def show_period_selection(query, plan):
    p = PRICING[plan]
    plan_name = "Basic" if plan == "basic" else "Premium"
    buttons = [
        [InlineKeyboardButton(
            f"📅 Месяц — ${p['monthly']:.0f}",
            callback_data=f"sub_period:{plan}:monthly"
        )],
        [InlineKeyboardButton(
            f"📅 Год — ${p['yearly']:.0f} (выгоднее!)",
            callback_data=f"sub_period:{plan}:yearly"
        )],
        [InlineKeyboardButton("⬅ Назад", callback_data="subscribe")],
    ]
    await query.edit_message_text(
        f"⭐ <b>План {plan_name}</b>\n\nВыберите период:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML
    )


async def show_payment_chain_selection(query, plan, period):
    buttons = [
        [InlineKeyboardButton("₿ Bitcoin (BTC)", callback_data=f"sub_pay:{plan}:{period}:BTC")],
        [InlineKeyboardButton("⟠ Ethereum (ETH)", callback_data=f"sub_pay:{plan}:{period}:ETH")],
        [InlineKeyboardButton("◈ TRON (TRX)", callback_data=f"sub_pay:{plan}:{period}:TRON")],
        [InlineKeyboardButton("⬡ BSC (BNB)", callback_data=f"sub_pay:{plan}:{period}:BSC")],
        [InlineKeyboardButton("⬅ Назад", callback_data=f"sub_plan:{plan}")],
    ]
    await query.edit_message_text(
        "💳 <b>Выберите криптовалюту для оплаты:</b>",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML
    )


async def create_and_show_invoice(query, context, user_id, plan, period, pay_chain):
    await query.edit_message_text("⏳ Генерирую платёжный адрес...")

    invoice = await create_payment_invoice(user_id, plan, period, pay_chain)
    if not invoice:
        await query.edit_message_text(
            "❌ Ошибка при создании счёта. Попробуйте позже или выберите другую криптовалюту."
        )
        return

    plan_name = "Basic" if plan == "basic" else "Premium"
    period_name = "месяц" if period == "monthly" else "год"

    text = (
        f"💳 <b>Счёт на оплату</b>\n\n"
        f"📦 План: {plan_name} / {period_name}\n"
        f"💵 Сумма: ${invoice['amount_usd']:.2f}\n"
        f"💰 К оплате: <b>{invoice['amount_crypto']} {invoice['symbol']}</b>\n\n"
        f"📍 Отправьте точную сумму на адрес:\n"
        f"<code>{invoice['address']}</code>\n\n"
        f"⏰ Адрес действителен {invoice['expires_in_minutes']} минут.\n"
        f"⚠️ Отправляйте <b>только {invoice['symbol']}</b> на этот адрес!\n\n"
        f"После отправки нажмите кнопку ниже для проверки."
    )

    buttons = [[
        InlineKeyboardButton(
            "🔍 Проверить оплату",
            callback_data=f"check_payment:{invoice['payment_id']}"
        )
    ]]

    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML
    )


async def check_payment_status(query, user_id, payment_id):
    await query.answer("🔍 Проверяю...")

    confirmed = await check_pending_payments()

    # Check if this payment was confirmed
    for c in confirmed:
        if c["user_id"] == user_id:
            await query.edit_message_text(
                f"✅ <b>Оплата подтверждена!</b>\n\n"
                f"💰 Получено: {c['amount']} {c['symbol']}\n"
                f"📦 План: {c['plan'].capitalize()} / {c['period']}\n"
                f"🔗 TX: <code>{c['tx_hash'][:16]}...</code>\n\n"
                f"🎉 Подписка активирована! Используйте /add для добавления адресов.",
                parse_mode=ParseMode.HTML
            )
            return

    # Not yet confirmed
    buttons = [[
        InlineKeyboardButton(
            "🔍 Проверить снова",
            callback_data=f"check_payment:{payment_id}"
        )
    ]]
    await query.edit_message_text(
        "⏳ Оплата пока не обнаружена.\n\n"
        "Транзакция может занять несколько минут.\n"
        "Нажмите кнопку для повторной проверки.",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML
    )


# ── List / Remove / Balance ──────────────────────────────────

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    addresses = db.get_user_addresses(user_id)

    if not addresses:
        await update.message.reply_text(
            "📭 У вас нет отслеживаемых адресов.\n"
            "Отправьте крипто-адрес, чтобы начать мониторинг."
        )
        return

    info = get_user_plan_info(user_id)
    msg = await update.message.reply_text(
        f"📋 <b>Ваши адреса</b> ({len(addresses)}/{info['max_addresses']})\n\n⏳ Загружаю балансы...",
        parse_mode=ParseMode.HTML
    )

    text = f"📋 <b>Ваши адреса</b> ({len(addresses)}/{info['max_addresses']})\n\n"

    for i, addr in enumerate(addresses, 1):
        chain_name = CHAIN_HANDLERS.get(addr["chain"], {}).get("name", addr["chain"])
        symbol = addr["token_symbol"] or CHAIN_HANDLERS.get(addr["chain"], {}).get("native_symbol", "?")

        # Fetch live balance for the correct token
        balance_info = await get_initial_balance(
            addr["chain"], addr["address"],
            addr["token_contract"], addr["token_symbol"]
        )
        balance = balance_info.get("balance", "—")
        balance_usd = balance_info.get("balance_usd", 0)
        display_symbol = balance_info.get("symbol", symbol)

        text += (
            f"<b>{i}.</b> {chain_name} — {display_symbol}\n"
            f"   <code>{addr['address'][:20]}...</code>\n"
            f"   🪙 {balance} {display_symbol} (${balance_usd:,.2f})\n\n"
        )

    await msg.edit_text(text, parse_mode=ParseMode.HTML)


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    addresses = db.get_user_addresses(user_id)

    if not addresses:
        await update.message.reply_text("📭 Нет адресов для удаления.")
        return

    buttons = []
    for addr in addresses:
        chain_name = CHAIN_HANDLERS.get(addr["chain"], {}).get("name", addr["chain"])
        symbol = addr["token_symbol"] or CHAIN_HANDLERS.get(addr["chain"], {}).get("native_symbol", "?")
        buttons.append([InlineKeyboardButton(
            f"🗑 {chain_name} {symbol} — {addr['address'][:12]}...",
            callback_data=f"rm:{addr['id']}"
        )])
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    await update.message.reply_text(
        "Выберите адрес для удаления:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    addresses = db.get_user_addresses(user_id)

    if not addresses:
        await update.message.reply_text("📭 Нет отслеживаемых адресов.")
        return

    msg = await update.message.reply_text("⏳ Проверяю балансы...")

    for addr in addresses:
        balance_info = await get_initial_balance(
            addr["chain"], addr["address"],
            addr["token_contract"], addr["token_symbol"]
        )
        text = format_balance_message(balance_info, addr["label"])
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    await msg.delete()


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    info = get_user_plan_info(user_id)

    text = f"📦 <b>Ваш план: {info['plan'].upper()}</b>\n\n"
    text += f"📊 Адресов: {info['current_addresses']} / {info['max_addresses']}\n"

    if info.get("expires_at"):
        exp = datetime.fromtimestamp(info["expires_at"]).strftime("%d.%m.%Y")
        days_left = max(0, int((info["expires_at"] - time.time()) / 86400))
        text += f"📅 Действует до: {exp} ({days_left} дн.)\n"

    if info["plan"] == "free":
        text += "\n⭐ Хотите больше адресов? /subscribe"

    kb = []
    if info["plan"] == "free":
        kb.append([InlineKeyboardButton("⭐ Оформить подписку", callback_data="subscribe")])

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(kb) if kb else None,
        parse_mode=ParseMode.HTML
    )


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = format_pricing_text()

    info = get_user_plan_info(user_id)
    text += f"\n📍 Текущий план: <b>{info['plan'].upper()}</b>"

    buttons = [
        [InlineKeyboardButton("⭐ Basic", callback_data="sub_plan:basic")],
        [InlineKeyboardButton("👑 Premium", callback_data="sub_plan:premium")],
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons),
                                    parse_mode=ParseMode.HTML)


# ── Message handler (address detection) ──────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle raw messages — try to detect crypto addresses."""
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    # Skip if it looks like a command
    if text.startswith("/"):
        return

    # Try to detect as crypto address
    chains = detect_chains(text)
    if chains:
        db.upsert_user(
            update.effective_user.id,
            update.effective_user.username,
            update.effective_user.first_name
        )
        await process_address(update, context, text)
    else:
        await update.message.reply_text(
            "🤔 Не похоже на крипто-адрес.\n"
            "Используйте /help для списка команд."
        )


# ══════════════════════════════════════════════════════════════
#  Background tasks
# ══════════════════════════════════════════════════════════════

async def monitoring_loop(app: Application):
    """Background loop that checks all monitored addresses."""
    logger.info("Monitoring loop started")
    while True:
        try:
            # Check for new transactions
            new_txs = await run_monitoring_cycle()
            for tx in new_txs:
                user_id = tx.get("user_id")
                if user_id:
                    text = format_transaction_notification(tx)
                    try:
                        await app.bot.send_message(
                            chat_id=user_id, text=text,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True
                        )
                    except Exception as e:
                        logger.error(f"Failed to notify user {user_id}: {e}")

            # Check pending payments
            confirmed = await check_pending_payments()
            for c in confirmed:
                try:
                    await app.bot.send_message(
                        chat_id=c["user_id"],
                        text=(
                            f"✅ <b>Оплата подтверждена!</b>\n\n"
                            f"📦 План: {c['plan'].capitalize()}\n"
                            f"🎉 Подписка активирована!"
                        ),
                        parse_mode=ParseMode.HTML
                    )
                except Exception as e:
                    logger.error(f"Failed to notify payment for user {c['user_id']}: {e}")

            # Check subscription reminders
            reminders = get_expiring_subscriptions_to_notify()
            for r in reminders:
                try:
                    days = r["days_left"]
                    text = (
                        f"⚠️ <b>Подписка истекает через {days} дн.</b>\n\n"
                        f"Продлите подписку, чтобы не потерять мониторинг.\n"
                        f"Используйте /subscribe для продления."
                    )
                    kb = [[InlineKeyboardButton("🔄 Продлить", callback_data="subscribe")]]
                    await app.bot.send_message(
                        chat_id=r["user_id"], text=text,
                        reply_markup=InlineKeyboardMarkup(kb),
                        parse_mode=ParseMode.HTML
                    )
                except Exception as e:
                    logger.error(f"Failed to send reminder to {r['user_id']}: {e}")

            # Expire old subscriptions
            db.expire_subscriptions()

        except Exception as e:
            logger.error(f"Monitoring loop error: {e}")

        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)


async def post_init(app: Application):
    """Post-initialization hook."""
    await app.bot.set_my_commands([
        BotCommand("start", "Начать работу"),
        BotCommand("add", "Добавить адрес"),
        BotCommand("list", "Мои адреса"),
        BotCommand("remove", "Удалить адрес"),
        BotCommand("balance", "Проверить балансы"),
        BotCommand("plan", "Мой план"),
        BotCommand("subscribe", "Оформить подписку"),
        BotCommand("help", "Помощь"),
    ])

    # Start monitoring loop
    asyncio.create_task(monitoring_loop(app))
    logger.info("Bot started successfully")


# ══════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set!")
        print("Set it in .env or environment variables.")
        return

    db.init_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # Command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))

    # Callback handler for inline buttons
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Message handler for address detection
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Starting bot...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
