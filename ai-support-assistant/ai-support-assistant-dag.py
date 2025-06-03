from __future__ import annotations

import datetime
import os
from airflow import DAG

DAG_ID = "ai-support-assistant-dag"

with DAG(
    dag_id=DAG_ID,
    start_date=datetime.datetime(1970, 1, 1),
    schedule="@once",
    catchup=False,
) as dag:

    @task
    def ask_ai():
        import requests

        url = 'http://host.docker.internal:8888/api/chat/completions'
        headers = {
            'Authorization': f'Bearer sk-5e51b3c0acfb472994348108befbe5fd',
            'Content-Type': 'application/json'
        }
        data = {
          "model": "ezua-support-assistant",
          "messages": [
            {
              "role": "user",
              "content": "Why is the sky blue?"
            }
          ]
        }
        response = requests.post(url, headers=headers, json=data)
        data = response.json()
        answer = data["choices"][0]["message"]["content"]

        print("\n\n" + answer)
    ask_ai()
