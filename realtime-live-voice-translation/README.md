# Real-Time Voice Translation

Live voice translation for HPE Private Cloud AI environments.

This application captures microphone audio in the browser, detects speech with Silero VAD, transcribes it with a Whisper-compatible ASR endpoint, translates it with an OpenAI-compatible LLM endpoint, and streams the results back to the UI in real time.

It also supports post-conversation exports such as transcripts, multilingual summaries, meeting minutes, and a packaged download when recording is enabled.

## What The App Does

1. Captures microphone audio in the browser.
2. Streams audio frames to the backend over WebSocket.
3. Segments speech with Silero VAD.
4. Sends speech segments to a Whisper-compatible ASR model.
5. Sends the transcript to an LLM for translation.
6. Displays the latest translated turn in the UI while keeping earlier turns in history.
7. Optionally generates meeting documents and export packages after the conversation.

## Key Capabilities

- Real-time transcription and translation over WebSocket
- Source/target language switching from the UI
- Browser microphone selection
- Recording acknowledgement flow for packaged exports
- Transcript history plus focused latest-turn view
- Generated summary and meeting minutes downloads
- Full export package with transcript artifacts and audio when recording is enabled

## Supported Languages

The backend currently supports these language codes:

- `ar` - Arabic
- `da` - Danish
- `en` - English
- `es` - Spanish
- `de` - German
- `fi` - Finnish
- `fr` - French
- `hi` - Hindi
- `it` - Italian
- `ja` - Japanese
- `ms` - Malay
- `no` - Norwegian
- `pt` - Portuguese
- `ru` - Russian
- `tr` - Turkish
- `km` - Khmer
- `vi` - Vietnamese
- `zh` - Chinese

Default language selection:

| Variable | Description | Default |
| --- | --- | --- |
| `SOURCE_LANGUAGE` | Default source language code | `en` |
| `TARGET_LANGUAGE` | Default target language code | `es` |

## Architecture

### Frontend

- Single static page: `index.html`
- Audio worklet helper: `pcm-worklet.js`
- Browser APIs used: `MediaRecorder`, `AudioContext`, `WebSocket`, `getUserMedia`

### Backend

- FastAPI application: `server.py`
- Speech segmentation: Silero VAD via `torch.hub`
- ASR client: OpenAI-compatible audio transcription API
- Translation and document generation: OpenAI-compatible chat completions API

### Main Runtime Flow

```text
Browser mic -> WebSocket -> Silero VAD -> Whisper ASR -> LLM translation -> Live UI
                                                   \-> summary/minutes/export package
```

## Repository Layout

| Path | Purpose |
| --- | --- |
| `index.html` | Frontend UI |
| `pcm-worklet.js` | Browser audio worklet for PCM streaming |
| `server.py` | FastAPI backend and export pipeline |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | Backend container image |
| `Dockerfile-FE` | Frontend container image |
| `charts/` | Helm packaging assets |

## Requirements

Before running the app, make sure you have:

- Python 3.11 or compatible runtime
- `ffmpeg` available on the host
- A Whisper-compatible ASR endpoint
- An OpenAI-compatible LLM endpoint for translation and document generation
- A Chromium-based browser recommended for microphone and recording support

## Configuration

Create a `.env` file in the project root for local backend defaults.

```env
# Whisper / ASR
WHISPER_BASE_URL=https://your-whisper-endpoint/v1
WHISPER_API_KEY=your-whisper-api-key
WHISPER_MODEL=openai/whisper-large-v3-turbo

# LLM / translation
LLM_BASE_URL=https://your-llm-endpoint/v1
LLM_API_KEY=your-llm-api-key
LLM_MODEL=Qwen/Qwen3-30B-A3B-Instruct-2507-FP8

# Default language pair
SOURCE_LANGUAGE=en
TARGET_LANGUAGE=es
```

### Backend Environment Variables

| Variable | Description | Default |
| --- | --- | --- |
| `WHISPER_BASE_URL` | Whisper-compatible API base URL | `""` |
| `WHISPER_API_KEY` | Whisper API key | `""` |
| `WHISPER_MODEL` | Whisper model name | `openai/whisper-large-v3-turbo` |
| `LLM_BASE_URL` | LLM API base URL | `""` |
| `LLM_API_KEY` | LLM API key | `""` |
| `LLM_MODEL` | LLM model name | `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8` |
| `SOURCE_LANGUAGE` | Default source language | `en` |
| `TARGET_LANGUAGE` | Default target language | `es` |
| `TORCH_NUM_THREADS` | Torch intra-op threads | `1` |
| `TORCH_NUM_INTEROP_THREADS` | Torch inter-op threads | `1` |
| `EXPORT_CHUNK_SECONDS` | Export chunk duration | `30` |
| `EXPORT_OVERLAP_SECONDS` | Export chunk overlap | `5` |
| `EXPORT_MAX_ASR_CONCURRENCY` | Parallel ASR jobs during export | `6` |
| `EXPORT_TEXT_BATCH_SEGMENTS` | Max transcript segments per LLM batch | `20` |
| `EXPORT_TEXT_BATCH_CHARS` | Max chars per LLM batch | `5000` |
| `EXPORT_VAD_PAD_MS` | Export VAD padding | `250` |
| `EXPORT_VAD_MERGE_GAP_MS` | Export VAD merge gap | `400` |
| `EXPORT_VAD_MAX_UNIT_SECONDS` | Max VAD unit duration | `15` |
| `EXPORT_JOB_TTL_SECONDS` | Retention time for export jobs | `3600` |

Note: model URL, model name, and API key can also be entered in the frontend advanced settings. Values from the backend are used as defaults.

## Local Development

### 1. Install Dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Start The Backend

```bash
source .venv/bin/activate
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

### 3. Serve The Frontend

Serve the static files with any simple web server, for example:

```bash
python3 -m http.server 5173
```

### 4. Important Local Networking Note

The current frontend behavior is mixed:

- the WebSocket target is hardcoded to `ws://localhost:8000/ws/translate`
- some HTTP requests use `window.location.origin` for `/defaults` and `/api/export-*`

Because of that, the cleanest production setup is a same-origin deployment behind AI Essentials, nginx, or another reverse proxy.

If you are running the frontend and backend on different local ports, you may need to:

- add a reverse proxy so the frontend and backend appear under one origin, or
- adjust the frontend HTTP calls for local testing

### 5. Open The App

Open the frontend in your browser and allow microphone access when prompted.

## PCAI Deployment

This project is intended to run well in HPE Private Cloud AI and AI Essentials environments.

### Recommended Model Endpoints

#### Whisper / ASR

Recommended example:

- Model: `openai/whisper-large-v3-turbo`
- Image: `tpomas/vllm-audio:0.11.0`
- CPU: `1`
- Memory: `10Gi`
- GPU: `1`
- Arguments: `--model openai/whisper-large-v3-turbo --port 8080`
- Optional env: `AIOLI_PROGRESS_DEADLINE=10000s`

#### LLM / Translation

Recommended example:

- Model: `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8`
- Image: `vllm/vllm-openai:latest`
- CPU: `4` to `6`
- Memory: `40Gi` to `60Gi`
- GPU: `1`
- Arguments: `--model Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 --enable-auto-tool-choice --tool-call-parser hermes --port 8080 --max-model-len 8192`
- Optional env:
  - `AIOLI_PROGRESS_DEADLINE=10000s`
  - `AIOLI_DISABLE_LOGGER=1`

### Import Into AI Essentials

Use the latest packaged release available for this project, for example the chart artifact referenced in `charts/`.

Provide workflow values similar to:

```yaml
env:
  WHISPER_BASE_URL: "ASR_OPENAI_API_COMPATIBLE_URI/v1"
  WHISPER_API_KEY: "ASR_MLIS_TOKEN"
  WHISPER_MODEL: "ASR_MODEL_NAME"
  LLM_BASE_URL: "LLM_OPENAI_API_COMPATIBLE_URI/v1"
  LLM_API_KEY: "LLM_MLIS_TOKEN"
  LLM_MODEL: "LLM_MODEL_NAME"
```

Suggested app metadata:

- Framework name: `Realtime live voice translation`
- Namespace: `realtime-translation`
- Description: `Supports transcription + translation from / to English, French, Italian, German, Spanish. Uses AI to summarize your meeting and create meeting minutes. Try it out.`

## API Surface

### Health And Defaults

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness check |
| `GET` | `/ready` | Readiness check |
| `GET` | `/defaults` | Frontend default settings |

### Export Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/export-documents` | Generate summary/minutes documents |
| `POST` | `/api/export-package` | Legacy package generation |
| `POST` | `/api/export-package/start` | Start async package generation |
| `GET` | `/api/export-package/status/{job_id}` | Poll export job status |
| `GET` | `/api/export-package/download/{job_id}` | Download generated archive |

### Streaming Endpoint

| Method | Path | Purpose |
| --- | --- | --- |
| `WS` | `/ws/translate` | Live transcription and translation stream |

## Operational Notes

- On first startup, Silero VAD weights are downloaded via `torch.hub` from `snakers4/silero-vad`.
- First boot can be slower because model assets may need to be pulled and cached.
- Recording is gated behind a browser acknowledgement flow before package export is enabled.
- The UI is optimized for live readability and keeps the latest turn in focus while preserving full transcript data for export.

## Troubleshooting

### The UI Loads But Translation Does Not Work

Check:

- browser microphone permission
- backend availability on port `8000`
- Whisper and LLM endpoint reachability
- frontend origin/proxy configuration for `/defaults`, `/api/export-*`, and `/ws/translate`

### Export Fails

Check:

- recording was enabled if you expect a full meeting package
- ASR and LLM credentials are valid
- `ffmpeg` is installed and available to the backend

### First Start Is Slow

This is expected the first time because Torch may download and cache Silero VAD assets.

## Tested Browser

- Google Chrome

## Summary

This application is best suited for live multilingual conversations running on HPE Private Cloud AI, with backend model endpoints provided through AI Essentials or compatible OpenAI-style services.
