from __future__ import annotations

import datetime
import os

from airflow import DAG
from airflow.decorators import task
from airflow.operators.empty import EmptyOperator
from airflow.operators.python import BranchPythonOperator
from airflow.hooks.postgres_hook import PostgresHook
from airflow.models import Variable

DAG_ID = "ai-support-assistant-dag"

with DAG(
    dag_id=DAG_ID,
    start_date=datetime.datetime(1970, 1, 1),
    schedule="* * * * *",
    catchup=False,
) as dag:

    @task()
    def query_postgres():
        hook = PostgresHook(postgres_conn_id='postgres')
        records = hook.get_records("SELECT case_id, max(msg) AS msg FROM msgs GROUP BY case_id HAVING count(case_id) = 1 LIMIT 1")
        return records if records else None

    def route_on_query_result(ti):
        records = ti.xcom_pull(task_ids='query_postgres')
        if not records:
            return 'end_no_data'
        return 'ask_ai'

    branch = BranchPythonOperator(
        task_id='branch_on_query_result',
        python_callable=route_on_query_result,
    )

    @task()
    def ask_ai(query_results):
        import requests
        import logging

        # Get the "msg" from the first (and only) row
        question = query_results[0][1]

        if not question:
            return('I am unable to answer as a question was not provided.')

        try:
            logger = logging.getLogger(__name__)
            logger.info("Processing question: " + question)

            url = Variable.get("aisa-model-url")

            # url = 'http://host.docker.internal:8888/api/chat/completions'

            try:
                token = Variable.get("aisa-model-authtoken")
            except:
                token = ""

            if token:
                headers = {
                    'Authorization': f'Bearer {token}',
                    'Content-Type': 'application/json'
                }
            else:
                headers = {
                    'Content-Type': 'application/json'
                }

            # headers = {
            #    'Authorization': f'Bearer sk-5e51b3c0acfb472994348108befbe5fd',
            #    'Content-Type': 'application/json'
            # }

            data = {
              "model": "ezua-support-assistant",
              "messages": [
                {
                  "role": "user",
                  "content": question
                }
              ]
            }
            
            response = requests.post(url, headers=headers, json=data)

            if not response.ok:
                answer = f"I am unable to answer as the model failed with status code: {response.status_code}"
            else:
                data = response.json()
                answer = data["choices"][0]["message"]["content"]

        except Exception as e:
            answer = f"I am unable to answer as an error occurred in the Airflow DAG: {e}"

        finally:
            logger.info(answer)
            return answer

    def decide_branch_path(ti):
        result = ti.xcom_pull(task_ids='ask_ai')

        if "unable to answer" in result:
            return 'post_internal_message'
        else:
            return 'post_customer_message'

    branch_on_answer = BranchPythonOperator(
        task_id='branch_on_answer',
        python_callable=decide_branch_path,
    )
 
    @task()
    def post_customer_message(results, msg):
        case_id = str(results[0][0])

        hook = PostgresHook(postgres_conn_id='postgres')
        sql = "INSERT INTO msgs (case_id, msg, msg_type) VALUES (%s, %s, %s);"
        hook.run(sql, parameters=(case_id, msg, 'Message to Customer'))
 
    @task()
    def post_internal_message(results, msg):
        case_id = str(results[0][0])

        hook = PostgresHook(postgres_conn_id='postgres')
        sql = "INSERT INTO msgs (case_id, msg, msg_type) VALUES (%s, %s, %s);"
        hook.run(sql, parameters=(case_id, msg, 'Internal Message'))

    end_no_data = EmptyOperator(task_id='end_no_data')

    # DAG flow
    records = query_postgres()
    branch.set_upstream(records)
    branch >> end_no_data
    answer = ask_ai(records)
    branch >> answer
    branch_on_answer.set_upstream(answer)
    post_customer_message(records, answer) << branch_on_answer
    post_internal_message(records, answer) << branch_on_answer

        