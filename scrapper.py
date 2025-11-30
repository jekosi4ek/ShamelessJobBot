import requests
import json
import os
import time
import schedule
import asyncio
from datetime import datetime
import sqlalchemy
from sqlalchemy import create_engine, Column, Integer, String, Date, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
from playwright.async_api import async_playwright

# --- Конфіг ---
LOGIN_URL = "https://shameless.sinch.cz/"
API_URL = "https://shameless.sinch.cz/api"
EMAIL = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")
DATABASE_URL = os.getenv("DATABASE_URL")
COOKIES_FILE = "cookies.json"
PAGE = 1
LIMIT = 700

# --- PostgreSQL setup ---
conn_str = DATABASE_URL  # формат: postgresql://user:pass@host:port/dbname
if not conn_str:
    raise RuntimeError("❌ DATABASE_URL is not set. Please configure environment variable.")

engine = sqlalchemy.create_engine(conn_str)
Base = declarative_base()
SessionLocal = sessionmaker(bind=engine)


class Position(Base):
    __tablename__ = "positions_v4"
    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date)
    position_id = Column(Integer, unique=True, nullable=False)
    name = Column(String(255))
    company = Column(String(255))
    company_id = Column(Integer)
    working_hours = Column(String(100))
    location = Column(String(255))
    role_id = Column(Integer)
    profession = Column(String(255))
    free_capacity = Column(Integer)
    total_capacity = Column(Integer)
    wage_hour = Column(Integer)
    wage_fix = Column(Integer)
    scrapped_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(engine)

# --- Telegram ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
CHAT_ID_PARTY = os.getenv("CHAT_ID_PARTY")


def send_to_telegram(message: str, chat_id: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    r = requests.post(url, data=payload)
    print("Telegram response:", r.text)


# --- Cookies ---
def save_cookies(cookies):
    with open(COOKIES_FILE, "w") as f:
        json.dump(cookies, f)


def load_cookies():
    if os.path.exists(COOKIES_FILE):
        with open(COOKIES_FILE, "r") as f:
            return json.load(f)
    return None


async def login_and_get_cookies():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(LOGIN_URL)

        await page.fill("#UserEmail", EMAIL)
        await page.fill("#UserPassword", PASSWORD)
        await page.click("input[data-cy='sign-in-btn']")
        await page.wait_for_timeout(5000)

        cookies_list = await page.context.cookies()
        await browser.close()

        cookies_dict = {c['name']: c['value'] for c in cookies_list}
        save_cookies(cookies_dict)
        print("🔑 Cookies refreshed!")
        return cookies_dict


def get_cookies():
    cookies = load_cookies()
    if not cookies:
        cookies = asyncio.run(login_and_get_cookies())
    return cookies


# --- Scraper ---
def scrape():
    cookies = get_cookies()
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "https://shameless.sinch.cz",
        "Referer": "https://shameless.sinch.cz/react/position",
        "User-Agent": "Mozilla/5.0"
    }

    payload_index = {
        "key": "worker/Positions/Index",
        "meta": {"page": PAGE, "limit": LIMIT},
        "params": {"attend": True}
    }

    resp = requests.post(API_URL, json=payload_index, headers=headers, cookies=cookies)
    if resp.status_code == 401:
        print("⚠️ Cookies expired, refreshing...")
        cookies = asyncio.run(login_and_get_cookies())
        resp = requests.post(API_URL, json=payload_index, headers=headers, cookies=cookies)

    positions = resp.json().get("entities", {}).get("Position", {})

    with SessionLocal() as db:
        for pos_id, pos in positions.items():
            #existing = db.execute(
                #sqlalchemy.text("SELECT 1 FROM positions_v4 WHERE position_id = :position_id"),
                #{"position_id": pos_id}
            #).fetchone()
            #if existing:
                #continue

            payload_view = {"key": f"worker/Positions/View/{pos_id}", "meta": {}, "params": {"id": pos_id}}
            detail_resp = requests.post(API_URL, json=payload_view, headers=headers, cookies=cookies)
            detail_data = detail_resp.json()

            if isinstance(detail_data, list):
                continue
            entities = detail_data.get("entities", {})
            if not isinstance(entities, dict):
                continue

            # витягуємо wage тільки якщо detail_data — dict
            wage = detail_data.get("result", {}).get("wage", {}) or {}
            wage_hour = wage.get("hour")
            wage_fix = wage.get("fix")

            entity = entities.get("Position", {}).get(str(pos_id), {})
            shift = entities.get("Shift", {}).get(str(entity.get("shift")), {})
            company = entities.get("Company", {}).get(str(shift.get("company")), {})
            profession = entities.get("Profession", {}).get(str(entity.get("profession")), {})
            location = entities.get("Location", {}).get(str(entity.get("location")), {})
            # координати
            point = entity.get("point") or location.get("point") or {}
            lat = point.get("lat")
            lng = point.get("lng")

            start = entity.get("startTime")
            end = entity.get("endTime")

            CZ_WEEKDAYS = {
                "Monday": "pondĕlí", "Tuesday": "úterý", "Wednesday": "středa",
                "Thursday": "čtvrtek", "Friday": "pátek", "Saturday": "sobota", "Sunday": "nedĕle"
            }

            ROLES = {
                0: "Pracovník",
                1: "Crewboss",
                2: "Záložník",
            }

            ROLE_ICONS = {
                0: "👷",  # робітник
                1: "💼",  # керівник / Crewboss
                2: "🛡️",  # резерв / Зáložník
            }

            start_fmt, end_fmt, sd_fmt, hours_diff = "", "", "", None
            if start:
                start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                start_fmt = start_dt.strftime("%H:%M")
                sd_fmt = start_dt.strftime("%d.%m.%Y")
                sd_weekday_en = start_dt.strftime("%A")
                sd_weekday_cz = CZ_WEEKDAYS.get(sd_weekday_en, sd_weekday_en)
            if end:
                end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                end_fmt = end_dt.strftime("%H:%M")
                hours_diff = (end_dt - start_dt).total_seconds() / 3600

            pretty = {
                "Дата": sd_fmt,
                "ID Позиції": pos_id,
                "Назва": shift.get("name", ""),
                "Компанія": company.get("name"),
                "ID Компанії": company.get("id"),
                "Час": f"{start_fmt} - {end_fmt} ({hours_diff} h)",
                "Локація": location.get("address"),
                "Локація (lat)": lat,
                "Локація (lng)": lng,
                "ID Role": entity.get('role'),
                "Icon": f"{ROLE_ICONS.get(entity.get('role'), entity.get('role'))}",
                "Професія": f"{profession.get('name')} - {ROLES.get(entity.get('role'), entity.get('role'))}",
                "freeCapacity": entity.get('freeCapacity'),
                "totalCapacity": entity.get('totalCapacity'),
                "Вільних місць з Усього": f"{entity.get('freeCapacity')}/{entity.get('totalCapacity')}",
                "Оплата (година)": wage_hour,
                "Оплата (фікс)": wage_fix,
                "scrapped_at": datetime.utcnow()
            }

            # новий стан
            new_free_capacity = pretty["freeCapacity"]

            # отримуємо попередній стан для цієї позиції
            prev_capacity_row = db.execute(sqlalchemy.text("""
                SELECT free_capacity FROM positions_v4
                WHERE position_id = :position_id
            """), {"position_id": pretty["ID Позиції"]}).fetchone()

            prev_capacity = prev_capacity_row[0] if prev_capacity_row else None

            # визначаємо статус
            if prev_capacity is None:
                status_text = "Нова вакансія"
            elif prev_capacity == 0 and new_free_capacity > 0:
                status_text = "Перевідкрита вакансія"
            else:
                status_text = None  # не відправляємо повідомлення

            # --- Повне повідомлення ---
            if status_text and pretty.get('ID Role') != 2:
                link = f"https://shameless.sinch.cz/react/position/{pretty['ID Позиції']}"
                if pretty["Локація (lat)"] and pretty["Локація (lng)"]:
                    maps_link = f"https://www.google.com/maps/search/?api=1&query={pretty['Локація (lat)']},{pretty['Локація (lng)']}"
                    maps_line = f"📍 <a href='{maps_link}'>{pretty['Локація']}</a>\n"
                else:
                    maps_line = f"📍 {pretty['Локація']}\n"
                message = (
                    f"📢 <b>{status_text}!</b>\n"
                    f'🎯 <a href="{link}">{pretty["Назва"]}</a>\n'
                    f"📅 {pretty['Дата']} ({sd_weekday_cz})\n"
                    f"🏢 {pretty['Компанія']}\n"
                    f"⏱️ {pretty['Час']}\n"
                    f"💰 {pretty['Оплата (година)']} Kč/h + {pretty['Оплата (фікс)']} Kč\n"
                    f"{maps_line}"
                    f"{pretty['Icon']} {pretty['Професія']}\n"
                    f"👥 Вільних місць: {pretty['Вільних місць з Усього']}\n"
                )

                send_to_telegram(message, CHAT_ID)
                if str(pretty["ID Компанії"]) == "555":
                    send_to_telegram(message, CHAT_ID_PARTY)

            # save to DB
            db.execute(sqlalchemy.text("""
                INSERT INTO positions_v4 (
                    date,
                    position_id,
                    name,
                    company,
                    company_id,
                    working_hours,
                    location,
                    role_id,
                    profession,
                    free_capacity,
                    total_capacity,
                    wage_hour,
                    wage_fix,
                    scrapped_at
                )
                VALUES (
                    :date, 
                    :position_id, 
                    :name, 
                    :company, 
                    :company_id, 
                    :working_hours, 
                    :location, 
                    :role_id,
                    :profession,
                    :free_capacity,
                    :total_capacity,
                    :wage_hour,
                    :wage_fix,
                    :scrapped_at)
                ON CONFLICT (position_id) DO UPDATE
                SET
                    date = EXCLUDED.date,
                    name = EXCLUDED.name,
                    company = EXCLUDED.company,
                    company_id = EXCLUDED.company_id,
                    working_hours = EXCLUDED.working_hours,
                    location = EXCLUDED.location,
                    role_id = EXCLUDED.role_id,
                    profession = EXCLUDED.profession,
                    free_capacity = EXCLUDED.free_capacity,
                    total_capacity = EXCLUDED.total_capacity,
                    wage_hour = EXCLUDED.wage_hour,
                    wage_fix = EXCLUDED.wage_fix,
                    scrapped_at = EXCLUDED.scrapped_at;
            """), {
                "date": pretty["Дата"],
                "position_id": pretty["ID Позиції"],
                "name": pretty["Назва"],
                "company": pretty["Компанія"],
                "company_id": pretty["ID Компанії"],
                "working_hours": pretty["Час"],
                "location": pretty["Локація"],
                "role_id": pretty["ID Role"],
                "profession": pretty["Професія"],
                "free_capacity": pretty["freeCapacity"],
                "total_capacity": pretty["totalCapacity"],
                "wage_hour": pretty["Оплата (година)"],
                "wage_fix": pretty["Оплата (фікс)"],
                "scrapped_at": datetime.utcnow()
            })
            db.commit()

            # print(json.dumps(pretty, ensure_ascii=False, indent=4))
            # print("-" * 10)
        print("Scraping done at", time.strftime("%Y-%m-%d %H:%M:%S"))


# --- Запуск ---
if __name__ == "__main__":
    # один раз при старті
    scrape()

    # далі кожну хвилину
    schedule.every(1).minutes.do(scrape)

    while True:
        schedule.run_pending()
        time.sleep(1)