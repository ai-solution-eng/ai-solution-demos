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

# Manually install Python dependencies (no requirements.txt)
RUN pip install --no-cache-dir \
    gradio \
    opencv-python-headless \
    numpy \
    openai \
    pillow

# Copy app code
COPY . /app

# Expose default Gradio port
EXPOSE 7860

# Entry point
CMD ["python", "app.py"]

