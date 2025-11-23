import time
import schedule
import requests
import sqlalchemy
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
import os
from playwright.sync_api import sync_playwright

LOGIN_URL = "https://shameless.sinch.cz/"
SCRAPE_URL = "https://shameless.sinch.cz/react/position"
EMAIL = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")

# --- SQLAlchemy setup ---
conn_str = os.getenv("DB_URL") or os.getenv("DATABASE_URL")
if not conn_str:
    raise ValueError("No DB_URL or DATABASE_URL found in environment")

engine = sqlalchemy.create_engine(conn_str)
Base = declarative_base()
SessionLocal = sessionmaker(bind=engine)

class Position(Base):
    __tablename__ = "positions"
    #_schema = os.getenv("DB_SCHEMA")
    #__table_args__ = {"schema": _schema} if _schema else {}

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

def scrape_page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # --- Login ---
        page.goto(LOGIN_URL)
        page.fill("#UserEmail", EMAIL)
        page.fill("#UserPassword", PASSWORD)
        page.click("input[data-cy='sign-in-btn']")
        page.wait_for_timeout(5000)
        print("Logged in successfully!")

        # --- Scrape ---
        page.goto(SCRAPE_URL)
        page.wait_for_timeout(3000)

        soup = BeautifulSoup(page.content(), "html.parser")
        table = soup.find("table", class_="MuiTable-root")
        if not table:
            print("No table found.")
            browser.close()
            return

        rows = table.find("tbody").find_all("tr", class_="MuiTableRow-root")
        with SessionLocal() as db:
            for row in rows:
                cells = row.find_all("td")
                if not cells:
                    continue

                # універсальний підхід: завжди 6 значень
                values = [c.get_text(strip=True) for c in cells]
                while len(values) < 6:
                    values.append("")  # доповнюємо пустими

                pozice, datum, cas, misto, profese, obsazenost = values[:6]

                # пропускаємо вакансії без дати
                if not datum:
                    continue

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
                    #print(f"New record added: {pozice} | {datum} | {cas}")

                    message = (f"📢 Нова вакансія!\n"
                               f"Позицiя: {pozice}\n"
                               f"Дата: {datum}\n"
                               f"Час: {cas}\n"
                               f"Мiсце: {misto}\n"
                               f"Професiя: {profese}\n"
                               f"Обiйнято: {obsazenost}")
                    send_to_telegram(message)

        browser.close()
        send_to_telegram("Web Scraping done at", time.strftime("%Y-%m-%d %H:%M:%S"))
        print("Scraping done at", time.strftime("%Y-%m-%d %H:%M:%S"))


# --- Main ---
scrape_page()
schedule.every(5).minutes.do(scrape_page)

while True:
    schedule.run_pending()
    time.sleep(1)
