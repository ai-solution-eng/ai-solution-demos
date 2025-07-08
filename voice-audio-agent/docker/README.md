<!-- File: docker/README.md -->

# Docker Build & Push

This folder contains everything needed to build and push the two Docker images used by the audio conversational agent.

## Prerequisites

- **Docker >= 20.10** with Buildx enabled
  - Note: Buildx is only for Mac machines that are using ARM CPUs, Amd based machines do not require buildx.
- A Docker registry account (Docker Hub, ECR, GCR, etc.)

## Configuration

1. Open `build.sh`.  
2. Set your registry prefix (e.g. your Docker Hub username) in the `DOCKER_USERNAME` variable.

   ```bash
   DOCKER_USERNAME="yourusername"
   ```
3. (Optional) Uncomment the login check, or simply log in manually:

```bash
docker login
```

## Building & Pushing
From this directory:
```bash
bash build.sh
```

This will:

Build web-socket-server:0.0.1 and push as ${DOCKER_USERNAME}/web-socket-server:0.0.1

Build web-socket-server-ui:0.0.1 and push as ${DOCKER_USERNAME}/web-socket-server-ui:0.0.1

## Running Locally
```bash
# WebSocket server
docker run -d --name web-socket-server -p 8765:8765 \
  -e LLM_API_BASE="https://<your-llm-endpoint>/v1" \
  -e OPENAI_API_KEY="<YOUR_KEY>" \
  -e LLM_MODEL_NAME="meta-llama/Llama-3.2-1B-Instruct" \
  -e ASR_SERVER_ADDRESS="parakeet-asr-api:50051" \
  -e TTS_SERVER_ADDRESS="tts-api:50051" \
  -e TTS_SAMPLE_RATE_HZ="44100" \
  yourusername/web-socket-server:0.0.1

# Gradio UI
docker run -d --name gradio-ui -p 8080:8080 \
  -e WEBSOCKET_URI="ws://host.docker.internal:8765" \
  -e TTS_SAMPLE_RATE_HZ="44100" \
  yourusername/web-socket-server-ui:0.0.1
```