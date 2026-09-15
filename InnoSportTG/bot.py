"""
InnoSport Telegram Bot — записаться/отменить запись + админка + автозапись.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict

import requests
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (
    ADMIN_USER_IDS,
    ALLOWED_USER_IDS,
    BOT_TOKEN,
    SUPER_ADMIN_ID,
)


API_BASE = "https://sport.innopolis.university/api"
SITE_ORIGIN = "https://sport.innopolis.university"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSIONS_FILE = os.path.join(BASE_DIR, "sessions.json")
SUBSCRIPTIONS_FILE = os.path.join(BASE_DIR, "subscriptions.json")
USERS_FILE = os.path.join(BASE_DIR, "allowed_users.json")
ADMINS_FILE = os.path.join(BASE_DIR, "admins.json")
USER_NAMES_FILE = os.path.join(BASE_DIR, "user_names.json")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("innosport-bot")


# ---------------------------------------------------------------------------
# Ошибки
# ---------------------------------------------------------------------------

class SessionExpiredError(Exception):
    pass


class NotLoggedInError(Exception):
    pass


class NetworkError(Exception):
    pass


# ---------------------------------------------------------------------------
# Имена пользователей
# ---------------------------------------------------------------------------

USER_NAMES: dict[int, dict[str, str]] = {}


def load_user_names() -> None:
    global USER_NAMES
    if not os.path.exists(USER_NAMES_FILE):
        USER_NAMES = {}
        return
    try:
        with open(USER_NAMES_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        USER_NAMES = {int(k): v for k, v in raw.items()}
    except Exception:
        log.exception("Не удалось загрузить user_names.json")
        USER_NAMES = {}


def save_user_names() -> None:
    try:
        with open(USER_NAMES_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {str(k): v for k, v in USER_NAMES.items()},
                f, ensure_ascii=False, indent=2,
            )
    except Exception:
        log.exception("Не удалось сохранить user_names.json")


def remember_user_name(user_id, first_name=None, username=None) -> None:
    entry = USER_NAMES.get(user_id, {})
    if first_name:
        entry["first_name"] = first_name
    if username:
        entry["username"] = username
    if entry:
        USER_NAMES[user_id] = entry
        save_user_names()


def format_user_label(user_id: int) -> str:
    entry = USER_NAMES.get(user_id) or {}
    first = entry.get("first_name")
    uname = entry.get("username")
    parts = []
    if first:
        parts.append(first)
    if uname:
        parts.append(f"@{uname}")
    return " ".join(parts) if parts else "—"


# ---------------------------------------------------------------------------
# Белый список и админы
# ---------------------------------------------------------------------------

def load_allowed_users() -> None:
    if not os.path.exists(USERS_FILE):
        return
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for x in raw:
            ALLOWED_USER_IDS.add(int(x))
    except Exception:
        log.exception("Не удалось загрузить allowed_users.json")


def save_allowed_users() -> None:
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(ALLOWED_USER_IDS), f,
                      ensure_ascii=False, indent=2)
    except Exception:
        log.exception("Не удалось сохранить allowed_users.json")


def add_allowed_user(user_id: int) -> bool:
    if user_id in ALLOWED_USER_IDS:
        return False
    ALLOWED_USER_IDS.add(user_id)
    save_allowed_users()
    return True


def remove_allowed_user(user_id: int) -> bool:
    if user_id == SUPER_ADMIN_ID or user_id in ADMIN_USER_IDS:
        return False
    if user_id not in ALLOWED_USER_IDS:
        return False
    ALLOWED_USER_IDS.discard(user_id)
    save_allowed_users()
    return True


def load_admins() -> None:
    if not os.path.exists(ADMINS_FILE):
        return
    try:
        with open(ADMINS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for x in raw:
            ADMIN_USER_IDS.add(int(x))
    except Exception:
        log.exception("Не удалось загрузить admins.json")


def save_admins() -> None:
    try:
        with open(ADMINS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(ADMIN_USER_IDS), f,
                      ensure_ascii=False, indent=2)
    except Exception:
        log.exception("Не удалось сохранить admins.json")


def is_admin(user_id: int) -> bool:
    return user_id == SUPER_ADMIN_ID or user_id in ADMIN_USER_IDS


def is_super_admin(user_id: int) -> bool:
    return user_id == SUPER_ADMIN_ID


def add_admin(user_id: int) -> bool:
    if user_id == SUPER_ADMIN_ID or user_id in ADMIN_USER_IDS:
        return False
    ADMIN_USER_IDS.add(user_id)
    save_admins()
    if user_id not in ALLOWED_USER_IDS:
        ALLOWED_USER_IDS.add(user_id)
        save_allowed_users()
    return True


def remove_admin(user_id: int) -> bool:
    if user_id == SUPER_ADMIN_ID:
        return False
    if user_id not in ADMIN_USER_IDS:
        return False
    ADMIN_USER_IDS.discard(user_id)
    save_admins()
    return True


# ---------------------------------------------------------------------------
# Per-user сессии
# ---------------------------------------------------------------------------

USER_COOKIES: dict[int, dict[str, str]] = {}
_SESSION_CACHE: dict[int, requests.Session] = {}


def load_user_cookies() -> None:
    global USER_COOKIES
    if not os.path.exists(SESSIONS_FILE):
        USER_COOKIES = {}
        return
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        USER_COOKIES = {int(k): v for k, v in raw.items()}
    except Exception:
        log.exception("Не удалось загрузить sessions.json")
        USER_COOKIES = {}


def save_user_cookies() -> None:
    try:
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in USER_COOKIES.items()},
                      f, ensure_ascii=False, indent=2)
    except Exception:
        log.exception("Не удалось сохранить sessions.json")


def set_user_cookies(user_id, sessionid, csrftoken) -> None:
    USER_COOKIES[user_id] = {
        "sessionid": sessionid,
        "csrftoken": csrftoken,
    }
    save_user_cookies()
    _SESSION_CACHE.pop(user_id, None)


def clear_user_cookies(user_id: int) -> None:
    USER_COOKIES.pop(user_id, None)
    save_user_cookies()
    _SESSION_CACHE.pop(user_id, None)


def _build_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3, connect=3, read=2, backoff_factor=1.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET", "POST", "PUT", "PATCH", "DELETE"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, */*",
        "Referer": f"{SITE_ORIGIN}/profile/",
    })
    return s


def get_session(user_id: int) -> requests.Session:
    creds = USER_COOKIES.get(user_id)
    if not creds:
        raise NotLoggedInError(str(user_id))
    sess = _SESSION_CACHE.get(user_id)
    if sess is None:
        sess = _build_session()
        sess.cookies.update({
            "sessionid": creds["sessionid"],
            "csrftoken": creds["csrftoken"],
        })
        _SESSION_CACHE[user_id] = sess
    return sess


def _request(method, path, user_id, **kw) -> requests.Response:
    creds = USER_COOKIES.get(user_id)
    if not creds:
        raise NotLoggedInError(str(user_id))

    headers = kw.pop("headers", {})
    if method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
        headers.setdefault("X-CSRFToken", creds["csrftoken"])

    last_exc = None
    resp = None

    for attempt in range(2):
        session = get_session(user_id)
        try:
            resp = session.request(
                method, f"{API_BASE}{path}",
                headers=headers, timeout=20, **kw,
            )
            break
        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError) as e:
            last_exc = e
            log.warning("Сетевая ошибка (%s) при %s %s, попытка %d/2",
                        e.__class__.__name__, method, path, attempt + 1)
            _SESSION_CACHE.pop(user_id, None)
            if attempt == 0:
                time.sleep(1.5)
    else:
        raise NetworkError(str(last_exc)) from last_exc

    assert resp is not None
    if resp.status_code in (401, 403):
        raise SessionExpiredError(f"{method} {path} -> {resp.status_code}")
    return resp


def api_get_me(user_id: int) -> dict:
    r = _request("GET", "/profile/student", user_id)
    r.raise_for_status()
    return r.json()


def api_get_trainings_range(user_id, start, end) -> list[dict]:
    r = _request("GET", "/calendar/trainings", user_id,
                 params={
                     "start": start.astimezone(timezone.utc).isoformat(),
                     "end": end.astimezone(timezone.utc).isoformat(),
                 })
    r.raise_for_status()
    return r.json()


def api_get_trainings(user_id, day: datetime) -> list[dict]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return api_get_trainings_range(user_id, start, start + timedelta(days=1))


def api_get_training(user_id, training_id) -> dict:
    r = _request("GET", f"/training/{training_id}", user_id)
    r.raise_for_status()
    return r.json()


def api_check_in(user_id, training_id) -> None:
    r = _request("POST", f"/training/{training_id}/check_in", user_id)
    r.raise_for_status()


def api_cancel_check_in(user_id, training_id) -> None:
    r = _request("POST", f"/training/{training_id}/cancel_check_in", user_id)
    r.raise_for_status()


MSK = timezone(timedelta(hours=3))

WEEKDAY_NAMES = [
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье",
]


# ---------------------------------------------------------------------------
# Подписки
# ---------------------------------------------------------------------------

SUBSCRIPTIONS: list[dict] = []
_next_sub_id = 1


def load_subscriptions() -> None:
    global SUBSCRIPTIONS, _next_sub_id
    if not os.path.exists(SUBSCRIPTIONS_FILE):
        SUBSCRIPTIONS = []
        _next_sub_id = 1
        return
    try:
        with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
            SUBSCRIPTIONS = json.load(f)
    except Exception:
        log.exception("Не удалось загрузить subscriptions.json")
        SUBSCRIPTIONS = []

    for s in SUBSCRIPTIONS:
        s.setdefault("kind", "autobook")
        s.setdefault("last_booked_for_date", None)
        s.setdefault("notified_for_training_id", None)
        s.setdefault("last_seen_can_check_in", None)

    _next_sub_id = max((s["id"] for s in SUBSCRIPTIONS), default=0) + 1


def save_subscriptions() -> None:
    try:
        with open(SUBSCRIPTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(SUBSCRIPTIONS, f, ensure_ascii=False, indent=2)
    except Exception:
        log.exception("Не удалось сохранить subscriptions.json")


def add_subscription(user_id, title, weekday, time_str,
                     kind="autobook") -> dict:
    global _next_sub_id
    sub = {
        "id": _next_sub_id,
        "user_id": user_id,
        "title": title,
        "weekday": weekday,
        "time": time_str,
        "kind": kind,
        "created_at": datetime.now(MSK).isoformat(),
        "last_booked_for_date": None,
        "notified_for_training_id": None,
        "last_seen_can_check_in": None,
    }
    _next_sub_id += 1
    SUBSCRIPTIONS.append(sub)
    save_subscriptions()
    return sub


def remove_subscription(sub_id: int, user_id: int) -> bool:
    for s in SUBSCRIPTIONS:
        if s["id"] == sub_id and s["user_id"] == user_id:
            SUBSCRIPTIONS.remove(s)
            save_subscriptions()
            return True
    return False


@dataclass
class TrainingInfo:
    id: int
    title: str
    sport: str
    starts_at: datetime
    ends_at: datetime
    checked_in: bool
    can_check_in: bool


def parse_trainings(raw: list[dict]) -> list[TrainingInfo]:
    out = []
    for t in raw:
        props = t.get("extendedProps") or {}
        tid = props.get("id") or t.get("id")
        if tid is None:
            continue
        try:
            starts = datetime.fromisoformat(t["start"].replace("Z", "+00:00"))
            ends = datetime.fromisoformat(t["end"].replace("Z", "+00:00"))
        except Exception:
            continue
        title = (t.get("title") or "Без названия").strip()
        out.append(TrainingInfo(
            id=int(tid), title=title, sport=title,
            starts_at=starts, ends_at=ends,
            checked_in=bool(props.get("checked_in")),
            can_check_in=bool(props.get("can_check_in")),
        ))
    out.sort(key=lambda x: x.starts_at)
    return out


def fmt_time(dt: datetime) -> str:
    return dt.astimezone(MSK).strftime("%H:%M")


def fmt_date(dt: datetime) -> str:
    return dt.astimezone(MSK).strftime("%d.%m.%Y (%a)")


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------

def training_keyboard(t: TrainingInfo, day_iso: str):
    if t.checked_in:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="❌ Отменить запись",
                callback_data=f"cancel:{t.id}:{day_iso}",
            )
        ]])
    if t.can_check_in:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Записаться",
                callback_data=f"checkin:{t.id}:{day_iso}",
            )
        ]])
    return None


def day_keyboard(date: datetime) -> InlineKeyboardMarkup:
    d = date.astimezone(MSK).date().isoformat()
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"refresh:{d}"),
        InlineKeyboardButton(text="📅 Другой день", callback_data="pickday"),
    ]])


def main_menu(user_id: int | None = None) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="🗓 Выбрать день"),
         KeyboardButton(text="📋 Мои записи")],
        [KeyboardButton(text="💪 Выбрать спорт")],
        [KeyboardButton(text="⏰ Автозапись"),
         KeyboardButton(text="👀 Отслеживать освободившиеся места")],
        [KeyboardButton(text="📌 Мои автозаписи")],
    ]
    if user_id is not None and is_admin(user_id):
        keyboard.append([KeyboardButton(text="👑 Админка")])
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder="Выбери действие…",
    )


EXPIRED_MSG = "⚠️ Сессия истекла.\nОтправь /login и авторизуйся заново."
ACCESS_DENIED_MSG = "⛔ У вас нет доступа к этому боту."
NOT_LOGGED_IN_MSG = ("🔑 Сначала нужно авторизоваться.\n"
                     "Отправь /login и следуй инструкции.")
NETWORK_ERROR_MSG = ("🌐 Не получилось достучаться до "
                     "sport.innopolis.university — проблема с сетью.\n"
                     "Попробуй ещё раз через несколько секунд, "
                     "либо на время отключи VPN.")


def handle_api_errors(func):
    sig = inspect.signature(func)

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
        try:
            return await func(*args, **kwargs)
        except NotLoggedInError:
            msg = args[0]
            if isinstance(msg, CallbackQuery):
                await msg.answer(NOT_LOGGED_IN_MSG, show_alert=True)
            else:
                await msg.answer(NOT_LOGGED_IN_MSG)
        except SessionExpiredError:
            msg = args[0]
            if isinstance(msg, CallbackQuery):
                await msg.answer(EXPIRED_MSG, show_alert=True)
            else:
                await msg.answer(EXPIRED_MSG)
        except NetworkError:
            msg = args[0]
            if isinstance(msg, CallbackQuery):
                await msg.answer(NETWORK_ERROR_MSG, show_alert=True)
            else:
                await msg.answer(NETWORK_ERROR_MSG)
        except requests.RequestException as e:
            msg = args[0]
            text = f"😵 Ошибка запроса: <code>{e}</code>"
            if isinstance(msg, CallbackQuery):
                await msg.answer(text, show_alert=True)
            else:
                await msg.answer(text)

    return wrapper


class AccessControlMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or user.id not in ALLOWED_USER_IDS:
            log.warning("Access denied for user_id=%s",
                        getattr(user, "id", None))
            if isinstance(event, CallbackQuery):
                await event.answer(ACCESS_DENIED_MSG, show_alert=True)
            elif isinstance(event, Message):
                await event.answer(ACCESS_DENIED_MSG)
            return
        return await handler(event, data)


dp = Dispatcher(storage=MemoryStorage())
dp.message.outer_middleware(AccessControlMiddleware())
dp.callback_query.outer_middleware(AccessControlMiddleware())


# ---------------------------------------------------------------------------
# FSM
# ---------------------------------------------------------------------------

class LoginStates(StatesGroup):
    waiting_for_cookies = State()


class AdminStates(StatesGroup):
    waiting_for_add_id = State()
    waiting_for_remove_id = State()
    waiting_for_add_admin = State()
    waiting_for_remove_admin = State()
    waiting_for_broadcast = State()


LOGIN_INSTRUCTIONS = (
    "🔑 <b>Авторизация</b>\n\n"
    "1. Открой sport.innopolis.university в браузере и войди под своим "
    "аккаунтом.\n"
    "2. Открой инструменты разработчика (F12) → вкладка "
    "Application/Storage → Cookies → sport.innopolis.university.\n"
    "3. Найди там два значения: <code>sessionid</code> и "
    "<code>csrftoken</code>.\n"
    "4. Пришли их мне одним сообщением через пробел, например:\n"
    "<code>c1cdfx64jhvcnape89dx2ir9os6b8rg8 "
    "SiO2fHHLRVZIoLnPKHwePyOqlRUkbRuq</code>\n\n"
    "Это нужно сделать только один раз — дальше бот сам будет "
    "использовать сохранённые куки. Сообщение с куками я сразу удалю.\n\n"
    "Отменить: /cancel"
)


@dp.message(Command("login"))
async def cmd_login(message: Message, state: FSMContext):
    remember_user_name(
        message.from_user.id,
        first_name=message.from_user.first_name,
        username=message.from_user.username,
    )
    await state.set_state(LoginStates.waiting_for_cookies)
    await message.answer(LOGIN_INSTRUCTIONS)


@dp.message(Command("cancel"), LoginStates.waiting_for_cookies)
async def cmd_cancel_login(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Ок, отменил авторизацию.")


@dp.message(Command("logout"))
async def cmd_logout(message: Message):
    clear_user_cookies(message.from_user.id)
    await message.answer(
        "Вышел из аккаунта sport.innopolis.university.\n"
        "Чтобы снова пользоваться ботом — /login"
    )


@dp.message(LoginStates.waiting_for_cookies, F.text)
async def process_login_cookies(message: Message, state: FSMContext):
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer(
            "Нужно прислать ровно два значения через пробел: "
            "сначала sessionid, потом csrftoken. Попробуй ещё раз "
            "или отправь /cancel."
        )
        return

    sessionid, csrftoken = parts
    set_user_cookies(message.from_user.id, sessionid, csrftoken)
    await state.clear()

    remember_user_name(
        message.from_user.id,
        first_name=message.from_user.first_name,
        username=message.from_user.username,
    )

    try:
        await message.delete()
    except Exception:
        pass

    try:
        me = await asyncio.to_thread(api_get_me, message.from_user.id)
    except NetworkError:
        await message.answer(NETWORK_ERROR_MSG)
        return
    except Exception:
        clear_user_cookies(message.from_user.id)
        await message.answer(
            "❌ Не получилось войти с этими куками — проверь, что "
            "скопировал правильные значения (и что не разлогинился на "
            "сайте), и попробуй /login ещё раз."
        )
        return

    await message.answer(
        f"✅ Готово, {me['name']}! Я запомнил твою сессию — "
        "логиниться заново не нужно.",
        reply_markup=main_menu(message.from_user.id),
    )


# ---------------------------------------------------------------------------
# Кэши
# ---------------------------------------------------------------------------

SPORT_CACHE: dict[int, dict[str, list[TrainingInfo]]] = {}
AUTOBOOK_SPORT_CACHE: dict[int, dict[str, list[TrainingInfo]]] = {}
ASUB_CACHE: dict[int, list[TrainingInfo]] = {}
WATCH_SPORT_CACHE: dict[int, dict[str, list[TrainingInfo]]] = {}
WATCH_CACHE: dict[int, list[TrainingInfo]] = {}

WEEK_DAYS = 7


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    SPORT_CACHE.pop(message.from_user.id, None)

    remember_user_name(
        message.from_user.id,
        first_name=message.from_user.first_name,
        username=message.from_user.username,
    )

    if message.from_user.id not in USER_COOKIES:
        await message.answer(
            "Привет! 🏃 Я бот для записи на спортивные занятия "
            "InnoSport.\n\n"
            "Сначала нужно один раз авторизоваться — отправь /login."
        )
        return

    try:
        me = await asyncio.to_thread(api_get_me, message.from_user.id)
    except (SessionExpiredError, NotLoggedInError):
        await message.answer(
            "Привет! Похоже, твоя сохранённая сессия больше не "
            "работает — отправь /login, чтобы войти заново."
        )
        return
    except (NetworkError, requests.RequestException):
        await message.answer(NETWORK_ERROR_MSG)
        return

    await message.answer(
        f"Привет, {me['name']}! 🏃\n\nИспользуй кнопки ниже 👇",
        reply_markup=main_menu(message.from_user.id),
    )


# ---------------------------------------------------------------------------
# Расписание дня / мои записи
# ---------------------------------------------------------------------------

async def send_day_schedule(target, date_local: datetime, edit: bool = False):
    user_id = target.from_user.id
    raw = api_get_trainings(user_id, date_local)
    trainings = parse_trainings(raw)
    actionable = [t for t in trainings if t.checked_in or t.can_check_in]

    title = fmt_date(date_local)
    day_iso = date_local.astimezone(MSK).date().isoformat()

    if isinstance(target, CallbackQuery):
        send = target.message.answer
        edit_func = target.message.edit_text
    else:
        send = target.answer
        edit_func = None

    if not actionable:
        text = f"<b>📆 {title}</b>\nНет занятий с доступными действиями."
        kb = day_keyboard(date_local)
        if edit and edit_func is not None:
            await edit_func(text, reply_markup=kb)
        else:
            await send(text, reply_markup=kb)
        return

    header = f"<b>📆 {title}</b>"
    kb = day_keyboard(date_local)
    if edit and edit_func is not None:
        await edit_func(header, reply_markup=kb)
    else:
        await send(header, reply_markup=kb)

    for t in actionable:
        status = "✅ ты записан" if t.checked_in else "🟢 запись открыта"
        card = (
            f"<b>{t.title}</b>\n"
            f"🕐 {fmt_time(t.starts_at)}–{fmt_time(t.ends_at)}\n"
            f"Статус: {status}"
        )
        await send(card, reply_markup=training_keyboard(t, day_iso))


async def send_my_bookings(target, edit: bool = False):
    user_id = target.from_user.id
    now = datetime.now(MSK)
    end = now + timedelta(days=30)

    raw = api_get_trainings_range(user_id, now, end)
    trainings = parse_trainings(raw)
    booked = [t for t in trainings if t.checked_in]

    if isinstance(target, CallbackQuery):
        send = target.message.answer
        edit_func = target.message.edit_text
    else:
        send = target.answer
        edit_func = None

    if not booked:
        text = "📋 <b>Мои записи</b>\n\nАктивных записей нет."
        if edit and edit_func is not None:
            await edit_func(text)
        else:
            await send(text)
        return

    header = f"📋 <b>Мои записи</b> (всего: {len(booked)})"
    if edit and edit_func is not None:
        await edit_func(header)
    else:
        await send(header)

    for t in booked:
        card = (
            f"<b>{t.title}</b>\n"
            f"📅 {fmt_date(t.starts_at)}\n"
            f"🕐 {fmt_time(t.starts_at)}–{fmt_time(t.ends_at)}"
        )
        day_iso = t.starts_at.astimezone(MSK).date().isoformat()
        await send(card, reply_markup=training_keyboard(t, day_iso))


def collect_week_sports(user_id: int):
    now = datetime.now(MSK)
    end = now + timedelta(days=WEEK_DAYS)
    raw = api_get_trainings_range(user_id, now, end)
    trainings = parse_trainings(raw)
    sports: dict[str, list[TrainingInfo]] = {}
    for t in trainings:
        sports.setdefault(t.sport, []).append(t)
    return sports


def sports_keyboard(sports, prefix: str = "sport") -> InlineKeyboardMarkup:
    names = sorted(sports.keys())
    rows = []
    for i in range(0, len(names), 2):
        row = [InlineKeyboardButton(text=names[i],
                                    callback_data=f"{prefix}:{names[i]}")]
        if i + 1 < len(names):
            row.append(InlineKeyboardButton(
                text=names[i + 1],
                callback_data=f"{prefix}:{names[i + 1]}",
            ))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def start_sport_choice(message: Message):
    sports = collect_week_sports(message.from_user.id)
    if not sports:
        await message.answer("За ближайшие 7 дней занятий нет.")
        return
    SPORT_CACHE[message.from_user.id] = sports
    await message.answer("<b>Выбери вид спорта:</b>",
                         reply_markup=sports_keyboard(sports))


async def send_sport_schedule(query: CallbackQuery, sport_name: str):
    sports = SPORT_CACHE.get(query.from_user.id)
    if not sports:
        sports = collect_week_sports(query.from_user.id)
        SPORT_CACHE[query.from_user.id] = sports
    trainings = sports.get(sport_name, [])
    if not trainings:
        await query.answer("Занятий нет", show_alert=True)
        return
    await query.message.edit_text(
        f"🏀 <b>{sport_name}</b>\n\nЗанятия на ближайшие {WEEK_DAYS} дней:"
    )
    for t in trainings:
        day_iso = t.starts_at.astimezone(MSK).date().isoformat()
        if t.checked_in:
            status = "✅ ты записан"
        elif t.can_check_in:
            status = "🟢 запись открыта"
        else:
            status = "⚪ запись закрыта"
        card = (
            f"<b>{t.title}</b>\n"
            f"📅 {fmt_date(t.starts_at)}\n"
            f"🕐 {fmt_time(t.starts_at)}–{fmt_time(t.ends_at)}\n"
            f"Статус: {status}"
        )
        await query.message.answer(card,
                                   reply_markup=training_keyboard(t, day_iso))
    await query.answer()


# ---------------------------------------------------------------------------
# Кнопки меню
# ---------------------------------------------------------------------------

@dp.message(F.text == "🗓 Выбрать день")
async def btn_pickday(message: Message):
    SPORT_CACHE.pop(message.from_user.id, None)
    await show_pickday(message)


@dp.message(F.text == "📋 Мои записи")
@handle_api_errors
async def btn_my_bookings(message: Message):
    SPORT_CACHE.pop(message.from_user.id, None)
    await send_my_bookings(message)


@dp.message(F.text == "💪 Выбрать спорт")
@handle_api_errors
async def btn_pick_sport(message: Message):
    await start_sport_choice(message)


@dp.message(Command("today"))
@handle_api_errors
async def cmd_today(message: Message):
    await send_day_schedule(message, datetime.now(MSK))


@dp.message(Command("tomorrow"))
@handle_api_errors
async def cmd_tomorrow(message: Message):
    await send_day_schedule(message, datetime.now(MSK) + timedelta(days=1))


@dp.message(Command("my"))
@handle_api_errors
async def cmd_my(message: Message):
    await send_my_bookings(message)


@dp.message(Command("sports"))
@handle_api_errors
async def cmd_sports(message: Message):
    await start_sport_choice(message)


@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    SPORT_CACHE.pop(message.from_user.id, None)
    await message.answer("Главное меню 👇",
                         reply_markup=main_menu(message.from_user.id))


async def show_pickday(target):
    today = datetime.now(MSK)
    buttons = [
        InlineKeyboardButton(
            text=(today + timedelta(days=i)).strftime("%d.%m %a"),
            callback_data=(
                f"day:{(today + timedelta(days=i)).date().isoformat()}"
            ),
        )
        for i in range(7)
    ]
    rows = [buttons[i:i + 3] for i in range(0, 7, 3)]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text("Выбери день:", reply_markup=kb)
        await target.answer()
    else:
        await target.answer("Выбери день:", reply_markup=kb)


@dp.callback_query(F.data == "pickday")
async def cb_pickday(query: CallbackQuery):
    await show_pickday(query)


@dp.callback_query(F.data.startswith("day:"))
@handle_api_errors
async def cb_day(query: CallbackQuery):
    date = datetime.fromisoformat(
        query.data.split(":", 1)[1]
    ).replace(tzinfo=MSK)
    await send_day_schedule(query, date, edit=False)
    await query.answer()


@dp.callback_query(F.data.startswith("refresh:"))
@handle_api_errors
async def cb_refresh(query: CallbackQuery):
    date = datetime.fromisoformat(
        query.data.split(":", 1)[1]
    ).replace(tzinfo=MSK)
    await send_day_schedule(query, date, edit=True)
    await query.answer("Обновлено")


@dp.callback_query(F.data.startswith("sport:"))
@handle_api_errors
async def cb_sport(query: CallbackQuery):
    sport_name = query.data.split(":", 1)[1]
    await send_sport_schedule(query, sport_name)


# ---------------------------------------------------------------------------
# Автозапись
# ---------------------------------------------------------------------------

@dp.message(F.text == "⏰ Автозапись")
@handle_api_errors
async def btn_autobook(message: Message):
    await start_autobook_choice(message)


async def start_autobook_choice(message: Message):
    sports = collect_week_sports(message.from_user.id)
    if not sports:
        await message.answer("За ближайшие 7 дней занятий нет.")
        return
    AUTOBOOK_SPORT_CACHE[message.from_user.id] = sports
    await message.answer(
        "⏰ <b>Автозапись</b>\n\n"
        "Выбери вид спорта — бот запомнит занятие "
        "(по дню недели и времени) и запишет тебя автоматически "
        "в момент открытия регистрации (ровно за 7 дней до занятия).",
        reply_markup=sports_keyboard(sports, prefix="asub_sport"),
    )


@dp.callback_query(F.data.startswith("asub_sport:"))
@handle_api_errors
async def cb_asub_sport(query: CallbackQuery):
    sport_name = query.data.split(":", 1)[1]
    sports = AUTOBOOK_SPORT_CACHE.get(query.from_user.id)
    if not sports:
        sports = collect_week_sports(query.from_user.id)
        AUTOBOOK_SPORT_CACHE[query.from_user.id] = sports
    trainings = sports.get(sport_name, [])
    if not trainings:
        await query.answer("Занятий нет", show_alert=True)
        return
    ASUB_CACHE[query.from_user.id] = trainings
    existing = {
        (s["title"], s["weekday"], s["time"])
        for s in SUBSCRIPTIONS
        if s["user_id"] == query.from_user.id and s.get("kind") == "autobook"
    }
    await query.message.edit_text(
        f"⏰ <b>{sport_name}</b>\n\n"
        "Выбери занятие, на которое нужно записываться каждую неделю:"
    )
    for idx, t in enumerate(trainings):
        weekday = t.starts_at.astimezone(MSK).weekday()
        time_str = fmt_time(t.starts_at)
        card = (
            f"<b>{t.title}</b>\n"
            f"📆 {WEEKDAY_NAMES[weekday]}, "
            f"{time_str}–{fmt_time(t.ends_at)}"
        )
        if (t.title, weekday, time_str) in existing:
            card += "\n\n⏰ Уже в автозаписи"
            kb = None
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="⏰ Записывать каждую неделю",
                    callback_data=f"asub_add:{idx}",
                )
            ]])
        await query.message.answer(card, reply_markup=kb)
    await query.answer()


@dp.callback_query(F.data.startswith("asub_add:"))
@handle_api_errors
async def cb_asub_add(query: CallbackQuery):
    idx = int(query.data.split(":", 1)[1])
    trainings = ASUB_CACHE.get(query.from_user.id) or []
    if idx >= len(trainings):
        await query.answer("Список устарел, выбери спорт заново",
                           show_alert=True)
        return

    t = trainings[idx]
    weekday = t.starts_at.astimezone(MSK).weekday()
    time_str = fmt_time(t.starts_at)

    add_subscription(
        query.from_user.id, t.title, weekday, time_str, kind="autobook"
    )

    await query.answer("Готово! Буду записывать автоматически.",
                       show_alert=True)
    await query.message.edit_reply_markup(reply_markup=None)

    base = (
        f"✅ «{t.title}» ({WEEKDAY_NAMES[weekday]}, {time_str}) "
        "добавлено в автозапись."
    )

    day_iso = t.starts_at.astimezone(MSK).date().isoformat()

    if t.checked_in:
        await query.message.answer(
            base
            + "\n\nℹ️ На ближайшее занятие "
            f"({fmt_date(t.starts_at)}, {time_str}) ты уже записан.\n"
            "Со следующей недели буду записывать тебя сам."
        )
        return

    if t.can_check_in:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Записаться сейчас",
                callback_data=f"checkin:{t.id}:{day_iso}",
            )
        ]])
        await query.message.answer(
            base
            + "\n\nℹ️ На ближайшее занятие "
            f"({fmt_date(t.starts_at)}, {time_str}) запись уже открыта.\n"
            "Можешь записаться прямо сейчас кнопкой ниже — "
            "или подожди, и я запишу тебя сам.\n\n"
            "Автозапись сохранена: со следующей недели буду записывать "
            "тебя без напоминаний.",
            reply_markup=kb,
        )
        return

    await query.message.answer(
        base
        + "\n\nКак только на сайте откроется регистрация — "
        "бот запишет тебя и пришлёт уведомление."
    )


# ---------------------------------------------------------------------------
# Отслеживание освободившихся мест (watch)
# ---------------------------------------------------------------------------

@dp.message(F.text == "👀 Отслеживать освободившиеся места")
@handle_api_errors
async def btn_watch(message: Message):
    await start_watch_choice(message)


async def start_watch_choice(message: Message):
    sports = collect_week_sports(message.from_user.id)
    if not sports:
        await message.answer("За ближайшие 7 дней занятий нет.")
        return
    WATCH_SPORT_CACHE[message.from_user.id] = sports
    await message.answer(
        "👀 <b>Отслеживать освободившиеся места</b>\n\n"
        "Выбери вид спорта — бот будет следить за этим занятием "
        "и пришлёт уведомление, если на нём освободится место "
        "(в пределах 7 дней до занятия).",
        reply_markup=sports_keyboard(sports, prefix="watch_sport"),
    )


@dp.callback_query(F.data.startswith("watch_sport:"))
@handle_api_errors
async def cb_watch_sport(query: CallbackQuery):
    sport_name = query.data.split(":", 1)[1]
    sports = WATCH_SPORT_CACHE.get(query.from_user.id)
    if not sports:
        sports = collect_week_sports(query.from_user.id)
        WATCH_SPORT_CACHE[query.from_user.id] = sports
    trainings = sports.get(sport_name, [])
    if not trainings:
        await query.answer("Занятий нет", show_alert=True)
        return
    WATCH_CACHE[query.from_user.id] = trainings

    existing = {
        (s["title"], s["weekday"], s["time"])
        for s in SUBSCRIPTIONS
        if s["user_id"] == query.from_user.id and s.get("kind") == "watch"
    }

    await query.message.edit_text(
        f"👀 <b>{sport_name}</b>\n\n"
        "Выбери занятие, за которым следить:"
    )
    for idx, t in enumerate(trainings):
        weekday = t.starts_at.astimezone(MSK).weekday()
        time_str = fmt_time(t.starts_at)
        card = (
            f"<b>{t.title}</b>\n"
            f"📆 {WEEKDAY_NAMES[weekday]}, "
            f"{time_str}–{fmt_time(t.ends_at)}"
        )
        if (t.title, weekday, time_str) in existing:
            card += "\n\n👀 Уже следим"
            kb = None
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="👀 Следить",
                    callback_data=f"watch_add:{idx}",
                )
            ]])
        await query.message.answer(card, reply_markup=kb)
    await query.answer()


@dp.callback_query(F.data.startswith("watch_add:"))
@handle_api_errors
async def cb_watch_add(query: CallbackQuery):
    idx = int(query.data.split(":", 1)[1])
    trainings = WATCH_CACHE.get(query.from_user.id) or []
    if idx >= len(trainings):
        await query.answer("Список устарел, выбери спорт заново",
                           show_alert=True)
        return
    t = trainings[idx]
    weekday = t.starts_at.astimezone(MSK).weekday()
    time_str = fmt_time(t.starts_at)

    add_subscription(query.from_user.id, t.title, weekday, time_str,
                     kind="watch")

    await query.answer("Готово! Буду следить за местом.", show_alert=True)
    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.answer(
        f"👀 «{t.title}» ({WEEKDAY_NAMES[weekday]}, {time_str}) "
        "добавлено в наблюдение.\n"
        "Как только на нём освободится место — пришлю уведомление "
        "с кнопкой «Записаться»."
    )


# ---------------------------------------------------------------------------
# Мои автозаписи
# ---------------------------------------------------------------------------

@dp.message(F.text == "📌 Мои автозаписи")
async def btn_my_subs(message: Message):
    await send_my_subscriptions(message)


async def send_my_subscriptions(message: Message):
    subs = [s for s in SUBSCRIPTIONS if s["user_id"] == message.from_user.id]
    if not subs:
        await message.answer(
            "📌 У тебя нет активных автозаписей.\n"
            "Добавить можно через «⏰ Автозапись» или "
            "«👀 Отслеживать освободившиеся места»."
        )
        return
    await message.answer(f"📌 <b>Твои автозаписи</b> (всего: {len(subs)})")
    for s in subs:
        kind = s.get("kind", "autobook")
        icon = "⏰" if kind == "autobook" else "👀"
        text = (
            f"{icon} <b>{s['title']}</b>\n"
            f"{WEEKDAY_NAMES[s['weekday']]}, {s['time']}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Отписаться",
                                 callback_data=f"unsub:{s['id']}")
        ]])
        await message.answer(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("unsub:"))
async def cb_unsub(query: CallbackQuery):
    sub_id = int(query.data.split(":", 1)[1])
    ok = remove_subscription(sub_id, query.from_user.id)
    if ok:
        await query.answer("Отписал", show_alert=True)
        await query.message.edit_reply_markup(reply_markup=None)
    else:
        await query.answer("Не найдено (уже отписан?)", show_alert=True)


# ---------------------------------------------------------------------------
# Check-in / cancel (ручные действия)
# ---------------------------------------------------------------------------

@dp.callback_query(
    F.data.regexp(r"^(checkin|cancel):(\d+):(\d{4}-\d{2}-\d{2})$").as_("m")
)
@handle_api_errors
async def cb_toggle(query: CallbackQuery, m):
    user_id = query.from_user.id
    action = m.group(1)
    tid = int(m.group(2))
    day = m.group(3)

    try:
        if action == "checkin":
            api_check_in(user_id, tid)
            await query.answer("✅ Записал!", show_alert=True)
        else:
            api_cancel_check_in(user_id, tid)
            await query.answer("❌ Запись отменена", show_alert=True)
    except requests.HTTPError as e:
        detail = ""
        try:
            detail = e.response.json().get("detail", "")
        except Exception:
            pass
        await query.answer(f"Не вышло: {detail or e.response.status_code}",
                           show_alert=True)
        return

    data = api_get_training(user_id, tid)
    raw = data["training"]
    starts = datetime.fromisoformat(raw["start"].replace("Z", "+00:00"))
    ends = datetime.fromisoformat(raw["end"].replace("Z", "+00:00"))
    checked = data.get("checked_in", False)
    can = data.get("can_check_in", False)
    title = raw.get("custom_name") or raw["group"]["name"]
    status = ("✅ ты записан" if checked
              else ("🟢 запись открыта" if can else "⚪ запись закрыта"))
    text = (f"<b>{title}</b>\n"
            f"🕐 {fmt_time(starts)}–{fmt_time(ends)}\n"
            f"Статус: {status}")
    kb = None
    if checked:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отменить запись",
                                 callback_data=f"cancel:{tid}:{day}")
        ]])
    elif can:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Записаться",
                                 callback_data=f"checkin:{tid}:{day}")
        ]])
    await query.message.edit_text(text, reply_markup=kb)


# ---------------------------------------------------------------------------
# Watch: уведомление "освободилось место"
# ---------------------------------------------------------------------------

async def _notify_watch(user_id: int, t: TrainingInfo, sub_id: int) -> None:
    if BOT_INSTANCE is None:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Записаться",
            callback_data=f"watch_book:{t.id}:{sub_id}",
        ),
        InlineKeyboardButton(
            text="❌ Пропустить",
            callback_data=f"watch_skip:{sub_id}",
        ),
    ]])
    try:
        await BOT_INSTANCE.send_message(
            user_id,
            "🔔 <b>Освободилось место!</b>\n\n"
            f"<b>{t.title}</b>\n"
            f"📅 {fmt_date(t.starts_at)}\n"
            f"🕐 {fmt_time(t.starts_at)}–{fmt_time(t.ends_at)}\n\n"
            "Записать тебя?",
            reply_markup=kb,
        )
    except Exception:
        log.exception("Не удалось уведомить пользователя %s", user_id)


@dp.callback_query(F.data.regexp(r"^watch_book:(\d+):(\d+)$").as_("m"))
@handle_api_errors
async def cb_watch_book(query: CallbackQuery, m):
    tid = int(m.group(1))
    try:
        api_check_in(query.from_user.id, tid)
    except requests.HTTPError as e:
        detail = ""
        try:
            detail = e.response.json().get("detail", "")
        except Exception:
            pass
        await query.answer(
            f"Не вышло: {detail or e.response.status_code}",
            show_alert=True,
        )
        return
    await query.answer("✅ Записал!", show_alert=True)
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@dp.callback_query(F.data.regexp(r"^watch_skip:(\d+)$").as_("m"))
async def cb_watch_skip(query: CallbackQuery, m):
    await query.answer("Ок, пропустил", show_alert=True)
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Админка
# ---------------------------------------------------------------------------

def admin_menu(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="➕ Добавить участника",
                              callback_data="admin:add")],
        [InlineKeyboardButton(text="➖ Удалить участника",
                              callback_data="admin:remove")],
        [InlineKeyboardButton(text="📋 Список участников",
                              callback_data="admin:list")],
        [InlineKeyboardButton(text="📢 Сделать объявление",
                              callback_data="admin:broadcast")],
    ]
    if is_super_admin(user_id):
        rows.append([InlineKeyboardButton(
            text="🛡 Добавить админа",
            callback_data="admin:addadmin",
        )])
        rows.append([InlineKeyboardButton(
            text="🛡 Удалить админа",
            callback_data="admin:removeadmin",
        )])
        rows.append([InlineKeyboardButton(
            text="🛡 Список админов",
            callback_data="admin:listadmins",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(F.text == "👑 Админка")
async def btn_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для админа.")
        return
    await state.clear()
    await message.answer(
        "👑 <b>Админ-панель</b>\n\n"
        f"Участников в белом списке: <b>{len(ALLOWED_USER_IDS)}</b>\n"
        f"Админов (кроме главного): <b>{len(ADMIN_USER_IDS)}</b>",
        reply_markup=admin_menu(message.from_user.id),
    )


@dp.callback_query(F.data == "admin:add")
async def cb_admin_add(query: CallbackQuery, state: FSMContext):
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Только для админа.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_add_id)
    await query.message.answer(
        "Пришли Telegram ID пользователя, которого нужно "
        "добавить в белый список.\nОтменить: /cancel"
    )
    await query.answer()


@dp.callback_query(F.data == "admin:remove")
async def cb_admin_remove(query: CallbackQuery, state: FSMContext):
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Только для админа.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_remove_id)
    await query.message.answer(
        "Пришли Telegram ID, которого нужно убрать из белого списка.\n"
        "Отменить: /cancel"
    )
    await query.answer()


@dp.callback_query(F.data == "admin:list")
async def cb_admin_list(query: CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Только для админа.", show_alert=True)
        return
    ids = sorted(ALLOWED_USER_IDS)
    lines = []
    for uid in ids:
        mark = "👑 " if is_admin(uid) else "• "
        lines.append(f"{mark}{format_user_label(uid)} — <code>{uid}</code>")
    await query.message.answer("📋 <b>Участники</b>\n\n" + "\n".join(lines))
    await query.answer()


@dp.callback_query(F.data == "admin:broadcast")
async def cb_admin_broadcast(query: CallbackQuery, state: FSMContext):
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Только для админа.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_broadcast)
    await query.message.answer(
        "📢 Пришли текст объявления. Он уйдёт всем, у кого есть сессия.\n"
        "Поддерживается HTML-разметка.\nОтменить: /cancel"
    )
    await query.answer()


@dp.callback_query(F.data == "admin:addadmin")
async def cb_admin_addadmin(query: CallbackQuery, state: FSMContext):
    if not is_super_admin(query.from_user.id):
        await query.answer("⛔ Только для главного админа.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_add_admin)
    await query.message.answer(
        "Пришли Telegram ID пользователя, которого сделать админом.\n"
        "Отменить: /cancel"
    )
    await query.answer()


@dp.callback_query(F.data == "admin:removeadmin")
async def cb_admin_removeadmin(query: CallbackQuery, state: FSMContext):
    if not is_super_admin(query.from_user.id):
        await query.answer("⛔ Только для главного админа.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_remove_admin)
    await query.message.answer(
        "Пришли Telegram ID админа, которого лишить прав.\n"
        "Отменить: /cancel"
    )
    await query.answer()


@dp.callback_query(F.data == "admin:listadmins")
async def cb_admin_listadmins(query: CallbackQuery):
    if not is_super_admin(query.from_user.id):
        await query.answer("⛔ Только для главного админа.", show_alert=True)
        return
    lines = [
        f"👑 <b>Главный админ</b> — "
        f"{format_user_label(SUPER_ADMIN_ID)} "
        f"<code>{SUPER_ADMIN_ID}</code>"
    ]
    others = sorted(uid for uid in ADMIN_USER_IDS if uid != SUPER_ADMIN_ID)
    if others:
        lines.append("")
        lines.append("<b>Админы:</b>")
        for uid in others:
            lines.append(f"🛡 {format_user_label(uid)} <code>{uid}</code>")
    else:
        lines.append("")
        lines.append("<i>Других админов нет.</i>")
    await query.message.answer("\n".join(lines))
    await query.answer()


@dp.message(Command("cancel"), AdminStates)
async def cmd_cancel_admin(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Ок, отменил.",
                         reply_markup=main_menu(message.from_user.id))


@dp.message(AdminStates.waiting_for_add_id, F.text)
async def process_admin_add(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    text = message.text.strip()
    if not text.isdigit():
        await message.answer(
            "Нужен числовой Telegram ID. Попробуй ещё раз или /cancel."
        )
        return
    new_id = int(text)
    added = add_allowed_user(new_id)
    await state.clear()
    if added:
        remember_user_name(
            message.from_user.id,
            first_name=message.from_user.first_name,
            username=message.from_user.username,
        )
        await message.answer(
            f"✅ Добавил <code>{new_id}</code> в белый список.",
            reply_markup=main_menu(message.from_user.id),
        )
        if BOT_INSTANCE is not None:
            try:
                await BOT_INSTANCE.send_message(
                    new_id,
                    "👋 Тебе выдали доступ к InnoSport-боту.\n"
                    "Отправь /login, чтобы авторизоваться.",
                )
            except Exception:
                pass
    else:
        await message.answer(
            f"ℹ️ <code>{new_id}</code> уже в списке.",
            reply_markup=main_menu(message.from_user.id),
        )


@dp.message(AdminStates.waiting_for_remove_id, F.text)
async def process_admin_remove(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    text = message.text.strip()
    if not text.isdigit():
        await message.answer(
            "Нужен числовой Telegram ID. Попробуй ещё раз или /cancel."
        )
        return
    target_id = int(text)

    if target_id == message.from_user.id:
        await state.clear()
        await message.answer("⛔ Нельзя удалить самого себя.",
                             reply_markup=main_menu(message.from_user.id))
        return

    if is_admin(target_id):
        await state.clear()
        await message.answer(
            "⛔ Нельзя удалить админа. Сначала снимите с него права "
            "(только главный админ может).",
            reply_markup=main_menu(message.from_user.id),
        )
        return

    removed = remove_allowed_user(target_id)
    await state.clear()
    if removed:
        clear_user_cookies(target_id)
        await message.answer(
            f"✅ Убрал <code>{target_id}</code> "
            f"({format_user_label(target_id)}) из белого списка "
            "и удалил его сессию.",
            reply_markup=main_menu(message.from_user.id),
        )
    else:
        await message.answer(
            f"ℹ️ <code>{target_id}</code> и так не в списке.",
            reply_markup=main_menu(message.from_user.id),
        )


@dp.message(AdminStates.waiting_for_add_admin, F.text)
async def process_add_admin(message: Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        await state.clear()
        return
    text = message.text.strip()
    if not text.isdigit():
        await message.answer("Нужен числовой Telegram ID. "
                             "Попробуй ещё раз или /cancel.")
        return
    new_id = int(text)
    added = add_admin(new_id)
    await state.clear()
    if added:
        await message.answer(
            f"🛡 <code>{new_id}</code> "
            f"({format_user_label(new_id)}) теперь админ.",
            reply_markup=main_menu(message.from_user.id),
        )
        if BOT_INSTANCE is not None:
            try:
                await BOT_INSTANCE.send_message(
                    new_id,
                    "🛡 Тебе выдали права админа в InnoSport-боте.",
                )
            except Exception:
                pass
    else:
        await message.answer(
            f"ℹ️ <code>{new_id}</code> уже админ.",
            reply_markup=main_menu(message.from_user.id),
        )


@dp.message(AdminStates.waiting_for_remove_admin, F.text)
async def process_remove_admin(message: Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        await state.clear()
        return
    text = message.text.strip()
    if not text.isdigit():
        await message.answer("Нужен числовой Telegram ID. "
                             "Попробуй ещё раз или /cancel.")
        return
    target_id = int(text)

    if target_id == SUPER_ADMIN_ID:
        await state.clear()
        await message.answer("⛔ Нельзя снять главного админа.",
                             reply_markup=main_menu(message.from_user.id))
        return

    removed = remove_admin(target_id)
    await state.clear()
    if removed:
        await message.answer(
            f"✅ <code>{target_id}</code> "
            f"({format_user_label(target_id)}) больше не админ.",
            reply_markup=main_menu(message.from_user.id),
        )
    else:
        await message.answer(
            f"ℹ️ <code>{target_id}</code> и так не админ.",
            reply_markup=main_menu(message.from_user.id),
        )


@dp.message(AdminStates.waiting_for_broadcast, F.text)
async def process_admin_broadcast(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    text = message.text
    await state.clear()
    if BOT_INSTANCE is None:
        await message.answer("Бот ещё не готов, попробуй позже.")
        return
    recipients = list(USER_COOKIES.keys())
    sent, failed = 0, 0
    for uid in recipients:
        try:
            await BOT_INSTANCE.send_message(
                uid, f"📢 <b>Объявление</b>\n\n{text}")
            sent += 1
        except Exception:
            failed += 1
            log.warning("Не смог отправить объявление user_id=%s", uid)
    await message.answer(
        f"📢 Готово. Доставлено: {sent}, не доставлено: {failed}.",
        reply_markup=main_menu(message.from_user.id),
    )


@dp.message(StateFilter(None), F.text)
@handle_api_errors
async def on_text(message: Message):
    await message.answer("Используй кнопки ниже 👇",
                         reply_markup=main_menu(message.from_user.id))


# ---------------------------------------------------------------------------
# Уведомления из фоновых задач
# ---------------------------------------------------------------------------

BOT_INSTANCE: Bot | None = None
_last_expired_notice: dict[int, datetime] = {}


async def _notify_autobooked(user_id: int, t: TrainingInfo) -> None:
    if BOT_INSTANCE is None:
        return
    try:
        await BOT_INSTANCE.send_message(
            user_id,
            "⏰ <b>Автозапись сработала!</b>\n\n"
            f"<b>{t.title}</b>\n"
            f"📅 {fmt_date(t.starts_at)}\n"
            f"🕐 {fmt_time(t.starts_at)}–{fmt_time(t.ends_at)}\n\n"
            "Ты записан ✅",
        )
    except Exception:
        log.exception("Не удалось уведомить пользователя %s", user_id)


async def _notify_session_expired(user_id: int) -> None:
    if BOT_INSTANCE is None:
        return
    last = _last_expired_notice.get(user_id)
    now = datetime.now(MSK)
    if last is not None and now - last < timedelta(minutes=30):
        return
    _last_expired_notice[user_id] = now
    try:
        await BOT_INSTANCE.send_message(
            user_id,
            "⚠️ Сессия истекла. Автозапись/наблюдение приостановлены.\n"
            "Отправь /login, чтобы войти заново — подписки сохранятся.",
        )
    except Exception:
        log.exception("Не удалось уведомить пользователя %s", user_id)


# ---------------------------------------------------------------------------
# Шедулер автозаписи
# ---------------------------------------------------------------------------

AUTOBOOK_WINDOW_SECONDS = 50


async def autobook_scheduler() -> None:
    while True:
        try:
            now = datetime.now(MSK)

            for sub in list(SUBSCRIPTIONS):
                if sub.get("kind") != "autobook":
                    continue
                if sub["user_id"] not in USER_COOKIES:
                    continue

                if now.weekday() != sub["weekday"]:
                    continue

                try:
                    hh, mm = map(int, sub["time"].split(":"))
                except Exception:
                    continue

                if now.hour != hh or now.minute != mm:
                    continue

                target_date = (now + timedelta(days=7)).date()
                target_iso = target_date.isoformat()

                if sub.get("last_booked_for_date") == target_iso:
                    continue

                if now.second > AUTOBOOK_WINDOW_SECONDS:
                    sub["last_booked_for_date"] = target_iso
                    save_subscriptions()
                    continue

                await _try_autobook_now(sub, target_date, hh, mm)

        except Exception:
            log.exception("autobook_scheduler упал с ошибкой")

        await asyncio.sleep(1)


async def _try_autobook_now(sub: dict, target_date, hh: int, mm: int) -> None:
    user_id = sub["user_id"]

    day_start = datetime(target_date.year, target_date.month, target_date.day,
                         0, 0, 0, tzinfo=MSK)
    day_end = day_start + timedelta(days=1)

    try:
        raw = await asyncio.to_thread(
            api_get_trainings_range, user_id, day_start, day_end
        )
    except (SessionExpiredError, NotLoggedInError):
        log.warning("Автозапись: сессия истекла у user_id=%s", user_id)
        await _notify_session_expired(user_id)
        return
    except (NetworkError, requests.RequestException):
        log.exception("Автозапись: не удалось получить расписание "
                      "для user_id=%s", user_id)
        return

    trainings = parse_trainings(raw)

    target = None
    for t in trainings:
        local = t.starts_at.astimezone(MSK)
        if (t.title == sub["title"]
                and local.date() == target_date
                and local.hour == hh and local.minute == mm):
            target = t
            break

    if target is None:
        log.info("Автозапись: занятие %s %s %02d:%02d не найдено",
                 sub["title"], target_date, hh, mm)
        return

    if target.checked_in:
        sub["last_booked_for_date"] = target_date.isoformat()
        save_subscriptions()
        return

    if not target.can_check_in:
        log.info("Автозапись: запись ещё закрыта (training_id=%s)", target.id)
        return

    try:
        await asyncio.to_thread(api_check_in, user_id, target.id)
    except Exception:
        log.warning("Автозапись: не удалось записаться sub_id=%s "
                    "training_id=%s", sub["id"], target.id)
        return

    sub["last_booked_for_date"] = target_date.isoformat()
    save_subscriptions()
    log.info("Автозапись: записал user_id=%s на training_id=%s",
             user_id, target.id)
    await _notify_autobooked(user_id, target)


# ---------------------------------------------------------------------------
# Watch: поллер освободившихся мест
# ---------------------------------------------------------------------------

def _find_matching_training(trainings, sub, max_days_ahead=7):
    now = datetime.now(MSK)
    limit = now + timedelta(days=max_days_ahead)

    candidates = []
    for t in trainings:
        local = t.starts_at.astimezone(MSK)
        if (t.title == sub["title"]
                and local.weekday() == sub["weekday"]
                and fmt_time(t.starts_at) == sub["time"]
                and now < local < limit):
            candidates.append(t)
    if not candidates:
        return None
    candidates.sort(key=lambda x: x.starts_at)
    return candidates[0]


async def process_watch_subs() -> None:
    watch_subs = [s for s in SUBSCRIPTIONS if s.get("kind") == "watch"]
    if not watch_subs:
        return

    now = datetime.now(MSK)
    end = now + timedelta(days=14)

    subs_by_user: dict[int, list[dict]] = {}
    for sub in watch_subs:
        subs_by_user.setdefault(sub["user_id"], []).append(sub)

    for user_id, subs in subs_by_user.items():
        if user_id not in USER_COOKIES:
            continue

        try:
            raw = await asyncio.to_thread(
                api_get_trainings_range, user_id, now, end
            )
        except (SessionExpiredError, NotLoggedInError):
            await _notify_session_expired(user_id)
            continue
        except (NetworkError, requests.RequestException):
            continue

        trainings = parse_trainings(raw)

        for sub in subs:
            match = _find_matching_training(trainings, sub, max_days_ahead=7)
            if match is None:
                continue

            prev = sub.get("last_seen_can_check_in")

            if not match.can_check_in:
                if prev is not False:
                    sub["last_seen_can_check_in"] = False
                    save_subscriptions()
                continue

            if prev is False and sub.get("notified_for_training_id") != match.id:
                await _notify_watch(user_id, match, sub["id"])
                sub["notified_for_training_id"] = match.id
                save_subscriptions()

            if prev is not True:
                sub["last_seen_can_check_in"] = True
                save_subscriptions()


async def watch_poller() -> None:
    while True:
        try:
            await process_watch_subs()
        except Exception:
            log.exception("watch_poller упал с ошибкой")
        await asyncio.sleep(30)


# ---------------------------------------------------------------------------
# Startup / main
# ---------------------------------------------------------------------------

async def on_startup(bot: Bot) -> None:
    global BOT_INSTANCE
    BOT_INSTANCE = bot

    load_allowed_users()
    load_admins()
    load_user_cookies()
    load_user_names()
    load_subscriptions()

    asyncio.create_task(autobook_scheduler())
    asyncio.create_task(watch_poller())

    log.info(
        "Bot started. Сессий: %d, подписок: %d, белый список: %d, "
        "админов: %d (+главный), имён: %d",
        len(USER_COOKIES), len(SUBSCRIPTIONS),
        len(ALLOWED_USER_IDS), len(ADMIN_USER_IDS), len(USER_NAMES),
    )


dp.startup.register(on_startup)


def main():
    from aiogram.client.default import DefaultBotProperties

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp.run_polling(bot)


if __name__ == "__main__":
    main()