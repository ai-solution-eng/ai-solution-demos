from __future__ import annotations

import datetime
import os

from airflow import DAG
from airflow.decorators import task
from airflow.operators.empty import EmptyOperator
from airflow.operators.python import BranchPythonOperator
from airflow.hooks.postgres_hook import PostgresHook



DAG_ID = "ai-support-assistant-dag"

with DAG(
    dag_id=DAG_ID,
    start_date=datetime.datetime(1970, 1, 1),
    params={
         "question": "Why is the sky blue?"
     },
    schedule=None,
    catchup=False,
) as dag:

    @task()
    def query_postgres():
        hook = PostgresHook(postgres_conn_id='postgres')
        records = hook.get_records("SELECT case_id, max(msg) AS msg FROM msgs GROUP BY case_id HAVING count(case_id) = 1 LIMIT 1")
        if not records:
            return None
        return records


    @task()
    def ask_ai(query_results):
        import requests
        import logging

        # Get the "msg" from the first (and only) row
        question = query_results[0][1]

        logger = logging.getLogger(__name__)
        logger.info("This is a log message")

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
              "content": question
            }
          ]
        }

        logger.info(question)

        response = requests.post(url, headers=headers, json=data)
        data = response.json()
        answer = data["choices"][0]["message"]["content"]

        logger.info(answer)

        # print("\n\n" + answer)


    # DAG flow
    results = query_postgres()
    ask_ai(results)
  