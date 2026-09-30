# WebSocket Server - Maximum Reliability Edition (v5.1.0)
# TTS Fallback Chain: XTTS → Edge-TTS → Text
# Enhanced caching, health monitoring, graceful degradation
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libpq-dev \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
# CRITICAL: edge-tts provides fallback when XTTS fails
RUN pip install --no-cache-dir \
    websockets \
    "httpx[http2]" \
    psycopg2-binary \
    asyncpg \
    edge-tts \
    aiohttp

COPY app.py /app/

ENV PYTHONUNBUFFERED=1

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8766/health || exit 1

# Ports:
# 8765 - WebSocket (main)
# 8766 - Health check
# 8767 - Circuit breaker reset
EXPOSE 8765 8766 8767

CMD ["python", "app.py"]
