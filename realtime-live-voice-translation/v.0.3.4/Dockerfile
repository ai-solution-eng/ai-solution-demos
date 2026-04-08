FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps (often needed for torch + certs)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && apt-get install -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*


COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini /app/alembic.ini
COPY alembic /app/alembic
COPY app /app/app
COPY silero-vad /app/silero-vad

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
