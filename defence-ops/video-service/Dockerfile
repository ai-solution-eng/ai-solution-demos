FROM --platform=linux/amd64 python:3.12-slim

WORKDIR /app

# Install system dependencies for OpenCV
RUN apt-get update && apt-get install -y \
    libglib2.0-0 libsm6 libxext6 libxrender-dev ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY video-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy common and video-service
COPY common/ ./common/
COPY video-service/main.py ./

# Bake assets into the image
COPY assets/videos/ ./assets/videos/

ENV PYTHONPATH=/app
EXPOSE 8000

# Run dev if ENV=development, otherwise run production
CMD if [ "$ENV" = "development" ]; then \
      uvicorn main:app --host 0.0.0.0 --port 8000 --reload; \
    else \
      uvicorn main:app --host 0.0.0.0 --port 8000; \
    fi

