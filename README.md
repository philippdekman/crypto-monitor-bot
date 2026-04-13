# 🔍 Crypto Monitor Bot

Telegram-бот для мониторинга крипто-адресов с уведомлениями о транзакциях.

## Возможности

- **Мультичейн**: Bitcoin, Ethereum, BNB Smart Chain, TRON
- **Токены**: ERC-20, BEP-20, TRC-20 (USDT, USDC и другие)
- **Автоопределение сети** по формату адреса
- **Фильтр**: только транзакции свыше $10
- **Подписки**: бесплатно 2 адреса, платно до 100/10000
- **Оплата в крипте**: HD-кошелёк генерирует уникальный адрес на каждый платёж
- **Напоминания**: за 30, 7, 3 и 1 день до истечения подписки

## Архитектура

```
bot.py              — Telegram-бот (команды, UI, inline-кнопки)
chains.py           — Взаимодействие с блокчейнами (Blockstream, Etherscan, TronGrid)
monitor.py          — Фоновый мониторинг транзакций
subscription.py     — Управление подписками и платежами
hd_wallet.py        — HD-кошелёк (BIP44) для генерации платёжных адресов
prices.py           — Курсы крипто через CoinGecko API
database.py         — SQLite — пользователи, адреса, транзакции, платежи
config.py           — Конфигурация через переменные окружения
```

## Быстрый старт

### 1. Клонировать и настроить

```bash
git clone <repo-url>
cd crypto-monitor-bot
cp .env.example .env
```

### 2. Получить API-ключи (бесплатно)

| Сервис | URL | Для чего |
|--------|-----|----------|
| Telegram BotFather | https://t.me/BotFather | Токен бота |
| Etherscan | https://etherscan.io/myapikey | ETH + BSC (Etherscan V2) |
| TronGrid | https://www.trongrid.io | TRON |

Bitcoin (Blockstream) — ключ не нужен.

### 3. Сгенерировать мнемонику для HD-кошелька

```bash
pip install hdwallet
python -c "from hdwallet.utils import generate_mnemonic; print(generate_mnemonic(language='english', strength=256))"
```

⚠️ **ВАЖНО**: Используйте отдельную мнемонику только для приёма платежей. Не используйте свой основной кошелёк!

Запишите мнемонику и вставьте в `.env` в поле `HD_WALLET_MNEMONIC`.

### 4. Заполнить .env

```env
TELEGRAM_BOT_TOKEN=123456:ABC...
ETHERSCAN_API_KEY=ABCDEF...
TRONGRID_API_KEY=abcdef...
HD_WALLET_MNEMONIC=word1 word2 word3 ...
ADMIN_USER_IDS=your_telegram_id
```

### 5. Запустить

**Docker (рекомендуется):**
```bash
docker compose up -d --build
docker compose logs -f
```

**Без Docker:**
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python bot.py
```

**Railway:**
1. Загрузите код в GitHub
2. Подключите репозиторий на [railway.app](https://railway.app)
3. Добавьте переменные из `.env` в Settings → Variables
4. Деплой произойдёт автоматически

## Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Начать работу |
| `/add <адрес>` | Добавить адрес для мониторинга |
| `/list` | Список отслеживаемых адресов |
| `/remove` | Удалить адрес |
| `/balance` | Проверить балансы |
| `/plan` | Текущий план |
| `/subscribe` | Оформить подписку |
| `/help` | Помощь |

Также можно просто отправить крипто-адрес — бот определит сеть автоматически.

## Тарифы

| План | Адресов | Месяц | Год |
|------|---------|-------|-----|
| Free | 2 | $0 | $0 |
| Basic | 100 | $5 | $20 |
| Premium | 10000 | $20 | $150 |

## Оплата

Бот генерирует уникальный BIP44 адрес для каждого платежа.
Поддерживаемые криптовалюты для оплаты: BTC, ETH, TRX, BNB.

Процесс:
1. Пользователь выбирает план → период → криптовалюту
2. Бот генерирует уникальный адрес из HD-кошелька
3. Пользователь отправляет точную сумму
4. Бот проверяет входящую транзакцию и активирует подписку

## API Rate Limits

| API | Лимит |
|-----|-------|
| Etherscan (Free) | 3 req/sec, 100K/day |
| TronGrid (Free) | 100K req/day |
| Blockstream | без ограничений |
| CoinGecko (Demo) | 30 req/min, 10K/month |

## Структура БД (SQLite)

- `users` — пользователи Telegram
- `subscriptions` — подписки (free/basic/premium)
- `monitored_addresses` — отслеживаемые адреса
- `transactions_log` — лог транзакций
- `payments` — платежи за подписку
- `hd_wallet_index` — счётчик HD-деривации

## Лицензия

MIT
