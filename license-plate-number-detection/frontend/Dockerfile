FROM python:3.11-slim

WORKDIR /app

# opencv-python dependencies
RUN apt-get update && apt-get install -y ffmpeg libsm6 libxext6 vim \
    && rm -rf /var/lib/apt/lists/*

COPY . .

RUN pip install --no-cache-dir --upgrade -r requirements.txt

EXPOSE 8501

CMD ["streamlit", "run", "main.py", "--server.port=8501", "--server.address=0.0.0.0"]