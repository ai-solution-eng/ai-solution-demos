# syntax=docker/dockerfile:1
FROM python:3.10-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    nano \
    ffmpeg \
    libsm6 \
    libxext6 \
    libgl1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . /app

# Expose default Gradio port
EXPOSE 7860

# Set Gradio environment variables
ENV GRADIO_SERVER_NAME="0.0.0.0"
ENV GRADIO_SERVER_PORT="7860"
ENV GRADIO_ANALYTICS_ENABLED="False"
ENV HF_HUB_DISABLE_TELEMETRY="1"

# Entry point with Gradio hot reload
CMD ["gradio", "app.py"]

