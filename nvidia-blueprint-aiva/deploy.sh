# /bin/bash

helm upgrade --install aiva . \
  --set global.ngcImagePullSecretName=ngc-docker-reg-secret \
  --set ranking-ms.applicationSpecs.ranking-deployment.containers.ranking-container.env[0].name=NGC_API_KEY \
  --set ranking-ms.applicationSpecs.ranking-deployment.containers.ranking-container.env[0].value=$NGC_API_KEY \
  --set nemollm-inference.applicationSpecs.nemollm-infer-deployment.containers.nemollm-infer-container.env[0].name=NGC_API_KEY \
  --set nemollm-inference.applicationSpecs.nemollm-infer-deployment.containers.nemollm-infer-container.env[0].value=$NGC_API_KEY \
  --set nemollm-embedding.applicationSpecs.embedding-deployment.containers.embedding-container.env[0].name=NGC_API_KEY \
  --set nemollm-embedding.applicationSpecs.embedding-deployment.containers.embedding-container.env[0].value=$NGC_API_KEY --force
