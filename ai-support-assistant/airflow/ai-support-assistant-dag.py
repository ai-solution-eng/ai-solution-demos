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
    schedule="*/5 * * * *",
    access_control={
        'All': {'can_read', 'can_edit', 'can_delete'}
    },
    catchup=False,
) as dag:

    @task()
    def process_support_request():
        import requests
        import logging

        logger = logging.getLogger(__name__)
        logger.info('Querying for new support cases to process.')

        try:
            # Query for any untriaged support cases
            hook = PostgresHook(postgres_conn_id='postgres')
            query_results = hook.get_records("SELECT case_id, msg FROM msgs, cases WHERE msgs.case_id = cases.id AND cases.triaged = FALSE AND msgs.msg_type = 'Message from Customer'")
        except Exception as e:
            logger.error(f"Failed to query for new support cases. Exception is: \n {e}")
            return

        if not query_results:
            logger.info('No new support cases to process.')
            return

        try:
            # Set "triaged" to true so overlapping runs don't pick them up. This is not a perfect solution and doesn't truly avoid race
            # conditions, but it is sufficient for this demo.  
            id_list = ""
            for support_request in query_results:
                if id_list:
                    id_list = id_list + ", "
                id_list = id_list + str(support_request[0])

            logger.info("Triaging the following support cases: " + id_list)
            sql = f"UPDATE cases SET triaged = TRUE WHERE id IN ({id_list});"
            hook.run(sql)
        except Exception as e:
            logger.error(f"Failed to update triage flag.  SQL was: \n {sql}")
            logger.error(f"Exception is: \n {e}")
            return

        # Loop through each support request and process them in turn
        for support_request in query_results:

            # Get the case ID and question
            case_id = str(support_request[0])
            question = support_request[1]

            if question:
                try:
                    # Start by assuming that we'll be able to answer the customer's question
                    respond_to_customer = True

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
                        respond_to_customer = False
                        answer = f"I am unable to answer as the model failed with status code: {response.status_code}"
                        logger.error(f"Response code: {response.status_code}")
                        logger.error(f"Response body: {response.text}")
                    else:
                        data = response.json()
                        answer = data["choices"][0]["message"]["content"]

                except Exception as e:
                    respond_to_customer = False
                    answer = f"I am unable to answer as an error occurred in the Airflow DAG: {e}"

            else:
                respond_to_customer = False
                answer = 'I am unable to answer as a question was not provided.'

            logger.info(answer)
            logger.info("Providing answer: " + answer)

            sql = "INSERT INTO msgs (case_id, msg, msg_type) VALUES (%s, %s, %s);"

            if respond_to_customer:
                hook.run(sql, parameters=(case_id, answer, 'Message to Customer'))
            else:
                hook.run(sql, parameters=(case_id, answer, 'Internal Message'))

    # DAG flow
    process_support_request()