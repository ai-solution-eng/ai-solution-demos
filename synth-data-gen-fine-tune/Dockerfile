FROM pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtim

WORKDIR /app

COPY requirements.txt .

ENV HF_HOME=/app/hf_cache
ENV TRANSFORMERS_CACHE=/app/hf_cache

RUN pip install --upgrade pip && \
    pip install -r requirements.txt

COPY app/ ./app/

RUN chmod -R 777 /app

EXPOSE 5000

CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:5000", "--timeout", "180", "--access-logfile", "-", "--error-logfile", "-", "--log-level", "info", "app.app:app"]
