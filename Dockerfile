# Використовуємо офіційний Python образ
FROM python:3.11-slim

# Встановлюємо системні залежності для Playwright
RUN apt-get update && apt-get install -y \
    wget \
    curl \
    unzip \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Встановлюємо Python залежності
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Встановлюємо Playwright Chromium
RUN playwright install chromium

# Копіюємо код
COPY . /app
WORKDIR /app

# Запускаємо скрипт
CMD ["python", "scraper.py"]
