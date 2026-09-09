# BERU — container image.
#
# The container safely binds 0.0.0.0: the application REFUSES to start on a
# non-localhost interface without BERU_API_KEY set (see backend/main.py), so
# production images MUST provide a key via environment. See docs/deployment.md.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System libraries needed by installed desktop-adjacent packages at import time.
# Browser/input/screenshot engines degrade to "unavailable" when the OS lacks a
# display, so this stays minimal.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libgomp1 \
    libx11-6 \
    libxext6 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY backend ./backend
COPY migrations ./migrations
COPY alembic.ini ./
COPY frontend ./frontend
COPY .env.example ./

EXPOSE 8000

# Graceful shutdown: uvicorn relays SIGTERM through the lifespan teardown
# (ledger flush, LLM client close, engine dispose, lock release) and the
# timeout lets in-flight requests finish before the container is killed.
CMD ["uvicorn", "backend.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--timeout-graceful-shutdown", "30"]