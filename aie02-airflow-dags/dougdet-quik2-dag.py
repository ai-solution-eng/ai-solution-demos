# Airflow
import boto3
import getpass
import logging
import os
import shutil
from datetime import datetime, timedelta
from os import path
from airflow import DAG
from airflow.decorators import task
from airflow.models.param import Param
from airflow.utils.dates import days_ago
from airflow.operators.python import get_current_context
# from airflow.providers.amazon.aws.hooks.s3 import S3Hook
from airflow.providers.weaviate.hooks.weaviate import WeaviateHook


default_args = {
    'owner': 'doug',
    'depends_on_past': False,
    'start_date': days_ago(1),
    'email': ['doug.parrish@hpe.com'],
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 0
}
today = datetime.today().strftime('%Y-%m-%d')
with DAG(
    'dougdet-quik2',
    default_args=default_args,
    schedule_interval=None,
    tags=['test', 'dougdet'],
    params={
        'dnld_dir': Param("/mnt/shared/dougdet/avdocs/", type="string"),
        's3_url': Param("http://ce-awsbuckets-service.ezdata-system.svc.cluster.local:30000", type="string"),
        's3_bucket': Param("ce-dougdet-pcaiexer", type="string"),
        's3_prefix': Param(f"documents/", type="string"),
        'weave_conn_id': Param("ce-dougdet-weaviate", type="string")
    },
    access_control={
        'All': {
            'can_read',
            'can_edit',
            'can_delete'
        }
    }
) as dag:

    @task
    def cleanup_export_dir():
        context = get_current_context()
        dnld_dir = context['params']['dnld_dir']
        if path.exists(dnld_dir):
            shutil.rmtree(dnld_dir)
            return f"Deleted directory {dnld_path}"
        return f"{dnld_dir} doesn't exist so nothing to delete"

    @task
    def get_all_filepaths_from_s3_path():

        context = get_current_context()
        s3_url = context['params']['s3_url']
        s3_bucket = context['params']['s3_bucket']
        s3_prefix = context['params']['s3_prefix']

        s3client = boto3.client('s3', endpoint_url=s3_url)
        paginator = s3client.get_paginator('list_objects_v2')
        page_iterator = paginator.paginate(Bucket=s3_bucket)

        filepaths = []  # empty list
        for page in page_iterator:
            if 'Contents' not in page:
                continue
            for obj in page['Contents']:
                s3okey = obj['Key']
                if s3okey[-1] == '/':  # ignore subdirectories
                    continue
                filepaths.append(s3okey)

        return filepaths

    @task
    def download_s3_file_to_shared_volume(s3path):
        logger = logging.getLogger(__name__)
        context = get_current_context()
        #
        # S3 identifiers
        s3_url = context['params']['s3_url']
        s3_bucket = context['params']['s3_bucket']
        s3_prefix = context['params']['s3_prefix']
        #
        # download dest identifiers
        dnld_dir = context['params']['dnld_dir']
        #
        # Make sure all subdirectories have been created before download
        s3_full_pfx = path.dirname(s3path)
        s3_sub_pfx = s3_full_pfx[(s3_full_pfx.find(s3_prefix)+len(s3_prefix)):]
        os.makedirs(dnld_dir, mode=0o775, exist_ok=True)
        os.umask(0o002)
        #
        # DOWNLOAD
        s3client = boto3.client('s3', endpoint_url=s3_url)
        file = s3path.rsplit('/', 1)[-1]
        file_path = dnld_dir + file
        logger.info(f"Downloading {s3path} to {file_path}")
        s3client.download_file(bucket, s3path, file_path)
        os.chmod(file_path, 0o664)
        return file_path

    cleanup_export_dir() >> download_s3_file_to_shared_volume.expand(s3path=get_all_filepaths_from_s3_path())
