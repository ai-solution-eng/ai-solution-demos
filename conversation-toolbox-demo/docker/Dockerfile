FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app

RUN apt-get update && apt-get install -y ffmpeg gcc && rm -rf /var/lib/apt/lists/*

WORKDIR ${APP_HOME}

COPY docker/requirements-lean.txt docker/requirements.txt ./
RUN pip install --no-cache-dir -r requirements-lean.txt

COPY src/speech_to_speech_tools/app.py ./
COPY src/speech_to_speech_tools/workers.py ./

COPY src/speech_to_speech_tools/main_components ./main_components
COPY src/speech_to_speech_tools/utils ./utils
COPY src/speech_to_speech_tools/static ./static

RUN mkdir -p recordings transcripts

ENV PYTHONPATH="/app"

EXPOSE 8000

CMD ["python", "app.py"]
