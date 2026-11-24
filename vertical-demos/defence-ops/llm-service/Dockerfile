FROM --platform=linux/amd64 python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv

RUN groupadd -g 10001 app && useradd -u 10001 -g app -M -d /app app
WORKDIR /app

# Dependencies exactly as locked, in a virtualenv outside the app folder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
COPY llm-service/pyproject.toml llm-service/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=10001:10001 common/ ./common/
COPY --chown=10001:10001 llm-service/main.py llm-service/settings.py llm-service/litellm_client.py llm-service/media.py llm-service/prompts.py ./

# The app user owns the code so that Tilt's live update can replace it
RUN chown app:app /app
USER 10001

EXPOSE 8000

# Run dev if ENV=development, otherwise run production
CMD if [ "$ENV" = "development" ]; then \
      uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --reload; \
    else \
      uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}; \
    fi
