# Conversation Toolbox (Speech‑to‑Speech)

A speech‑to‑speech conversational AI and transcription platform built for PCAI.
It provides a streaming, voice‑driven assistant with tool calling, real‑time
multi‑user transcription rooms, single‑file and batched audio transcription
(with optional ML speaker diarization), and Fish S2‑pro voice‑profile cloning.

The backend is a single FastAPI service (`app.py`) backed by Redis and arq
workers. All model inference (ASR, LLM, TTS) talks to external vLLM endpoints
shown in the app as configurable base URLs — swap endpoints without rebuilding.

---

## Demo Presentation

A video recording demonstrating the features of this app is available [here](https://storage.googleapis.com/ai-solution-engineering-videos/public/ConversationToolboxDemo.mkv)

---

## Features

### Conversational voice assistant (home page `/`)
- Streaming speech‑to‑speech loop: ASR → LLM → TTS with **token‑level streaming** (TTS starts before the LLM finishes) for low latency.
- **Voice interrupts** — stop the assistant mid‑utterance; configurable grace period.
- **Tool calling** via a configurable "Response‑API JSON" of MCP‑style tools passed
  into the UI (see the [Tool calling](#tool-calling) section).
- On‑the‑fly reconfiguration: TTS voice / endpoint, system prompt, ASR & LLM endpoints, hallucination filters — no restart required.
- Session transcripts written to disk; optional local audio recording per session.
- Config is persisted in `localStorage` and pushed to the backend via `/api/config`.

### Multi‑user live transcription (`/multi-user`)
- Create a shareable session ID; every connected client speaks and all speech is
  transcribed into a shared live transcript with speaker attribution.

### Single‑file transcription (`/transcribe-file`)
- Upload one audio file (MP3, WAV, M4A, FLAC, OGG, AAC, …) and get an
  already‑labelled, timestamped transcript written to `TRANSCRIPTS_DIR`.
- **Number of speakers** defaults to **1** (fast, no diarization needed).
- ML diarization with **pyannote** is optional — when disabled/unreachable the
  pipeline transparently falls back to energy‑based segmentation and warns the
  user that speaker separation may be inaccurate.
- **Language** is honoured per‑request: a language dropdown on the page
  (pre‑loaded from the homepage config) overrides the conversation config.

### Batch transcription (`/batch-transcription`)
- Upload many files at once; each is WAV‑converted, split, and transcribed by a
  **Redis + arq worker pool** (HPA‑scalable, KEDA‑ready).
- Real‑time progress via SSE (`/api/batch/stream`) or polling; jobs survive
  container restarts because state and audio live on the mounted PVC.
- Per‑job `num_speakers` and `language` control; diarization status is reported
  per job so the UI can flag energy‑based fallback.
- Optional webhook URL per batch.

### Voice profiles (`/voice-profiles`)
- List/upload/clone voices on the Fish S2‑pro TTS endpoint (or vLLM‑Omni).
- Auto‑generate `ref_text` from a reference clip (`/api/transcribe-clip`), test
  a voice TTS‑wise, and download the original uploaded sample.
- Fish S2 Pro itself enables tags which can modify the conversation meaningfully. Examples in the default prompt include [laughing], [emphasis], [sad], [sigh], [shouting], or [short pause].

### ML speaker diarization (optional microservice)
- A standalone pyannote-based `/diarize` microservice that runs on a GPU node.
  **Disabled by default** to save GPUs (`diarization.enabled: false`).
  See [Diarization](#diarization-microservice-optional).

---

## Architecture

```text
Browser (5 pages)
      │  HTTPS (Ingress/istio + oauth2-proxy)
      ▼
┌────────────────────────────────────────────────────────────┐
│ FastAPI app (app.py, port 8000)     Worker pods (arq)      │
│  • /ws                voice loop   │   • process_batch_job │
│  • /ws/multi-user/{id} multi-user  │   └─ ASR via vLLM     │
│  • REST API (config, voices,       │                        │
│    transcribe, batch)              │                        │
└───────────────┬────────────────┬────────────────────────────┘
                │ Redis (sessions, job store, pub/sub, arq queue)
                │                                               
        ASR (vLLM Cohere)   LLM (vLLM Gemma)   TTS (Fish S2-pro)
                │                │                │
                └── optional ────┴── diarization microservice
                                   (pyannote, GPU, port 8001)

Persistent storage (PVCs):
  /mnt/persistent/recordings   - conversational session audio
  /mnt/persistent/transcripts  - transcripts + batch staging
  /mnt/persistent/hf-cache     - HF model cache (diarization pipeline)
```

Memory note: audio chunks are kept small; the batch worker loads only one
segment at a time (`AudioSegment.load_audio()`).

---

## Pages / routes

| Page | Route | Frontend |
|---|---|---|
| Voice conversation (homepage) | `/` | `conversational_ai.html` |
| Multi-user transcription | `/multi-user` | `multi_user_transcription.html` |
| File transcription | `/transcribe-file` | `mp3_transcription.html` |
| Batch transcription | `/batch-transcription` | `batch_transcription.html` |
| Voice profiles | `/voice-profiles` | `voice_profiles.html` |

Health/readiness: `/health`, `/ready`.

---

## Configuration

### UI/backend config (shared `ConversationSession.current_config`)

| Field | Description |
|---|---|
| `ASR_BASE_URL`, `ASR_MODEL_NAME`, `ASR_API_KEY` | Remote (vLLM) transcription endpoint — model auto-discovered via `/v1/models` |
| `remote` | Whether ASR runs outside PCAI (affects key resolution) |
| `language` | ASR language (Auto-detect = empty). On transcription pages this is a per-request override |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL_NAME` | Conversational LLM endpoint |
| `systemPrompt` | Assistant system prompt (voice‑optimized) |
| `toolCalls` / `tool-json` | JSON array of MCP tools to enable; the LLM receives them as tools |
| `TTS_BASE_URL`, `TTS_API_KEY`, `TTS_VOICE` | Fish S2-pro (or vLLM-Omni) TTS endpoint + voice |
| `asrHallucinationPatterns`, `vadAggression`, `rmsThreshold`, `sampleRate` | ASR heuristics: hallucination filters, VAD aggression, energy threshold, sample rate |

Non-secret config can be fetched via `GET /api/config`; updates are pushed
`POST /api/config` (empty API-key fields are ignored so a cleared key doesn't
overwrite the stored server key).

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `ASR_BASE_URL` / `ASR_MODEL_NAME` / `ASR_API_KEY` | Cohere vLLM transcription endpoint | ASR/conversation worker + single-file transcription |
| `ASR_LANGUAGE` | en | Batch worker fallback language (overridden by the per-job `language` field) |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL_NAME` | Gemma vLLM URL | Conversation LLM |
| `TTS_BASE_URL` / `TTS_API_KEY` / `TTS_VOICE` | Fish S2-pro URL | TTS |
| `LANGUAGE` | "" | Defaults to AUTO. ASR language for conversation |
| `SAMPLE_RATE` | 16000 | Audio sample rate (Hz) |
| `VAD_AGGRESSION` | 2 | webrtcvad aggression level |
| `RMS_THRESHOLD` | 200 | Energy-based splitting threshold |
| `REDIS_URL` | `redis://conversation-toolbox-redis:6379/0` | Sessions + job store + queue |
| `BATCH_TRANSCRIPTION_WORKER_COUNT` | `4` | App-side split concurrency for batch files |
| `BATCH_MAX_CONCURRENT_JOBS` | `3` | Max jobs in flight |
| `BATCH_MAX_MEMORY_PERCENT` | `75` | Redis memory cap for job metadata |
| `BATCH_USE_BOUNDED_QUEUE` | `true` | Bounded Redis queue |
| `DIARIZATION_BASE_URL` | `http://conversation-toolbox-diarization:8001` | pyannote microservice |
| `TRANSCRIPTS_DIR` | `/mnt/persistent/transcripts` | Transcript/staging storage |
| `AUDIO_DIR` | `/mnt/persistent/recordings` | Recording/voice-profile storage |
| `HF_HOME` / `HF_TOKEN` | — | Hugging Face cache/token (diarization) |

---

## Tool calling

Tools are passed to the LLM as the **"Tool Calling Response-API Json"** in the
homepage configuration, or set as `toolCalls` directly. The expected shape is a
plain JSON **object mapping a tool name to its MCP server config** — no
`description`/`definition` fields are needed:

```json
{
  "sql_mcp": {
    "url": "http://mcp-ezpresto-server.mcp-ezpresto-server.svc.cluster.local:9097/mcp",
    "headers": {"Authorization": "Bearer <token>"},
    "transport": "streamable-http"
  },
  "k8s_ops": {
    "url": "http://k8s-mcp-svc.project-user-francesco-caliva.svc.cluster.local:9090/mcp",
    "transport": "streamable-http"
  }
}
```

That config is parsed by `conversation_manager._parse_tool_calls_from_config` and
passed to `VoiceModel.aagent(tool_json)` (`utils/pcai_model_classes.py`), which
registers one MCP server per tool with the OpenAI Agents SDK.

**Use the helper script to generate this JSON** — it reads the current PCAI
bearer token from `/etc/secrets/ezua/.auth_token` (skipped if absent) and prints
the ready-to-paste object for several commonly configured MCP tools:

```bash
python src/speech_to_speech_tools/helper_scripts/get_some_tools.py
```

See [`get_some_tools.py`](src/speech_to_speech_tools/helper_scripts/get_some_tools.py)
for the full tool list, and add your own tools by following the same pattern.

---

## Transcription internals

- `POST /api/transcribe-mp3` — file → WAV → optional diarization → per-segment ASR → transcript `.txt` in `TRANSCRIPTS_DIR`.
- `POST /api/transcribe-single-optimized` — same but parallel ASR (`asyncio.gather`), bounded by `max_concurrent`.
- `POST /api/batch/upload` — multi-file, Redis+arq queue, SSE status; per-job `num_speakers`/`language` stored in job metadata.

Behavior:
- `num_speakers == 1` → whole-file fast path (no diarization/VAD). Default is `1`.
- `num_speakers > 1` and diarization reachable → pyannote segments with `SPEAKER_xx` labels.
- Diarization disabled/failed → energy-based fallback; the API reports `diarization_used: false` and the UI warns "speaker separation may be inaccurate".

The single-file responses include `requested_speakers` and `diarization_used` so
clients can show the appropriate notice.

---

## Running locally

### Local development (Docker)

docker images are available [here](https://hub.docker.com/repository/docker/andrewbydlon/conversation-toolbox).

```bash
docker build -f docker/Dockerfile -t conversation-toolbox .

docker run --rm -p 8000:8000 \
  -e REDIS_URL=redis://<host>:6379/0 \
  -e ASR_BASE_URL=https://cohere-transcribe-03-2026... \
  -e ASR_API_KEY=<token> \
  -e LLM_BASE_URL=https://gemma-4-31b-ab... \
  -e LLM_API_KEY=<token> \
  -e TTS_BASE_URL=https://fish-s2-pro... \
  conversation-toolbox
```

The image installs the "lean" runtime deps (`requirements-lean.txt`) — `ffmpeg`
is baked in for WAV conversion. The diarization microservice is a separate image
(`docker/Dockerfile.diarization`) that must be built and published with a
`-diarization` suffix, e.g. `conversation-toolbox:v4.3.0-diarization`.

---

## Kubernetes deployment

Everything is in a single Helm chart (`helm/`):

```bash
# one-time
helm dependency update helm   # if needed

# install / upgrade
helm upgrade --install conversation-toolbox ./helm --namespace conversation-toolbox

# bump the version across chart + image tag (see bump_version.sh)
./bump_version.sh 4.4.0
```

The chart manages:

- **App Deployment + Service** (port 8000) — FastAPI app and static UI.
- **Redis** — sessions, job store, pub/sub, (single-pod stateful).
- **Workers Deployment** — arq worker pod(s) processing the job queue;
  optional **HPA** (memory-targeted) and **KEDA** scaling on queue depth.
- **PVCs** — `recordings`, `transcripts`, `hf-cache` (use RWX filesystem class).
- **Secrets** — `asrApiKey`, `llmApiKey`, `ttsApiKey`, `hfToken` (the app
  supports empty-key fallback so a client config can't clobber server secrets).
- **Ingress** — istio VirtualService + oauth2-proxy + Kyverno policy; DNS is
  `${DOMAIN_NAME}` templated in `values.yaml`.
- **Optional diarization** — the `diarization:` block (disabled by default).

### Mandatory env variables

The same variables shown in the `docker run` example must be set in the cluster.
In the Helm chart they come from a **ConfigMap** and **Secret** that the
Deployment consumes via `envFrom` (see `helm/templates/deployment.yaml`). Configure them in the `values.yaml` file to have sane defaults for the user:

| Env var | `values.yaml` key | Notes |
|---|---|---|
| `REDIS_URL` | `redis.url` | `redis://conversation-toolbox-redis:6379/0` in-chart |
| `ASR_BASE_URL` | `config.asrBaseUrl` | no `/v1` suffix |
| `ASR_API_KEY` | `secrets.asrApiKey` | |
| `LLM_BASE_URL` | `config.llmBaseUrl` | no `/v1` suffix |
| `LLM_API_KEY` | `secrets.llmApiKey` | |
| `TTS_BASE_URL` | `config.ttsBaseUrl` | no `/v1` suffix |
| `TTS_API_KEY` | `secrets.ttsApiKey` | can be empty if keys are resolved server-side |

Example:

```yaml
redis:
  url: redis://conversation-toolbox-redis:6379/0
config:
  asrBaseUrl: https://cohere-transcribe-03-2026...
  llmBaseUrl: https://gemma-4-31b-ab...
  ttsBaseUrl: https://fish-s2-pro...
secrets:
  asrApiKey: <token>
  llmApiKey: <token>
  ttsApiKey: <token>
```

Additional `config.*` values (system prompt, hallucination patterns, speaker count,
VAD/sample-rate) are templated into the ConfigMap alongside the mandatory ones.
The values are loaded into the backend's `current_config` at startup and can
still be edited at runtime from the UI (`/api/config`).

---

## Diarization microservice (optional)

Add to `helm/values.yaml`:

```yaml
diarization:
  enabled: true
  # cudaVisibleDevices: GPU-...   # pick a free GPU on pcai-se-scs04.hst.lab
  nodeName: pcai-se-scs04.hst.lab
  pipeline: pyannote/speaker-diarization-community-1
```

Requires:

- The `-diarization` image built (`docker/Dockerfile.diarization`).
- `hfToken` secret owning access to `pyannote/speaker-diarization-community-1`
  and `pyannote/segmentation-3.0`.
- GPU nodes (see `helm/templates/diarization-deployment.yaml`).

When disabled, the app works fine; transcription falls back to energy-based
segmentation and the UI makes the limitation visible.

---

## Deploying Models

### Audio Docker Images

vllm[audio] and vllm[video] are not enabled by default to avoid some trademark issues. So I use two specialized images that I built for cohere and fish s2 pro (resp.):
* andrewbydlon/vllmaudio:v0.22.1
* andrewbydlon/vllmaudio:omni-v24.0

I am not 100% that this is required for cohere, but it enables the model to resample audio dynamically as needed in the vllm checkpoint. Here are the docker configs in case they need to be rebuilt:

```

```

```
FROM vllm/vllm-omni:latest

# Install fish-speech (required for DAC codec decoding)
RUN apt-get update && apt-get install -y \
    portaudio19-dev libportaudio2 \
    && rm -rf /var/lib/apt/lists/*

# Install fish-speech Python package
RUN uv pip install --system fish-speech

ENTRYPOINT ["vllm", "serve"]
```

### Example deployment configs

#### Cohere

```
  {
    "uri": "pvc://models-pvc/large-models/CohereLabs/cohere-transcribe-03-2026?containerPath=/mnt/models",
    "image": "andrewbydlon/vllmaudio:v0.22.1",
    "model_format": "custom",
    "arguments": ["CohereLabs/cohere-transcribe-03-2026", "--download-dir", "/mnt/models/", "--trust-remote-code", "--port", "8080", "--gpu-memory-utilization", "0.15"],
    "environment": {"HF_TOKEN": "", "AIOLI_DISABLE_LOGGER": "1", "AIOLI_PROGRESS_DEADLINE": "15000s", "HF_XET_CLIENT_AC_MAX_DOWNLOAD_CONCURRENCY": "1", "HF_XET_CLIENT_ENABLE_ADAPTIVE_CONCURRENCY": "false", "HF_XET_DATA_MAX_CONCURRENT_FILE_DOWNLOADS": "1"},
    "resource_request_cpu": "4",
    "resource_request_memory": "20Gi",
    "resource_request_gpu": "0",
    "resource_limit_cpu": "8",
    "resource_limit_memory": "40Gi",
    "resource_limit_gpu": "0",
  },
```

#### Gemma

```
  {
    "uri": "pvc://models-pvc/large-models/RedHatAI/gemma-4-31B-it-FP8-block/?containerPath=/mnt/models",
    "image": "vllm/vllm-openai:gemma4-cu130",
    "model_format": "custom",
    "arguments": ["RedHatAI/gemma-4-31B-it-FP8-block", "--download-dir", "/mnt/models/", "--served-model-name", "Gemma4-31B-FP8", "--kv-cache-dtype", "fp8", "--kv-cache-dtype-skip-layers", "sliding_window", "--enable-auto-tool-choice", "--reasoning-parser", "gemma4", "--tool-call-parser", "gemma4", "--limit-mm-per-prompt", "{\"image\":1,\"video\":1}", "--async-scheduling", "--port", "8080", "--max-model-len", "262144", "--chat-template", "/mnt/models/chat_template.jinja"],
    "environment": {"HF_TOKEN": "", "AIOLI_DISABLE_LOGGER": "1", "AIOLI_PROGRESS_DEADLINE": "15000s"},
    "resource_request_cpu": "8",
    "resource_request_memory": "60Gi",
    "resource_request_gpu": "1",
    "resource_limit_cpu": "16",
    "resource_limit_memory": "100Gi",
    "resource_limit_gpu": "1",
  },
```

#### Fish S2

```
  {
    "uri": "pvc://models-pvc/large-models/fishaudio/s2-pro?containerPath=/mnt/models",
    "image": "andrewbydlon/vllmaudio:omni-v24.0",
    "model_format": "custom",
    "arguments": ["fishaudio/s2-pro", "--download-dir", "/mnt/models/", "--omni", "--port", "8080"],
    "environment": {"HF_TOKEN": "", "AIOLI_DISABLE_LOGGER": "1", "AIOLI_PROGRESS_DEADLINE": "15000s", "HF_HUB_DISABLE_SSL_VERIFICATION": "1", "HF_XET_CLIENT_AC_MAX_DOWNLOAD_CONCURRENCY": "1", "HF_XET_CLIENT_ENABLE_ADAPTIVE_CONCURRENCY": "false", "HF_XET_DATA_MAX_CONCURRENT_FILE_DOWNLOADS": "1"},
    "resource_request_cpu": "4",
    "resource_request_memory": "32Gi",
    "resource_request_gpu": "1",
    "resource_limit_cpu": "8",
    "resource_limit_memory": "64Gi",
    "resource_limit_gpu": "1",
  },
```


---

## Project layout

```
src/speech_to_speech_tools/
  app.py                     # FastAPI app: routes, websockets, static hosting
  workers.py                 # arq worker settings + batch/ASR/TTS functions
  main_components/           # conversation, multi-user, batch, constants
  utils/                     # audio handling, model adapters, tts sanitizer
  diarization_service/       # optional pyannote microservice (FastAPI :8001)
  static/                    # 5 HTML UI pages
helm/                        # Helm chart (app, workers, redis, pvcs, ingress)
bump_version.sh              # bump chart version + image tag + appVersion
docker/Dockerfile / docker/Dockerfile.diarization
```

---

## Troubleshooting

- **Model Connections** - You can run test connection from the frontend to verify all of your model endpoints are available. This may help debug syntax.
- **Batch HPA/KEDA not scaling** — memory stays low because the workload is
  I/O-bound; KEDA queue-depth scaling is recommended when available.
