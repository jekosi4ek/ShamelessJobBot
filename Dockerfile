FROM python:3.13-slim

RUN apt-get update && apt-get install -y wget gnupg libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpangocairo-1.0-0 libpango-1.0-0 libgtk-3-0 libx11-xcb1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install --with-deps

COPY . .
CMD ["python", "scrapper.py"]
