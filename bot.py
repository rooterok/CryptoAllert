import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import db
import exchanges
from pushover import send_pushover

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("price-alerts-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "15"))
ALLOWED_USER_IDS = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()}

POPULAR_EXCHANGES = ["binance", "bybit", "okx", "aster", "bitget", "kucoin"]
EXCHANGE_LABELS = {
    "binance": "Binance",
    "binanceusdm": "Binance Futures",
    "bybit": "Bybit",
    "okx": "OKX",
    "aster": "Aster",
    "bitget": "Bitget",
    "kucoin": "KuCoin",
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class AddAlert(StatesGroup):
    choosing_exchange = State()
    entering_custom_exchange = State()
    entering_symbol = State()
    choosing_condition = State()
    entering_price = State()
    choosing_mode = State()
    confirming = State()


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


def exchange_label(ex_id: str) -> str:
    return EXCHANGE_LABELS.get(ex_id, ex_id.capitalize())


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить алерт", callback_data="menu:add")],
            [InlineKeyboardButton(text="📋 Мои алерты", callback_data="menu:list")],
            [InlineKeyboardButton(text="ℹ️ Помощь", callback_data="menu:help")],
        ]
    )


def exchange_choice_kb() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for ex_id in POPULAR_EXCHANGES:
        row.append(InlineKeyboardButton(text=exchange_label(ex_id), callback_data=f"ex:{ex_id}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="Другая биржа →", callback_data="ex:other")])
    rows.append([InlineKeyboardButton(text="✖ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def condition_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📈 Выше", callback_data="cond:above"),
                InlineKeyboardButton(text="📉 Ниже", callback_data="cond:below"),
            ],
            [InlineKeyboardButton(text="✖ Отмена", callback_data="cancel")],
        ]
    )


def confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm:yes"),
                InlineKeyboardButton(text="✖ Отмена", callback_data="confirm:no"),
            ],
        ]
    )


def mode_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔂 Один раз", callback_data="mode:one_time")],
            [InlineKeyboardButton(text="🔁 Повторяющийся", callback_data="mode:recurring")],
            [InlineKeyboardButton(text="✖ Отмена", callback_data="cancel")],
        ]
    )


MODE_LABELS = {"one_time": "один раз", "recurring": "повторяющийся"}


def alert_line(a: dict) -> str:
    arrow = "выше" if a["condition"] == "above" else "ниже"
    icon = "🔁" if a.get("mode") == "recurring" else "🔂"
    return f"{icon} {exchange_label(a['exchange'])} · {a['symbol']} · {arrow} {a['target_price']:g}"


def alerts_list_kb(alerts: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for a in alerts:
        rows.append(
            [
                InlineKeyboardButton(text=alert_line(a), callback_data="noop"),
                InlineKeyboardButton(text="🗑", callback_data=f"del:{a['id']}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅ В меню", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if not is_allowed(message.from_user.id):
        await message.answer("Доступ к этому боту ограничен.")
        return
    await message.answer(
        "Привет! Это бот ценовых алертов по крипте.\n"
        "Настраиваешь условие (биржа, пара, цена) — как только оно срабатывает, "
        "прилетает push-уведомление в Pushover.",
        reply_markup=main_menu_kb(),
    )


@dp.message(Command("myalerts"))
async def cmd_myalerts(message: Message):
    if not is_allowed(message.from_user.id):
        return
    alerts = db.get_active_alerts(message.from_user.id)
    if not alerts:
        await message.answer("Активных алертов пока нет.", reply_markup=main_menu_kb())
    else:
        await message.answer(f"Активные алерты ({len(alerts)}):", reply_markup=alerts_list_kb(alerts))


@dp.message(Command("debug"))
async def cmd_debug(message: Message):
    """Raw dump of this user's last 20 alerts (any status) - for diagnosing why one didn't fire."""
    if not is_allowed(message.from_user.id):
        return
    alerts = db.get_all_alerts(message.from_user.id)
    if not alerts:
        await message.answer("Алертов нет вообще (ни активных, ни сработавших).")
        return
    lines = []
    for a in alerts:
        word = "выше" if a["condition"] == "above" else "ниже"
        lines.append(
            f"#{a['id']} status={a['status']} mode={a.get('mode')}\n"
            f"  {exchange_label(a['exchange'])} · {a['symbol']} · {word} {a['target_price']:g}\n"
            f"  создан: {a['created_at']}\n"
            f"  last_triggered_at: {a.get('last_triggered_at')} | triggered_price: {a.get('triggered_price')}"
        )
    await message.answer("\n\n".join(lines))


@dp.callback_query(F.data == "menu:back")
async def cb_menu_back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Главное меню:", reply_markup=main_menu_kb())
    await callback.answer()


@dp.callback_query(F.data == "menu:help")
async def cb_help(callback: CallbackQuery):
    await callback.message.edit_text(
        "Как это работает:\n\n"
        "1. «Добавить алерт» — выбираешь биржу, пару (например BTC/USDT), цену "
        "(выше/ниже которой сработать) и режим: 🔂 один раз, или 🔁 повторяющийся "
        f"(будет присылать снова при каждом срабатывании, не чаще раза в {db.RECURRING_COOLDOWN_MINUTES} мин).\n"
        "2. Бот раз в несколько секунд проверяет цену через биржевые API.\n"
        "3. Как только условие выполняется — приходит push в Pushover. "
        "Одноразовый алерт после этого переходит в сработавшие, повторяющийся остаётся активным.\n"
        "4. «Мои алерты» — список активных (🔂/🔁 показывает тип), с кнопкой 🗑 для удаления.",
        reply_markup=main_menu_kb(),
    )
    await callback.answer()


@dp.callback_query(F.data == "menu:list")
async def cb_list(callback: CallbackQuery):
    alerts = db.get_active_alerts(callback.from_user.id)
    if not alerts:
        await callback.message.edit_text("Активных алертов пока нет.", reply_markup=main_menu_kb())
    else:
        await callback.message.edit_text(f"Активные алерты ({len(alerts)}):", reply_markup=alerts_list_kb(alerts))
    await callback.answer()


@dp.callback_query(F.data.startswith("del:"))
async def cb_delete(callback: CallbackQuery):
    alert_id = int(callback.data.split(":", 1)[1])
    db.delete_alert(alert_id, callback.from_user.id)
    alerts = db.get_active_alerts(callback.from_user.id)
    if not alerts:
        await callback.message.edit_text("Активных алертов пока нет.", reply_markup=main_menu_kb())
    else:
        await callback.message.edit_text(f"Активные алерты ({len(alerts)}):", reply_markup=alerts_list_kb(alerts))
    await callback.answer("Удалено")


@dp.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data == "cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Отменено.", reply_markup=main_menu_kb())
    await callback.answer()


@dp.callback_query(F.data == "menu:add")
async def cb_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddAlert.choosing_exchange)
    await callback.message.edit_text("Выбери биржу:", reply_markup=exchange_choice_kb())
    await callback.answer()


@dp.callback_query(AddAlert.choosing_exchange, F.data.startswith("ex:"))
async def cb_choose_exchange(callback: CallbackQuery, state: FSMContext):
    ex_id = callback.data.split(":", 1)[1]
    if ex_id == "other":
        await state.set_state(AddAlert.entering_custom_exchange)
        await callback.message.edit_text(
            "Введи id биржи латиницей, как в ccxt (например: kraken, gate, mexc, htx):"
        )
        await callback.answer()
        return
    await callback.answer("Проверяю биржу...")
    try:
        await exchanges.get_exchange(ex_id)
    except Exception as e:
        await callback.message.edit_text(f"Ошибка подключения к {ex_id}: {e}", reply_markup=main_menu_kb())
        await state.clear()
        return
    await state.update_data(exchange=ex_id)
    await state.set_state(AddAlert.entering_symbol)
    await callback.message.edit_text(
        f"Биржа: {exchange_label(ex_id)}\nТеперь введи пару, например: BTC/USDT"
    )


@dp.message(AddAlert.entering_custom_exchange)
async def msg_custom_exchange(message: Message, state: FSMContext):
    ex_id = message.text.strip().lower()
    try:
        await exchanges.get_exchange(ex_id)
    except Exception as e:
        await message.answer(f"Не получилось подключиться к бирже '{ex_id}': {e}\nПопробуй ещё раз, или /start заново.")
        return
    await state.update_data(exchange=ex_id)
    await state.set_state(AddAlert.entering_symbol)
    await message.answer(f"Биржа: {exchange_label(ex_id)}\nТеперь введи пару, например: BTC/USDT")


@dp.message(AddAlert.entering_symbol)
async def msg_symbol(message: Message, state: FSMContext):
    data = await state.get_data()
    ex_id = data["exchange"]
    symbol = message.text.strip().upper()
    try:
        actual_ex_id, actual_symbol, price = await exchanges.resolve_symbol(ex_id, symbol)
    except Exception as e:
        await message.answer(f"{e}\nПроверь формат (например BTC/USDT) и попробуй ещё раз.")
        return
    # Store the exchange/symbol that actually resolved (may be the futures
    # variant, e.g. binance -> binanceusdm, or a perpetual notation like BTC/USDT:USDT)
    # and the current price, so it stays visible on every later step of this flow.
    await state.update_data(exchange=actual_ex_id, symbol=actual_symbol, current_price=price)
    await state.set_state(AddAlert.choosing_condition)
    note = ""
    if actual_ex_id != ex_id:
        note = f" (нашёл на {exchange_label(actual_ex_id)})"
    await message.answer(
        f"Текущая цена {actual_symbol} на {exchange_label(actual_ex_id)}{note}: {price:g}\nУсловие срабатывания:",
        reply_markup=condition_kb(),
    )


@dp.callback_query(AddAlert.choosing_condition, F.data.startswith("cond:"))
async def cb_condition(callback: CallbackQuery, state: FSMContext):
    condition = callback.data.split(":", 1)[1]
    await state.update_data(condition=condition)
    await state.set_state(AddAlert.entering_price)
    data = await state.get_data()
    word = "выше" if condition == "above" else "ниже"
    current = data.get("current_price")
    price_line = f"Текущая цена {data['symbol']}: {current:g}\n" if current is not None else ""
    await callback.message.edit_text(
        f"{price_line}Введи целевую цену (сработает, когда цена станет {word} этого значения):"
    )
    await callback.answer()


@dp.message(AddAlert.entering_price)
async def msg_price(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        price = float(message.text.strip().replace(",", "."))
        if price <= 0:
            raise ValueError
    except ValueError:
        current = data.get("current_price")
        hint = f" (текущая цена {data['symbol']}: {current:g})" if current is not None else ""
        await message.answer(f"Введи число, например 65000 или 0.015{hint}")
        return
    await state.update_data(target_price=price)
    await state.set_state(AddAlert.choosing_mode)
    await message.answer(
        "Один раз — сработает и отключится.\n"
        f"Повторяющийся — будет присылать уведомление каждый раз, когда условие выполняется "
        f"(не чаще раза в {db.RECURRING_COOLDOWN_MINUTES} мин).\n\nКакой режим?",
        reply_markup=mode_kb(),
    )


@dp.callback_query(AddAlert.choosing_mode, F.data.startswith("mode:"))
async def cb_mode(callback: CallbackQuery, state: FSMContext):
    mode = callback.data.split(":", 1)[1]
    await state.update_data(mode=mode)
    data = await state.get_data()
    word = "выше" if data["condition"] == "above" else "ниже"
    current = data.get("current_price")
    current_line = f"Текущая цена: {current:g}\n" if current is not None else ""
    await state.set_state(AddAlert.confirming)
    await callback.message.edit_text(
        f"Биржа: {exchange_label(data['exchange'])}\n"
        f"Пара: {data['symbol']}\n"
        f"{current_line}"
        f"Условие: цена {word} {data['target_price']:g}\n"
        f"Режим: {MODE_LABELS[mode]}\n\nСоздать алерт?",
        reply_markup=confirm_kb(),
    )
    await callback.answer()


@dp.callback_query(AddAlert.confirming, F.data.startswith("confirm:"))
async def cb_confirm(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split(":", 1)[1]
    if action == "yes":
        data = await state.get_data()
        db.add_alert(
            callback.from_user.id,
            data["exchange"],
            data["symbol"],
            data["condition"],
            data["target_price"],
            data.get("mode", "one_time"),
        )
        await callback.message.edit_text("Алерт создан ✅", reply_markup=main_menu_kb())
    else:
        await callback.message.edit_text("Отменено.", reply_markup=main_menu_kb())
    await state.clear()
    await callback.answer()


async def check_alerts_job():
    alerts = db.get_active_alerts()
    if not alerts:
        return
    by_exchange: dict[str, list[dict]] = {}
    for a in alerts:
        by_exchange.setdefault(a["exchange"], []).append(a)

    for ex_id, ex_alerts in by_exchange.items():
        symbols = list({a["symbol"] for a in ex_alerts})
        try:
            prices = await exchanges.fetch_prices_for_exchange(ex_id, symbols)
        except Exception as e:
            logger.warning("Failed to fetch prices for %s: %s", ex_id, e)
            continue
        for a in ex_alerts:
            price = prices.get(a["symbol"])
            if price is None:
                logger.warning(
                    "No price for alert %s (%s %s) - fetch failed or symbol not in ticker response",
                    a["id"], ex_id, a["symbol"],
                )
                continue
            triggered = (a["condition"] == "above" and price >= a["target_price"]) or (
                a["condition"] == "below" and price <= a["target_price"]
            )
            if not triggered:
                continue
            is_recurring = a.get("mode") == "recurring"
            if is_recurring and db.is_in_cooldown(a):
                continue  # already notified recently, price is still past the threshold
            if is_recurring:
                db.record_recurring_trigger(a["id"], price)
            else:
                db.mark_triggered(a["id"], price)
            word = "выше" if a["condition"] == "above" else "ниже"
            suffix = " (повторяющийся)" if is_recurring else ""
            await send_pushover(
                title=f"{exchange_label(ex_id)} {a['symbol']}",
                message=f"Цена {word} {a['target_price']:g}: сейчас {price:g}{suffix}",
            )
            logger.info("Alert %s triggered at %s (mode=%s)", a["id"], price, a.get("mode"))


async def main():
    db.init_db()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_alerts_job, "interval", seconds=POLL_INTERVAL, id="check_alerts", max_instances=1)
    scheduler.start()
    try:
        await dp.start_polling(bot)
    finally:
        await exchanges.close_all_exchanges()


if __name__ == "__main__":
    asyncio.run(main())
