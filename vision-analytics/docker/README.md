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

2.  **Run the Application**:
    ```bash
    uvicorn app:app --host 0.0.0.0 --port 7860 --reload
    ```
    The app will be available at `http://localhost:7860`.

## Running with Docker

1.  **Build the Image**:
    ```bash
    docker build -t vision-analytics-demo .
    ```

2.  **Run the Container**:
    ```bash
    docker run -p 7860:7860 vision-analytics-demo
    ```

## Configuration

The application requires an OpenAI-compatible API endpoint (e.g., vLLM served model). You can configure this in the UI under "Vision Model Configuration" or set the following environment variables:

- `OPENAI_API_KEY`: Your API key.
- `OPENAI_API_BASE`: Your API base URL.

## Project Structure

- `app.py`: Main application entry point, UI definition, and business logic.
- `Dockerfile`: Docker configuration for building the application image.
- `requirements.txt`: Python dependencies.
