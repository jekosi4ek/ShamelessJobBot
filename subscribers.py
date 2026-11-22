"""
Особисті підписки в боті: фільтр за днем/часом зміни та за компанією.

Працює поруч з основним скрейпером і не змінює його поведінку:
- окремий потік читає команди (getUpdates);
- окремий потік розсилає повідомлення підписникам з черги;
- будь-яка помилка тут лише логується й не зупиняє scrape().

Змінні середовища:
- ADMIN_CHANNEL_ID — числовий id приватного адмін-каналу (-100...). Туди йдуть сповіщення про нових
  підписників з кнопкою блокування; адміни цього каналу можуть натискати кнопки і виконувати /users, /block;
- ADMIN_CHAT_ID (або CHATBOT_ID) — твій особистий chat id; завжди адмін. Якщо адмін-каналу немає,
  сповіщення про нових підписників ідуть сюди;
- SUBSCRIBER_EXCLUDED_COMPANIES — id компаній через кому, які підписники не отримують (за замовчуванням "555", як і канал);
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
    c.strip() for c in os.getenv("SUBSCRIBER_EXCLUDED_COMPANIES", "555").split(",") if c.strip()
}
ENABLED = os.getenv("SUBSCRIBERS_ENABLED", "1") != "0"

Base = declarative_base()


class BotUser(Base):
    __tablename__ = "bot_users"
    chat_id = Column(BigInteger, primary_key=True, autoincrement=False)
    username = Column(String(255))
    first_name = Column(String(255))
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
#  Чиста логіка (без БД і мережі) — покрита тестами
# =====================================================================
DAY_NAMES = {1: "пн", 2: "вт", 3: "ср", 4: "чт", 5: "пт", 6: "сб", 7: "нд"}
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
    "вихідні": [6, 7], "weekend": [6, 7],
    "все": list(range(1, 8)), "всі": list(range(1, 8)), "щодня": list(range(1, 8)), "all": list(range(1, 8)),
}


def parse_days(text):
    """'пн-пт', 'сб,нд', 'пт-пн', 'будні' -> відсортований список 1..7. ValueError, якщо не вдалося."""
    text = text.lower().replace(" ", "").replace("’", "'")
    if not text:
        raise ValueError("не вказано дні")
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
                raise ValueError(f"невідомий день у «{token}»")
            d, end = DAY_LOOKUP[a], DAY_LOOKUP[b]
            days.add(d)
            while d != end:  # дозволяємо перехід через тиждень: пт-пн
                d = d % 7 + 1
                days.add(d)
            continue
        if token not in DAY_LOOKUP:
            raise ValueError(f"невідомий день «{token}»")
        days.add(DAY_LOOKUP[token])
    if not days:
        raise ValueError("не вказано дні")
    return sorted(days)


_TIME_RE = re.compile(r"^(\d{1,2})(?:[:.](\d{2}))?$")


def _parse_hhmm(text):
    m = _TIME_RE.match(text.strip())
    if not m:
        raise ValueError(f"неправильний час «{text}»")
    h, mnt = int(m.group(1)), int(m.group(2) or 0)
    if mnt > 59 or h > 24 or (h == 24 and mnt != 0):
        raise ValueError(f"неправильний час «{text}»")
    return h * 60 + mnt


def parse_time_range(text):
    """'08:00-18:00', '8-18', '22:00-07:00' -> (start_min, end_min)."""
    text = text.replace("–", "-").replace("—", "-").replace(" ", "")
    a, sep, b = text.partition("-")
    if not sep:
        raise ValueError("час має бути у форматі 08:00-18:00")
    start, end = _parse_hhmm(a), _parse_hhmm(b)
    if start == 1440:
        start = 0
    if start == end:
        raise ValueError("початок і кінець вікна однакові")
    return start, end


def parse_schedule_args(args):
    """['пн-пт', '17:00-23:00'] або ['сб,', 'нд', '10-14'] -> (days, start_min, end_min)."""
    if len(args) < 2:
        raise ValueError("приклад: /set пн-пт 17:00-23:00")
    start, end = parse_time_range(args[-1])
    days = parse_days("".join(args[:-1]))
    return days, start, end


def fmt_min(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def fmt_window(weekday, start_min, end_min):
    suffix = " (до ранку наступного дня)" if end_min <= start_min else ""
    return f"{DAY_NAMES[weekday]} {fmt_min(start_min)}-{fmt_min(end_min)}{suffix}"


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


def _block_markup(chat_id, blocked):
    if blocked:
        return {"inline_keyboard": [[{"text": "✅ Розблокувати", "callback_data": f"unb:{chat_id}"}]]}
    return {"inline_keyboard": [[{"text": "🚫 Заблокувати", "callback_data": f"blk:{chat_id}"}]]}


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
HELP_TEXT = (
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
    "/pause, /resume — зупинити або відновити сповіщення"
)
ADMIN_HELP = "\n\n<b>Адмін</b>: /users, /block id, /unblock id"


def _settings_text(db, chat_id):
    user = db.get(BotUser, chat_id)
    windows = db.query(BotUserWindow).filter_by(chat_id=chat_id).order_by(
        BotUserWindow.weekday, BotUserWindow.start_min).all()
    companies = db.query(BotUserCompany).filter_by(chat_id=chat_id).order_by(BotUserCompany.company_id).all()
    lines = ["<b>Мої налаштування</b>"]
    lines.append("Статус: " + ("✅ активні" if user.is_active else "⏸ пауза"))
    if windows:
        lines.append("Вікна змін:")
        lines += [" • " + fmt_window(w.weekday, w.start_min, w.end_min) for w in windows]
    else:
        lines.append("Вікна змін: всі дні й години")
    if companies:
        lines.append("Компанії:")
        lines += [f" • {html.escape(c.company_name or '?')} (<code>{c.company_id}</code>)" for c in companies]
    else:
        lines.append("Компанії: всі")
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


def _cmd_company(db, chat_id, args):
    if not args:
        return "Приклад: /company add 123, /company del 123, /company clear"
    action, rest = args[0].lower(), args[1:]
    if action == "clear":
        db.query(BotUserCompany).filter_by(chat_id=chat_id).delete()
        db.commit()
        return "Фільтр компаній прибрано: надсилатиму всі компанії."
    if action in ("del", "rm", "remove"):
        ids = [int(a) for a in rest if a.isdigit()]
        if not ids:
            return "Вкажи id: /company del 123"
        db.query(BotUserCompany).filter(BotUserCompany.chat_id == chat_id,
                                        BotUserCompany.company_id.in_(ids)).delete(synchronize_session=False)
        db.commit()
        return _settings_text(db, chat_id)
    if action == "add":
        if not rest:
            return "Вкажи id або назву: /company add 123"
        if all(a.isdigit() for a in rest):
            found = []
            for cid in map(int, rest):
                if str(cid) in EXCLUDED_COMPANIES:
                    continue
                name = _company_name(db, cid)
                if name is None:
                    return f"Компанію з id {cid} не знайдено. Подивись /companies"
                found.append((cid, name))
        else:
            matches = _find_companies(db, " ".join(rest), limit=10)
            if len(matches) != 1:
                if not matches:
                    return "Нічого не знайдено. Подивись /companies"
                return "Знайдено кілька, вкажи id:\n" + "\n".join(
                    f"<code>{cid}</code> {html.escape(name or '')}" for cid, name, _ in matches)
            found = [(matches[0][0], matches[0][1])]
        if not found:
            return "Ця компанія недоступна."
        for cid, name in found:
            if not db.get(BotUserCompany, (chat_id, cid)):
                db.add(BotUserCompany(chat_id=chat_id, company_id=cid, company_name=name))
        db.commit()
        return _settings_text(db, chat_id)
    return "Приклад: /company add 123, /company del 123, /company clear"


def _cmd_windows(db, chat_id, cmd, args):
    try:
        if cmd == "/off":
            days = parse_days("".join(args))
            start = end = None
        else:
            days, start, end = parse_schedule_args(args)
    except ValueError as e:
        return f"⚠️ {e}"
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


def _cmd_users(db):
    users = db.query(BotUser).order_by(BotUser.created_at).all()
    if not users:
        return "Підписників ще немає."
    lines = [f"<b>Підписники: {len(users)}</b>"]
    for u in users:
        flags = "🚫" if u.is_blocked else ("⏸" if not u.is_active else "✅")
        n_win = db.query(BotUserWindow).filter_by(chat_id=u.chat_id).count()
        n_comp = db.query(BotUserCompany).filter_by(chat_id=u.chat_id).count()
        since = u.created_at.strftime("%d.%m.%Y") if u.created_at else "?"
        lines.append(f"{flags} {_user_label(u)} · з {since} · вікон {n_win} · компаній {n_comp}")
    return "\n".join(lines)


def _handle_admin_command(db, cmd, args):
    if cmd == "/users":
        return _cmd_users(db)
    if cmd in ("/block", "/unblock"):
        if not args or not args[0].lstrip("-").isdigit():
            return f"Приклад: {cmd} 123456789"
        target = int(args[0])
        if _is_admin(target):
            return "Адміна заблокувати не можна."
        user = _set_blocked(db, target, cmd == "/block")
        if not user:
            return "Такого підписника немає."
        return ("🚫 Заблоковано: " if user.is_blocked else "✅ Розблоковано: ") + _user_label(user)
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
        user = BotUser(chat_id=chat_id, is_active=True, is_blocked=False, created_at=datetime.utcnow())
        db.add(user)
    user.username = sender.get("username")
    user.first_name = sender.get("first_name")
    user.last_seen_at = datetime.utcnow()
    db.commit()

    if is_new and _admin_alert_chat() and not _is_admin(chat_id):
        _send(_admin_alert_chat(), "🆕 Новий підписник: " + _user_label(user), _block_markup(chat_id, False))

    if user.is_blocked:
        _send(chat_id, "Доступ закрито.")
        return

    if _is_admin(chat_id):
        reply = _handle_admin_command(db, cmd, args)
        if reply is not None:
            _send(chat_id, reply)
            return

    help_text = HELP_TEXT + (ADMIN_HELP if _is_admin(chat_id) else "")
    if cmd == "/start":
        if not user.is_active:
            user.is_active = True
            db.commit()
        reply = "Привіт! " + help_text
    elif cmd == "/help":
        reply = help_text
    elif cmd in ("/schedule", "/settings"):
        reply = _settings_text(db, chat_id)
    elif cmd in ("/set", "/add", "/off"):
        reply = _cmd_windows(db, chat_id, cmd, args)
    elif cmd == "/clear":
        db.query(BotUserWindow).filter_by(chat_id=chat_id).delete()
        db.commit()
        reply = "Вікна прибрано: надсилатиму зміни в будь-який день і час."
    elif cmd == "/companies":
        rows = _find_companies(db, " ".join(args) or None)
        reply = ("Компанії за 90 днів (id, назва, к-сть змін):\n" + "\n".join(
            f"<code>{cid}</code> {html.escape(name or '')} · {n}" for cid, name, n in rows)) if rows else "Нічого не знайдено."
    elif cmd == "/company":
        reply = _cmd_company(db, chat_id, args)
    elif cmd == "/pause":
        user.is_active = False
        db.commit()
        reply = "⏸ Сповіщення на паузі. /resume — відновити."
    elif cmd == "/resume":
        user.is_active = True
        db.commit()
        reply = "▶️ Сповіщення відновлено."
    else:
        reply = "Невідома команда. /help"
    _send(chat_id, reply)


def _handle_callback(db, cq):
    from_id = (cq.get("from") or {}).get("id")
    data = cq.get("data") or ""
    if not _is_admin(from_id) or ":" not in data:
        _api("answerCallbackQuery", callback_query_id=cq.get("id"))
        return
    action, _, target = data.partition(":")
    valid = action in ("blk", "unb") and target.lstrip("-").isdigit()
    if valid and action == "blk" and _is_admin(int(target)):
        _api("answerCallbackQuery", callback_query_id=cq.get("id"), text="Адміна заблокувати не можна")
        return
    user = _set_blocked(db, int(target), action == "blk") if valid else None
    _api("answerCallbackQuery", callback_query_id=cq.get("id"),
         text=("Заблоковано" if user and user.is_blocked else "Розблоковано") if user else "Не знайдено")
    msg = cq.get("message") or {}
    if user and msg:
        state = "🚫 Заблоковано" if user.is_blocked else "✅ Активний"
        _api("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
             text=f"Підписник: {_user_label(user)}\n{state}", parse_mode="HTML",
             reply_markup=_block_markup(user.chat_id, user.is_blocked))


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
def init(engine, token):
    """Створює таблиці (positions_v4 не чіпає) і готує сесії. Без потоків."""
    global _engine, _Session, _token
    _engine, _token = engine, token
    _Session = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)


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
