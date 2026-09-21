# Verification

Post-deployment checks for **Conversation Toolbox** on PCAI: health endpoints, a UI smoke conversation, API-level smoke tests, and optional operator `kubectl` checks. Run these after every apply.

## Health endpoints

| Endpoint | What it does |
|---|---|
| `GET /health` | Liveness — returns `{"status": "healthy"}`; wired to the pod liveness probe. |
| `GET /ready` | Readiness — pings Redis and reports process memory; wired to the readiness probe, so the pod only takes traffic when Redis is reachable. |

Quick check from your workstation (the endpoint is behind PCAI oauth2-proxy SSO, so expect a redirect to login for browser access; from inside the cluster use the service DNS):

```bash
curl -s https://conversation-toolbox.<domain>/health      # → {"status":"healthy"}
curl -s https://conversation-toolbox.<domain>/ready       # → includes redis + memory status
```

## UI smoke test (recommended path)

1. Open `https://<your-endpoint>/` in a browser and sign in via PCAI SSO (oauth2-proxy).
2. On the home page, fill the ASR / LLM / TTS endpoint fields (pre-loaded from the chart's `config.*` values) and click **Test connection** — this calls `POST /api/test-connection`, which probes all three endpoints and is the fastest way to catch typos, wrong keys, or an unreachable model.
3. Speak into the microphone: you should see your speech transcribed and the assistant reply **streamed back as audio** (TTS starts before the LLM finishes). Interrupt it mid-utterance to verify barge-in works.
4. Switch to each page and try its core action:

| Page | Route | Smoke action |
|---|---|---|
| Voice conversation | `/` | Speak → streamed reply (step 3 above) |
| Multi-user transcription | `/multi-user` | Create a session, open the session URL in a second tab, speak in both — one shared transcript with speaker attribution |
| File transcription | `/transcribe-file` | Upload a short WAV/MP3, speakers = 1 → timestamped transcript |
| Batch transcription | `/batch-transcription` | Upload 2–3 files → live progress (SSE) → per-job status and transcript download |
| Voice profiles | `/voice-profiles` | List voices from the TTS endpoint, upload a short clip from `_speech_examples/libritts_voices/`, test it |

Session audio lands in `/mnt/persistent/recordings` and transcripts in `/mnt/persistent/transcripts` (PVC mounts) — verify files appear there for a deeper end-to-end check.

## API smoke tests

All routes live in `src/speech_to_speech_tools/app.py`; run these against the public endpoint (with an SSO cookie/bearer) or from inside the cluster against `http://conversation-toolbox-service.<namespace>.svc.cluster.local`.

```bash
BASE=https://conversation-toolbox.<domain>

# 1. Transcription round-trip (single file; also proves ASR + diarization path)
curl -s -F "file=@sample.wav" -F "num_speakers=1" "$BASE/api/transcribe-mp3"
#    → transcript JSON; note "requested_speakers" and "diarization_used" fields

# 2. Voice-profile listing (proves TTS endpoint + key)
curl -s "$BASE/api/voices"

# 3. Multi-user session creation (proves Redis)
curl -s -X POST "$BASE/api/multi-user/create-session"

# 4. Batch upload (proves Redis + arq workers + PVC staging), then poll
curl -s -F "files=@a.wav" -F "files=@b.wav" "$BASE/api/batch/upload"
curl -s "$BASE/api/batch/status/<job_id>"

# 5. Config as seen by the backend (proves ConfigMap wiring)
curl -s "$BASE/api/config"
```

Transcription behaviour to expect:

- `num_speakers == 1` → whole-file fast path, no diarization/VAD (default; fastest).
- `num_speakers > 1` with diarization reachable → pyannote segments labelled `SPEAKER_xx`.
- Diarization disabled/unreachable → **energy-based fallback**; responses report `diarization_used: false` and the UI warns that speaker separation may be inaccurate. That warning is expected unless you enabled the GPU microservice.
- Batch: per-job `num_speakers`/`language` are honoured; jobs survive pod restarts (state in Redis, audio on PVC); progress via `GET /api/batch/stream` (SSE) or polling.

## Operator checks (`kubectl` — optional)

Only for someone with cluster read access; PCAI users normally don't need this. Namespace = the project namespace the chart was applied to.

```bash
# Pods: app + workers (+ redis) Running, 0 restarts
kubectl get pods -n <namespace> -l app=conversation-toolbox

# App logs — look for startup config load and websocket connections
kubectl logs deploy/conversation-toolbox -n <namespace> --tail=100

# Worker logs — batch job processing
kubectl logs deploy/conversation-toolbox-workers -n <namespace> --tail=100

# Redis reachable from the app pod?
kubectl exec -n <namespace> deploy/conversation-toolbox -- \
  python -c "import redis,os; redis.Redis.from_url(os.environ['REDIS_URL']).ping()"

# PVCs bound (recordings, transcripts, hf-cache)
kubectl get pvc -n <namespace>

# Ingress objects: VirtualService host + oauth2-proxy AuthorizationPolicy
kubectl get virtualservice -n <namespace>
kubectl get authorizationpolicy -n istio-system | grep <release>

# HPA making decisions
kubectl get hpa -n <namespace>
```

Also useful: the PCAI UI's own pod/event view, and the Kyverno policy status (`kubectl get clusterpolicy add-vendor-app-labels-<release>-conversation-toolbox`).

## Troubleshooting

- **"Test connection" fails for one endpoint** — the base URLs must have **no `/v1` suffix** (the app appends API paths itself and auto-discovers the model via `/v1/models`). Check the key in `secrets.*`; empty keys are only valid when the endpoint resolves auth server-side.
- **Browser loops back to login / 401** — the oauth2-proxy AuthorizationPolicy matches the host `ezua.virtualService.endpoint` exactly; a mismatch between the URL you use and that value leaves the route unprotected or unauthorised. The policy is created in `istio-system` (`ezua.authorizationPolicy.namespace`).
- **Page loads but WebSockets/SSE stall** — the VirtualService timeout is `660s` (`ezua.virtualService.timeout`); if a proxy in front of PCAI cuts connections earlier, raise the mismatch or use shorter interactions.
- **Transcript says speaker separation may be inaccurate** — diarization is disabled or unreachable; either accept energy-based segmentation or enable the GPU microservice (see DEPLOYMENT.md) and confirm `DIARIZATION_BASE_URL` resolves.
- **Batch jobs stuck / Redis OOM** — `redis.maxmemory` must stay below the Redis container memory limit and uses integer MB; bounded queue + `BATCH_MAX_MEMORY_PERCENT` cap job metadata. Check `GET /api/batch/memory-stats` and `GET /api/system/memory`.
- **Batch HPA/KEDA not scaling** — memory stays low because the workload is I/O-bound; the HPA keys on memory (70%), so single small jobs won't scale it out. KEDA on `arq:queue` depth is the better fit when KEDA is installed (disable the HPA first — they fight over the same Deployment).
- **Pod stuck `NotReady` after apply** — `/ready` requires Redis; check the Redis pod (`kubectl get pods -n <namespace>`) and that the PVCs bound (RWX storage class must exist).
