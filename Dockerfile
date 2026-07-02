FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Runtime files (bot.log, positions.log, trades.jsonl) go here
RUN mkdir /data
WORKDIR /data

ENV PYTHONPATH=/app

CMD ["python", "-u", "/app/main.py"]
