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
    get_product_stats,
    get_campaign_skus,
    set_campaign_bid,
    activate_campaign,
    deactivate_campaign,
    get_balance,
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
BID_PAGE_SIZE = 6
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


class BidForm(StatesGroup):
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


# ---------- МЕНЮ ----------
def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="menu:stats")],
        [InlineKeyboardButton(text="📋 Кампании", callback_data="menu:campaigns")],
        [InlineKeyboardButton(text="💰 Настройка ставок", callback_data="menu:bids")],
        [InlineKeyboardButton(text="💳 Баланс", callback_data="menu:balance")],
    ])


def balance_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Текущий баланс", callback_data="balance:current")],
        [InlineKeyboardButton(text="📊 Расходы", callback_data="balance:expenses")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="menu:home")],
    ])


def balance_expenses_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 За сегодня", callback_data="balance_period:1")],
        [InlineKeyboardButton(text="📊 За 7 дней", callback_data="balance_period:7")],
        [InlineKeyboardButton(text="📈 За месяц", callback_data="balance_period:30")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:balance")],
    ])


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="menu:home")],
    ])


def stats_period_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 За 1 день", callback_data="stats_period:1")],
        [InlineKeyboardButton(text="📊 За 7 дней", callback_data="stats_period:7")],
        [InlineKeyboardButton(text="📈 За 30 дней", callback_data="stats_period:30")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="menu:home")],
    ])


# ---------- КАМПАНИИ ----------
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

        if limit > 0:
            text = f"{hint}{icon} {title} — {spent:,.2f} / {limit:,.0f} ₽"
        else:
            text = f"{hint}{icon} {title} — {spent:,.2f} ₽"

        buttons.append([
            InlineKeyboardButton(text=text[:64], callback_data=f"{action}:{cid}")
        ])

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

    buttons.append([InlineKeyboardButton(text="📈 Статистика кампаний", callback_data="stats_menu:0")])
    buttons.append([InlineKeyboardButton(text="⚙️ Настроить лимиты", callback_data="limits_menu:0")])

    if mode == "cpc":
        buttons.append([InlineKeyboardButton(text="💰 Оплата за заказ", callback_data="pg:cpo:0")])
    else:
        buttons.append([InlineKeyboardButton(text="💳 Оплата за клик", callback_data="pg:cpc:0")])

    buttons.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="menu:home")])

    label = "оплата за клик (CPC)" if mode == "cpc" else "оплата за заказ (CPO)"
    text = (
        f"📊 <b>Расход за сегодня ({today_str}, МСК): "
        f"{total_expense_today:,.2f} ₽</b>\n"
        f"🛒 Заказов: {total_orders_today} · "
        f"📈 Выручка: {total_sales_today:,.2f} ₽ · "
        f"📉 ДРР: {drr_today:.1f}%\n"
        f"──────────────\n"
        f"📋 <b>Кампании</b> ({label}) — найдено <b>{len(filtered)}</b>, "
        f"страница {page+1}/{total_pages}"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------- ЛИМИТЫ ----------
async def build_limits_keyboard(page: int):
    try:
        campaigns = await get_campaigns()
    except Exception as e:
        return f"❌ Ошибка получения кампаний: {e}", None

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

    buttons.append([InlineKeyboardButton(text="⬅️ Назад к кампаниям", callback_data="menu:campaigns")])
    buttons.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="menu:home")])

    text = (
        f"⚙️ <b>Настройка дневных лимитов</b>\n\n"
        f"Нажми на кампанию, чтобы задать или изменить лимит.\n"
        f"Отправь <code>0</code> при вводе, чтобы <b>убрать</b> лимит.\n"
        f"Кампании без лимита <b>не отключаются</b> автоматически.\n\n"
        f"Страница {page+1}/{total_pages}"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------- СТАТИСТИКА КАМПАНИЙ ----------
async def build_stats_menu_keyboard(page: int):
    try:
        campaigns = await get_campaigns()
    except Exception as e:
        return f"❌ Ошибка получения кампаний: {e}", None

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
        if len(title) > 28:
            title = title[:25] + "..."
        state = c.get("state")
        icon = "🟢" if state == "CAMPAIGN_STATE_RUNNING" else "⚪"
        text = f"{icon} {title}"
        buttons.append([InlineKeyboardButton(text=text[:64], callback_data=f"stats:{cid}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"stats_menu:{page-1}"))
    else:
        nav.append(InlineKeyboardButton(text="·", callback_data="noop"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"stats_menu:{page+1}"))
    else:
        nav.append(InlineKeyboardButton(text="·", callback_data="noop"))
    buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="⬅️ Назад к кампаниям", callback_data="menu:campaigns")])
    buttons.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="menu:home")])

    text = (
        f"📈 <b>Статистика кампаний</b>\n\n"
        f"Выбери кампанию, чтобы посмотреть подробную статистику за сегодня.\n\n"
        f"Страница {page+1}/{total_pages}"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def build_campaign_stats_view(campaign_id: str):
    try:
        campaigns = await get_campaigns()
    except Exception as e:
        return f"❌ Ошибка получения кампаний: {e}", None

    campaign = next((c for c in campaigns if str(c.get("id")) == campaign_id), None)
    if not campaign:
        return f"❌ Кампания {campaign_id} не найдена.", None

    name = campaign.get("title") or campaign.get("advObjectType") or "Кампания"
    state = campaign.get("state")
    today_str = datetime.now(MOSCOW_TZ).date().isoformat()

    row = None
    try:
        rows = await get_product_stats(today_str, today_str, [campaign_id])
        row = next((r for r in rows if str(r.get("id")) == campaign_id), None)
    except Exception as e:
        print(f"Не удалось получить статистику: {e}")

    if row:
        views = parse_int(row.get("views"))
        clicks = parse_int(row.get("clicks"))
        orders = parse_int(row.get("orders"))
        spent = parse_money(row.get("moneySpent"))
        sales = parse_money(row.get("ordersMoney"))
        cart_adds = parse_int(row.get("toCart"))
    else:
        views = clicks = orders = 0
        spent = sales = 0.0
        cart_adds = 0

    cpc = (spent / clicks) if clicks > 0 else 0
    drr = (spent / sales * 100) if sales > 0 else 0
    cr = (orders / clicks * 100) if clicks > 0 else 0
    ctr = (clicks / views * 100) if views > 0 else 0

    status_icon = "🟢 активна" if state == "CAMPAIGN_STATE_RUNNING" else "⚪ неактивна"

    lines = [
        f"📈 <b>{name}</b>",
        f"ID: <code>{campaign_id}</code> · {status_icon}",
        f"Дата: {today_str} (МСК)",
        "",
        f"👁 <b>Показы:</b> {views:,}".replace(",", " "),
        f"🖱 <b>Клики:</b> {clicks:,}".replace(",", " "),
        f"🛒 <b>Добавления в корзину:</b> {cart_adds}",
        f"📦 <b>Заказы:</b> {orders}",
        f"💰 <b>Расход за сегодня:</b> {spent:,.2f} ₽",
        "",
        "──────────────",
        f"📊 CTR: <b>{ctr:.2f}%</b>",
        f"💵 CPC: {cpc:,.2f} ₽",
        f"📈 Конверсия: {cr:.1f}%",
        f"💰 Выручка: {sales:,.2f} ₽",
        f"📉 ДРР: <b>{drr:.1f}%</b>",
    ]

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"stats:{campaign_id}")],
        [InlineKeyboardButton(text="⬅️ Назад к списку", callback_data="stats_menu:0")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="menu:home")],
    ])
    return "\n".join(lines), keyboard


# ---------- СТАВКИ ----------
async def build_bids_menu_keyboard(page: int):
    try:
        campaigns = await get_campaigns()
    except Exception as e:
        return f"❌ Ошибка получения кампаний: {e}", None

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
        if len(title) > 28:
            title = title[:25] + "..."
        state = c.get("state")
        icon = "🟢" if state == "CAMPAIGN_STATE_RUNNING" else "⚪"
        text = f"{icon} {title}"
        buttons.append([InlineKeyboardButton(text=text[:64], callback_data=f"bids:{cid}:0")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"bids_menu:{page-1}"))
    else:
        nav.append(InlineKeyboardButton(text="·", callback_data="noop"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"bids_menu:{page+1}"))
    else:
        nav.append(InlineKeyboardButton(text="·", callback_data="noop"))
    buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="menu:home")])

    text = (
        f"💰 <b>Настройка ставок</b>\n\n"
        f"Выбери кампанию, чтобы посмотреть ставки по её SKU.\n\n"
        f"Страница {page+1}/{total_pages}"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def build_bids_view_keyboard(campaign_id: str, page: int):
    try:
        skus = await get_campaign_skus(int(campaign_id))
    except Exception as e:
        return f"❌ Ошибка: {e}", None

    if not skus:
        return (
            f"⚠️ Не удалось получить SKU для кампании <b>{campaign_id}</b>.\n\n"
            f"Список пуст.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К списку кампаний", callback_data="bids_menu:0")],
                [InlineKeyboardButton(text="🏠 В главное меню", callback_data="menu:home")],
            ])
        )

    camp_name = f"ID {campaign_id}"
    try:
        campaigns = await get_campaigns()
        camp = next((c for c in campaigns if str(c.get("id")) == campaign_id), None)
        if camp:
            camp_name = camp.get("title") or camp_name
    except Exception:
        pass

    total_pages = max(1, (len(skus) + BID_PAGE_SIZE - 1) // BID_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * BID_PAGE_SIZE
    chunk = skus[start:start + BID_PAGE_SIZE]

    buttons = []
    for p in chunk:
        sku = p["sku"]
        title = p["title"]
        if len(title) > 22:
            title = title[:19] + "..."
        bid = p["bid"]

        text = f"✏️ {title} — {bid:.2f} ₽"
        buttons.append([
            InlineKeyboardButton(
                text=text[:64],
                callback_data=f"setbid:{campaign_id}:{sku}"
            )
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"bids:{campaign_id}:{page-1}"))
    else:
        nav.append(InlineKeyboardButton(text="·", callback_data="noop"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"bids:{campaign_id}:{page+1}"))
    else:
        nav.append(InlineKeyboardButton(text="·", callback_data="noop"))
    buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"bids:{campaign_id}:0")])
    buttons.append([InlineKeyboardButton(text="⬅️ К списку кампаний", callback_data="bids_menu:0")])
    buttons.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="menu:home")])

    text = (
        f"💰 <b>{camp_name}</b> (ID: {campaign_id})\n"
        f"Всего SKU: <b>{len(skus)}</b>, страница {page+1}/{total_pages}\n\n"
        f"Нажми на <b>товар</b>, чтобы изменить ставку клика."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------- ВСПОМОГАТЕЛЬНЫЕ ----------
async def do_period(days: int) -> str:
    date_to_msk = datetime.now(MOSCOW_TZ).date()
    date_from_msk = date_to_msk - timedelta(days=days - 1)
    rows = await get_daily_stats(date_from_msk.isoformat(), date_to_msk.isoformat())
    if days == 1:
        title = f"Расходы за сегодня ({date_to_msk.isoformat()}, МСК)"
    elif days == 7:
        title = "Расходы за 7 дней (МСК)"
    elif days == 30:
        title = "Расходы за месяц (МСК)"
    else:
        title = f"Расходы за {days} дней (МСК)"
    return format_daily_report(rows, title)


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
        "👋 <b>Привет!</b>\n\n"
        "Я помогаю следить за рекламными расходами Ozon.\n"
        "Выбери, что тебя интересует:",
        reply_markup=main_menu_keyboard(),
        parse_mode="HTML"
    )


@dp.message(Command("today"))
async def cmd_today(msg: Message):
    if not has_access(msg.from_user.id):
        return
    try:
        text = await do_period(1)
    except Exception as e:
        await msg.answer(f"❌ Ошибка: <code>{e}</code>", parse_mode="HTML")
        return
    await msg.answer(text, parse_mode="HTML", reply_markup=back_to_menu_keyboard())


@dp.message(Command("week"))
async def cmd_week(msg: Message):
    if not has_access(msg.from_user.id):
        return
    try:
        text = await do_period(7)
    except Exception as e:
        await msg.answer(f"❌ Ошибка: <code>{e}</code>", parse_mode="HTML")
        return
    await msg.answer(text, parse_mode="HTML", reply_markup=back_to_menu_keyboard())


@dp.message(Command("campaigns"))
async def cmd_campaigns(msg: Message):
    if not has_access(msg.from_user.id):
        return
    text, kb = await build_campaigns_keyboard("cpc", 0)
    if kb is None:
        await msg.answer(text)
    else:
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")


# ---------- ГЛАВНОЕ МЕНЮ ----------
@dp.callback_query(F.data == "menu:home")
async def cb_menu_home(cb: CallbackQuery):
    if not has_access(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    try:
        await cb.message.edit_text(
            "👋 <b>Главное меню</b>\n\nВыбери, что тебя интересует:",
            reply_markup=main_menu_keyboard(),
            parse_mode="HTML"
        )
    except Exception:
        pass
    await cb.answer()


@dp.callback_query(F.data == "menu:stats")
async def cb_menu_stats(cb: CallbackQuery):
    if not has_access(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    try:
        await cb.message.edit_text(
            "📊 <b>Статистика</b>\n\nВыбери период:",
            reply_markup=stats_period_keyboard(),
            parse_mode="HTML"
        )
    except Exception:
        pass
    await cb.answer()


@dp.callback_query(F.data.startswith("stats_period:"))
async def cb_stats_period(cb: CallbackQuery):
    if not has_access(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    try:
        _, days_str = cb.data.split(":")
        days = int(days_str)
    except Exception:
        days = 1

    await cb.answer("Загружаю...")
    try:
        text = await do_period(days)
        await cb.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад к периодам", callback_data="menu:stats")],
                [InlineKeyboardButton(text="🏠 В главное меню", callback_data="menu:home")],
            ])
        )
    except Exception as e:
        await cb.answer(f"❌ Ошибка: {e}", show_alert=True)


@dp.callback_query(F.data == "menu:balance")
async def cb_menu_balance(cb: CallbackQuery):
    if not has_access(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    try:
        await cb.message.edit_text(
            "💳 <b>Баланс</b>\n\nВыбери, что тебя интересует:",
            reply_markup=balance_menu_keyboard(),
            parse_mode="HTML"
        )
    except Exception:
        pass
    await cb.answer()


@dp.callback_query(F.data == "balance:current")
async def cb_balance_current(cb: CallbackQuery):
    if not has_access(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    await cb.answer("Загружаю баланс...")
    try:
        data = await get_balance()
        total = data.get("total", {})

        closing = total.get("closing_balance", {}) or {}
        opening = total.get("opening_balance", {}) or {}
        accrued = total.get("accrued", {}) or {}

        closing_val = closing.get("value", 0)
        opening_val = opening.get("value", 0)
        accrued_val = accrued.get("value", 0)

        today_str = datetime.now(MOSCOW_TZ).date().isoformat()

        text = (
            f"💰 <b>Баланс рекламного кабинета</b>\n\n"
            f"<b>Текущий баланс:</b> {closing_val:,.2f} ₽\n"
            f"На начало периода: {opening_val:,.2f} ₽\n"
            f"Начислено: {accrued_val:,.2f} ₽\n\n"
            f"<i>Данные на {today_str} (МСК)</i>"
        )
        await cb.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="balance:current")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:balance")],
                [InlineKeyboardButton(text="🏠 В главное меню", callback_data="menu:home")],
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        await cb.message.edit_text(
            f"❌ Не удалось получить баланс.\n\n"
            f"Ошибка: <code>{e}</code>\n\n"
            f"Проверь ключи <code>OZON_SELLER_CLIENT_ID</code> и "
            f"<code>OZON_SELLER_API_KEY</code> в <code>.env</code>.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Повторить", callback_data="balance:current")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:balance")],
            ]),
            parse_mode="HTML"
        )


@dp.callback_query(F.data == "balance:expenses")
async def cb_balance_expenses(cb: CallbackQuery):
    if not has_access(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    try:
        await cb.message.edit_text(
            "📊 <b>Расходы</b>\n\nВыбери период:",
            reply_markup=balance_expenses_keyboard(),
            parse_mode="HTML"
        )
    except Exception:
        pass
    await cb.answer()


@dp.callback_query(F.data.startswith("balance_period:"))
async def cb_balance_period(cb: CallbackQuery):
    if not has_access(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    try:
        _, days_str = cb.data.split(":")
        days = int(days_str)
    except Exception:
        days = 1

    await cb.answer("Загружаю...")
    try:
        text = await do_period(days)
        await cb.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад к периодам", callback_data="balance:expenses")],
                [InlineKeyboardButton(text="🏠 В главное меню", callback_data="menu:home")],
            ])
        )
    except Exception as e:
        await cb.answer(f"❌ Ошибка: {e}", show_alert=True)


@dp.callback_query(F.data == "menu:campaigns")
async def cb_menu_campaigns(cb: CallbackQuery):
    if not has_access(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await cb.answer("Загружаю...")
    try:
        text, kb = await build_campaigns_keyboard("cpc", 0)
        if kb is None:
            await cb.message.edit_text(text)
        else:
            await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        await cb.answer(f"❌ Ошибка: {e}", show_alert=True)


@dp.callback_query(F.data == "menu:bids")
async def cb_menu_bids(cb: CallbackQuery):
    if not has_access(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await cb.answer("Загружаю...")
    try:
        text, kb = await build_bids_menu_keyboard(0)
        if kb is None:
            await cb.message.edit_text(text)
        else:
            await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        await cb.answer(f"❌ Ошибка: {e}", show_alert=True)


# ---------- КНОПКИ КАМПАНИЙ ----------
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


@dp.callback_query(F.data.startswith("stats_menu:"))
async def cb_stats_menu(cb: CallbackQuery):
    if not has_access(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    try:
        _, page_str = cb.data.split(":")
        page = int(page_str)
    except Exception:
        page = 0

    text, kb = await build_stats_menu_keyboard(page)
    if kb is None:
        await cb.answer(text, show_alert=True)
        return
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass
    await cb.answer()


@dp.callback_query(F.data.startswith("stats:"))
async def cb_stats_view(cb: CallbackQuery):
    if not has_access(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    cid = cb.data.split(":", 1)[1]
    await cb.answer("Загружаю...")
    try:
        text, kb = await build_campaign_stats_view(cid)
        if kb is None:
            await cb.message.edit_text(text)
        else:
            await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        await cb.answer(f"❌ Ошибка: {e}", show_alert=True)


# ---------- СТАВКИ: КНОПКИ ----------
@dp.callback_query(F.data.startswith("bids_menu:"))
async def cb_bids_menu(cb: CallbackQuery):
    if not has_access(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    try:
        _, page_str = cb.data.split(":")
        page = int(page_str)
    except Exception:
        page = 0

    text, kb = await build_bids_menu_keyboard(page)
    if kb is None:
        await cb.answer(text, show_alert=True)
        return
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass
    await cb.answer()


@dp.callback_query(F.data.startswith("bids:"))
async def cb_bids_view(cb: CallbackQuery):
    if not has_access(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    try:
        _, campaign_id, page_str = cb.data.split(":")
        page = int(page_str)
    except Exception:
        await cb.answer("Ошибка разбора данных", show_alert=True)
        return

    await cb.answer("Загружаю SKU...")
    try:
        text, kb = await build_bids_view_keyboard(campaign_id, page)
        if kb is None:
            await cb.message.edit_text(text)
        else:
            await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        await cb.answer(f"❌ Ошибка: {e}", show_alert=True)


@dp.callback_query(F.data.startswith("setbid:"))
async def cb_setbid(cb: CallbackQuery, state: FSMContext):
    if not has_access(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    try:
        _, campaign_id, sku = cb.data.split(":", 2)
    except Exception:
        await cb.answer("Ошибка разбора данных", show_alert=True)
        return

    await state.update_data(campaign_id=campaign_id, sku=sku)
    await state.set_state(BidForm.waiting_amount)

    await cb.message.answer(
        f"✏️ <b>Изменение ставки</b>\n"
        f"Кампания ID: <code>{campaign_id}</code>\n"
        f"SKU: <code>{sku}</code>\n\n"
        f"Введи новую ставку в <b>рублях</b> (например: <code>2.6</code> или <code>10</code>).\n\n"
        f"Отмена — /cancel",
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(BidForm.waiting_amount)
async def process_bid_amount(msg: Message, state: FSMContext):
    if not has_access(msg.from_user.id):
        return

    data = await state.get_data()
    campaign_id = data.get("campaign_id")
    sku = data.get("sku")

    text_raw = msg.text.strip().replace(" ", "").replace(",", ".")
    try:
        amount = float(text_raw)
    except ValueError:
        await msg.answer("❌ Введи число, например: <code>2.6</code>", parse_mode="HTML")
        return

    if amount <= 0:
        await msg.answer("❌ Ставка должна быть больше 0.")
        return

    try:
        await set_campaign_bid(int(campaign_id), sku, amount)
    except Exception as e:
        await state.clear()
        await msg.answer(f"❌ Не удалось обновить: <code>{e}</code>", parse_mode="HTML")
        return

    await state.clear()
    await msg.answer(
        f"✅ Ставка <b>{amount:.2f} ₽</b> установлена для SKU <code>{sku}</code>.",
        parse_mode="HTML"
    )

    try:
        text_view, kb = await build_bids_view_keyboard(campaign_id, 0)
        if kb:
            await msg.answer(text_view, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass


# ---------- ЛИМИТЫ ----------
@dp.callback_query(F.data.startswith("limit:"))
async def cb_limit(cb: CallbackQuery, state: FSMContext):
    if not has_access(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    cid = cb.data.split(":", 1)[1]
    current_limit = get_limit(cid)

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
    await msg.answer("❌ Действие отменено.")


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
            "❌ Не понял сумму. Введи число, например: <code>500</code>",
            parse_mode="HTML"
        )
        return

    if amount < 0:
        await msg.answer("❌ Сумма не может быть отрицательной.")
        return

    set_limit(cid, amount)
    await state.clear()

    if amount == 0:
        await msg.answer(f"✅ Лимит для кампании <b>{cid}</b> убран.", parse_mode="HTML")
    else:
        await msg.answer(
            f"✅ Лимит <b>{amount:,.2f} ₽</b> установлен для кампании <b>{cid}</b>.",
            parse_mode="HTML"
        )

    try:
        text, kb = await build_limits_keyboard(0)
        if kb:
            await msg.answer(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass


# ---------- ON / OFF ----------
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


# ---------- ПОРОГИ И ЛИМИТЫ ----------
async def check_thresholds():
    try:
        today_str = datetime.now(MOSCOW_TZ).date().isoformat()
        rows = await get_daily_stats(today_str, today_str)
        agg = aggregate_daily(rows)
        total = sum(v["expense"] for v in agg.values())

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
