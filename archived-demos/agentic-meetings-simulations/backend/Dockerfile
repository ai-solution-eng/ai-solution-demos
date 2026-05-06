FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copy dependency files
COPY pyproject.toml .
COPY uv.lock .

RUN uv sync --frozen --no-dev

# Copy application code
COPY app/ app/
COPY scripts/ scripts/

EXPOSE 8000

# Default command to start the application
CMD ["sh", "-c", "if [ \"$ENV\" = \"development\" ]; then uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload; else uv run uvicorn app.main:app --host 0.0.0.0 --port 8000; fi"]
