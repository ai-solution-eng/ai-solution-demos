# Conversation Toolbox

**Conversation Toolbox** (formerly Speech-to-Speech Onboarding Project) is a speech-to-speech conversational AI and transcription toolkit for **HPE PCAI** (HPE Private Cloud AI / Ezmeral Unified Analytics). It ships as a single Helm chart (`conversation-toolbox`) that deploys one FastAPI web service plus Redis-backed batch workers, and connects to external vLLM model endpoints for ASR, LLM, and TTS — swap model endpoints from the UI without rebuilding. It is not tied to any specific voice stack: the browser does audio capture/VAD, the backend orchestrates ASR → LLM → TTS, and Fish S2-pro emotional voice tags are supported out of the box.

A video demo of the app is available [here](https://storage.googleapis.com/ai-solution-engineering-videos/public/ConversationToolboxDemo.mkv).

---

## What problem(s) it solves

- **Talk to your documents/systems by voice** — a low-latency, streaming voice assistant (ASR → LLM → TTS with token-level streaming, so TTS starts before the LLM finishes) that can call MCP tools (SQL, Kubernetes, RAG, web search…) configured at runtime.
- **Meetings and multi-party capture** — a shared, live, multi-user transcription room with speaker attribution via a shareable session ID.
- **Audio → usable transcripts** — single-file and batch transcription of MP3/WAV/M4A/FLAC/OGG/AAC with timestamps, optional ML speaker diarization (with automatic energy-based fallback), and per-job language/speaker controls.
- **Reusable brand voices** — upload, test, and clone voice profiles on the Fish S2-pro TTS endpoint, including auto-generated reference text from a sample clip.
- **Demo-ready deployment on PCAI** — the chart wires the app into the PCAI Istio gateway with oauth2-proxy SSO, RWX persistent storage, HPA/KEDA-ready batch workers, and an optional GPU diarization microservice — no `helm install` or `kubectl` required from the user.

## Features

### Voice assistant (`/`)
- Streaming speech-to-speech loop with **voice interrupts** (stop the assistant mid-utterance; configurable grace period).
- **Tool calling** via an MCP-style JSON pasted into the UI — the LLM receives the tools and can call them mid-conversation.
- On-the-fly reconfiguration from the page: TTS voice/endpoint, system prompt, ASR & LLM endpoints, hallucination filters — no restart; persisted in `localStorage` and pushed to `POST /api/config`.
- Session transcripts written to disk; optional per-session audio recording.

### Multi-user live transcription (`/multi-user`)
- Shareable session ID; every connected client's speech lands in one live transcript with speaker attribution.

### Single-file transcription (`/transcribe-file`)
- One upload → labelled, timestamped transcript. Number of speakers defaults to 1 (fast path, no diarization); pyannote diarization is optional and degrades gracefully to energy-based segmentation with a UI warning.
- Per-request language override via dropdown.

### Batch transcription (`/batch-transcription`)
- Many files at once, processed by a Redis + arq worker pool (HPA on memory; KEDA on queue depth when available). Real-time progress via SSE or polling; jobs survive pod restarts (state on PVC). Per-job speakers/language; optional webhook per batch.

### Voice profiles (`/voice-profiles`)
- List/upload/clone voices on Fish S2-pro (or vLLM-Omni); test a voice, auto-generate `ref_text` from a reference clip, download the original sample. Fish S2-pro emotional tags ([laughing], [emphasis], [sad], [sigh], [shouting], [short pause]…) work in the default system prompt.

### Optional GPU diarization microservice
- Standalone pyannote `/diarize` service on a GPU node — **disabled by default** (`diarization.enabled: false`) to save GPUs.

---

## Deployment (the PCAI way)

On PCAI you never run `helm install` or `kubectl apply`. The flow is:

1. **Import the packaged chart once** — upload `conversation-toolbox-<version>.tar.gz` into PCAI (AI Applications / chart catalog).
2. **Create the application** from the imported chart and open the **Helm Values** editor.
3. **Paste a full values document** (start from [`helm/values-examples/`](helm/values-examples/)), adjust the `# SITE:` lines and credentials, and **apply**. PCAI resolves `${DOMAIN_NAME}` before rendering.

Required values (everything else has sane chart defaults):

```yaml
image:
  repository: andrewbydlon/conversation-toolbox   # chart default; change only for a private mirror
  tag: v4.5.2
config:
  asrBaseUrl: https://<asr-vllm-endpoint>    # no /v1 suffix
  llmBaseUrl: https://<llm-vllm-endpoint>    # no /v1 suffix
  ttsBaseUrl: https://<tts-vllm-endpoint>    # no /v1 suffix
secrets:
  asrApiKey: <ASR_API_KEY>                   # may be "" if the endpoint resolves keys server-side
  llmApiKey: <LLM_API_KEY>
  ttsApiKey: <TTS_API_KEY>
ezua:
  virtualService:
    endpoint: conversation-toolbox.${DOMAIN_NAME}   # ${DOMAIN_NAME} is resolved by PCAI
```

Optional values (all defaulted in the chart — see [`documentation/DEPLOYMENT.md`](documentation/DEPLOYMENT.md) for the full walkthrough): worker autoscaling (HPA/KEDA), Redis memory, PVC sizes/storage class, resources, probes, system prompt, hallucination patterns, TTS voice, sample rate/VAD tuning, and the GPU diarization microservice.

Underlying chart detail (rarely edited): runtime behaviour env vars (`REDIS_URL`, `BATCH_*`, `DIARIZATION_BASE_URL`, …) are rendered from `env:` / `config:` / `secrets:` values into a ConfigMap + Secret consumed by the app and worker pods — you configure them through the values keys above, not by editing manifests. The full env-var table is in the deployment doc.

### Deployment targets

**SE G2** — HPE-internal PCAI cluster at `pcai-se-ai-application.hst.rdlabs.hpecorp.net` (accessed through the HPE proxy). Workloads land in your personal `project-user-*` namespace. Start from [`helm/values-examples/values.g2.yaml`](helm/values-examples/values.g2.yaml), which already points `ezua.virtualService.endpoint` at the G2 domain.

**Hosted trial** — a customer-managed PCAI. Keep `${DOMAIN_NAME}` in `ezua.virtualService.endpoint` (PCAI substitutes it), keep the oauth2-proxy `authorizationPolicy` block, and be aware the chart ships a Kyverno vendor-label **ClusterPolicy** as a pre-install hook, which needs cluster-scoped permissions on the target PCAI. Start from [`helm/values-examples/values.hosted-trial.yaml`](helm/values-examples/values.hosted-trial.yaml).

---

## Documentation

| Document | Contents |
|---|---|
| [`documentation/DEPLOYMENT.md`](documentation/DEPLOYMENT.md) | Full PCAI deployment walkthrough: values reference, ezua/Istio/oauth2-proxy/Kyverno wiring, model-endpoint prerequisites, diarization, tool calling, local Docker development, upgrading. |
| [`documentation/VERIFICATION.md`](documentation/VERIFICATION.md) | Health checks, UI + API smoke tests, optional operator `kubectl` checks, troubleshooting. |
| [`helm/values-examples/`](helm/values-examples/) | Secret-free, paste-ready full values for the SE G2 and hosted-trial targets (real per-site values live in `helm/local/`, which is git-ignored and never packaged or mirrored). |
| [`_speech_examples/`](_speech_examples/) | Sample voice clips (LibriTTS) for voice-profile uploads — local working data only; not mirrored or packaged. |
