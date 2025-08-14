#!/usr/bin/env bash
set -euo pipefail

# --- Config ---
IMAGE="mendeza/vanna-tulane-demo:0.0.1"
CONTEXT="."                 # change if your Dockerfile/context is elsewhere
PLATFORM="linux/amd64"      # adjust or remove if you want default multi-arch

echo "==> Building image with buildx: ${IMAGE}"

# Build with buildx and LOAD into the local Docker daemon so we can run a check.
# (We push later, after verification.)
docker buildx build \
  --platform "${PLATFORM}" \
  --tag "${IMAGE}" \
  --load \
  "${CONTEXT}"

echo "==> Verifying file exists inside image: /app/purchase_orders.sqlite"

# Run a one-off container to check for the file; --rm ensures cleanup.
docker run --rm --entrypoint sh "${IMAGE}" -c '
  set -e
  if [ -f /app/purchase_orders.sqlite ]; then
    echo "OK: /app/purchase_orders.sqlite found."
  else
    echo "ERROR: /app/purchase_orders.sqlite not found in the image under /app."
    exit 1
  fi
'

# If we reach here, the file exists. Push the image.
echo "==> Pushing image to Docker Hub: ${IMAGE}"
docker push "${IMAGE}"

echo "==> Done. Build, verify, and push completed for ${IMAGE}"