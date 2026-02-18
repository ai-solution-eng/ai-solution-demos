# Gradio UI - Voice Agent (v4.1.0) - Natural Conversation
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Step 1: Install gradio and dependencies
RUN pip install --no-cache-dir gradio==4.19.2 websockets httpx requests psycopg2-binary pandas

# Step 2: FORCE downgrade huggingface_hub (must be AFTER gradio)
RUN pip uninstall -y huggingface_hub && \
    pip install --no-cache-dir huggingface_hub==0.20.3

# Verify
RUN python -c "from huggingface_hub import HfFolder; print('HfFolder OK')"

COPY app_ui.py /app/

ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python", "app_ui.py"]