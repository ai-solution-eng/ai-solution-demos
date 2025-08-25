FROM python:3.11.9-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ACCEPT_EULA=Y \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# --- System packages ---
RUN apt-get update -y \
  && apt-get install -y --no-install-recommends \
       curl gcc g++ gnupg unixodbc-dev \
  && curl https://packages.microsoft.com/keys/microsoft.asc | apt-key add - \
  && curl https://packages.microsoft.com/config/debian/10/prod.list > /etc/apt/sources.list.d/mssql-release.list \
  && apt-get update \
  && apt-get install -y --no-install-recommends --allow-unauthenticated \
       msodbcsql17 mssql-tools \
  && rm -rf /var/lib/apt/lists/*

# --- Python deps ---
# Keep requirements separate for better caching. Add fastapi here or keep it in your requirements.txt.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir \
         fastapi \
         uvicorn \
         pyodbc openai chromadb loguru iso-639

# --- App code ---
COPY . /app/

# Pre-fetch models/assets if needed
RUN python /app/onnx_download.py

# Your app uses Flask entrypoint; keep it.
CMD ["python3", "vanna-flask.py"]

