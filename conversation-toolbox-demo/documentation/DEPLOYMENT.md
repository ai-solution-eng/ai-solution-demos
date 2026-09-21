# Deployment

How to deploy and operate **Conversation Toolbox** on HPE PCAI (HPE Private Cloud AI / Ezmeral Unified Analytics): prerequisites, the full `values.yaml` walkthrough, the PCAI/Istio wiring, and upgrades.

On PCAI you **never run `helm install` or `kubectl apply` yourself**: the packaged chart is imported once, and every deployment/upgrade is done by editing the chart's values in the PCAI **Helm Values** editor (or the PCAI API) and applying. PCAI resolves `${DOMAIN_NAME}` before rendering the templates.

---

## How deployment works on PCAI

1. **Import the chart once.** Upload the packaged chart (`conversation-toolbox-<version>.tar.gz`) into PCAI's chart catalog. The chart's `helm/.helmignore` excludes `local/`, so per-site credentials can never enter the tarball.
2. **Create the application** from the imported chart. PCAI asks for a target namespace (on SE G2 this is your personal `project-user-*` namespace) and shows the **Helm Values** editor.
3. **Paste a full values document** — the chart's `values.yaml` is the base, but the editor wants a complete document, so start from a file in [`helm/values-examples/`](../helm/values-examples/) and adjust the `# SITE:` markers.
4. **Apply.** PCAI resolves `${DOMAIN_NAME}`, renders the templates, and creates the Deployment, Service, Redis, arq workers (+ HPA), PVCs, Istio VirtualService, oauth2-proxy AuthorizationPolicy, and the Kyverno vendor-label policy.
5. **Upgrade = edit values + re-apply.** Change values in the Helm Values editor and apply again; the rendered resources are updated in place. A new chart *version* requires importing the new package and pointing the application at it.

Notes baked into the templates:

- Both the app and worker Deployments use `strategy: Recreate` — the SE G2 worker nodes sit at pod capacity (240 pods/node) and RollingUpdate surge pods deadlock. Brief downtime during applies is expected.
- PVCs carry the `helm.sh/resource-policy: keep` annotation, so recordings/transcripts/HF cache survive re-applies and uninstalls.

## Architecture

```text
Browser (5 pages)
      │  HTTPS (Istio gateway + oauth2-proxy)
      ▼
┌────────────────────────────────────────────────────────────┐
│ FastAPI app (app.py, port 8000)     Worker pods (arq)      │
│  • /ws                 voice loop   │   • process_batch_job │
│  • /ws/multi-user/{id} multi-user   │   └─ ASR via vLLM     │
│  • REST API (config, voices,        │                       │
│    transcribe, batch)               │                       │
└───────────────┬─────────────────┬───────────────────────────┘
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

## Prerequisites

### Model endpoints (the real dependency)

The app performs no inference itself — it talks to three external vLLM endpoints over HTTP. Configure their base URLs in `config.*` (no `/v1` suffix) and keys in `secrets.*`:

| Role | Model | Served by |
|---|---|---|
| ASR | `CohereLabs/cohere-transcribe-03-2026` | vLLM |
| LLM | `RedHatAI/gemma-4-31B-it-FP8-block` | vLLM |
| TTS | `fishaudio/s2-pro` | vLLM-Omni (Fish S2-pro) |

`vllm[audio]`/`vllm[video]` extras are not enabled upstream (trademark constraints), so purpose-built images are used for the audio models:

- ASR: `andrewbydlon/vllmaudio:v0.22.1`
- TTS: `andrewbydlon/vllmaudio:omni-v24.0` (vLLM-Omni + fish-speech for DAC codec decoding; adds `portaudio19-dev libportaudio2`)

Example PCAI model-deployment configs (JSON shape used by PCAI model serving):

Cohere (ASR):

```json
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
  "resource_limit_gpu": "0"
}
```

Gemma (LLM):

```json
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
  "resource_limit_gpu": "1"
}
```

Fish S2-pro (TTS):

```json
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
  "resource_limit_gpu": "1"
}
```

### Cluster prerequisites

- **Istio gateway** `istio-system/ezaf-gateway` (standard on PCAI) and the **oauth2-proxy** auth provider registered in `istio-system`.
- **RWX storage** for the PVCs — default `storageClass: gl4f-filesystem`, `ReadWriteMany`.
- **Permissions**: writing the AuthorizationPolicy into `istio-system` and creating the Kyverno ClusterPolicy (see [PCAI/Istio wiring](#pcaiistio-wiring)) require cluster-scoped rights; on locked-down customer clusters an operator may need to grant or pre-create these once.
- **(Optional)** a GPU node for the diarization microservice.

## Values walkthrough

The chart's own [`helm/values.yaml`](../helm/values.yaml) is fully commented; the tables below summarise what each block does and when to change it.

### Required

| Key | Purpose |
|---|---|
| `image.repository` / `image.tag` | App image. Default `andrewbydlon/conversation-toolbox:v4.5.2` (Docker Hub). |
| `config.asrBaseUrl` / `llmBaseUrl` / `ttsBaseUrl` | Model endpoint base URLs — **no `/v1` suffix**. |
| `secrets.asrApiKey` / `llmApiKey` / `ttsApiKey` | Bearer keys for those endpoints. May be `""` when the endpoint resolves keys server-side; empty UI keys never clobber the stored server key. |
| `ezua.virtualService.endpoint` | Public hostname, e.g. `conversation-toolbox.${DOMAIN_NAME}` (PCAI substitutes `${DOMAIN_NAME}`). Must be unique on the gateway. |

### Optional (defaults in `helm/values.yaml`)

| Key | Default | Notes |
|---|---|---|
| `deployment.replicaCount` | `1` | App is stateless except for PVC mounts. |
| `service` | ClusterIP 80 → 8000 | Fronted by the VirtualService. |
| `resources` | 2CPU/2Gi → 4CPU/4Gi | App pod. |
| `livenessProbe` / `readinessProbe` | `/health` / `/ready` on 8000 | `/ready` pings Redis and checks memory. |
| `env` | batch-tuning vars | See [Environment variables](#environment-variables-underlying-chart-detail). |
| `redis.*` | in-chart Redis, `maxmemory: 1536mb`, `noeviction` | Batch job state only (segment audio lives on PVC). `maxmemory` must stay **below** the container memory limit; integer MB only. Set `redis.external: true` + `redis.url` to use an existing Redis. |
| `workers.*` | enabled, 1 replica | arq batch workers; same image, `command: arq workers.WorkerSettings`. |
| `workers.autoscaling` | HPA on, 1→3 replicas, memory target 70% | CPU target is low because the workload is I/O-bound; `workers.replicaCount` is only the initial count. |
| `workers.autoscaling.keda` | off | KEDA ScaledObject on the `arq:queue` Redis list depth — better signal than memory, but requires KEDA on the cluster. Disable the HPA if you enable it (they fight over the same Deployment). |
| `diarization.*` | `enabled: false` | GPU pyannote microservice — see [below](#enabling-the-diarization-microservice). |
| `config.*` (rest) | prompt, hallucination patterns, voice, VAD… | Rendered into the ConfigMap; editable at runtime from the UI afterwards. |
| `secrets.hfToken` | `""` | Only needed with diarization enabled. |
| `persistence.*` | 40Gi/20Gi/10Gi on `gl4f-filesystem` RWX | recordings / transcripts / hf-cache; supports `existingClaim` to skip creation. |

### Environment variables (underlying chart detail)

These are rendered from `env:`/`config:`/`secrets:` values into a ConfigMap + Secret consumed via `envFrom` (`helm/templates/deployment.yaml`, `workers-deployment.yaml`). You normally set them through the values keys in the right-hand column:

| Env var | `values.yaml` key | Notes |
|---|---|---|
| `REDIS_URL` | `redis.url` | `redis://conversation-toolbox-redis:6379/0` in-chart |
| `ASR_BASE_URL` | `config.asrBaseUrl` | no `/v1` suffix |
| `ASR_API_KEY` | `secrets.asrApiKey` | |
| `LLM_BASE_URL` | `config.llmBaseUrl` | no `/v1` suffix |
| `LLM_API_KEY` | `secrets.llmApiKey` | |
| `TTS_BASE_URL` | `config.ttsBaseUrl` | no `/v1` suffix |
| `TTS_API_KEY` | `secrets.ttsApiKey` | can be empty if keys are resolved server-side |
| `ASR_LANGUAGE` | — (app default `en`) | Batch worker fallback language (per-job `language` overrides) |
| `LANGUAGE` | — (default AUTO) | ASR language for the conversation UI |
| `SAMPLE_RATE` | `config.sampleRate` | `16000` |
| `VAD_AGGRESSION` | `config.vadAggression` | webrtcvad aggression level |
| `HALLUCINATION_PATTERNS` | `config.hallucinationPatterns` | ASR hallucination filters |
| `SYSTEM_PROMPT` | `config.systemPrompt` | Voice-optimised default prompt |
| `TTS_VOICE` | `config.ttsVoice` | `alys` |
| `BATCH_TRANSCRIPTION_WORKER_COUNT` | `env` / `config.batchTranscriptionWorkerCount` | App-side split concurrency per batch file |
| `BATCH_MAX_CONCURRENT_JOBS` | `env` | Max jobs in flight |
| `BATCH_MAX_MEMORY_PERCENT` | `env` | Redis memory cap for job metadata |
| `BATCH_USE_BOUNDED_QUEUE` | `env` | Bounded Redis queue |
| `DIARIZATION_BASE_URL` | `diarization.baseUrl` | `http://conversation-toolbox-diarization:8001` |
| `TRANSCRIPTS_DIR` / `AUDIO_DIR` | fixed mount paths | `/mnt/persistent/transcripts` / `/mnt/persistent/recordings` |
| `HF_HOME` / `HF_TOKEN` | `persistence.hf_cache_dir` / `secrets.hfToken` | Hugging Face cache/token (diarization) |

## PCAI/Istio wiring

`ezua.*` values drive three resources:

```yaml
ezua:
  enabled: true
  domainName: ${DOMAIN_NAME}             # informational; templates use the endpoint directly
  virtualService:
    endpoint: conversation-toolbox.${DOMAIN_NAME}   # public host
    istioGateway: istio-system/ezaf-gateway
    timeout: 660s                        # generous: covers SSE streams and WebSockets
  authorizationPolicy:
    namespace: istio-system              # where the AuthorizationPolicy is created
    providerName: oauth2-proxy
```

- **VirtualService** — host `ezua.virtualService.endpoint` on gateway `istio-system/ezaf-gateway`, routing `/` to `<deployment.name>-service.<release-namespace>.svc.cluster.local:80`.
- **AuthorizationPolicy** — `action: CUSTOM` with provider `oauth2-proxy`, created in `ezua.authorizationPolicy.namespace` (`istio-system`), selecting the ingress gateway and matching the endpoint host. This is what puts the app behind PCAI SSO; unauthenticated requests are redirected to the oauth2-proxy login.
- **Kyverno vendor-label policy** — the chart ships a `ClusterPolicy` (`add-vendor-app-labels-<release>-conversation-toolbox`) as a **pre-install hook** that adds `hpe-ezua/type: vendor-service` and `hpe-ezua/app: conversation-toolbox` labels to Pods/Deployments/Services in the release namespace. Because it is cluster-scoped, applying it requires ClusterPolicy permissions on the target PCAI — on a hosted customer cluster this may need one-time approval from the platform admin. The `hpe-ezua` labels are also set directly on the worker Deployment and VirtualService.

## Enabling the diarization microservice

Optional GPU service (pyannote). Disabled by default to save GPUs:

```yaml
diarization:
  enabled: true
  # cudaVisibleDevices: GPU-...   # pin a free GPU, or leave empty to let the scheduler decide
  nodeName: pcai-se-scs04.hst.lab  # example: a G2 GPU node
  pipeline: pyannote/speaker-diarization-community-1
```

Requires:

- The `-diarization` image built from `docker/Dockerfile.diarization` and published alongside the app image — the template derives it as `<image.repository>:<image.tag>-diarization` (e.g. `andrewbydlon/conversation-toolbox:v4.5.2-diarization`).
- `secrets.hfToken` owning access to `pyannote/speaker-diarization-community-1` and `pyannote/segmentation-3.0`.
- GPU nodes (see `helm/templates/diarization-deployment.yaml`).

When disabled, the app works fine; transcription falls back to energy-based segmentation and the UI makes the limitation visible (`diarization_used: false`).

## Configuring tool calling

Tools are passed to the LLM as the **"Tool Calling Response-API Json"** in the homepage configuration (or `toolCalls` directly). The expected shape is a plain JSON **object mapping a tool name to its MCP server config** — no `description`/`definition` fields needed:

```json
{
  "sql_mcp": {
    "url": "http://mcp-ezpresto-server.mcp-ezpresto-server.svc.cluster.local:9097/mcp",
    "headers": {"Authorization": "Bearer <token>"},
    "transport": "streamable-http"
  },
  "k8s_ops": {
    "url": "http://k8s-mcp-svc.project-user-<you>.svc.cluster.local:9090/mcp",
    "transport": "streamable-http"
  }
}
```

The config is parsed by `conversation_manager._parse_tool_calls_from_config` and passed to `VoiceModel.aagent(tool_json)` (`utils/pcai_model_classes.py`), which registers one MCP server per tool with the OpenAI Agents SDK.

**Helper script** — prints the ready-to-paste object for several commonly configured MCP tools, reading the current PCAI bearer token from `/etc/secrets/ezua/.auth_token` when present (skipped if absent):

```bash
python src/speech_to_speech_tools/helper_scripts/get_some_tools.py
```

See [`get_some_tools.py`](../src/speech_to_speech_tools/helper_scripts/get_some_tools.py) for the full tool list; add your own tools following the same pattern.

## Runtime (UI) configuration

Config shown in the UI is the shared `ConversationSession.current_config`; non-secret parts can be read via `GET /api/config` and updated via `POST /api/config` (empty API-key fields are ignored so a cleared key can't overwrite the stored server key). Values loaded at startup come from the ConfigMap, so `config.*` values are the initial state and the UI overrides are per-session.

| Field | Description |
|---|---|
| `ASR_BASE_URL`, `ASR_MODEL_NAME`, `ASR_API_KEY` | Remote (vLLM) transcription endpoint — model auto-discovered via `/v1/models` |
| `remote` | Whether ASR runs outside PCAI (affects key resolution) |
| `language` | ASR language (Auto-detect = empty); per-request override on transcription pages |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL_NAME` | Conversational LLM endpoint |
| `systemPrompt` | Assistant system prompt (voice-optimised) |
| `toolCalls` / `tool-json` | JSON array/object of MCP tools to enable (see above) |
| `TTS_BASE_URL`, `TTS_API_KEY`, `TTS_VOICE` | Fish S2-pro (or vLLM-Omni) TTS endpoint + voice |
| `asrHallucinationPatterns`, `vadAggression`, `rmsThreshold`, `sampleRate` | ASR heuristics |

## Running locally (Docker)

Images are published on [Docker Hub (`andrewbydlon/conversation-toolbox`)](https://hub.docker.com/repository/docker/andrewbydlon/conversation-toolbox).

```bash
docker build -f docker/Dockerfile -t conversation-toolbox .

docker run --rm -p 8000:8000 \
  -e REDIS_URL=redis://<host>:6379/0 \
  -e ASR_BASE_URL=https://<asr-vllm-endpoint> \
  -e ASR_API_KEY=<token> \
  -e LLM_BASE_URL=https://<llm-vllm-endpoint> \
  -e LLM_API_KEY=<token> \
  -e TTS_BASE_URL=https://<tts-vllm-endpoint> \
  conversation-toolbox
```

The image installs the "lean" runtime deps (`docker/requirements-lean.txt`) with `ffmpeg` baked in for WAV conversion.

## Upgrading

- **Values change:** edit the Helm Values in PCAI and re-apply — that is the whole upgrade path for configuration.
- **New chart version:** import the new `conversation-toolbox-<version>.tar.gz` and point the application at it. For maintainers, `./automation.sh <version>` bumps chart + image tag, builds/pushes the image, and packages the chart (`bump_version.sh` handles the version edits).
- **Data safety:** PVCs are annotated `helm.sh/resource-policy: keep`, so transcripts/recordings/model cache survive re-applies and uninstall.
- **Expected downtime:** Deployments use `strategy: Recreate`, so a re-apply restarts the app pod (seconds of downtime, no surge pods).

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
  values-examples/           # paste-ready site values (secret-free)
  local/                     # per-site real values — git-ignored, never packaged
bump_version.sh              # bump chart version + image tag + appVersion
docker/Dockerfile / docker/Dockerfile.diarization
```
