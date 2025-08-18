FROM python:3.11.9-slim

WORKDIR /app

RUN pip install -U pip

RUN pip install streamlit==1.47.1
RUN pip install torch==2.7.1
RUN pip install torchvision==0.22.1
RUN pip install pillow==11.0.0
RUN pip install mlflow==2.16.0
RUN pip install boto3==1.35.40
RUN pip install pyjwt==2.9.0

COPY app.py /app
COPY app_utils.py /app
COPY custom_unet.py /app
COPY hpe_pri_wht_rev_rgb.png /app
COPY static/ /app/static
COPY .streamlit /app/.streamlit

RUN mkdir -p /root/.aws
RUN mkdir -p /opt/boto3_cred_helper

COPY s3_config_files/config /root/.aws
COPY s3_config_files/boto3_credential_process.py /opt/boto3_cred_helper

CMD ["streamlit", "run", "app.py"]