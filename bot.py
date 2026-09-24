import asyncio
import json
import os
from datetime import date, timedelta, datetime, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ozon_perf import (
    get_campaigns,
    get_daily_stats,
    activate_campaign,
    deactivate_campaign,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

_allowed_raw = os.getenv("ALLOWED_IDS", "")
ALLOWED_IDS = {ADMIN_ID}
for x in _allowed_raw.split(","):
    x = x.strip()
    if x.isdigit():
        ALLOWED_IDS.add(int(x))

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

PAGE_SIZE = 8
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

THRESHOLDS = [500, 1000, 1500, 2000]
STATE_FILE = "thresholds_state.json"
LIMITS_FILE = "daily_limits.json"


# ---------- JSON ----------
def load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_json(path: str, data: dict):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"Не удалось сохранить {path}: {e}")


def get_notified_today() -> set:
    state = load_json(STATE_FILE)
    today = datetime.now(MOSCOW_TZ).date().isoformat()
    return set(state.get(today, []))


def mark_notified(threshold: int):
    state = load_json(STATE_FILE)
    today = datetime.now(MOSCOW_TZ).date().isoformat()
    today_list = set(state.get(today, []))
    today_list.add(threshold)
    state[today] = sorted(today_list)
    for old_date in list(state.keys()):
        if old_date != today:
            del state[old_date]
    save_json(STATE_FILE, state)


def load_limits() -> dict:
    return load_json(LIMITS_FILE)


def save_limits(limits: dict):
    save_json(LIMITS_FILE, limits)


def set_limit(campaign_id: str, limit: float):
    limits = load_limits()
    limits[str(campaign_id)] = limit
    save_limits(limits)


def get_limit(campaign_id: str) -> float:
    limits = load_limits()
    return limits.get(str(campaign_id), 0.0)


# ---------- FSM ----------
class LimitForm(StatesGroup):
    waiting_amount = State()


def has_access(user_id: int) -> bool:
    return user_id in ALLOWED_IDS


# ---------- ПАРСИНГ ----------
def parse_money(s) -> float:
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip().replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return 0.0


def parse_int(s) -> int:
    try:
        return int(parse_money(s))
    except Exception:
        return 0


def parse_ozon_date(s: str):
    if not s:
        return None
    try:
        s2 = s.replace("Z", "+00:00")
        if "." in s2:
            head, tail = s2.split(".", 1)
            frac, _, rest = tail.partition("+")
            frac = frac[:6]
            s2 = f"{head}.{frac}+{rest}" if rest else f"{head}.{frac}"
        return datetime.fromisoformat(s2)
    except Exception:
        return None


def campaign_priority(c: dict, now: datetime):
    state = c.get("state")
    updated = parse_ozon_date(c.get("updatedAt") or c.get("createdAt") or "")
    fresh_ts = updated.timestamp() if updated else 0

    if state == "CAMPAIGN_STATE_RUNNING":
        group = 0
    elif updated and (now - updated) <= timedelta(days=30):
        group = 1
    else:
        group = 2
    return (group, -fresh_ts)


# ---------- АГРЕГАЦИЯ ----------
def aggregate_daily(rows: list) -> dict:
    agg: dict = {}
    for r in rows:
        cid = str(r.get("id", "?"))
        name = r.get("title") or cid
        if cid not in agg:
            agg[cid] = {
                "name": name,
                "expense": 0.0,
                "sales": 0.0,
                "orders": 0,
                "clicks": 0,
                "views": 0,
            }
        agg[cid]["expense"] += parse_money(r.get("moneySpent"))
        agg[cid]["sales"] += parse_money(r.get("ordersMoney"))
        agg[cid]["orders"] += parse_int(r.get("orders"))
        agg[cid]["clicks"] += parse_int(r.get("clicks"))
        agg[cid]["views"] += parse_int(r.get("views"))
    return agg


# ---------- ОТЧЁТЫ ----------
def format_daily_report(rows: list, title: str) -> str:
    if not rows:
        return f"📊 <b>{title}</b>\n\nЗа этот период данных нет."

    agg = aggregate_daily(rows)
    total_expense = sum(v["expense"] for v in agg.values())
    total_sales = sum(v["sales"] for v in agg.values())
    total_orders = sum(v["orders"] for v in agg.values())
    total_clicks = sum(v["clicks"] for v in agg.values())
    total_views = sum(v["views"] for v in agg.values())

    drr = (total_expense / total_sales * 100) if total_sales > 0 else 0
    cpc = (total_expense / total_clicks) if total_clicks > 0 else 0

    dates = sorted({r.get("date", "") for r in rows if r.get("date")})
    period = f"{dates[0]} — {dates[-1]}" if dates else "?"

    lines = [
        f"📊 <b>{title}</b>",
        f"Период: {period}",
        "",
        f"💰 Расход: <b>{total_expense:,.2f} ₽</b>",
        f"📈 Выручка: <b>{total_sales:,.2f} ₽</b>",
        f"🛒 Заказов: {total_orders}",
        f"👁 Показов: {total_views}",
        f"🖱 Кликов: {total_clicks}",
        f"📉 ДРР: <b>{drr:.1f}%</b>",
        f"💵 CPC: {cpc:,.2f} ₽",
        "",
        "<b>По кампаниям:</b>",
    ]
    for cid, info in sorted(agg.items(), key=lambda x: -x[1]["expense"]):
        lines.append(
            f"• {info['name']} (ID: {cid}): "
            f"{info['expense']:,.2f} ₽ · {info['orders']} зак."
        )
    return "\n".join(lines)


def format_threshold_alert(threshold: int, total: float, agg: dict) -> str:
    today_str = datetime.now(MOSCOW_TZ).date().isoformat()
    lines = [
        f"🚨 <b>Превышен порог {threshold:,} ₽</b>",
        f"Дата: {today_str} (МСК)",
        "",
        f"💰 Текущий общий расход: <b>{total:,.2f} ₽</b>",
        "",
        "<b>Кампании, которые потратили:</b>",
    ]
    spent_campaigns = [(cid, v) for cid, v in agg.items() if v["expense"] > 0]
    spent_campaigns.sort(key=lambda x: -x[1]["expense"])
    if not spent_campaigns:
        lines.append("—")
    else:
        for cid, info in spent_campaigns:
            lines.append(
                f"• {info['name']} (ID: {cid}) — "
                f"<b>{info['expense']:,.2f} ₽</b> · {info['orders']} зак."
            )
    return "\n".join(lines)


def format_limit_alert(campaign_name: str, campaign_id: str, limit: float, spent: float) -> str:
    today_str = datetime.now(MOSCOW_TZ).date().isoformat()
    return (
        f"🚨 <b>Превышен дневной лимит</b>\n"
        f"Дата: {today_str} (МСК)\n\n"
        f"📋 Кампания: <b>{campaign_name}</b> (ID: {campaign_id})\n"
        f"💰 Лимит: <b>{limit:,.2f} ₽</b>\n"
        f"💸 Потрачено: <b>{spent:,.2f} ₽</b>\n\n"
        f"⏹ <b>Кампания автоматически отключена.</b>"
    )


# ---------- КЛАВИАТУРА: КАМПАНИИ ----------
async def build_campaigns_keyboard(mode: str, page: int):
    try:
        campaigns = await get_campaigns()
    except Exception as e:
        return f"❌ Ошибка получения кампаний: {e}", None

    today_str = datetime.now(MOSCOW_TZ).date().isoformat()
    try:
        rows = await get_daily_stats(today_str, today_str)
    except Exception:
        rows = []

    agg = aggregate_daily(rows)
    expense_by_campaign = {cid: v["expense"] for cid, v in agg.items()}

    total_expense_today = sum(v["expense"] for v in agg.values())
    total_orders_today = sum(v["orders"] for v in agg.values())
    total_sales_today = sum(v["sales"] for v in agg.values())
    drr_today = (total_expense_today / total_sales_today * 100) if total_sales_today > 0 else 0

    if mode == "cpc":
        filtered = [c for c in campaigns if c.get("PaymentType") == "CPC"]
    elif mode == "cpo":
        filtered = [c for c in campaigns if c.get("PaymentType") == "CPO"]
    else:
        filtered = list(campaigns)

    now = datetime.now(timezone.utc)
    filtered.sort(key=lambda c: campaign_priority(c, now))

    total_pages = max(1, (len(filtered) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    chunk = filtered[start:start + PAGE_SIZE]

    buttons = []
    for c in chunk:
        cid = str(c.get("id"))
        title = c.get("title") or c.get("advObjectType") or "Кампания"
        if len(title) > 22:
            title = title[:19] + "..."

        spent = expense_by_campaign.get(cid, 0.0)
        limit = get_limit(cid)
        state = c.get("state")

        if state == "CAMPAIGN_STATE_RUNNING":
            icon = "🟢"
            action = "off"
            hint = "⏹"
        else:
            updated = parse_ozon_date(c.get("updatedAt") or c.get("createdAt") or "")
            if updated and (now - updated) <= timedelta(days=30):
                icon = "🟡"
            else:
                icon = "⚪"
            action = "on"
            hint = "▶️"

        # Лимит рядом с расходом: "296.37 / 500 ₽" если лимит есть
        if limit > 0:
            text = f"{hint}{icon} {title} — {spent:,.2f} / {limit:,.0f} ₽"
        else:
            text = f"{hint}{icon} {title} — {spent:,.2f} ₽"

        # Одна широкая кнопка на кампанию (действие: включить/выключить)
        buttons.append([
            InlineKeyboardButton(text=text[:64], callback_data=f"{action}:{cid}")
        ])

    # Навигация по страницам
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"pg:{mode}:{page-1}"))
    else:
        nav.append(InlineKeyboardButton(text="·", callback_data="noop"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"pg:{mode}:{page+1}"))
    else:
        nav.append(InlineKeyboardButton(text="·", callback_data="noop"))
    buttons.append(nav)

    # Кнопка лимитов
    buttons.append([InlineKeyboardButton(text="⚙️ Настроить лимиты", callback_data="limits_menu:0")])

    # Переключатель CPC/CPO
    if mode == "cpc":
        buttons.append([InlineKeyboardButton(text="💰 Оплата за заказ", callback_data="pg:cpo:0")])
    else:
        buttons.append([InlineKeyboardButton(text="💳 Оплата за клик", callback_data="pg:cpc:0")])

    label = "оплата за клик (CPC)" if mode == "cpc" else "оплата за заказ (CPO)"
    text = (
        f"📊 <b>Расход за сегодня ({today_str}, МСК): "
        f"{total_expense_today:,.2f} ₽</b>\n"
        f"🛒 Заказов: {total_orders_today} · "
        f"📈 Выручка: {total_sales_today:,.2f} ₽ · "
        f"📉 ДРР: {drr_today:.1f}%\n"
        f"──────────────\n"
        f"📋 <b>Кампании</b> ({label}) — найдено <b>{len(filtered)}</b>, "
        f"страница {page+1}/{total_pages}\n\n"
        f"▶️ — включить · ⏹ — выключить\n"
        f"Сумма вида <code>296.37 / 500 ₽</code> = расход / лимит\n"
        f"🟢 активные · 🟡 за последний месяц · ⚪ архив"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------- КЛАВИАТУРА: ЭКРАН ЛИМИТОВ ----------
async def build_limits_keyboard(page: int):
    """Отдельный экран для настройки лимитов."""
    try:
        campaigns = await get_campaigns()
    except Exception as e:
        return f"❌ Ошибка получения кампаний: {e}", None

    # Показываем только CPC — как и основной список
    filtered = [c for c in campaigns if c.get("PaymentType") == "CPC"]
    now = datetime.now(timezone.utc)
    filtered.sort(key=lambda c: campaign_priority(c, now))

    total_pages = max(1, (len(filtered) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    chunk = filtered[start:start + PAGE_SIZE]

    buttons = []
    for c in chunk:
        cid = str(c.get("id"))
        title = c.get("title") or c.get("advObjectType") or "Кампания"
        if len(title) > 22:
            title = title[:19] + "..."

        limit = get_limit(cid)
        state = c.get("state")
        icon = "🟢" if state == "CAMPAIGN_STATE_RUNNING" else "⚪"

        if limit > 0:
            text = f"{icon} {title} — лимит {limit:,.0f} ₽"
        else:
            text = f"{icon} {title} — без лимита"

        buttons.append([
            InlineKeyboardButton(text=text[:64], callback_data=f"limit:{cid}")
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"limits_menu:{page-1}"))
    else:
        nav.append(InlineKeyboardButton(text="·", callback_data="noop"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"limits_menu:{page+1}"))
    else:
        nav.append(InlineKeyboardButton(text="·", callback_data="noop"))
    buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="⬅️ Назад к кампаниям", callback_data="pg:cpc:0")])

    text = (
        f"⚙️ <b>Настройка дневных лимитов</b>\n\n"
        f"Нажми на кампанию, чтобы задать или изменить лимит.\n"
        f"Отправь <code>0</code> при вводе, чтобы <b>убрать</b> лимит.\n"
        f"Кампании без лимита <b>не отключаются</b> автоматически.\n\n"
        f"Страница {page+1}/{total_pages}"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------- КОМАНДЫ ----------
@dp.message(Command("myid"))
async def cmd_myid(msg: Message):
    await msg.answer(
        f"Твой Telegram ID: <code>{msg.from_user.id}</code>\n"
        f"Отправь его владельцу бота, чтобы получить доступ.",
        parse_mode="HTML"
    )


@dp.message(Command("start"))
async def cmd_start(msg: Message):
    if not has_access(msg.from_user.id):
        await msg.answer(
            "⛔ У тебя нет доступа к этому боту.\n\n"
            "Узнай свой ID командой /myid и отправь его владельцу."
        )
        return
    await msg.answer(
        "Привет! Я слежу за рекламными расходами Ozon.\n\n"
        "Команды:\n"
        "/today — расходы за сегодня (МСК)\n"
        "/week — расходы за 7 дней\n"
        "/campaigns — список кампаний: включить / выключить / лимит"
    )


@dp.message(Command("today"))
async def cmd_today(msg: Message):
    if not has_access(msg.from_user.id):
        return
    try:
        today = datetime.now(MOSCOW_TZ).date().isoformat()
        rows = await get_daily_stats(today, today)
    except Exception as e:
        await msg.answer(f"❌ Ошибка: <code>{e}</code>", parse_mode="HTML")
        return
    title = f"Расходы за сегодня ({datetime.now(MOSCOW_TZ).date().isoformat()}, МСК)"
    await msg.answer(format_daily_report(rows, title), parse_mode="HTML")


@dp.message(Command("week"))
async def cmd_week(msg: Message):
    if not has_access(msg.from_user.id):
        return
    date_to_msk = datetime.now(MOSCOW_TZ).date()
    date_from_msk = date_to_msk - timedelta(days=6)
    try:
        rows = await get_daily_stats(date_from_msk.isoformat(), date_to_msk.isoformat())
    except Exception as e:
        await msg.answer(f"❌ Ошибка: <code>{e}</code>", parse_mode="HTML")
        return
    await msg.answer(format_daily_report(rows, "Расходы за 7 дней (МСК)"), parse_mode="HTML")


@dp.message(Command("campaigns"))
async def cmd_campaigns(msg: Message):
    if not has_access(msg.from_user.id):
        return
    text, kb = await build_campaigns_keyboard("cpc", 0)
    if kb is None:
        await msg.answer(text)
    else:
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")


# ---------- КНОПКИ ----------
@dp.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


@dp.callback_query(F.data.startswith("pg:"))
async def cb_paginate(cb: CallbackQuery):
    if not has_access(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    try:
        _, mode, page_str = cb.data.split(":")
        page = int(page_str)
    except Exception:
        mode, page = "cpc", 0

    text, kb = await build_campaigns_keyboard(mode, page)
    if kb is None:
        await cb.answer(text, show_alert=True)
        return
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass
    await cb.answer()


@dp.callback_query(F.data.startswith("limits_menu:"))
async def cb_limits_menu(cb: CallbackQuery):
    if not has_access(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    try:
        _, page_str = cb.data.split(":")
        page = int(page_str)
    except Exception:
        page = 0

    text, kb = await build_limits_keyboard(page)
    if kb is None:
        await cb.answer(text, show_alert=True)
        return
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass
    await cb.answer()


@dp.callback_query(F.data.startswith("off:"))
async def cb_off(cb: CallbackQuery):
    if not has_access(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    cid = cb.data.split(":", 1)[1]
    try:
        await deactivate_campaign(int(cid))
        await cb.answer(f"⏹ Кампания {cid} выключена", show_alert=True)
        text, kb = await build_campaigns_keyboard("cpc", 0)
        if kb:
            try:
                await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
    except Exception as e:
        await cb.answer(f"❌ Ошибка: {e}", show_alert=True)


@dp.callback_query(F.data.startswith("on:"))
async def cb_on(cb: CallbackQuery):
    if not has_access(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    cid = cb.data.split(":", 1)[1]
    try:
        await activate_campaign(int(cid))
        await cb.answer(f"▶️ Кампания {cid} включена", show_alert=True)
        text, kb = await build_campaigns_keyboard("cpc", 0)
        if kb:
            try:
                await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
    except Exception as e:
        await cb.answer(f"❌ Ошибка: {e}", show_alert=True)


# ---------- УСТАНОВКА ЛИМИТА ----------
@dp.callback_query(F.data.startswith("limit:"))
async def cb_limit(cb: CallbackQuery, state: FSMContext):
    if not has_access(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    cid = cb.data.split(":", 1)[1]
    current_limit = get_limit(cid)

    # Найдём название кампании
    try:
        campaigns = await get_campaigns()
        name = next(
            (c.get("title") or cid for c in campaigns if str(c.get("id")) == cid),
            cid
        )
    except Exception:
        name = cid

    await state.update_data(campaign_id=cid)
    await state.set_state(LimitForm.waiting_amount)

    await cb.message.answer(
        f"⚙️ Кампания: <b>{name}</b> (ID: {cid})\n\n"
        f"Введи дневной лимит в рублях.\n"
        f"Текущий лимит: <b>{current_limit:,.2f} ₽</b>\n"
        f"Чтобы убрать лимит — отправь <code>0</code>.\n\n"
        f"Отмена — /cancel",
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext):
    if await state.get_state() is None:
        return
    await state.clear()
    await msg.answer("❌ Установка лимита отменена.")


@dp.message(LimitForm.waiting_amount)
async def process_limit_amount(msg: Message, state: FSMContext):
    if not has_access(msg.from_user.id):
        return

    data = await state.get_data()
    cid = data.get("campaign_id")

    text = msg.text.strip().replace(" ", "").replace(",", ".")
    try:
        amount = float(text)
    except ValueError:
        await msg.answer(
            "❌ Не понял сумму. Введи число, например: <code>500</code> или <code>1500.50</code>",
            parse_mode="HTML"
        )
        return

    if amount < 0:
        await msg.answer("❌ Сумма не может быть отрицательной.")
        return

    set_limit(cid, amount)
    await state.clear()

    if amount == 0:
        await msg.answer(
            f"✅ Лимит для кампании <b>{cid}</b> убран.\n"
            f"Кампания больше не будет проверяться.",
            parse_mode="HTML"
        )
    else:
        await msg.answer(
            f"✅ Лимит <b>{amount:,.2f} ₽</b> установлен для кампании <b>{cid}</b>.\n\n"
            f"Бот проверяет расход каждые 30 минут и отключит кампанию при превышении.",
            parse_mode="HTML"
        )

    # Обновляем экран лимитов
    try:
        text, kb = await build_limits_keyboard(0)
        if kb:
            await msg.answer(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass


# ---------- ПРОВЕРКА ПОРОГОВ И ЛИМИТОВ ----------
async def check_thresholds():
    try:
        today_str = datetime.now(MOSCOW_TZ).date().isoformat()
        rows = await get_daily_stats(today_str, today_str)
        agg = aggregate_daily(rows)
        total = sum(v["expense"] for v in agg.values())

        # --- Пороги ---
        notified = get_notified_today()
        for threshold in THRESHOLDS:
            if total >= threshold and threshold not in notified:
                text = format_threshold_alert(threshold, total, agg)
                for uid in ALLOWED_IDS:
                    try:
                        await bot.send_message(uid, text, parse_mode="HTML")
                    except Exception:
                        pass
                mark_notified(threshold)
                print(f"Порог {threshold} — уведомление отправлено. Расход: {total:.2f}")

        # --- Лимиты ---
        limits = load_limits()
        if not limits:
            return

        campaigns = await get_campaigns()
        camp_names = {
            str(c.get("id")): (c.get("title") or str(c.get("id")))
            for c in campaigns
        }

        for cid_str, limit in list(limits.items()):
            if not limit or limit <= 0:
                continue

            spent = agg.get(cid_str, {}).get("expense", 0.0)
            if spent >= limit:
                name = camp_names.get(cid_str, cid_str)
                try:
                    await deactivate_campaign(int(cid_str))
                    alert = format_limit_alert(name, cid_str, limit, spent)
                    for uid in ALLOWED_IDS:
                        try:
                            await bot.send_message(uid, alert, parse_mode="HTML")
                        except Exception:
                            pass
                    limits[cid_str] = 0
                    save_limits(limits)
                    print(f"Кампания {cid_str} отключена (лимит {limit}, расход {spent:.2f})")
                except Exception as e:
                    print(f"Не удалось отключить кампанию {cid_str}: {e}")
    except Exception as e:
        print(f"Ошибка проверки: {e}")


# ---------- ЗАПУСК ----------
async def main():
    scheduler.add_job(check_thresholds, "interval", minutes=30)
    scheduler.start()
    print(f"Бот запущен. Доступ у: {ALLOWED_IDS}. Пороги: {THRESHOLDS}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())