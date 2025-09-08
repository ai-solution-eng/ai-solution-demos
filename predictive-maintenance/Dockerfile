# Use official Python image
FROM python:3.10-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Set work directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install dependencies
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    rm -rf /root/.cache/pip

# Copy the rest of the app
COPY . .

# Expose Streamlit default port
EXPOSE 8501

# Streamlit entrypoint
CMD ["streamlit", "run", "main.py", "--server.port=8501", "--server.address=0.0.0.0"]
