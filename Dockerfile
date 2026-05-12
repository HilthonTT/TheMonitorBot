# syntax=docker/dockerfile:1.6
FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Copy source.
COPY src ./src

# Run as a non-root user. The /data volume is owned by this user so the
# SQLite DB can be written on persistent storage.
RUN useradd --create-home --uid 1000 botuser \
    && mkdir -p /data \
    && chown -R botuser:botuser /app /data
USER botuser

ENV BOT_DB_PATH=/data/bot.sqlite3
VOLUME ["/data"]

WORKDIR /app/src
CMD ["python", "-u", "bot.py"]
