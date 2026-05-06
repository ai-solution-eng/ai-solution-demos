#
# Dockerfile for the Medical Summary Streamlit App
#

# Step 1: Lightweight Python base image
FROM python:3.10-slim

# Step 2: Environment setup
ENV PYTHONUNBUFFERED=1
ENV STREAMLIT_SERVER_PORT=8501
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0
ENV HOME=/app

WORKDIR /app

RUN mkdir -p /app/.streamlit && \
    chmod -R a+rwX /app/.streamlit

# Step 3: Install system dependencies + headless JDK
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        nano \
        vim \
        dnsutils \
        openjdk-21-jdk-headless && \
    rm -rf /var/lib/apt/lists/*

# Step 4: Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Step 5: Copy application code & drivers
COPY app.py .
COPY drivers/ ./drivers/

# Step 6: Expose Streamlit port
EXPOSE ${STREAMLIT_SERVER_PORT}

# Step 7: JVM / JDBC paths
ENV JVM_PATH="/usr/lib/jvm/java-21-openjdk-amd64/lib/server/libjvm.so"
ENV JDBC_JAR_PATH="/app/drivers/jdbc-15.0.0.1.1.jar"
ENV MONGO_JAR_PATH="/app/drivers/mongo-java-driver.jar"

# Step 8: Run app
CMD ["streamlit", "run", "app.py", "--server.fileWatcherType=poll"]
