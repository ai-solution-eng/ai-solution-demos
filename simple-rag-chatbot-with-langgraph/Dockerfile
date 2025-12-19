# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Set the working directory in the container
WORKDIR /app


# Install system dependencies for debugging (curl, vim, dnsutils, ping)
# This is done in a single layer to optimize image size.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl \
    vim \
    build-essential \
    gcc \
    g++ \
    python3-dev \
    dnsutils \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r /app/requirements.txt  

# Copy the Python script into the container
COPY main.py .

EXPOSE 7860
CMD ["python", "main.py"]