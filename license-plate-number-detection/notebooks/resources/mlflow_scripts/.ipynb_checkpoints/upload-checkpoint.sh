#!/bin/bash

export MLFLOW_TRACKING_URI='https://mlflow.ingress.pcai0103.sy6.hpecolo.net'
export MLFLOW_TRACKING_TOKEN=$(cat /etc/secrets/ezua/.auth_token)
export MLFLOW_S3_ENDPOINT_URL='http://local-s3-service.ezdata-system.svc.cluster.local:30000'
python mlflow_scripts/publish_model_to_mlflow.py --model_name license-detector --model_directory ./triton_engines --flavor triton

