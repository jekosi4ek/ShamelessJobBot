import time
import schedule
import requests
import sqlalchemy
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
import os

LOGIN_URL = "https://shameless.sinch.cz/"
SCRAPE_URL = "https://shameless.sinch.cz/react/position"
EMAIL = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")

# --- SQLAlchemy setup ---
# Connection parameters

# connection
conn_str = os.getenv("DB_URL") or os.getenv("DATABASE_URL")
if not conn_str:
    raise ValueError("No DB_URL or DATABASE_URL found in environment")
engine = sqlalchemy.create_engine(conn_str)


engine = sqlalchemy.create_engine(conn_str)
Base = declarative_base()
SessionLocal = sessionmaker(bind=engine)

class Position(Base):
    __tablename__ = os.getenv("DB_TABLE", "positions")
    schema = os.getenv("DB_SCHEMA")
    __table_args__ = {"schema": schema} if schema else {}

    id = Column(Integer, primary_key=True, autoincrement=True)
    pozice = Column(String(255))
    datum = Column(String(50))
    cas = Column(String(50))
    misto = Column(String(255))
    profese = Column(String(255))
    obsazenost = Column(String(50))
    scrape_time = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(engine)

# --- Telegram bot config ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

def send_to_telegram(message: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}
    try:
        requests.post(url, data=payload)
        print("Sent to Telegram:", message)
    except Exception as e:
        print("Telegram error:", e)

def login_with_credentials():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(options=options)

    driver.get(LOGIN_URL)
    wait = WebDriverWait(driver, 10)

    email_input = wait.until(EC.presence_of_element_located((By.ID, "UserEmail")))
    email_input.send_keys(EMAIL)

    password_input = driver.find_element(By.ID, "UserPassword")
    password_input.send_keys(PASSWORD)

    login_button = driver.find_element(By.XPATH, "//input[@data-cy='sign-in-btn']")
    login_button.click()

    time.sleep(5)
    print("Logged in successfully!")
    return driver

def scrape_page(driver):
    driver.get(SCRAPE_URL)
    time.sleep(3)

    soup = BeautifulSoup(driver.page_source, "html.parser")
    table = soup.find("table", class_="MuiTable-root")
    if not table:
        print("No table found.")
        return

    rows = table.find("tbody").find_all("tr", class_="MuiTableRow-root")
    with SessionLocal() as db:

    for row in rows:
        cells = row.find_all("td")
        if not cells:
            continue

        pozice = cells[0].get_text(strip=True) if len(cells) > 0 else ""
        datum = cells[1].get_text(strip=True) if len(cells) > 1 else ""
        cas = cells[2].get_text(strip=True) if len(cells) > 2 else ""
        misto = cells[3].get_text(strip=True) if len(cells) > 3 else ""
        profese = cells[4].get_text(strip=True) if len(cells) > 4 else ""
        obsazenost = cells[5].get_text(strip=True) if len(cells) > 5 else ""

        # --- Check if record exists ---
        existing = db.query(Position).filter_by(
            pozice=pozice, datum=datum, cas=cas,
            misto=misto, profese=profese, obsazenost=obsazenost
        ).first()

        if not existing:
            new_pos = Position(
                pozice=pozice, datum=datum, cas=cas,
                misto=misto, profese=profese, obsazenost=obsazenost
            )
            db.add(new_pos)
            db.commit()
            print(f"New record added: {pozice} | {datum} | {cas}")

            # --- Send to Telegram ---
            message = (f"📢 Нова вакансія!\n"
                       f"Позицiя: {pozice}\n"
                       f"Дата: {datum}\n"
                       f"Час: {cas}\n"
                       f"Мiсце: {misto}\n"
                       f"Професiя: {profese}\n"
                       f"Обiйнято: {obsazenost}")
            send_to_telegram(message)

    db.close()
    print("Scraping done at", time.strftime("%Y-%m-%d %H:%M:%S"))

# --- Main ---
driver = login_with_credentials()
scrape_page(driver)

schedule.every(5).minutes.do(scrape_page, driver)

while True:
    schedule.run_pending()
    time.sleep(1)
