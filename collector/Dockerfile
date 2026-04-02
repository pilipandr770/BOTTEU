# BOTTEU Data Collector
FROM python:3.13-slim

RUN apt-get update && apt-get install -y gcc && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip --root-user-action=ignore && pip install --no-cache-dir -r requirements.txt --root-user-action=ignore

COPY . .

RUN mkdir -p /app/data

ENV SYMBOLS=BTCUSDT
ENV DATA_DIR=/app/data
ENV ROLL_WINDOW=7770
ENV MAX_RECONNECTS=20

CMD ["python", "-u", "collector.py"]

