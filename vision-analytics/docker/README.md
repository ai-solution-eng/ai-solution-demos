# Vision Analytics Demo

This application provides a visual analytics studio powered by HPE Private Cloud AI. It supports image understanding, video analysis, and live RTSP stream analysis using Large Language Models (LLMs).

## Features

- **Image Understanding**: Upload and analyze images with natural language prompts.
- **Video Understanding**: Upload videos, extract frames, and analyze them with LLMs.
- **RTSP Stream Analysis**: Connect to live RTSP streams and analyze frames in real-time.

## Prerequisites

- Docker
- Python 3.10+ (for local development)

## Running Locally

1.  **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

2.  **Run the Application** (ffmpeg must be installed):
    ```bash
    python app.py
    ```
    The app will be available at `http://localhost:7860`. For auto-reload while you edit, run `gradio app.py` instead.

3.  **Run the Tests** (inside the app image, which has ffmpeg):
    ```bash
    docker run --rm -v "$PWD":/src -w /src --entrypoint sh erdincka/vision_analytics_demo:latest -c "pip install -q -r requirements-dev.txt && python -m pytest -q tests"
    ```

## Running with Docker

1.  **Build the Image**:
    ```bash
    docker build -t erdincka/vision_analytics_demo:latest .
    ```

2.  **Run the Container**:
    ```bash
    docker run -p 7860:7860 erdincka/vision_analytics_demo:latest
    ```

## Configuration

The application requires an OpenAI-compatible API endpoint for a vision language model (for example vLLM, an NVIDIA NIM or a LiteLLM gateway). You can configure this in the UI under "Vision Model Configuration" or set the following environment variables:

- `OPENAI_API_KEY`: Your API key.
- `OPENAI_API_BASE`: Your API base URL.

## Project Structure

- `app.py`: Main application entry point, UI definition, and business logic.
- `video_sampling.py`: Samples videos with ffmpeg before they are sent to the model.
- `tests/`: Tests for the video sampling (`requirements-dev.txt` has the test dependencies).
- `Dockerfile`: Docker configuration for building the application image.
- `requirements.txt`: Python dependencies.
