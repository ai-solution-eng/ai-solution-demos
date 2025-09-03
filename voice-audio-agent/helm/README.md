<!-- File: helm/README.md -->

# Helm Chart for Audio Pipeline

This Helm chart (`audio-pipeline-chart`) deploys both the WebSocket server and the Gradio UI on any Kubernetes cluster (designed for HPE PCAI).

## Prerequisites

- Helm 3.x
- A Kubernetes cluster with Istio (for the VirtualService)
- The models deployed in PCAI (see root README)


### Configure environment for the Helm chart

This chart deploys two containers in one Pod:

* **websocket-server** (talks to LLM + ASR + TTS)
* **gradio-ui** (browser UI that connects to the websocket server)

Youll edit `helm/values.yaml` to point at your MLIS endpoints and set defaults for the UI.

---

### 1) What you’ll change (at a glance)

**Websocket Server → `websocketServer.env`**

| Key                   | Required | Example                                                      | Notes                                                                               |
| --------------------- | -------- | ------------------------------------------------------------ | ----------------------------------------------------------------------------------- |
| `LLM_API_BASE`        | ✅        | `https://<mlis-endpoint>/v1`                                 | OpenAI-style HTTP base deployed using MLIS.                                           |
| `LLM_API_KEY`         | ✅        | `"<key>"`                                                    | to an API key created for a deployed MLIS endpoint  |
| `LLM_MODEL_NAME`      | ✅        | `"meta-llama/Llama-3.2-1B-Instruct"`                         | Must match the model name in your MLIS deployment (see packaged model “arguments”). |
| `LLM_PROMPT_TEMPLATE` | ✅        | `'Answer the question: "{transcript}"\n\nAnswer concisely.'` | `{transcript}` will be interpolated with ASR text.                                  |
| `ASR_SERVER_ADDRESS`  | ✅        | `parakeet-...svc.cluster.local:50051`                        | Internal DNS + port for your **Nvidia ASR** gRPC service.                           |
| `ASR_USE_SSL`         | ✅        | `"false"`                                                    | Keep `false` until MLIS supports gRPC TLS directly.                                 |
| `ASR_LANGUAGE_CODE`   | ✅        | `"en-US"`                                                    | Parakeet US supports English; pick the code that matches your ASR model.            |
| `TTS_SERVER_ADDRESS`  | ✅        | `tts-...svc.cluster.local:50051`                             | Internal DNS + port for your **Nvidia TTS** gRPC service.                           |
| `TTS_USE_SSL`         | ✅        | `"false"`                                                    | Keep `false` until MLIS supports gRPC TLS directly.                                 |
| `TTS_VOICE`           | ✅        | `"Magpie-Multilingual.EN-US.Sofia"`                          | Must be valid for your TTS model (list below).                                      |
| `TTS_LANGUAGE_CODE`   | ✅        | `"en-US"`                                                    | Must match the selected voice.                                                      |
| `TTS_SAMPLE_RATE_HZ`  | ✅        | `"44100"`                                                    | Use a rate supported by your TTS model/output pipeline.                             |

**Gradio UI → `gradioUi.env`**
These are **defaults shown in the UI** and should match the server settings.

| Key                   | Example                                                      | Notes                                                             |
| --------------------- | ------------------------------------------------------------ | ----------------------------------------------------------------- |
| `WEBSOCKET_URI`       | `ws://localhost:8765`                                        | Works because UI + server are sidecar containers in the same Pod. |
| `ASR_SERVER_ADDRESS`  | `parakeet-...svc.cluster.local:50051`                        | Keep in sync with server.                                         |
| `TTS_SERVER_ADDRESS`  | `tts-...svc.cluster.local:50051`                             | Keep in sync with server.                                         |
| `LLM_PROMPT_TEMPLATE` | `'Answer the question: "{transcript}"\n\nAnswer concisely.'` | Keep in sync with server.                                         |
| `TTS_VOICE`           | `"Magpie-Multilingual.EN-US.Sofia"`                          | Keep in sync with server.                                         |
| `TTS_LANGUAGE_CODE`   | `"en-US"`                                                    | Keep in sync with server.                                         |
| `TTS_SAMPLE_RATE_HZ`  | `"44100"`                                                    | Keep in sync with server.                                         |

---

### 2) Set up the LLM (HTTP / OpenAI-style)

1. In MLIS, deploy your LLM and note the **HTTP base URL** (usually ends with `/v1`) and **API key**.
2. In `values.yaml` set:

   ```yaml
   LLM_API_BASE: "https://<your-mlis-llm-endpoint>/v1"
   LLM_API_KEY: "<placeholder>" # API Key generated from MLIS
   LLM_MODEL_NAME: "meta-llama/Llama-3.2-1B-Instruct"
   LLM_PROMPT_TEMPLATE: 'Answer the question: "{transcript}"\n\nAnswer concisely.'
   ```

---

### 3) Set up ASR (gRPC)

> MLIS v1.2 + AIE v1.6.1 note: MLIS does not natively expose gRPC through Istio for these models. Youll expose the Pods gRPC port inside the cluster.

**Deploy & expose**

1. Deploy the Nvidia **ASR** NIM model on MLIS. Ensure the **Aioli service port matches your models gRPC port** (commonly `50051`).
2. Find the InferenceService (example namespace shown; replace it):

   ```bash
   kubectl -n andrew-mendez-h-b88c4d11 get isvc
   ```
3. **Disable Istio sidecar** on the serving workload to avoid HTTP capture:

   ```bash
   # Edit the isvc and set pod template annotation:
   kubectl -n andrew-mendez-h-b88c4d11 edit isvc <your-asr-isvc-name>
   # Under .spec.predictor.template.metadata.annotations add:
   # sidecar.istio.io/inject: "false"
   ```
4. Expose the **gRPC port** as a ClusterIP Service (adjust names/labels/ports to match your deployment):

   ```bash
   kubectl -n andrew-mendez-h-b88c4d11 expose deploy <asr-deployment-name> \
     --port=50051 --target-port=50051 --name=<asr-svc-name>
   ```
5. Your in-cluster address will look like:

   ```
   <asr-svc-name>.<namespace>.svc.cluster.local:50051
   ```
6. Put that into both server and UI `ASR_SERVER_ADDRESS`.

---

### 4) Set up TTS (gRPC)

Follow the **same steps** as ASR for your Nvidia **TTS** NIM model (disable sidecar, expose `50051`, record the service DNS).
Use that FQDN for `TTS_SERVER_ADDRESS` in both server and UI.

---

### 5) Valid voices (Magpie TTS Multilingual)

<details>
<summary>Click to expand the full list</summary>

* "Magpie-Multilingual.EN-US.Sofia", "Magpie-Multilingual.EN-US.Ray",
  "Magpie-Multilingual.EN-US.Sofia.Calm", "Magpie-Multilingual.EN-US.Sofia.Fearful",
  "Magpie-Multilingual.EN-US.Sofia.Happy", "Magpie-Multilingual.EN-US.Sofia.Angry",
  "Magpie-Multilingual.EN-US.Sofia.Neutral", "Magpie-Multilingual.EN-US.Sofia.Disgusted",
  "Magpie-Multilingual.EN-US.Sofia.Sad", "Magpie-Multilingual.EN-US.Ray.Angry",
  "Magpie-Multilingual.EN-US.Ray.Calm", "Magpie-Multilingual.EN-US.Ray.Disgusted",
  "Magpie-Multilingual.EN-US.Ray.Fearful", "Magpie-Multilingual.EN-US.Ray.Happy",
  "Magpie-Multilingual.EN-US.Ray.Neutral", "Magpie-Multilingual.EN-US.Ray.Sad",
  "Magpie-Multilingual.ES-US.Diego", "Magpie-Multilingual.ES-US.Isabela",
  "Magpie-Multilingual.ES-US.Isabela.Neutral", "Magpie-Multilingual.ES-US.Diego.Neutral",
  "Magpie-Multilingual.ES-US.Isabela.Fearful", "Magpie-Multilingual.ES-US.Diego.Fearful",
  "Magpie-Multilingual.ES-US.Isabela.Happy", "Magpie-Multilingual.ES-US.Diego.Happy",
  "Magpie-Multilingual.ES-US.Isabela.Sad", "Magpie-Multilingual.ES-US.Diego.Sad",
  "Magpie-Multilingual.ES-US.Isabela.Pleasant\_Surprise", "Magpie-Multilingual.ES-US.Diego.Pleasant\_Surprise",
  "Magpie-Multilingual.ES-US.Isabela.Angry", "Magpie-Multilingual.ES-US.Diego.Angry",
  "Magpie-Multilingual.ES-US.Isabela.Calm", "Magpie-Multilingual.ES-US.Diego.Calm",
  "Magpie-Multilingual.ES-US.Isabela.Disgust", "Magpie-Multilingual.ES-US.Diego.Disgust",
  "Magpie-Multilingual.FR-FR.Louise", "Magpie-Multilingual.FR-FR.Pascal",
  "Magpie-Multilingual.FR-FR.Louise.Neutral", "Magpie-Multilingual.FR-FR.Pascal.Neutral",
  "Magpie-Multilingual.FR-FR.Louise.Angry", "Magpie-Multilingual.FR-FR.Louise.Calm",
  "Magpie-Multilingual.FR-FR.Louise.Disgust", "Magpie-Multilingual.FR-FR.Louise.Fearful",
  "Magpie-Multilingual.FR-FR.Louise.Happy", "Magpie-Multilingual.FR-FR.Louise.Pleasant\_Surprise",
  "Magpie-Multilingual.FR-FR.Louise.Sad", "Magpie-Multilingual.FR-FR.Pascal.Angry",
  "Magpie-Multilingual.FR-FR.Pascal.Calm", "Magpie-Multilingual.FR-FR.Pascal.Disgust",
  "Magpie-Multilingual.FR-FR.Pascal.Fearful", "Magpie-Multilingual.FR-FR.Pascal.Happy",
  "Magpie-Multilingual.FR-FR.Pascal.Pleasant\_Surprise", "Magpie-Multilingual.FR-FR.Pascal.Sad",
  "Magpie-Multilingual.EN-US.Mia", "Magpie-Multilingual.DE-DE.Jason",
  "Magpie-Multilingual.DE-DE.Leo", "Magpie-Multilingual.DE-DE.Aria"

</details>

For language support details, see Nvidia’s docs you referenced:
`https://docs.nvidia.com/nim/riva/tts/latest/getting-started.html`

---

### 6) Example `values.yaml` edits (minimal)

```yaml
websocketServer:
  env:
    LLM_API_BASE: "https://llama-32-1b.../v1"
    LLM_API_KEY: "<placeholder>"
    LLM_MODEL_NAME: "meta-llama/Llama-3.2-1B-Instruct"
    LLM_PROMPT_TEMPLATE: 'Answer the question: "{transcript}"\n\nAnswer concisely.'
    ASR_SERVER_ADDRESS: "parakeet-asr-api-predictor-00004-deployment.andrew-mendez-184335fb.svc.cluster.local:50051"
    ASR_USE_SSL: "false"
    ASR_LANGUAGE_CODE: "en-US"
    TTS_SERVER_ADDRESS: "tts-api-predictor-00002-deployment.andrew-mendez-184335fb.svc.cluster.local:50051"
    TTS_USE_SSL: "false"
    TTS_VOICE: "Magpie-Multilingual.EN-US.Sofia"
    TTS_LANGUAGE_CODE: "en-US"
    TTS_SAMPLE_RATE_HZ: "44100"

gradioUi:
  env:
    WEBSOCKET_URI: "ws://localhost:8765"
    ASR_SERVER_ADDRESS: "parakeet-asr-api-predictor-00004-deployment.andrew-mendez-184335fb.svc.cluster.local:50051"
    TTS_SERVER_ADDRESS: "tts-api-predictor-00002-deployment.andrew-mendez-184335fb.svc.cluster.local:50051"
    LLM_PROMPT_TEMPLATE: 'Answer the question: "{transcript}"\n\nAnswer concisely.'
    TTS_VOICE: "Magpie-Multilingual.EN-US.Sofia"
    TTS_LANGUAGE_CODE: "en-US"
    TTS_SAMPLE_RATE_HZ: "44100"
```

---

### 7) Sanity checks

* **ASR/TTS Pod has no Istio sidecar**

  ```bash
  kubectl -n <ns> get pod -l app=<your-app> -o json | jq -r '.items[].spec.containers[].name'
  # Should NOT list "istio-proxy"
  ```
* **Service resolves in-cluster**

  ```bash
  kubectl -n <ns> get svc <asr-svc-name> -o wide
  ```
* **LLM base reachable** (from any Pod with curl):

  ```bash
  curl -s -H "Authorization: Bearer $LLM_API_KEY" https://<your-mlis-llm-endpoint>/v1/models
  ```

---

### 8) Install / upgrade

```bash
helm upgrade --install audio-pipeline ./helm \
  -n <your-namespace> --create-namespace
```

If youre using the built-in Istio VirtualService:

* Set `ezua.virtualService.endpoint` and ensure `${DOMAIN_NAME}` is defined in your environment (or hardcode the FQDN in `values.yaml`):

  ```yaml
  ezua:
    enabled: true
    virtualService:
      endpoint: "riva-websocket.${DOMAIN_NAME}"
      istioGateway: "istio-system/ezaf-gateway"
  ```