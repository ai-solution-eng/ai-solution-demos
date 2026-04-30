# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /app

# Install system dependencies for debugging (curl, vim, dnsutils, ping)
# This is done in a single layer to optimize image size.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl \
    vim \
    dnsutils \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

# Copy the Python script into the container
COPY app.py .

# Install required python packages
# The [extras] for riva-client pulls in dependencies like grpcio-tools
RUN pip install --no-cache-dir websockets openai "nvidia-riva-client"

# Make port 8765 available to the world outside this container
EXPOSE 8765

# Command to run the application
CMD ["python", "-u", "app.py"]