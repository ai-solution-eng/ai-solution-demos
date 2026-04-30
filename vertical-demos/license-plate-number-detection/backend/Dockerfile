FROM python:3.11-slim

WORKDIR /app

# opencv-python dependencies
RUN apt-get update && apt-get install -y ffmpeg libsm6 libxext6 vim \
    build-essential cmake git python3-dev python3-numpy \
    && rm -rf /var/lib/apt/lists/* 

RUN apt-get update && apt-get install -y libavcodec-dev libavformat-dev libswscale-dev \
    libgstreamer-plugins-base1.0-dev libgstreamer1.0-dev libgtk-3-dev \
    libpng-dev libjpeg-dev libopenexr-dev libtiff-dev libwebp-dev libopencv-dev x264 libx264-dev libssl-dev ffmpeg \
    && rm -rf /var/lib/apt/lists/* 

RUN python -m pip install --no-binary opencv-python opencv-python

COPY . .

RUN pip install --no-cache-dir --upgrade -r requirements.txt

# patch triton util. ( read token value from config.yaml and set the header )
COPY ./triton_token.py /usr/local/lib/python3.11/site-packages/ultralytics/utils/triton.py


ENV EASYOCR_MODULE_PATH="/app/.EasyOCR"

EXPOSE 8000

CMD ["fastapi", "run", "main.py", "--port", "8000"]