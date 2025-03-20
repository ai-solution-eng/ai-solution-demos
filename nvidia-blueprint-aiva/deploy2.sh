# /bin/bash

helm upgrade --install aiva . \
  --set global.ngcImagePullSecretName=ngc-docker-reg-secret \
  --set nvcf.dockerRegSecrets[0].name=ngc-docker-reg-secret \
  --set nvcf.dockerRegSecrets[0].username='$oauthtoken' \
  --set nvcf.dockerRegSecrets[0].password="$NGC_API_KEY" \
  --set nvcf.additionalSecrets[0].name=ngc-secret \
  --set nvcf.additionalSecrets[0].stringData.key=ngc-api-key \
  --set nvcf.additionalSecrets[0].stringData.value="$NGC_API_KEY" \
  --set ranking-ms.applicationSpecs.ranking-deployment.containers.ranking-container.env[0].name=NGC_API_KEY \
  --set ranking-ms.applicationSpecs.ranking-deployment.containers.ranking-container.env[0].value="$NGC_API_KEY" \
  --set nemollm-inference.applicationSpecs.nemollm-infer-deployment.containers.nemollm-infer-container.env[0].name=NGC_API_KEY \
  --set nemollm-inference.applicationSpecs.nemollm-infer-deployment.containers.nemollm-infer-container.env[0].value="$NGC_API_KEY" \
  --set nemollm-embedding.applicationSpecs.embedding-deployment.containers.embedding-container.env[0].name=NGC_API_KEY \
  --set nemollm-embedding.applicationSpecs.embedding-deployment.containers.embedding-container.env[0].value="$NGC_API_KEY"