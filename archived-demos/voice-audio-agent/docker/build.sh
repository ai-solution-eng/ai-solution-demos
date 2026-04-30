#!/bin/bash
set -e # Exit immediately if a command exits with a non-zero status.

# --- Configuration ---
# IMPORTANT: Change this to your Docker Hub username or registry prefix
DOCKER_USERNAME="mendeza" 

# --- Image Definitions ---
WEBSOCKET_SERVER_IMAGE_NAME="web-socket-server"
WEBSOCKET_SERVER_TAG="0.0.3"

GRADIO_UI_IMAGE_NAME="web-socket-server-ui"
GRADIO_UI_TAG="0.0.3"

# --- Prerequisites Check ---
if ! docker buildx version > /dev/null 2>&1; then
    echo "Error: docker buildx is not available."
    echo "Please ensure you have a recent version of Docker Desktop or have installed the buildx plugin."
    exit 1
fi

# --- Login Check ---
# if ! docker info | grep -q "Username: ${DOCKER_USERNAME}"; then
#     echo "You are not logged in to Docker as '${DOCKER_USERNAME}'."
#     echo "Please run 'docker login' with your credentials."
#     exit 1
# fi

echo "--- Using docker buildx to build for linux/amd64 architecture ---"
echo ""

# --- Build and Push WebSocket Server ---
echo "Building and pushing WebSocket Server image..."
# Change into the correct directory first
cd websocket_server
docker buildx build \
  --platform linux/amd64 \
  --no-cache -t "${DOCKER_USERNAME}/${WEBSOCKET_SERVER_IMAGE_NAME}:${WEBSOCKET_SERVER_TAG}" \
  --push . # <-- CORRECTED: Use '.' for the current directory
# Go back to the root directory
cd ..
echo "WebSocket Server image build and push complete."

echo "" # Newline for readability

# --- Build and Push Gradio UI ---
echo "Building and pushing Gradio UI image..."
# Change into the correct directory first
cd gradio_ui
docker buildx build \
  --platform linux/amd64 \
  --no-cache -t "${DOCKER_USERNAME}/${GRADIO_UI_IMAGE_NAME}:${GRADIO_UI_TAG}" \
  --push . # <-- CORRECTED: Use '.' for the current directory
# Go back to the root directory
cd ..
echo "Gradio UI image build and push complete."

echo ""
echo "--- All images successfully built and pushed for linux/amd64! ---"
echo "Server Image: ${DOCKER_USERNAME}/${WEBSOCKET_SERVER_IMAGE_NAME}:${WEBSOCKET_SERVER_TAG}"
echo "UI Image:     ${DOCKER_USERNAME}/${GRADIO_UI_IMAGE_NAME}:${GRADIO_UI_TAG}"