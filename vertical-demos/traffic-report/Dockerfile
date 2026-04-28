FROM python:3.9-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglx-mesa0\
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .

# Install Python dependencies with OpenCV headless
RUN pip install --no-cache-dir -r requirements.txt

# Copy only necessary application files
COPY app.py .
COPY detection_service.py .
COPY analysis_service.py .
COPY database_service.py .
COPY config.py .


# Create assets directory if needed
RUN mkdir -p assets

# Copy assets folder
COPY assets/ ./assets/

# Expose port
EXPOSE 8501

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8501/ || exit 1

# Run the application
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]