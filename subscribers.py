"""
Особисті підписки в боті: фільтр за днем/часом зміни та за компанією.

Працює поруч з основним скрейпером і не змінює його поведінку:
- окремий потік читає команди (getUpdates);
- окремий потік розсилає повідомлення підписникам з черги;
- будь-яка помилка тут лише логується й не зупиняє scrape().

Інтерфейс бота — українською або чеською (/lang). Самі повідомлення про зміни лишаються такими,
як у каналі.

Змінні середовища:
- ADMIN_CHANNEL_ID — числовий id приватного адмін-каналу (-100...). Туди йдуть сповіщення про нових
  підписників з кнопкою блокування; адміни цього каналу можуть натискати кнопки і виконувати /users, /block;
- ADMIN_CHAT_ID (або CHATBOT_ID) — твій особистий chat id; завжди адмін. Якщо адмін-каналу немає,
  сповіщення про нових підписників ідуть сюди;
- ADMIN_LANG — мова сповіщень в адмін-канал: "uk" (за замовчуванням) або "cs";
- SUBSCRIBER_EXCLUDED_COMPANIES — id компаній через кому, які підписники не отримують (за замовчуванням жодної);
- SUBSCRIBERS_ENABLED — "0", щоб вимкнути модуль повністю.
"""
import html
import json
import os
import queue
import re
import threading
import time
import traceback
from datetime import datetime, timedelta

import requests
import sqlalchemy
from sqlalchemy import (Boolean, BigInteger, Column, DateTime, ForeignKey, Integer,
                        SmallInteger, String, Text)
from sqlalchemy.orm import declarative_base, sessionmaker

# --- Конфіг ---
ADMIN_CHAT_ID = (os.getenv("ADMIN_CHAT_ID") or os.getenv("CHATBOT_ID") or "").strip()
ADMIN_CHANNEL_ID = (os.getenv("ADMIN_CHANNEL_ID") or "").strip()
ADMIN_CACHE_SECONDS = 300
EXCLUDED_COMPANIES = {
    c.strip() for c in os.getenv("SUBSCRIBER_EXCLUDED_COMPANIES", "").split(",") if c.strip()
}
ENABLED = os.getenv("SUBSCRIBERS_ENABLED", "1") != "0"

LANGS = ("uk", "cs")
DEFAULT_LANG = "uk"
ADMIN_LANG = os.getenv("ADMIN_LANG", DEFAULT_LANG) if os.getenv("ADMIN_LANG") in LANGS else DEFAULT_LANG

Base = declarative_base()


class BotUser(Base):
    __tablename__ = "bot_users"
    chat_id = Column(BigInteger, primary_key=True, autoincrement=False)
    username = Column(String(255))
    first_name = Column(String(255))
    lang = Column(String(2))                                     # uk / cs; NULL = DEFAULT_LANG
    is_active = Column(Boolean, nullable=False, default=True)    # пауза користувача або 403
    is_blocked = Column(Boolean, nullable=False, default=False)  # блокування адміном
    blocked_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_seen_at = Column(DateTime)


class BotUserWindow(Base):
    """Вікно для зміни: день початку + хвилини від півночі.
    end_min <= start_min означає вікно через північ (захоплює ранок наступного дня).
    Хвилини замість TIME, щоб можна було записати 24:00."""
    __tablename__ = "bot_user_windows"
    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(BigInteger, ForeignKey("bot_users.chat_id", ondelete="CASCADE"), nullable=False, index=True)
    weekday = Column(SmallInteger, nullable=False)    # 1 = пн ... 7 = нд
    start_min = Column(SmallInteger, nullable=False)  # 0..1439
    end_min = Column(SmallInteger, nullable=False)    # 0..1440


class BotUserCompany(Base):
    __tablename__ = "bot_user_companies"
    chat_id = Column(BigInteger, ForeignKey("bot_users.chat_id", ondelete="CASCADE"), primary_key=True)
    company_id = Column(Integer, primary_key=True, autoincrement=False)
    company_name = Column(Text)


# --- Стан модуля ---
_engine = None
_Session = None
_token = None
_queue = queue.Queue()
_started = False
_admin_cache = {}  # user_id -> (is_admin, checked_at)


# =====================================================================
#  Тексти інтерфейсу
# =====================================================================
TEXTS = {
    "uk": {
        "help": (
            "Я надсилаю нові та знову відкриті зміни з Shameless.\n\n"
            "<b>Коли тобі зручно працювати</b> (день і час самої зміни, час Праги):\n"
            "/set пн-пт 17:00-23:00 — замінити вікна на ці дні\n"
            "/add пт 00:00-08:00 — додати ще одне вікно\n"
            "/off сб — прибрати вікна на день\n"
            "/clear — прибрати всі вікна (отримувати всі зміни)\n"
            "Вікно через північ, напр. <code>/set чт 22:00-07:00</code>, покриває і нічні зміни, "
            "що починаються в пт зранку. Зміна має повністю вміщуватися у вікно.\n"
            "Якщо вікна задані, дні без вікон не надсилаються.\n\n"
            "<b>Компанії</b>:\n"
            "/companies [текст] — список компаній з id\n"
            "/company add 123 456 або /company add назва\n"
            "/company del 123, /company clear — всі компанії\n\n"
            "/schedule — мої налаштування\n"
            "/pause, /resume — зупинити або відновити сповіщення\n"
            "/lang — мова / jazyk"
        ),
        "admin_help": "\n\n<b>Адмін</b>: /users, /block id, /unblock id",
        "hello": "Привіт! ",
        "settings_title": "<b>Мої налаштування</b>",
        "status": "Статус: {v}",
        "active": "✅ активні",
        "paused": "⏸ пауза",
        "windows": "Вікна змін:",
        "windows_all": "Вікна змін: всі дні й години",
        "companies": "Компанії:",
        "companies_all": "Компанії: всі",
        "language": "Мова: українська",
        "next_morning": " (до ранку наступного дня)",
        "unknown_cmd": "Невідома команда. /help",
        "access_closed": "Доступ закрито.",
        "paused_msg": "⏸ Сповіщення на паузі. /resume — відновити.",
        "resumed_msg": "▶️ Сповіщення відновлено.",
        "windows_cleared": "Вікна прибрано: надсилатиму зміни в будь-який день і час.",
        "companies_cleared": "Фільтр компаній прибрано: надсилатиму всі компанії.",
        "company_usage": "Приклад: /company add 123, /company del 123, /company clear",
        "company_del_usage": "Вкажи id: /company del 123",
        "company_add_usage": "Вкажи id або назву: /company add 123",
        "company_not_found": "Компанію з id {v} не знайдено. Подивись /companies",
        "company_unavailable": "Ця компанія недоступна.",
        "nothing_found": "Нічого не знайдено.",
        "nothing_found_hint": "Нічого не знайдено. Подивись /companies",
        "several_found": "Знайдено кілька, вкажи id:",
        "companies_header": "Компанії за 90 днів (id, назва, к-сть змін):",
        "no_users": "Підписників ще немає.",
        "users_header": "<b>Підписники: {v}</b>",
        "user_row": "{flags} {label} · з {since} · вікон {w} · компаній {c} · {ul}",
        "block_usage": "Приклад: {v} 123456789",
        "cant_block_admin": "Адміна заблокувати не можна.",
        "no_such_user": "Такого підписника немає.",
        "blocked": "🚫 Заблоковано: ",
        "unblocked": "✅ Розблоковано: ",
        "new_subscriber": "🆕 Новий підписник: ",
        "subscriber": "Підписник: ",
        "state_blocked": "🚫 Заблоковано",
        "state_active": "✅ Активний",
        "btn_block": "🚫 Заблокувати",
        "btn_unblock": "✅ Розблокувати",
        "cb_blocked": "Заблоковано",
        "cb_unblocked": "Розблоковано",
        "cb_not_found": "Не знайдено",
        "lang_prompt": "Вибери мову / Vyber jazyk:",
        "lang_set": "Мову змінено: українська.",
        "err_no_days": "не вказано дні",
        "err_bad_day": "невідомий день «{v}»",
        "err_time_format": "час має бути у форматі 08:00-18:00",
        "err_bad_time": "неправильний час «{v}»",
        "err_same": "початок і кінець вікна однакові",
        "err_set_example": "приклад: /set пн-пт 17:00-23:00",
    },
    "cs": {
        "help": (
            "Posílám nové a znovu otevřené směny ze Shameless.\n\n"
            "<b>Kdy ti vyhovuje pracovat</b> (den a čas samotné směny, pražský čas):\n"
            "/set po-pá 17:00-23:00 — nahradit okna pro tyto dny\n"
            "/add pá 00:00-08:00 — přidat další okno\n"
            "/off so — odebrat okna pro den\n"
            "/clear — odebrat všechna okna (dostávat všechny směny)\n"
            "Okno přes půlnoc, např. <code>/set čt 22:00-07:00</code>, pokryje i noční směny, "
            "které začínají v pátek ráno. Směna se do okna musí vejít celá.\n"
            "Když máš nastavená okna, dny bez okna se neposílají.\n\n"
            "<b>Firmy</b>:\n"
            "/companies [text] — seznam firem s id\n"
            "/company add 123 456 nebo /company add název\n"
            "/company del 123, /company clear — všechny firmy\n\n"
            "/schedule — moje nastavení\n"
            "/pause, /resume — pozastavit nebo obnovit upozornění\n"
            "/lang — jazyk / мова"
        ),
        "admin_help": "\n\n<b>Admin</b>: /users, /block id, /unblock id",
        "hello": "Ahoj! ",
        "settings_title": "<b>Moje nastavení</b>",
        "status": "Stav: {v}",
        "active": "✅ aktivní",
        "paused": "⏸ pozastaveno",
        "windows": "Okna směn:",
        "windows_all": "Okna směn: všechny dny a hodiny",
        "companies": "Firmy:",
        "companies_all": "Firmy: všechny",
        "language": "Jazyk: čeština",
        "next_morning": " (do rána následujícího dne)",
        "unknown_cmd": "Neznámý příkaz. /help",
        "access_closed": "Přístup zamítnut.",
        "paused_msg": "⏸ Upozornění pozastavena. /resume — obnovit.",
        "resumed_msg": "▶️ Upozornění obnovena.",
        "windows_cleared": "Okna odebrána: budu posílat směny v jakýkoli den a čas.",
        "companies_cleared": "Filtr firem odebrán: budu posílat všechny firmy.",
        "company_usage": "Příklad: /company add 123, /company del 123, /company clear",
        "company_del_usage": "Zadej id: /company del 123",
        "company_add_usage": "Zadej id nebo název: /company add 123",
        "company_not_found": "Firma s id {v} nenalezena. Podívej se na /companies",
        "company_unavailable": "Tato firma není dostupná.",
        "nothing_found": "Nic nenalezeno.",
        "nothing_found_hint": "Nic nenalezeno. Podívej se na /companies",
        "several_found": "Nalezeno více firem, zadej id:",
        "companies_header": "Firmy za 90 dní (id, název, počet směn):",
        "no_users": "Zatím žádní odběratelé.",
        "users_header": "<b>Odběratelé: {v}</b>",
        "user_row": "{flags} {label} · od {since} · oken {w} · firem {c} · {ul}",
        "block_usage": "Příklad: {v} 123456789",
        "cant_block_admin": "Admina nelze zablokovat.",
        "no_such_user": "Takový odběratel neexistuje.",
        "blocked": "🚫 Zablokováno: ",
        "unblocked": "✅ Odblokováno: ",
        "new_subscriber": "🆕 Nový odběratel: ",
        "subscriber": "Odběratel: ",
        "state_blocked": "🚫 Zablokován",
        "state_active": "✅ Aktivní",
        "btn_block": "🚫 Zablokovat",
        "btn_unblock": "✅ Odblokovat",
        "cb_blocked": "Zablokováno",
        "cb_unblocked": "Odblokováno",
        "cb_not_found": "Nenalezeno",
        "lang_prompt": "Vyber jazyk / Вибери мову:",
        "lang_set": "Jazyk změněn: čeština.",
        "err_no_days": "nejsou zadány dny",
        "err_bad_day": "neznámý den „{v}“",
        "err_time_format": "čas musí být ve formátu 08:00-18:00",
        "err_bad_time": "nesprávný čas „{v}“",
        "err_same": "začátek a konec okna jsou stejné",
        "err_set_example": "příklad: /set po-pá 17:00-23:00",
    },
}

DAY_NAMES = {
    "uk": {1: "пн", 2: "вт", 3: "ср", 4: "чт", 5: "пт", 6: "сб", 7: "нд"},
    "cs": {1: "po", 2: "út", 3: "st", 4: "čt", 5: "pá", 6: "so", 7: "ne"},
}


def t(lang, key, **kw):
    lang = lang if lang in TEXTS else DEFAULT_LANG
    text = TEXTS[lang].get(key) or TEXTS[DEFAULT_LANG][key]
    return text.format(**kw) if kw else text


def lang_of(user):
    return user.lang if user is not None and user.lang in LANGS else DEFAULT_LANG


def detect_lang(language_code):
    """Мова за замовчуванням з налаштувань Telegram: чеська/словацька -> cs, інакше uk."""
    return "cs" if (language_code or "").lower()[:2] in ("cs", "sk") else DEFAULT_LANG


class ParseError(ValueError):
    """Помилка розбору команди: key — ключ у TEXTS, value — що саме не так."""
    def __init__(self, key, value=None):
        super().__init__(key, value)
        self.key, self.value = key, value

    def render(self, lang):
        return t(lang, self.key, v=self.value) if self.value is not None else t(lang, self.key)


# =====================================================================
#  Чиста логіка (без БД і мережі) — покрита тестами
# =====================================================================
_DAY_ALIASES = {
    1: ["пн", "пон", "понеділок", "po", "pondeli", "pondělí", "mon", "monday", "1"],
    2: ["вт", "вів", "вівторок", "ut", "út", "utery", "úterý", "tue", "tuesday", "2"],
    3: ["ср", "сер", "середа", "st", "streda", "středa", "wed", "wednesday", "3"],
    4: ["чт", "чет", "четвер", "ct", "čt", "ctvrtek", "čtvrtek", "thu", "thursday", "4"],
    5: ["пт", "пят", "п'ят", "пʼят", "пятниця", "п'ятниця", "пʼятниця", "pa", "pá", "patek", "pátek", "fri", "friday", "5"],
    6: ["сб", "суб", "субота", "so", "sobota", "sat", "saturday", "6"],
    7: ["нд", "нед", "неділя", "ne", "nedele", "neděle", "sun", "sunday", "7"],
}
DAY_LOOKUP = {alias: d for d, aliases in _DAY_ALIASES.items() for alias in aliases}
_GROUPS = {
    "будні": [1, 2, 3, 4, 5], "робочі": [1, 2, 3, 4, 5], "weekdays": [1, 2, 3, 4, 5],
    "všední": [1, 2, 3, 4, 5], "vsedni": [1, 2, 3, 4, 5],
    "вихідні": [6, 7], "weekend": [6, 7], "víkend": [6, 7], "vikend": [6, 7],
    "все": list(range(1, 8)), "всі": list(range(1, 8)), "щодня": list(range(1, 8)), "all": list(range(1, 8)),
    "vše": list(range(1, 8)), "vse": list(range(1, 8)), "denně": list(range(1, 8)), "denne": list(range(1, 8)),
}


def parse_days(text):
    """'пн-пт', 'po-pá', 'сб,нд', 'пт-пн', 'будні' -> відсортований список 1..7. ParseError, якщо не вдалося."""
    text = text.lower().replace(" ", "").replace("’", "'")
    if not text:
        raise ParseError("err_no_days")
    days = set()
    for token in text.split(","):
        if not token:
            continue
        if token in _GROUPS:
            days.update(_GROUPS[token])
            continue
        if "-" in token:
            a, _, b = token.partition("-")
            if a not in DAY_LOOKUP or b not in DAY_LOOKUP:
                raise ParseError("err_bad_day", token)
            d, end = DAY_LOOKUP[a], DAY_LOOKUP[b]
            days.add(d)
            while d != end:  # дозволяємо перехід через тиждень: пт-пн
                d = d % 7 + 1
                days.add(d)
            continue
        if token not in DAY_LOOKUP:
            raise ParseError("err_bad_day", token)
        days.add(DAY_LOOKUP[token])
    if not days:
        raise ParseError("err_no_days")
    return sorted(days)


_TIME_RE = re.compile(r"^(\d{1,2})(?:[:.](\d{2}))?$")


def _parse_hhmm(text):
    m = _TIME_RE.match(text.strip())
    if not m:
        raise ParseError("err_bad_time", text)
    h, mnt = int(m.group(1)), int(m.group(2) or 0)
    if mnt > 59 or h > 24 or (h == 24 and mnt != 0):
        raise ParseError("err_bad_time", text)
    return h * 60 + mnt


def parse_time_range(text):
    """'08:00-18:00', '8-18', '22:00-07:00' -> (start_min, end_min)."""
    text = text.replace("–", "-").replace("—", "-").replace(" ", "")
    a, sep, b = text.partition("-")
    if not sep:
        raise ParseError("err_time_format")
    start, end = _parse_hhmm(a), _parse_hhmm(b)
    if start == 1440:
        start = 0
    if start == end:
        raise ParseError("err_same")
    return start, end


def parse_schedule_args(args):
    """['пн-пт', '17:00-23:00'] або ['сб,', 'нд', '10-14'] -> (days, start_min, end_min)."""
    if len(args) < 2:
        raise ParseError("err_set_example")
    start, end = parse_time_range(args[-1])
    days = parse_days("".join(args[:-1]))
    return days, start, end


def fmt_min(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def fmt_window(weekday, start_min, end_min, lang=DEFAULT_LANG):
    suffix = t(lang, "next_morning") if end_min <= start_min else ""
    names = DAY_NAMES.get(lang, DAY_NAMES[DEFAULT_LANG])
    return f"{names[weekday]} {fmt_min(start_min)}-{fmt_min(end_min)}{suffix}"


def _naive(dt):
    return dt.replace(tzinfo=None) if dt is not None and dt.tzinfo else dt


def shift_matches(start_dt, end_dt, windows):
    """Чи вміщується зміна повністю хоча б в одне вікно.

    start_dt/end_dt — час зміни в Europe/Prague (як у повідомленні).
    windows — список (weekday, start_min, end_min).
    Без вікон — підходить усе. Без часу зміни і з вікнами — не підходить.
    """
    if not windows:
        return True
    if start_dt is None:
        return False
    s = _naive(start_dt)
    e = _naive(end_dt) if end_dt is not None else s
    if e < s:
        e = s
    for base_date in (s.date(), s.date() - timedelta(days=1)):
        midnight = datetime.combine(base_date, datetime.min.time())
        for weekday, start_min, end_min in windows:
            if weekday != base_date.isoweekday():
                continue
            w_start = midnight + timedelta(minutes=start_min)
            w_end = midnight + timedelta(minutes=end_min if end_min > start_min else 1440 + end_min)
            if w_start <= s and e <= w_end:
                return True
    return False


def company_matches(company_id, allowed_ids):
    """Без вибраних компаній — підходить будь-яка (крім виключених глобально)."""
    if company_id is not None and str(company_id) in EXCLUDED_COMPANIES:
        return False
    if not allowed_ids:
        return True
    return company_id is not None and int(company_id) in allowed_ids


# =====================================================================
#  Telegram API
# =====================================================================
def _api(method, **params):
    """Виклик Bot API. Повертає розібраний JSON або {'ok': False, ...}."""
    url = f"https://api.telegram.org/bot{_token}/{method}"
    for key, val in list(params.items()):
        if isinstance(val, (dict, list)):
            params[key] = json.dumps(val)
    try:
        r = requests.post(url, data=params, timeout=65)
        return r.json()
    except Exception as e:
        return {"ok": False, "description": str(e)}


def _send(chat_id, text, reply_markup=None):
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup:
        params["reply_markup"] = reply_markup
    return _api("sendMessage", **params)


def _is_admin(user_id):
    """Адмін = ADMIN_CHAT_ID або власник/адміністратор адмін-каналу (перевірка кешується на 5 хв)."""
    if user_id is None:
        return False
    if ADMIN_CHAT_ID and str(user_id) == ADMIN_CHAT_ID:
        return True
    if not ADMIN_CHANNEL_ID:
        return False
    cached = _admin_cache.get(user_id)
    if cached and time.time() - cached[1] < ADMIN_CACHE_SECONDS:
        return cached[0]
    resp = _api("getChatMember", chat_id=ADMIN_CHANNEL_ID, user_id=user_id)
    if not resp.get("ok"):
        # мережева/тимчасова помилка: не кешуємо, щоб наступна перевірка спробувала знову
        if resp.get("error_code") != 400:
            return bool(cached and cached[0])
        result = False  # 400: користувача в каналі немає
    else:
        result = (resp.get("result") or {}).get("status") in ("creator", "administrator")
    _admin_cache[user_id] = (result, time.time())
    return result


def _admin_alert_chat():
    return ADMIN_CHANNEL_ID or ADMIN_CHAT_ID


def _block_markup(chat_id, blocked, lang):
    if blocked:
        return {"inline_keyboard": [[{"text": t(lang, "btn_unblock"), "callback_data": f"unb:{chat_id}"}]]}
    return {"inline_keyboard": [[{"text": t(lang, "btn_block"), "callback_data": f"blk:{chat_id}"}]]}


def _lang_markup():
    return {"inline_keyboard": [[
        {"text": "🇺🇦 Українська", "callback_data": "lang:uk"},
        {"text": "🇨🇿 Čeština", "callback_data": "lang:cs"},
    ]]}


def _user_label(user):
    name = html.escape(user.first_name or "")
    uname = f" (@{html.escape(user.username)})" if user.username else ""
    return f"{name}{uname}, id <code>{user.chat_id}</code>"


# =====================================================================
#  Розсилка
# =====================================================================
def enqueue(message, start_dt, end_dt, company_id):
    """Викликається з scrape(). Нічого не робить, якщо модуль не запущено."""
    if _started:
        _queue.put((message, start_dt, end_dt, company_id))


def recipients_for(db, start_dt, end_dt, company_id):
    users = db.query(BotUser.chat_id).filter(BotUser.is_active.is_(True), BotUser.is_blocked.is_(False)).all()
    ids = [u.chat_id for u in users]
    if not ids:
        return []
    windows, companies = {}, {}
    for w in db.query(BotUserWindow).filter(BotUserWindow.chat_id.in_(ids)).all():
        windows.setdefault(w.chat_id, []).append((w.weekday, w.start_min, w.end_min))
    for c in db.query(BotUserCompany).filter(BotUserCompany.chat_id.in_(ids)).all():
        companies.setdefault(c.chat_id, set()).add(c.company_id)
    return [
        cid for cid in ids
        if company_matches(company_id, companies.get(cid)) and shift_matches(start_dt, end_dt, windows.get(cid))
    ]


def _deliver(message, start_dt, end_dt, company_id):
    with _Session() as db:
        chat_ids = recipients_for(db, start_dt, end_dt, company_id)
    for chat_id in chat_ids:
        resp = _send(chat_id, message)
        if not resp.get("ok") and resp.get("error_code") == 429:
            time.sleep(int((resp.get("parameters") or {}).get("retry_after", 5)) + 1)
            resp = _send(chat_id, message)
        if not resp.get("ok"):
            print(f"[subscribers] send to {chat_id} failed: {resp.get('description')}")
            if resp.get("error_code") == 403:  # користувач заблокував бота
                with _Session() as db:
                    user = db.get(BotUser, chat_id)
                    if user:
                        user.is_active = False
                        db.commit()
        time.sleep(0.05)


def _send_loop():
    while True:
        item = _queue.get()
        try:
            _deliver(*item)
        except Exception:
            print("[subscribers] delivery error")
            traceback.print_exc()


# =====================================================================
#  Команди
# =====================================================================
def _settings_text(db, chat_id):
    user = db.get(BotUser, chat_id)
    lang = lang_of(user)
    windows = db.query(BotUserWindow).filter_by(chat_id=chat_id).order_by(
        BotUserWindow.weekday, BotUserWindow.start_min).all()
    companies = db.query(BotUserCompany).filter_by(chat_id=chat_id).order_by(BotUserCompany.company_id).all()
    lines = [t(lang, "settings_title")]
    lines.append(t(lang, "status", v=t(lang, "active" if user.is_active else "paused")))
    if windows:
        lines.append(t(lang, "windows"))
        lines += [" • " + fmt_window(w.weekday, w.start_min, w.end_min, lang) for w in windows]
    else:
        lines.append(t(lang, "windows_all"))
    if companies:
        lines.append(t(lang, "companies"))
        lines += [f" • {html.escape(c.company_name or '?')} (<code>{c.company_id}</code>)" for c in companies]
    else:
        lines.append(t(lang, "companies_all"))
    lines.append(t(lang, "language"))
    return "\n".join(lines)


def _find_companies(db, search=None, limit=40):
    sql = """
        SELECT company_id, MAX(company) AS name, COUNT(*) AS n
        FROM positions_v4
        WHERE company_id IS NOT NULL AND date >= :since
    """
    params = {"since": (datetime.utcnow() - timedelta(days=90)).date()}
    if search:
        sql += " AND LOWER(company) LIKE :q"
        params["q"] = f"%{search.lower()}%"
    sql += " GROUP BY company_id ORDER BY n DESC LIMIT :lim"
    params["lim"] = limit + len(EXCLUDED_COMPANIES)
    rows = db.execute(sqlalchemy.text(sql), params).fetchall()
    return [(int(r[0]), r[1], r[2]) for r in rows if str(r[0]) not in EXCLUDED_COMPANIES][:limit]


def _company_name(db, company_id):
    row = db.execute(sqlalchemy.text(
        "SELECT MAX(company) FROM positions_v4 WHERE company_id = :cid"), {"cid": company_id}).fetchone()
    return row[0] if row else None


def _cmd_company(db, chat_id, args, lang):
    if not args:
        return t(lang, "company_usage")
    action, rest = args[0].lower(), args[1:]
    if action == "clear":
        db.query(BotUserCompany).filter_by(chat_id=chat_id).delete()
        db.commit()
        return t(lang, "companies_cleared")
    if action in ("del", "rm", "remove"):
        ids = [int(a) for a in rest if a.isdigit()]
        if not ids:
            return t(lang, "company_del_usage")
        db.query(BotUserCompany).filter(BotUserCompany.chat_id == chat_id,
                                        BotUserCompany.company_id.in_(ids)).delete(synchronize_session=False)
        db.commit()
        return _settings_text(db, chat_id)
    if action == "add":
        if not rest:
            return t(lang, "company_add_usage")
        if all(a.isdigit() for a in rest):
            found = []
            for cid in map(int, rest):
                if str(cid) in EXCLUDED_COMPANIES:
                    continue
                name = _company_name(db, cid)
                if name is None:
                    return t(lang, "company_not_found", v=cid)
                found.append((cid, name))
        else:
            matches = _find_companies(db, " ".join(rest), limit=10)
            if len(matches) != 1:
                if not matches:
                    return t(lang, "nothing_found_hint")
                return t(lang, "several_found") + "\n" + "\n".join(
                    f"<code>{cid}</code> {html.escape(name or '')}" for cid, name, _ in matches)
            found = [(matches[0][0], matches[0][1])]
        if not found:
            return t(lang, "company_unavailable")
        for cid, name in found:
            if not db.get(BotUserCompany, (chat_id, cid)):
                db.add(BotUserCompany(chat_id=chat_id, company_id=cid, company_name=name))
        db.commit()
        return _settings_text(db, chat_id)
    return t(lang, "company_usage")


def _cmd_windows(db, chat_id, cmd, args, lang):
    try:
        if cmd == "/off":
            days = parse_days("".join(args))
            start = end = None
        else:
            days, start, end = parse_schedule_args(args)
    except ParseError as e:
        return f"⚠️ {e.render(lang)}"
    if cmd in ("/set", "/off"):
        db.query(BotUserWindow).filter(BotUserWindow.chat_id == chat_id,
                                       BotUserWindow.weekday.in_(days)).delete(synchronize_session=False)
    if cmd in ("/set", "/add"):
        for d in days:
            db.add(BotUserWindow(chat_id=chat_id, weekday=d, start_min=start, end_min=end))
    db.commit()
    return _settings_text(db, chat_id)


def _set_blocked(db, target_id, blocked):
    user = db.get(BotUser, target_id)
    if not user:
        return None
    user.is_blocked = blocked
    user.blocked_at = datetime.utcnow() if blocked else None
    db.commit()
    return user


def _cmd_users(db, lang):
    users = db.query(BotUser).order_by(BotUser.created_at).all()
    if not users:
        return t(lang, "no_users")
    lines = [t(lang, "users_header", v=len(users))]
    for u in users:
        flags = "🚫" if u.is_blocked else ("⏸" if not u.is_active else "✅")
        lines.append(t(
            lang, "user_row", flags=flags, label=_user_label(u),
            since=u.created_at.strftime("%d.%m.%Y") if u.created_at else "?",
            w=db.query(BotUserWindow).filter_by(chat_id=u.chat_id).count(),
            c=db.query(BotUserCompany).filter_by(chat_id=u.chat_id).count(),
            ul=lang_of(u),
        ))
    return "\n".join(lines)


def _handle_admin_command(db, cmd, args, lang):
    if cmd == "/users":
        return _cmd_users(db, lang)
    if cmd in ("/block", "/unblock"):
        if not args or not args[0].lstrip("-").isdigit():
            return t(lang, "block_usage", v=cmd)
        target = int(args[0])
        if _is_admin(target):
            return t(lang, "cant_block_admin")
        user = _set_blocked(db, target, cmd == "/block")
        if not user:
            return t(lang, "no_such_user")
        return t(lang, "blocked" if user.is_blocked else "unblocked") + _user_label(user)
    return None


def _handle_message(db, msg):
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    chat_id = chat["id"]
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]
    sender = msg.get("from") or {}

    user = db.get(BotUser, chat_id)
    is_new = user is None
    if is_new:
        user = BotUser(chat_id=chat_id, is_active=True, is_blocked=False, created_at=datetime.utcnow(),
                       lang=detect_lang(sender.get("language_code")))
        db.add(user)
    user.username = sender.get("username")
    user.first_name = sender.get("first_name")
    user.last_seen_at = datetime.utcnow()
    db.commit()
    lang = lang_of(user)

    if is_new and _admin_alert_chat() and not _is_admin(chat_id):
        _send(_admin_alert_chat(), t(ADMIN_LANG, "new_subscriber") + _user_label(user),
              _block_markup(chat_id, False, ADMIN_LANG))

    if user.is_blocked:
        _send(chat_id, t(lang, "access_closed"))
        return

    if _is_admin(chat_id):
        reply = _handle_admin_command(db, cmd, args, lang)
        if reply is not None:
            _send(chat_id, reply)
            return

    help_text = t(lang, "help") + (t(lang, "admin_help") if _is_admin(chat_id) else "")
    markup = None
    if cmd == "/start":
        if not user.is_active:
            user.is_active = True
            db.commit()
        reply = t(lang, "hello") + help_text
        if is_new:
            markup = _lang_markup()  # одразу пропонуємо змінити мову
    elif cmd == "/help":
        reply = help_text
    elif cmd in ("/lang", "/language", "/jazyk", "/mova"):
        reply, markup = t(lang, "lang_prompt"), _lang_markup()
    elif cmd in ("/schedule", "/settings"):
        reply = _settings_text(db, chat_id)
    elif cmd in ("/set", "/add", "/off"):
        reply = _cmd_windows(db, chat_id, cmd, args, lang)
    elif cmd == "/clear":
        db.query(BotUserWindow).filter_by(chat_id=chat_id).delete()
        db.commit()
        reply = t(lang, "windows_cleared")
    elif cmd == "/companies":
        rows = _find_companies(db, " ".join(args) or None)
        reply = (t(lang, "companies_header") + "\n" + "\n".join(
            f"<code>{cid}</code> {html.escape(name or '')} · {n}" for cid, name, n in rows)
        ) if rows else t(lang, "nothing_found")
    elif cmd == "/company":
        reply = _cmd_company(db, chat_id, args, lang)
    elif cmd == "/pause":
        user.is_active = False
        db.commit()
        reply = t(lang, "paused_msg")
    elif cmd == "/resume":
        user.is_active = True
        db.commit()
        reply = t(lang, "resumed_msg")
    else:
        reply = t(lang, "unknown_cmd")
    _send(chat_id, reply, markup)


def _handle_lang_callback(db, cq, new_lang):
    from_id = (cq.get("from") or {}).get("id")
    user = db.get(BotUser, from_id)
    if not user or user.is_blocked or new_lang not in LANGS:
        _api("answerCallbackQuery", callback_query_id=cq.get("id"))
        return
    user.lang = new_lang
    db.commit()
    _api("answerCallbackQuery", callback_query_id=cq.get("id"), text=t(new_lang, "lang_set"))
    msg = cq.get("message") or {}
    if msg:
        _api("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
             text=t(new_lang, "lang_set") + "\n\n" + t(new_lang, "help")
             + (t(new_lang, "admin_help") if _is_admin(from_id) else ""),
             parse_mode="HTML", disable_web_page_preview=True)


def _handle_callback(db, cq):
    from_id = (cq.get("from") or {}).get("id")
    data = cq.get("data") or ""
    action, _, target = data.partition(":")
    if action == "lang":
        _handle_lang_callback(db, cq, target)
        return
    if not _is_admin(from_id) or action not in ("blk", "unb") or not target.lstrip("-").isdigit():
        _api("answerCallbackQuery", callback_query_id=cq.get("id"))
        return
    msg = cq.get("message") or {}
    # мова відповіді: в адмін-каналі — ADMIN_LANG, в особистому чаті — мова адміна
    in_private = (msg.get("chat") or {}).get("type") == "private"
    lang = lang_of(db.get(BotUser, from_id)) if in_private else ADMIN_LANG
    if action == "blk" and _is_admin(int(target)):
        _api("answerCallbackQuery", callback_query_id=cq.get("id"), text=t(lang, "cant_block_admin"))
        return
    user = _set_blocked(db, int(target), action == "blk")
    _api("answerCallbackQuery", callback_query_id=cq.get("id"),
         text=t(lang, ("cb_blocked" if user.is_blocked else "cb_unblocked") if user else "cb_not_found"))
    if user and msg:
        state = t(lang, "state_blocked" if user.is_blocked else "state_active")
        _api("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
             text=f"{t(lang, 'subscriber')}{_user_label(user)}\n{state}", parse_mode="HTML",
             reply_markup=_block_markup(user.chat_id, user.is_blocked, lang))


def _handle_member(db, upd):
    """Користувач заблокував / розблокував бота у своєму чаті."""
    chat = upd.get("chat") or {}
    if chat.get("type") != "private":
        return
    status = (upd.get("new_chat_member") or {}).get("status")
    user = db.get(BotUser, chat.get("id"))
    if user and status == "kicked":
        user.is_active = False
        db.commit()


def handle_update(upd):
    with _Session() as db:
        if "message" in upd:
            _handle_message(db, upd["message"])
        elif "callback_query" in upd:
            _handle_callback(db, upd["callback_query"])
        elif "my_chat_member" in upd:
            _handle_member(db, upd["my_chat_member"])


def _poll_loop():
    offset = None
    while True:
        try:
            params = {"timeout": 50, "allowed_updates": ["message", "callback_query", "my_chat_member"]}
            if offset is not None:
                params["offset"] = offset
            data = _api("getUpdates", **params)
            if not data.get("ok"):
                print(f"[subscribers] getUpdates failed: {data.get('description')}")
                time.sleep(30 if data.get("error_code") == 409 else 10)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    handle_update(upd)
                except Exception:
                    print("[subscribers] update handling error")
                    traceback.print_exc()
        except Exception:
            traceback.print_exc()
            time.sleep(10)


# =====================================================================
#  Запуск
# =====================================================================
def _migrate(engine):
    """Додає колонки, яких немає в уже створених таблицях (create_all їх не додає)."""
    cols = {c["name"] for c in sqlalchemy.inspect(engine).get_columns("bot_users")}
    if "lang" not in cols:
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("ALTER TABLE bot_users ADD COLUMN lang VARCHAR(2)"))
        print("[subscribers] added column bot_users.lang")


def init(engine, token):
    """Створює таблиці (positions_v4 не чіпає) і готує сесії. Без потоків."""
    global _engine, _Session, _token
    _engine, _token = engine, token
    _Session = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    _migrate(engine)


def start(engine, token):
    """Запускає потоки команд і розсилки. Помилки тут не зупиняють скрейпер."""
    global _started
    if not ENABLED:
        print("[subscribers] disabled via SUBSCRIBERS_ENABLED=0")
        return
    if not token:
        print("[subscribers] BOT_TOKEN is not set, subscriptions disabled")
        return
    try:
        init(engine, token)
    except Exception:
        print("[subscribers] init failed, subscriptions disabled")
        traceback.print_exc()
        return
    if not ADMIN_CHAT_ID and not ADMIN_CHANNEL_ID:
        print("[subscribers] neither ADMIN_CHANNEL_ID nor ADMIN_CHAT_ID is set: no new-user alerts and no /block")
    elif ADMIN_CHANNEL_ID:
        check = _api("getChat", chat_id=ADMIN_CHANNEL_ID)
        if not check.get("ok"):
            print(f"[subscribers] admin channel is not reachable: {check.get('description')}")
        elif (check.get("result") or {}).get("username"):
            print("[subscribers] WARNING: admin channel is public, subscriber details will be visible")
    threading.Thread(target=_poll_loop, name="tg-commands", daemon=True).start()
    threading.Thread(target=_send_loop, name="tg-subscribers", daemon=True).start()
    _started = True
    print("[subscribers] started")
