#
# Dockerfile for the Vessel Geometry Analysis Streamlit App
#
# Optimized (v0.0.11):
#   - python:3.10-slim base (Python + pip already baked in)
#   - Only open3d headless-rendering system libs via apt
#   - Debug tools (nano/vim/dnsutils) removed
#   - BuildKit pip cache mount for fast rebuilds
#

FROM python:3.10-slim-bookworm

# --- Environment ---
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV STREAMLIT_SERVER_PORT=8501
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0
ENV HOME=/app
ENV MPLCONFIGDIR=/app/.config/matplotlib

# --- System libs (only what open3d needs for headless rendering) ---
# Note: libgl1-mesa-glx was removed in Debian Bookworm; use libgl1 instead.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libgl1 \
        libgomp1 \
        libglib2.0-0 \
        libx11-6 \
        libxext6 \
        libxrender1 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN mkdir -p /app/.streamlit /app/.config/matplotlib && \
    chmod -R a+rwX /app/.streamlit /app/.config/matplotlib

# --- Python deps (with BuildKit pip cache mount) ---
# First build same speed; subsequent rebuilds drop from ~25 min to ~3-5 min
# because pip wheels are cached across builds.
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && \
    pip install -r requirements.txt

# --- Application code ---
COPY app.py .
COPY analysis_pipeline.py .
COPY utils/ ./utils/

EXPOSE ${STREAMLIT_SERVER_PORT}

CMD ["streamlit", "run", "app.py", "--server.fileWatcherType=poll"]
