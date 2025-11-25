import os
import json
import time
import schedule
import requests
import sqlalchemy
from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# --- Config ---
LOGIN_URL = "https://shameless.sinch.cz/"
API_URL = "https://shameless.sinch.cz/api"
EMAIL = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("DB_URL")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
COOKIES_FILE = "cookies.json"

if not all([EMAIL, PASSWORD, DATABASE_URL, BOT_TOKEN, CHAT_ID]):
    raise ValueError("Missing required environment variables: EMAIL, PASSWORD, DATABASE_URL, BOT_TOKEN, CHAT_ID")

# --- DB setup (PostgreSQL via SQLAlchemy) ---
engine = sqlalchemy.create_engine(DATABASE_URL, pool_pre_ping=True)
Base = declarative_base()
SessionLocal = sessionmaker(bind=engine)

class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    datum = Column(String(50))          # "Дата"
    position_id = Column(Integer)       # "ID Позиції"
    nazva = Column(String(255))         # "Назва"
    company = Column(String(255))       # "Компанія"
    company_id = Column(Integer)        # "ID Компанії"
    cas = Column(String(100))           # "Час"
    location = Column(String(255))      # "Локація"
    profese = Column(String(255))       # "Професія"
    capacity = Column(String(50))       # "Вільних місць з Усього"
    scrapped_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(engine)

# --- Telegram ---
def send_to_telegram(message: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}
    try:
        requests.post(url, data=payload, timeout=15)
        print("Sent to Telegram")
    except Exception as e:
        print("Telegram error:", e)

# --- Cookies cache ---
cookies_cache = None

def save_cookies(cookies: dict):
    try:
        with open(COOKIES_FILE, "w") as f:
            json.dump(cookies, f)
    except Exception as e:
        print("Save cookies error:", e)

def load_cookies() -> dict | None:
    if os.path.exists(COOKIES_FILE):
        try:
            with open(COOKIES_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print("Load cookies error:", e)
    return None

def login_and_get_cookies():
    global cookies_cache
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")  # set visible if you need to debug
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(options=options)

    driver.get(LOGIN_URL)
    wait = WebDriverWait(driver, 20)

    email_input = wait.until(EC.presence_of_element_located((By.ID, "UserEmail")))
    email_input.send_keys(EMAIL)

    password_input = driver.find_element(By.ID, "UserPassword")
    password_input.send_keys(PASSWORD)

    login_button = driver.find_element(By.XPATH, "//input[@data-cy='sign-in-btn']")
    login_button.click()

    # wait for redirect and session to be established
    time.sleep(5)

    cookies_list = driver.get_cookies()
    driver.quit()
    cookies_cache = {c['name']: c['value'] for c in cookies_list}
    save_cookies(cookies_cache)
    print("Cookies refreshed")

def api_post(payload: dict) -> requests.Response:
    global cookies_cache
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "https://shameless.sinch.cz",
        "Referer": "https://shameless.sinch.cz/react/position",
        "User-Agent": "Mozilla/5.0"
    }
    if cookies_cache is None:
        cookies_cache = load_cookies()
    if cookies_cache is None:
        login_and_get_cookies()

    resp = requests.post(API_URL, json=payload, headers=headers, cookies=cookies_cache, timeout=30)

    if resp.status_code == 401:
        print("Cookies expired, refreshing...")
        login_and_get_cookies()
        resp = requests.post(API_URL, json=payload, headers=headers, cookies=cookies_cache, timeout=30)

    return resp

def format_time_range(start: str | None, end: str | None) -> tuple[str, str, float | None]:
    """
    Returns (date_str, time_range_str, hours_diff)
    date_str: "dd.mm.yyyy"
    time_range_str: "HH:MM - HH:MM (x.x h)" or "HH:MM - dd.mm.yyyy HH:MM (x.x h)" if next day
    """
    if not start or not end:
        return "", "", None

    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    sd_fmt = start_dt.strftime("%d.%m.%Y")
    start_fmt = start_dt.strftime("%H:%M")

    if end_dt.strftime("%d.%m.%Y") != sd_fmt:
        end_fmt = end_dt.strftime("%d.%m.%Y %H:%M")
    else:
        end_fmt = end_dt.strftime("%H:%M")

    hours_diff = (end_dt - start_dt).total_seconds() / 3600
    time_range = f"{start_fmt} - {end_fmt} ({hours_diff:.1f} h)"
    return sd_fmt, time_range, hours_diff

def scrape_api():
    # fetch index
    payload_index = {
        "key": "worker/Positions/Index",
        "meta": {"page": 1, "limit": 50},
        "params": {"attend": True}
    }
    resp = api_post(payload_index)
    data = resp.json()
    positions = data.get("entities", {}).get("Position", {})
    if not positions:
        print("No positions found")
        return

    with SessionLocal() as db:
        for pos_id, _ in positions.items():
            # fetch view/details
            payload_view = {
                "key": f"worker/Positions/View/{pos_id}",
                "meta": {},
                "params": {"id": pos_id}
            }
            detail_resp = api_post(payload_view)
            detail = detail_resp.json()

            entity = detail.get("entities", {}).get("Position", {}).get(str(pos_id), {})
            shift = detail.get("entities", {}).get("Shift", {}).get(str(entity.get("shift")), {})
            company = detail.get("entities", {}).get("Company", {}).get(str(shift.get("company")), {})
            location = detail.get("entities", {}).get("Location", {}).get(str(entity.get("location")), {})
            profession = detail.get("entities", {}).get("Profession", {}).get(str(entity.get("profession")), {})

            start = entity.get("startTime")
            end = entity.get("endTime")

            sd_fmt, time_range, hours_diff = format_time_range(start, end)

            pretty = {
                "Дата": sd_fmt,
                "ID Позиції": int(pos_id),
                "Назва": shift.get("name", ""),
                "Компанія": company.get("name"),
                "ID Компанії": company.get("id"),
                "Час": time_range,
                "Локація": location.get("address"),
                "Професія": profession.get("name"),
                "Вільних місць з Усього": f"{entity.get('freeCapacity')}/{entity.get('totalCapacity')}",
                "scrapped_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            }

            # upsert-like logic by unique tuple of values
            existing = db.query(Position).filter_by(
                datum=pretty["Дата"],
                position_id=pretty["ID Позиції"],
                nazva=pretty["Назва"],
                company=pretty["Компанія"],
                company_id=pretty["ID Компанії"],
                cas=pretty["Час"],
                location=pretty["Локація"],
                profese=pretty["Професія"],
                capacity=pretty["Вільних місць з Усього"],
            ).first()

            if not existing:
                new_pos = Position(
                    datum=pretty["Дата"],
                    position_id=pretty["ID Позиції"],
                    nazva=pretty["Назва"],
                    company=pretty["Компанія"],
                    company_id=pretty["ID Компанії"],
                    cas=pretty["Час"],
                    location=pretty["Локація"],
                    profese=pretty["Професія"],
                    capacity=pretty["Вільних місць з Усього"],
                    scrapped_at=datetime.utcnow()
                )
                db.add(new_pos)
                db.commit()

                # Telegram publication
                message = (
                    "📢 Нова вакансія!\n"
                    f"Дата: {pretty['Дата']}\n"
                    f"ID Позиції: {pretty['ID Позиції']}\n"
                    f"Назва: {pretty['Назва']}\n"
                    f"Компанія: {pretty['Компанія']} (ID {pretty['ID Компанії']})\n"
                    f"Час: {pretty['Час']}\n"
                    f"Локація: {pretty['Локація']}\n"
                    f"Професія: {pretty['Професія']}\n"
                    f"Вільних місць: {pretty['Вільних місць з Усього']}\n"
                    f"⏱ Scrapped at: {pretty['scrapped_at']}"
                )
                send_to_telegram(message)

    print("Scraping done at", time.strftime("%Y-%m-%d %H:%M:%S"))

def main():
    # initial run
    scrape_api()
    # schedule
    schedule.every(5).minutes.do(scrape_api)
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
