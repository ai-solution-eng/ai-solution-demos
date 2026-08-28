FROM python:3.14-slim AS builder

WORKDIR /app

# Build deps (gcc for webrtcvad C extension)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libc6-dev && \
    rm -rf /var/lib/apt/lists/*

# Install all runtime dependencies
COPY docker/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download Qwen tokenizer for token-count chunking
ADD https://huggingface.co/Qwen/Qwen3-VL-Embedding-8B/resolve/main/tokenizer.json /app/tokenizer.json

COPY src/ src/

ENV PYTHONPATH=/app/src

FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Create non-root user (UID/GID 1000)
RUN groupadd -g 1000 app && \
    useradd -u 1000 -g app -m -s /bin/bash app && \
    mkdir -p /data && chown app:app /data

COPY --from=builder /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app /app

# Ensure tokenizer.json (downloaded as root in builder) is world-readable
RUN chmod 644 /app/tokenizer.json

ENV PYTHONPATH=/app/src
ENV PYTHONDONTWRITEBYTECODE=1

USER 1000

EXPOSE 8000

ENTRYPOINT ["python", "-m", "multimodal_rag.api_server"]
CMD ["--host", "0.0.0.0", "--port", "8000"]
