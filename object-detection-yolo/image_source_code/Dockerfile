FROM ultralytics/ultralytics:8.4.60

RUN pip install --no-cache-dir streamlit

WORKDIR /app

COPY app.py .
COPY default_samples/ default_samples/

RUN mkdir -p uploads

EXPOSE 8501

CMD ["streamlit", "run", "app.py", "--server.port", "8501", "--server.headless", "true", "--server.maxUploadSize", "200"]
