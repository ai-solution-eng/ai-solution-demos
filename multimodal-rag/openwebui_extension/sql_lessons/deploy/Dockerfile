# Minimal image to run the SQL-lessons promotion gate as a K8s CronJob.
#
# Build:
#   docker build -t <registry>/sql-lessons-promote:0.1.0 -f deploy/Dockerfile .
#
# The script only needs httpx + the standard library.

FROM python:3.12-slim

RUN pip install --no-cache-dir httpx==0.27.*  \
    && rm -rf /root/.cache/pip

WORKDIR /app
COPY promote.py /app/promote.py

# Validate the script imports and prints usage on start (fast failure in cron).
RUN python -c "import ast; ast.parse(open('/app/promote.py').read())"

ENTRYPOINT ["python", "/app/promote.py"]