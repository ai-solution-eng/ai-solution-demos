# Real-Time Live Voice Translation

| Owner                 | Name              | Email                              |
| ----------------------|-------------------|------------------------------------|
| Use Case Owner              | Francesco Caliva | francesco.caliva@hpe.com |
| PCAI Deployment Owner       | Francesco Caliva | francesco.caliva@hpe.com |


Realtime Live Voice Transcription and Translation in HPE Private Cloud AI environments.

This application captures microphone audio in the browser, detects speech activity with Voice Activity Detection (using Silero available in the torch hub), transcribes audio by using Automatic Speech Recognition (ASR) by means of Whisper Large v3, translates it with an LLM, and streams the results back to the UI in real time.

The presenter can create a meeting room code, which attendees can join, and within the room, they can attend the meeting transcribed in the language they prefer. Presenter and attendees can dynamically change language used for transcription and translation, directly from the front end. 

When recording is enabled, this app supports post-conversation exports such as transcripts, multilingual summaries, meeting minutes, and audio download.


## What The App Does

1. Captures microphone audio in the browser.
2. Streams audio frames to the backend over WebSocket.
3. Segments speech with Silero VAD.
4. Sends speech segments to a Whisper-compatible ASR model.
5. Sends the transcript to an LLM for translation.
6. Displays the latest translated turn in the UI while keeping earlier turns in history.
7. Optionally generates meeting documents and export packages after the conversation.
8. Presenter can provide personal Meeting Rooms where attendees can follow the conversation along in their preferred language.

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

The list of languages can be expanded as long as Whisper + the LLM support the desired language. 

Default language selection:

| Variable | Description | Default |
| --- | --- | --- |
| `SOURCE_LANGUAGE` | Default source language code | `en` |
| `TARGET_LANGUAGE` | Default target language code | `es` |

## Architecture

### Core components Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   Microphone    │────▶│   Frontend      │────▶│  FastAPI Backend │────▶│  AI Services    │
│                 │     |   (HTML/JS)     │◀────│  (WebSocket)     │◀────│  (ASR/LLM)  │
└─────────────────┘     └─────────────────┘     └──────────────────┘     └─────────────────┘
                               │                          │                                                         
                               ▼                          ▼
                        ┌──────────────────┐
                        │  PostgreSQL DB   │
                        │  (Persistence)   │
                        └──────────────────┘
```

```text
Browser mic -> WebSocket -> Silero VAD -> ASR -> LLM translation -> Live UI
                                                   \-> summary/minutes/export package
```

### Multi-Room Architecture

Presenter can generate a room link, and share with meeting attendees, who can follow along in their desired language.
If the audio is being recorded, a persisten visual banner will inform them. Once a particular language (e.g. Italian) has been used for translation, the download package (which includes recording, transcripts, translation, minutes and summary) will include content also in that language.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                           FastAPI Application                            │
│  ┌──────────────────────────────────────────────────────────────────┐    │
│  │                      In-Memory ROOMS Dict                        │    │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐               │    │
│  │  │  Room A     │  │  Room B     │  │  Room C     │               │    │
│  │  │  src: en    │  │  src: es    │  │  src: en    │               │    │
│  │  │  tgt: es    │  │  tgt: en    │  │  tgt: fr    │               │    │
│  │  │  ┌─────────┐│  │  ┌─────────┐│  │  ┌─────────┐│               │    │
│  │  │  │Presenter││  │  │Presenter││  │  │Presenter││               │    │
│  │  │  └───┬─────┘│  │  └───┬─────┘│  │  └───┬─────┘│               │    │
│  │  │      │ 1:N  │  │      │ 1:N  │  │      │ 1:N  │               │    │
│  │  │  ┌───┴─────┐│  │  ┌───┴─────┐│  │  ┌───┴─────┐│               │    │
│  │  │  │Attendees││  │  │Attendees││  │  │Attendees││               │    │
│  │  │  │[es][es] ││  │  │[en][fr] ││  │  │[de][it] ││               │    │
│  │  │  └─────────┘│  │  └─────────┘│  │  └─────────┘│               │    │
│  │  └─────────────┘  └─────────────┘  └─────────────┘               │    │
│  └──────────────────────────────────────────────────────────────────┘    │
│                                    │                                     │
│                                    │ async writes                        │
│                                    ▼                                     │
│  ┌──────────────────────────────────────────────────────────────────┐    │
│  │                    SQLAlchemy (asyncpg)                          │    │
│  └──────────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────────┘
                                     │
                                     │
                            ┌────────┴────────┐
                            │   PostgreSQL    │
                            │   (persistence) │
                            └─────────────────┘
```

For a detailed technical description, refer to [README_TECHNICAL.md](v.0.3.4/README_TECHNICAL.md)

---

# Deployment

## Requirements

Before running the app, make sure you have:

- Python 3.11 or compatible runtime
- `ffmpeg` available on the host
- A OpenAI compatible ASR endpoint
- An OpenAI-compatible LLM endpoint for translation and document generation
- A Chromium-based browser recommended for microphone and recording support

> **PostgreSQL is automatically provisioned** when deploying via Helm (`postgresql.enabled: true` in values.yaml).
> No manual PostgreSQL setup is required. To use an external database instead, set `postgresql.enabled: false`
> and provide `DATABASE_URL` in the `env` section.

## Configuration

Create a `.env` file in the project root for local backend defaults.

```env
# Whisper / ASR
ASR_BASE_URL=https://your-asr-endpoint/v1
ASR_API_KEY=your-asr-api-key
ASR_MODEL=openai/whisper-large-v3-turbo

# LLM / translation
LLM_BASE_URL=https://your-llm-endpoint/v1
LLM_API_KEY=your-llm-api-key
LLM_MODEL=Qwen/Qwen3-30B-A3B-Instruct-2507-FP8

# Default language pair
SOURCE_LANGUAGE=en
TARGET_LANGUAGE=es

```

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

### 5. Open The App

Open the frontend in your browser at `localhost:5173` and allow microphone access when prompted.

## PCAI Deployment

This project is intended to run in HPE Private Cloud AI and AI Essentials environments.

### Recommended Model Endpoints

#### ASR models

Recommended models :

- Model: `openai/whisper-large-v3-turbo`
- Image: `tpomas/vllm-audio:0.11.0`
- CPU: `1->1`
- Memory: `10Gi->10Gi`
- GPU: `1->1`
- Arguments: `--model openai/whisper-large-v3-turbo --port 8080`
- Optional env: `AIOLI_PROGRESS_DEADLINE=10000s`

or

- Model: `CohereLabs/cohere-transcribe-03-2026`
- Image: `andrewbydlon/vllmaudio:v0.22.1`
- CPU: `4->8`
- Memory: `20Gi->40Gi`
- GPU: `1->1`
- Arguments: `CohereLabs/cohere-transcribe-03-2026 --trust-remote-code --port 8080 --gpu-memory-utilization 0.15`
- Optional env: 
  - `AIOLI_DISABLE_LOGGER=1` 
  - `AIOLI_PROGRESS_DEADLINE=15000`
  - `HF_TOKEN=$HF_TOKEN`
  - `HF_XET_CLIENT_AC_MAX_DOWNLOAD_CONCURRENCY=1`
  - `HF_XET_CLIENT_ENABLE_ADAPTIVE_CONCURRENCY=false`
  - `HF_XET_DATA_MAX_CONCURRENT_FILE_DOWNLOADS=1`


#### LLM / Translation

Recommended models:

- Model: `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8`
- Image: `vllm/vllm-openai:latest`
- CPU: `4` to `6`
- Memory: `40Gi` to `60Gi`
- GPU: `1`
- Arguments: `--model Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 --enable-auto-tool-choice --tool-call-parser hermes --port 8080 --max-model-len 8192`
- Optional env:
  - `AIOLI_DISABLE_LOGGER=1`
  - `AIOLI_PROGRESS_DEADLINE=15000`
  - `HF_TOKEN=$HF_TOKEN`
  - `HF_XET_CLIENT_AC_MAX_DOWNLOAD_CONCURRENCY=1`
  - `HF_XET_CLIENT_ENABLE_ADAPTIVE_CONCURRENCY=false`
  - `HF_XET_DATA_MAX_CONCURRENT_FILE_DOWNLOADS=1`

or

- Model: ``
- Image: `vllm/vllm-openai:gemma4`
- CPU: `8->16`
- Memory: `60Gi` to `100Gi`
- GPU: `1->1`
- Arguments: `google/gemma-4-31B-it --download-dir /mnt/models/ --enable-auto-tool-choice --reasoning-parser gemma4 --tool-call-parser gemma4 --limit-mm-per-prompt {"image":4,"video":1} --async-scheduling --port 8080`
- Optional env:
  - `AIOLI_PROGRESS_DEADLINE=10000s`
  - `AIOLI_DISABLE_LOGGER=1`

### PostgreSQL

PostgreSQL is **automatically provisioned** when deploying via Helm. The chart includes:

1. A **PostgreSQL StatefulSet** (`postgres:16-alpine`) with persistent storage for call recordings, transcripts, and exports.
2. A **db-init Job** that runs on install/upgrade to:
   - Create the application role and database if they don't exist
   - Run Alembic migrations to initialize or update the schema
3. A **Kubernetes Secret** to securely store database credentials.

This means no manual PostgreSQL setup is required — the entire database stack is deployed alongside the application.

#### Using an External Database

To use an external PostgreSQL instead of the bundled one, set `postgresql.enabled: false` in your values.yaml and provide the `DATABASE_URL` explicitly:

```yaml
postgresql:
  enabled: false

env:
  DATABASE_URL: "postgresql+asyncpg://realtime_voice_user:realtime_voice_password@postgresql.postgresql.svc.cluster.local:5432/realtime_voice_db"
```

#### Inspecting the Database

When using the app, data in the database can be retrieved by connecting to the PostgreSQL service:

```python
import psycopg
conn = psycopg.connect(
    host="<postgresql-service>",
    port=5432,
    dbname="realtime_voice_db",
    user="realtime_voice_user",
    password="realtime_voice_password",
    connect_timeout=5,
)
print("Connected")
with conn.cursor() as cur:
    cur.execute("select table_name from information_schema.tables where table_schema='public' order by table_name;")
    print(cur.fetchall())
    cur.execute("select id, room_id, status, recording_state, started_at from call_sessions order by started_at desc limit 10;")
    print(cur.fetchall())
    cur.execute("select session_id, segment_id, source_text from call_segments order by created_at desc limit 10;")
    print(cur.fetchall())
```

> **Note:** Persistence is active only if the presenter enables `Recording`. Attendees will be prompted that recording is in progress by means of a visual banner.


### Import Into AI Essentials

Use the latest packaged release available for this project, for example the chart artifact referenced in `charts/`.

Provide workflow values similar to:

```yaml
env:
  ASR_BASE_URL: "ASR_OPENAI_API_COMPATIBLE_URI/v1"
  ASR_API_KEY: "ASR_MLIS_TOKEN"
  ASR_MODEL: "ASR_MODEL_NAME"
  LLM_BASE_URL: "LLM_OPENAI_API_COMPATIBLE_URI/v1"
  LLM_API_KEY: "LLM_MLIS_TOKEN"
  LLM_MODEL: "LLM_MODEL_NAME"

# DATABASE_URL is auto-constructed from postgresql.* when postgresql.enabled=true.
# For an external database, set postgresql.enabled: false and provide DATABASE_URL:
# DATABASE_URL: "postgresql+asyncpg://realtime_voice_user:realtime_voice_password@postgresdb-postgresql.postgresdb.svc.cluster.local:5432/realtime_voice_db"
```

Suggested app metadata:

Import using Helm chart [realtime-translation-0.3.7.tgz](realtime-translation-0.3.7.tgz).
```
- Framework name: `Realtime Live Voice Translation`
- Namespace: `realtime-translation`
- Description: `Transcribe and translate meetings. Use AI to summarize your meeting and create meeting minutes. Presenters can share a room code with attendees allowing them to follow along in their preferred language.`
```