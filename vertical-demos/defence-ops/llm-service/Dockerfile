FROM --platform=linux/amd64 python:3.12-slim

WORKDIR /app

# Install python dependencies
COPY llm-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy common and llm-service
COPY common/ ./common/
COPY llm-service/main.py llm-service/model_registry.py ./

ENV PYTHONPATH=/app
EXPOSE 8000

# Run dev if ENV=development, otherwise run production
CMD if [ "$ENV" = "development" ]; then \
      uvicorn main:app --host 0.0.0.0 --port 8000 --reload; \
    else \
      uvicorn main:app --host 0.0.0.0 --port 8000; \
    fi

