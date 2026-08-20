# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /app


# Install system dependencies for debugging (curl, vim, dnsutils, ping)
# This is done in a single layer to optimize image size.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    vim \
    dnsutils \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

# Copy the Python script into the container
COPY app_ui.py .

# Install required python packages
RUN pip install --no-cache-dir gradio websockets

# Make port 8080 available to the world outside this container
EXPOSE 8080

# Command to run the application
# Gradio's launch() will listen on 0.0.0.0 by default in this environment
CMD ["python", "app_ui.py"]