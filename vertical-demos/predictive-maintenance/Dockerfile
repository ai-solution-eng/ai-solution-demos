FROM python:3.10-slim

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY *.py .

# Copy model artifacts and fine-tuned model
COPY ./artifacts /app/artifacts
COPY ./model /app/model

# Config will be mounted from ConfigMap, but include a default
COPY config.yaml /app/config.yaml

# Copy data directory 
COPY ./data /app/data
COPY ./assets /app/assets

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import requests; requests.get('http://localhost:8501/_stcore/health')"

CMD ["streamlit", "run", "main.py", "--server.port=8501", "--server.address=0.0.0.0"]