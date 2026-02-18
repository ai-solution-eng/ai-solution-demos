# XTTS v2 Server Dockerfile - Optimized
# Voice Agent v4.1.0 - Slim image with runtime model download
# Image size: ~3GB (down from ~10GB)
# 
# USAGE:
# Build: docker build -t xtts-server:slim .
# Run:   docker run -d --gpus all -v xtts-cache:/model-cache -p 8000:8000 xtts-server:slim
#
# PVC MOUNT: Mount a PVC to /model-cache for persistent model storage
# First run will download the model (~2GB), subsequent runs use cache

FROM pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime

WORKDIR /app

# Prevent timezone prompt
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Install system dependencies (minimal)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    tzdata \
    ffmpeg \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Uninstall existing numpy to avoid conflicts
RUN pip uninstall -y numpy || true

# Install TTS and dependencies (but don't download model yet)
RUN pip install --no-cache-dir TTS==0.22.0

# Downgrade transformers for torch 2.1.0 compatibility
RUN pip install --no-cache-dir transformers==4.36.0

# Install server dependencies
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    python-multipart

# Clean pip cache to reduce image size
RUN pip cache purge || true

# Create directories
RUN mkdir -p /app/speakers /model-cache

# Copy server script
COPY xtts_server.py /app/xtts_server.py

# Environment variables
ENV PORT=8000
ENV DEFAULT_LANGUAGE=en
ENV DEFAULT_SPEAKER="Ana Florence"
ENV SPEAKERS_DIR=/app/speakers

# MODEL CACHE: Mount a PVC to /model-cache for persistent storage
# The model will be downloaded on first run and cached
ENV TTS_HOME=/model-cache
ENV XDG_CACHE_HOME=/model-cache

# IMPORTANT: Set to false - model downloads at runtime, not build time
# This reduces image size from ~10GB to ~3GB
ENV PRELOAD_MODEL=false

# GPU configuration
ENV CUDA_VISIBLE_DEVICES=0
ENV COQUI_TOS_AGREED=1

# Reduce memory fragmentation for GPU
ENV PYTORCH_CUDA_ALLOC_CONF=max_split_size_mb:512

# Expose port
EXPOSE 8000

# Health check with longer start period for model download
HEALTHCHECK --interval=30s --timeout=30s --start-period=300s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Run server
CMD ["python", "/app/xtts_server.py"]