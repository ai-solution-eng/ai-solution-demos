#
# Dockerfile for the Vessel Geometry Analysis Streamlit App
#

# Step 1: Use a lightweight Ubuntu base image
# We use Ubuntu 22.04 LTS as a stable, well-supported base.
FROM ubuntu:22.04

# Step 2: Set environment variables
# Prevents apt-get from asking interactive questions during the build.
ENV DEBIAN_FRONTEND=noninteractive
# Set Python to be unbuffered, which is good for logging in containers.
ENV PYTHONUNBUFFERED=1
# Set the configurable Streamlit port and host.
# Streamlit will automatically pick up these environment variables.
ENV STREAMLIT_SERVER_PORT=8501
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0
# Set HOME to a writable directory so Streamlit and others write configs there
ENV HOME=/app
# Set Matplotlib config directory to avoid permission errors
ENV MPLCONFIGDIR=/app/.config/matplotlib

# Step 3: Install system dependencies
# - python3.10 and pip for running the application.
# - The requested utilities: nano, vim, and dnsutils for debugging.
# - We clean up the apt cache in the same layer to reduce image size.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    python3.10 \
    python3-pip \
    nano \
    vim \
    dnsutils \
    # system dependencies for Open3D (headless rendering)
    libgl1-mesa-glx \
    libgomp1 \ 
    libglib2.0-0 \
    libx11-6 \
    libxext6 \
    libxrender1 && \
    rm -rf /var/lib/apt/lists/*

# Step 4: Set up the working directory
# All subsequent commands will run from this directory.
WORKDIR /app

# Step 5: Create config directories for Streamlit and Matplotlib
RUN mkdir -p /app/.streamlit /app/.config/matplotlib && \
    chmod -R a+rwX /app/.streamlit /app/.config/matplotlib

# Step 6: Install Python dependencies
# First, copy only the requirements file and install dependencies.
# This leverages Docker's layer caching. This layer will only be rebuilt
# if requirements.txt changes, making subsequent builds much faster.
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Step 6: Copy the application code into the container
# Copy all necessary Python files and the utils directory.
COPY app.py .
COPY analysis_pipeline.py .
COPY utils/ ./utils/

# Step 7: Expose the port the app runs on
# This informs Docker that the container listens on the specified network port.
EXPOSE ${STREAMLIT_SERVER_PORT}

RUN mkdir -p /app/.streamlit

# Step 8: Define the command to run the application
# This command is executed when the container starts.
# It uses the exec form for better signal handling.
CMD ["streamlit", "run", "app.py","--server.fileWatcherType=poll"]