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
FUNDING_POLL_INTERVAL = int(os.getenv("FUNDING_POLL_INTERVAL_SECONDS", "300"))
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
    choosing_kind = State()
    choosing_exchange = State()
    entering_custom_exchange = State()
    entering_symbol = State()
    choosing_condition = State()
    choosing_side = State()
    entering_price = State()
    choosing_mode = State()
    confirming = State()


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


def exchange_label(ex_id: str) -> str:
    return EXCHANGE_LABELS.get(ex_id, ex_id.capitalize())


async def _clear_old_markup(callback: CallbackQuery) -> None:
    """Strip the inline keyboard off the message the button lives on.

    Every navigation step now sends a brand-new message instead of editing
    the old one in place, so the current state always shows up at the
    bottom of the chat (not stuck wherever the very first menu message
    happened to be sent). This just makes sure a stale button on that old,
    now-scrolled-away message can't be pressed again.
    """
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить алерт", callback_data="menu:add")],
            [InlineKeyboardButton(text="📋 Мои алерты", callback_data="menu:list")],
            [InlineKeyboardButton(text="ℹ️ Помощь", callback_data="menu:help")],
        ]
    )


def kind_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💰 Цена", callback_data="kind:price")],
            [InlineKeyboardButton(text="📊 Фандинг", callback_data="kind:funding")],
            [InlineKeyboardButton(text="✖ Отмена", callback_data="cancel")],
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


def side_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔻 Шорт", callback_data="side:short"),
                InlineKeyboardButton(text="🔺 Лонг", callback_data="side:long"),
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
# A funding alert's "position" only ever exists in the chat flow / confirmation
# text - it's not stored in the DB. It maps 1:1 onto the same condition
# above/below that price alerts already use: a short pays funding once the
# rate goes negative (below the threshold), a long once it goes positive
# (above the threshold). That's exactly the semantics check_funding_job needs,
# so no extra DB column is required.
SIDE_TO_CONDITION = {"short": "below", "long": "above"}
SIDE_LABELS = {"short": "шорт", "long": "лонг"}


def alert_line(a: dict) -> str:
    icon = "🔁" if a.get("mode") == "recurring" else "🔂"
    if a.get("kind") == "funding":
        side = SIDE_LABELS.get("short" if a["condition"] == "below" else "long")
        arrow = "ниже" if a["condition"] == "below" else "выше"
        return (
            f"{icon} {exchange_label(a['exchange'])} · {a['symbol']} · "
            f"фандинг {arrow} {a['target_price'] * 100:.4f}% ({side})"
        )
    arrow = "выше" if a["condition"] == "above" else "ниже"
    return f"{icon} {exchange_label(a['exchange'])} · {a['symbol']} · {arrow} {a['target_price']:g}"


def alerts_list_text(alerts: list[dict]) -> str:
    lines = [f"Активные алерты ({len(alerts)}):", ""]
    for i, a in enumerate(alerts, 1):
        lines.append(f"{i}. {alert_line(a)}")
    return "\n".join(lines)


def alerts_list_kb(alerts: list[dict]) -> InlineKeyboardMarkup:
    # Full details live in the message text above (unlimited width, wraps
    # normally) - buttons just reference the alert by number, since cramming
    # exchange/pair/price into a button label gets clipped on a phone screen.
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for i, a in enumerate(alerts, 1):
        row.append(InlineKeyboardButton(text=f"🗑 {i}", callback_data=f"del:{a['id']}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅ В меню", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if not is_allowed(message.from_user.id):
        await message.answer("Доступ к этому боту ограничен.")
        return
    await message.answer(
        "Привет! Это бот алертов по крипте: цена и фандинг.\n"
        "Настраиваешь условие — как только оно срабатывает, "
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
        await message.answer(alerts_list_text(alerts), reply_markup=alerts_list_kb(alerts))


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
        if a.get("kind") == "funding":
            detail = f"фандинг {'ниже' if a['condition'] == 'below' else 'выше'} {a['target_price'] * 100:.4f}%"
        else:
            detail = f"{'выше' if a['condition'] == 'above' else 'ниже'} {a['target_price']:g}"
        lines.append(
            f"#{a['id']} status={a['status']} mode={a.get('mode')} kind={a.get('kind', 'price')}\n"
            f"  {exchange_label(a['exchange'])} · {a['symbol']} · {detail}\n"
            f"  создан: {a['created_at']}\n"
            f"  last_triggered_at: {a.get('last_triggered_at')} | triggered_price: {a.get('triggered_price')}"
        )
    await message.answer("\n\n".join(lines))


@dp.callback_query(F.data == "menu:back")
async def cb_menu_back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await _clear_old_markup(callback)
    await callback.message.answer("Главное меню:", reply_markup=main_menu_kb())
    await callback.answer()


@dp.callback_query(F.data == "menu:help")
async def cb_help(callback: CallbackQuery):
    await _clear_old_markup(callback)
    await callback.message.answer(
        "Как это работает:\n\n"
        "1. «Добавить алерт» — сначала выбираешь тип: 💰 цена или 📊 фандинг.\n"
        "   • Цена: биржа, пара, цена (выше/ниже которой сработать).\n"
        "   • Фандинг: биржа, пара, твоя позиция (🔻 шорт / 🔺 лонг) и порог в % "
        "— сработает, когда фандинг перейдёт в невыгодную для этой позиции сторону "
        "(шорт платит при отрицательном фандинге, лонг — при положительном).\n"
        "   Дальше для обоих типов — режим: 🔂 один раз, или 🔁 повторяющийся "
        f"(будет присылать снова при каждом срабатывании, не чаще раза в {db.RECURRING_COOLDOWN_MINUTES} мин).\n"
        f"2. Цену бот проверяет каждые {POLL_INTERVAL} сек, фандинг — раз в {FUNDING_POLL_INTERVAL // 60} мин "
        "(он и так меняется медленно).\n"
        "3. Как только условие выполняется — приходит push в Pushover. "
        "Одноразовый алерт после этого переходит в сработавшие, повторяющийся остаётся активным.\n"
        "4. «Мои алерты» — список активных (🔂/🔁 показывает тип режима), с кнопкой 🗑 для удаления.",
        reply_markup=main_menu_kb(),
    )
    await callback.answer()


@dp.callback_query(F.data == "menu:list")
async def cb_list(callback: CallbackQuery):
    await _clear_old_markup(callback)
    alerts = db.get_active_alerts(callback.from_user.id)
    if not alerts:
        await callback.message.answer("Активных алертов пока нет.", reply_markup=main_menu_kb())
    else:
        await callback.message.answer(alerts_list_text(alerts), reply_markup=alerts_list_kb(alerts))
    await callback.answer()


@dp.callback_query(F.data.startswith("del:"))
async def cb_delete(callback: CallbackQuery):
    await _clear_old_markup(callback)
    alert_id = int(callback.data.split(":", 1)[1])
    db.delete_alert(alert_id, callback.from_user.id)
    alerts = db.get_active_alerts(callback.from_user.id)
    if not alerts:
        await callback.message.answer("Активных алертов пока нет.", reply_markup=main_menu_kb())
    else:
        await callback.message.answer(alerts_list_text(alerts), reply_markup=alerts_list_kb(alerts))
    await callback.answer("Удалено")


@dp.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data == "cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await _clear_old_markup(callback)
    await callback.message.answer("Отменено.", reply_markup=main_menu_kb())
    await callback.answer()


@dp.callback_query(F.data == "menu:add")
async def cb_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddAlert.choosing_kind)
    await _clear_old_markup(callback)
    await callback.message.answer("Что отслеживаем?", reply_markup=kind_kb())
    await callback.answer()


@dp.callback_query(AddAlert.choosing_kind, F.data.startswith("kind:"))
async def cb_choose_kind(callback: CallbackQuery, state: FSMContext):
    kind = callback.data.split(":", 1)[1]
    await state.update_data(kind=kind)
    await state.set_state(AddAlert.choosing_exchange)
    await _clear_old_markup(callback)
    await callback.message.answer("Выбери биржу:", reply_markup=exchange_choice_kb())
    await callback.answer()


@dp.callback_query(AddAlert.choosing_exchange, F.data.startswith("ex:"))
async def cb_choose_exchange(callback: CallbackQuery, state: FSMContext):
    ex_id = callback.data.split(":", 1)[1]
    await _clear_old_markup(callback)
    if ex_id == "other":
        await state.set_state(AddAlert.entering_custom_exchange)
        await callback.message.answer(
            "Введи id биржи латиницей, как в ccxt (например: kraken, gate, mexc, htx):"
        )
        await callback.answer()
        return
    await callback.answer("Проверяю биржу...")
    try:
        await exchanges.get_exchange(ex_id)
    except Exception as e:
        await callback.message.answer(f"Ошибка подключения к {ex_id}: {e}", reply_markup=main_menu_kb())
        await state.clear()
        return
    await state.update_data(exchange=ex_id)
    await state.set_state(AddAlert.entering_symbol)
    await callback.message.answer(
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
    kind = data.get("kind", "price")
    symbol = message.text.strip().upper()

    if kind == "funding":
        try:
            actual_ex_id, actual_symbol, rate = await exchanges.resolve_funding_symbol(ex_id, symbol)
        except Exception as e:
            await message.answer(f"{e}\nПроверь формат (например BTC/USDT) и попробуй ещё раз.")
            return
        await state.update_data(exchange=actual_ex_id, symbol=actual_symbol, current_rate=rate)
        await state.set_state(AddAlert.choosing_side)
        note = ""
        if actual_ex_id != ex_id:
            note = f" (нашёл на {exchange_label(actual_ex_id)})"
        await message.answer(
            f"Текущий фандинг {actual_symbol} на {exchange_label(actual_ex_id)}{note}: {rate * 100:.4f}%\n"
            "Какая у тебя позиция по этой паре?",
            reply_markup=side_kb(),
        )
        return

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
    await _clear_old_markup(callback)
    await callback.message.answer(
        f"{price_line}Введи целевую цену (сработает, когда цена станет {word} этого значения):"
    )
    await callback.answer()


@dp.callback_query(AddAlert.choosing_side, F.data.startswith("side:"))
async def cb_side(callback: CallbackQuery, state: FSMContext):
    side = callback.data.split(":", 1)[1]
    await state.update_data(side=side, condition=SIDE_TO_CONDITION[side])
    await state.set_state(AddAlert.entering_price)
    data = await state.get_data()
    current = data.get("current_rate")
    rate_line = f"Текущий фандинг {data['symbol']}: {current * 100:.4f}%\n" if current is not None else ""
    direction = "в минус" if side == "short" else "в плюс"
    await _clear_old_markup(callback)
    await callback.message.answer(
        f"{rate_line}Введи порог в % (например 0 — сработает, как только фандинг перейдёт {direction} "
        f"для твоей позиции ({SIDE_LABELS[side]})):"
    )
    await callback.answer()


@dp.message(AddAlert.entering_price)
async def msg_price(message: Message, state: FSMContext):
    data = await state.get_data()
    kind = data.get("kind", "price")
    raw = message.text.strip().replace(",", ".").replace("%", "")
    try:
        value = float(raw)
        if kind == "price" and value <= 0:
            raise ValueError
    except ValueError:
        if kind == "funding":
            current = data.get("current_rate")
            hint = f" (текущий фандинг {data['symbol']}: {current * 100:.4f}%)" if current is not None else ""
            await message.answer(f"Введи число в процентах, например 0 или -0.01{hint}")
        else:
            current = data.get("current_price")
            hint = f" (текущая цена {data['symbol']}: {current:g})" if current is not None else ""
            await message.answer(f"Введи число, например 65000 или 0.015{hint}")
        return
    target = value / 100 if kind == "funding" else value
    await state.update_data(target_price=target)
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
    kind = data.get("kind", "price")

    if kind == "funding":
        current = data.get("current_rate")
        current_line = f"Текущий фандинг: {current * 100:.4f}%\n" if current is not None else ""
        side_label = SIDE_LABELS.get(data.get("side"), "")
        word = "ниже" if data["condition"] == "below" else "выше"
        condition_line = (
            f"Позиция: {side_label}\n"
            f"Порог: фандинг {word} {data['target_price'] * 100:.4f}%\n"
        )
    else:
        current = data.get("current_price")
        current_line = f"Текущая цена: {current:g}\n" if current is not None else ""
        word = "выше" if data["condition"] == "above" else "ниже"
        condition_line = f"Условие: цена {word} {data['target_price']:g}\n"

    await state.set_state(AddAlert.confirming)
    await _clear_old_markup(callback)
    await callback.message.answer(
        f"Биржа: {exchange_label(data['exchange'])}\n"
        f"Пара: {data['symbol']}\n"
        f"{current_line}"
        f"{condition_line}"
        f"Режим: {MODE_LABELS[mode]}\n\nСоздать алерт?",
        reply_markup=confirm_kb(),
    )
    await callback.answer()


@dp.callback_query(AddAlert.confirming, F.data.startswith("confirm:"))
async def cb_confirm(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split(":", 1)[1]
    await _clear_old_markup(callback)
    if action == "yes":
        data = await state.get_data()
        db.add_alert(
            callback.from_user.id,
            data["exchange"],
            data["symbol"],
            data["condition"],
            data["target_price"],
            data.get("mode", "one_time"),
            kind=data.get("kind", "price"),
        )
        await callback.message.answer("Алерт создан ✅", reply_markup=main_menu_kb())
    else:
        await callback.message.answer("Отменено.", reply_markup=main_menu_kb())
    await state.clear()
    await callback.answer()


async def check_alerts_job():
    price_alerts = db.get_active_alerts(kind="price")

    # Keep exchange instances alive as long as ANY active alert - price or
    # funding - still needs them. This job runs on the tightest interval, so
    # it's the natural place for the sweep regardless of which kind of alert
    # actually needs a given exchange.
    await exchanges.evict_unused(db.get_active_exchange_ids())

    if not price_alerts:
        return
    by_exchange: dict[str, list[dict]] = {}
    for a in price_alerts:
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


async def check_funding_job():
    funding_alerts = db.get_active_alerts(kind="funding")
    if not funding_alerts:
        return
    by_exchange: dict[str, list[dict]] = {}
    for a in funding_alerts:
        by_exchange.setdefault(a["exchange"], []).append(a)

    for ex_id, ex_alerts in by_exchange.items():
        symbols = list({a["symbol"] for a in ex_alerts})
        rates = await exchanges.fetch_funding_rates_for_exchange(ex_id, symbols)
        for a in ex_alerts:
            rate = rates.get(a["symbol"])
            if rate is None:
                logger.warning(
                    "No funding rate for alert %s (%s %s) - fetch failed",
                    a["id"], ex_id, a["symbol"],
                )
                continue
            triggered = (a["condition"] == "above" and rate >= a["target_price"]) or (
                a["condition"] == "below" and rate <= a["target_price"]
            )
            if not triggered:
                continue
            is_recurring = a.get("mode") == "recurring"
            if is_recurring and db.is_in_cooldown(a):
                continue
            if is_recurring:
                db.record_recurring_trigger(a["id"], rate)
            else:
                db.mark_triggered(a["id"], rate)
            side = "шорт" if a["condition"] == "below" else "лонг"
            suffix = " (повторяющийся)" if is_recurring else ""
            await send_pushover(
                title=f"Фандинг {exchange_label(ex_id)} {a['symbol']}",
                message=f"Теперь платишь фандинг ({side}): {rate * 100:.4f}%{suffix}",
            )
            logger.info("Funding alert %s triggered at %.6f (mode=%s)", a["id"], rate, a.get("mode"))


async def main():
    db.init_db()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_alerts_job, "interval", seconds=POLL_INTERVAL, id="check_alerts", max_instances=1)
    scheduler.add_job(
        check_funding_job, "interval", seconds=FUNDING_POLL_INTERVAL, id="check_funding", max_instances=1
    )
    scheduler.start()
    try:
        await dp.start_polling(bot)
    finally:
        await exchanges.close_all_exchanges()


if __name__ == "__main__":
    asyncio.run(main())
