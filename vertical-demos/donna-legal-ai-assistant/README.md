# Donna — Private Legal AI Assistant

| Owner                 | Name           | Email                    |
| ----------------------|----------------|--------------------------|
| Use Case Owner        | Mauro Barberis | mauro.barberis@hpe.com   |
| PCAI Deployment Owner | Daniel Cao     | daniel.cao@hpe.com       |

*Use case Owner: who brought (or created) this demo. Can be contacted to get more information*

*PCAI Deployment Owner: who helped to port the demo to PCAI. Can be contacted on deployment issues*

## Abstract

Law firms cannot send client matter files to public LLM APIs — privilege, confidentiality, and data-residency rules forbid it. **Donna** is a local-first, fail-closed legal work platform (a self-hostable Harvey.AI-style assistant) that keeps every document, embedding, vector and model call inside the customer's trust boundary. This demo ports Donna onto HPE Private Cloud AI (PCAI / AI Essentials) as a BYOA / Import Framework app and wires it entirely to on-cluster services — no public egress, no Ollama, no phone-home. It is positioned as a private, self-hosted alternative to SaaS legal AI (e.g. Harvey.ai), with no per-seat licensing and multimodal retrieval over scans, tables and signatures.

Outcomes:

- **Accelerates** legal drafting and matter review with a cited assistant that reads the firm's own vault (matters + libraries).
- **Provides** a fully private inference path — chat, embeddings and reranking are all served by **HPE MLIS** (via LiteLLM), not Ollama or a public vendor.
- **Enables** a repeatable BYOA pattern on PCAI: a custom app composed with **validated external frameworks** (MinIO, Qdrant) plus native platform services (MLIS, Istio, Kyverno).

Features:

- Cited **Legal Assistant** chat over matter vaults and firm libraries (citation chips → source snippet).
- **Multimodal retrieval with Qwen3-VL** — retrieve by *page image* (signatures, fee tables, flow diagrams) that text-only pipelines miss.
- **Retrieval-augmented ingestion** — chunks embedded with `Qwen/Qwen3-VL-Embedding-8B` (4096-dim), reranked with `Qwen/Qwen3-VL-Reranker-8B`, stored/queried in external **Qdrant**.
- **Hero LLM `glm-5.3-flash` on MLIS** for chat + specialist worker, via LiteLLM.
- **Fully in-cluster & auditable** — source files in MinIO, vectors in Qdrant, all inference on MLIS; nothing leaves PCAI (`log_prompts: false`, no egress).
- **PCAI-native import** — Helm framework imported from the dashboard, Istio VirtualService routing, Kyverno `hpe-ezua/type=vendor-service` labeling.

Recordings:

- *(to be recorded — short, customer-focused)*
- *(to be recorded — long, technical walkthrough)*

## Description

### Overview

Donna is a control-plane / data-plane app. The **API (FastAPI)** owns all policy — the UI never talks to models, object storage, or the vector DB directly (the Next.js web tier server-side proxies `/v1/*` to the api). A **worker (ARQ)** performs ingest / embed / agent runs. The web UI is reached through the cluster Istio gateway. All stateful dependencies are on-cluster: bundled Postgres + Redis (this chart), validated external frameworks **MinIO** (S3 object store) and **Qdrant** (vectors), with **MLIS via LiteLLM** serving all models.

```
Browser ──> donna-web (Next.js) ──> donna-api (FastAPI) ──┬─> MinIO   (s3://donna, original files)
                                                          ├─> Qdrant  (donna_chunks_4096, 4096-dim)
                                                          ├─> Postgres (metadata)
                                                          └─> LiteLLM ─> MLIS: chat glm-5.3-flash
                                                                               embed Qwen3-VL-Embedding-8B
                                                                               rerank Qwen3-VL-Reranker-8B
```

Component map:

| Component | Runs as | Role |
|---|---|---|
| `donna-api` | Deployment (FastAPI) | REST API, retrieval, agentic chat, migrations/seed |
| `donna-web` | Deployment (Next.js) | UI; server-side proxies `/v1/*` to the api |
| `donna-worker` | Deployment (ARQ) | background jobs |
| Postgres | bundled (POC) | metadata: files ↔ matters ↔ users ↔ providers |
| Qdrant | in-cluster framework | embeddings/chunks (collection auto-named by dim) |
| MinIO | in-cluster framework | object store — original uploaded files (bucket `donna`) |
| Chat / embed / rerank | MLIS via LiteLLM | OpenAI-compatible `/v1/*` |

Value-proposition mapping (why this demo exists):

| PCAI value proposition | How Donna proves it |
|---|---|
| LLMs on **HPE MLIS** instead of Ollama | `inference.base_url` → in-cluster LiteLLM → MLIS; `glm-5.3-flash` (chat/worker) + `Qwen3-VL-Embedding-8B` + `Qwen3-VL-Reranker-8B`. No Ollama pulled. |
| **External MinIO S3** as a validated framework | `s3.*` → imported MinIO; uploaded matter files land in the `donna` bucket. |
| **External Qdrant** as a validated framework | `vector.*` → imported Qdrant; embeddings in the `donna_chunks_4096` collection. |
| Any other validated framework / BYOA | App imported as a Helm framework via the dashboard; Istio VirtualService + Kyverno vendor-service labeling; bundled Postgres/Redis. |

### Workflow

*(Workflow diagram to be added)*

**Ingestion:** file → object in `s3://donna` (MinIO) → text extracted + chunked → embedded (`POST /v1/embeddings`) → vectors in Qdrant (`donna_chunks_4096`); a Postgres row ties file ↔ chunks ↔ matter ↔ user together, and citations link back to the MinIO object.

**Chat:** question → embed → Qdrant retrieval → rerank → `glm-5.3-flash` answers with citations → answer + citation chips rendered in the UI, each chip quoting the source span and linking back to the MinIO object.

Data landing points: source documents → **MinIO** `s3://donna`; embeddings → **Qdrant** `donna_chunks_4096`; metadata/sessions → **Postgres**; job queue → **Redis**; model calls → **LiteLLM → MLIS**.

## Deployment

### Prerequisites

**PCAI / AIE Software:** validated on **AI Essentials Software 1.10.x**, Istio `ezaf-gateway`, Kyverno `hpe-ezua/type=vendor-service`.

**GPUs & models (served on MLIS, registered in LiteLLM — names must match the chart values):**

| Role | Model | Notes |
|---|---|---|
| Chat | **`glm-5.3-flash`** (or `deepseek-v4.1-flash`) | Reasoning model — see Limitations for token/parser notes. |
| Embeddings | **`Qwen/Qwen3-VL-Embedding-8B`** | 4096-dim, multimodal; must answer `POST /v1/embeddings`. |
| Reranker | **`Qwen/Qwen3-VL-Reranker-8B`** | Must answer `POST /v1/rerank` (Cohere/Jina shape). |

> **Serving note:** `Qwen3-VL-Embedding-8B` has no "official" OpenAI embeddings route in its model card, but served through LiteLLM it **does** answer `/v1/embeddings` (verified). If your LiteLLM returns 404/400 on embeddings or rerank, the model isn't registered for that route — fix it in LiteLLM config, not the chart.

**Validated frameworks to install first (Tools & Frameworks):**

- **LiteLLM** — reachable at `http://litellm-helm.litellm.svc.cluster.local:4000/v1` with the three models above registered.
- **Qdrant** — reachable at `http://qdrant.qdrant.svc.cluster.local:6333`; Donna auto-creates its collection.
- **MinIO** — reachable at `http://minio.minio.svc.cluster.local:9000`; create the `donna` bucket and capture access key + secret. Path-style + S3v4 addressing required.

**Images:** build and push the three Donna images to a registry the cluster can pull (`pcai.image_registry`): `caovd/donna-api`, `caovd/donna-web`, `caovd/donna-worker`.

**Bundled by this chart** (POC): **Postgres** + **Redis** (`postgres.deploy` / `redis.deploy: true`).

**Cluster facts to collect:** Istio gateway (`istio-system/ezaf-gateway`), the `${DOMAIN_NAME}` that AIE substitutes at import, a StorageClass for the PVCs, the image registry, and a LiteLLM virtual key.

### Installation and configuration

#### 1. Configure `values.yaml` (the blocks that matter)

```yaml
images: { api: caovd/donna-api, web: caovd/donna-web, worker: caovd/donna-worker }

app:
  api_host: donna-api            # must match the <release>-api Service
  public_url: "https://donna.${DOMAIN_NAME}"

security:
  encryption_secret: ""          # SEE post-install step 3 — pin this; blank = generated once & preserved on upgrade

inference:
  base_url: "http://litellm-helm.litellm.svc.cluster.local:4000/v1"  # chat + embeddings
  api_key: "sk-..."              # LiteLLM virtual key
  chat_model:  "glm-5.3-flash"
  embed_model: "Qwen/Qwen3-VL-Embedding-8B"
  rerank_enabled: true
  rerank_model:    "Qwen/Qwen3-VL-Reranker-8B"
  rerank_base_url: "http://litellm-helm.litellm.svc.cluster.local:4000/v1"  # -> POST {url}/rerank

vector:
  endpoint: "http://qdrant.qdrant.svc.cluster.local:6333"
  collection: donna_chunks       # NOTE: app appends the embedding dim -> live name is donna_chunks_4096

s3:
  endpoint: "http://minio.minio.svc.cluster.local:9000"
  bucket: donna
  access_key: "..."; secret_key: "..."
  path_style: true               # MinIO requires path-style + s3v4

ezua:
  virtualService:
    endpoint: "donna.${DOMAIN_NAME}"       # ${DOMAIN_NAME} substituted by AIE at import
    istioGateway: "istio-system/ezaf-gateway"
```

The `pcai.*` block holds **import-UI hints only** — keep each mirror key in sync with its top-level value.

#### 2. Import via the PCAI Import Framework (no CLI)

The chart package (`donna-0.3.4-pcai.tgz`) is pre-built; you only upload and configure it.

1. In the PCAI dashboard, open **Tools & Frameworks → Frameworks → Import Framework**.
2. Set **Framework name** = `donna`, **Namespace** = `donna`, pick a category and (optionally) a logo.
3. **Upload the Helm package:** `donna-0.3.4-pcai.tgz`.
4. **Provide the values** — paste/upload your configured `values.yaml` overrides: in-cluster DNS (never IPs), `ezua.virtualService.endpoint` / `istioGateway`, the LiteLLM/Qdrant/MinIO endpoints, and the pinned `security.encryption_secret`.
5. Submit **Import**.

AIE then substitutes `${DOMAIN_NAME}`, applies the Kyverno `hpe-ezua/type=vendor-service` label, and creates: 3 Deployments + Services, bundled Postgres/Redis, Secret, ConfigMap, and the Istio VirtualService. The app appears under **Tools & Frameworks** with its endpoint `https://donna.${DOMAIN_NAME}`.

**To update** (a new values change or a 0.3.5 package): re-import / use the framework's **Upgrade** action in the same screen with the new `.tgz` — again, no CLI.

#### 3. Required post-install steps & known gotchas

**Pin the encryption secret (critical).** Stored model-provider API keys are encrypted with `USER_API_KEYS_ENCRYPTION_SECRET`. If it changes, **every saved key becomes undecryptable and chat 500s**. 0.3.4 injects it from `donna-secrets` and **preserves it across re-imports/upgrades** (lookup). For reproducibility across reinstalls, set `security.encryption_secret` to a fixed 32+ char string. After a first deploy on a fresh secret, **re-save the provider key once** in the UI.

**Add the `api` alias Service (web proxy hardcodes `api`).** The Next.js web tier proxies to host **`api`**, ignoring `APP__API_*`. The api Service is `donna-api`, so registration/login fail with `ENOTFOUND api` until you add an alias:

```yaml
apiVersion: v1
kind: Service
metadata: { name: api, namespace: donna }
spec:
  selector:
    app.kubernetes.io/name: donna
    app.kubernetes.io/instance: donna
    app.kubernetes.io/component: api
  ports: [ { name: http, port: 8080, targetPort: http } ]
```

`kubectl -n donna get endpoints api` must list the api pod IP. *(Fold into the chart in 0.3.5.)*

**Migrations: one migrator, clean DB.** On boot the api creates schema + seeds. Two gotchas:

- **api and worker can race** the migration → `duplicate key … alembic_version`. Scale the worker to 0 for the first clean boot.
- A **reused Postgres PVC** with a half-migrated schema aborts the seed ("serving health without seed") → registration 500. Reset for a clean run:

```bash
kubectl -n donna scale deploy/donna-api deploy/donna-worker --replicas=0
kubectl -n donna exec deploy/postgres -- psql -U donna -d postgres -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='donna' AND pid<>pg_backend_pid();"
kubectl -n donna exec deploy/postgres -- psql -U donna -d postgres -c "DROP DATABASE donna;"
kubectl -n donna exec deploy/postgres -- psql -U donna -d postgres -c "CREATE DATABASE donna OWNER donna;"
kubectl -n donna scale deploy/donna-api --replicas=1   # clean migrate+seed, then scale worker back to 1
```

*(0.3.4 adds wait-for-postgres initContainers; a once-only migration Job is the 0.3.5 durable fix.)*

**Collection is auto-named by dimension.** The app ignores `vector.collection` and names the collection `donna_chunks_<dim>` — with `Qwen3-VL-Embedding-8B` that's **`donna_chunks_4096`**. Confirm the live name:

```bash
curl -s localhost:6333/collections | jq -r '.result.collections[].name'
```

#### 4. Verify the pipeline

```bash
kubectl -n qdrant port-forward svc/qdrant 6333:6333 &
# vectors after ingesting one PDF (~6 chunks @ 4096-dim)
curl -s localhost:6333/collections/donna_chunks_4096 \
  | jq '{points:.result.points_count, dim:.result.config.params.vectors.size}'

kubectl -n minio port-forward svc/minio 9000:9000 &
export AWS_ACCESS_KEY_ID=$(kubectl -n donna get secret donna-secrets -o jsonpath='{.data.S3__ACCESS_KEY}'|base64 -d)
export AWS_SECRET_ACCESS_KEY=$(kubectl -n donna get secret donna-secrets -o jsonpath='{.data.S3__SECRET_KEY}'|base64 -d)
aws --endpoint-url http://localhost:9000 s3 ls s3://donna --recursive --human-readable

# model traffic (embeddings / chat / rerank all 200)
kubectl -n litellm logs -f deploy/litellm-helm | grep -E "/v1/(embeddings|chat/completions|rerank)"
```

A healthy run: upload a file → object appears in `s3://donna` → `POST /v1/embeddings 200` → `donna_chunks_4096` point count rises → a chat question yields `POST /v1/matters/<id>/chat 200` with a citation.

Then open `https://donna.${DOMAIN_NAME}/`, **Register** the first account (becomes admin), and upload the demo files to a matter.

## Running the demo

### Demo 1 — Private cited RAG over a matter (happy path)

1. Create a **Matter**, upload a sample contract/PDF via **(+)** — messages show "uploaded → ingested into matter RAG".
2. In **MinIO**, confirm the blob under `s3://donna`; in **Qdrant**, confirm points in `donna_chunks_4096` (see *Verify the pipeline* above).
3. Ask Legal Assistant a question about the document — the answer renders with **citation chips**; open a chip to see the quoted span.
4. Point out: chat from **`glm-5.3-flash` on MLIS**, embeddings from **`Qwen3-VL-Embedding-8B` on MLIS**, rerank from **`Qwen3-VL-Reranker-8B`**, retrieval from **Qdrant**, bytes from **MinIO** — zero public egress.

### Demo 2 — Fail-closed egress & platform integration

1. Show **Administration → System Tools**: egress tools (`web_search`, `http`, `mcp`) are **Off**; `bash` cannot be enabled.
2. With `inference.base_url` temporarily unset, show the assistant fails closed (no public fallback).
3. Show the imported resources carry `hpe-ezua/type=vendor-service` (`kubectl -n donna get deploy,svc,pvc --show-labels`) and that the Istio **VirtualService** routes `/v1/*` to the api and the rest to the web UI.

### Demo 3 — Multimodal page-image retrieval (direct VL path)

Retrieval by *page image* (signatures, fee tables, flow diagrams) runs via the **direct VL path** with `Qwen3-VL-Embedding-8B` — smoke-test and VL multimodal-retrieval notebook cells ship with the demo. Note that Donna's UI ingestion embeds extracted *text*; productionising in-UI image retrieval is an ingestion change (see Limitations).

## Limitations

- **`glm-5.3-flash` specifics are operator-verified.** Exact parameter count, weight/quant format, required vLLM version, and per-GPU VRAM footprint must be confirmed from the model's card at deploy time — they are not asserted here. If MLIS's default vLLM image predates the model's architecture, switch to a custom MLIS model and set parameters manually.
- **Reasoning models (GLM/Qwen3) burn tokens thinking first** — use `max_tokens >= 256` and read `content or reasoning_content`. `chat_template_kwargs:{enable_thinking:false}` at top level 500s on some stacks; pass via `extra_body` or omit. **Tool-call parser must match the model's output** — if the model emits XML `<function=…><parameter=…>`, use `--tool-call-parser qwen3_coder`, not `hermes` (which JSON-parses and throws, leaking the call as text).
- **Web proxy hardcodes the `api` host** — the alias Service from Installation step 3 is required until it is folded into the chart (0.3.5).
- **Migration race / dirty PVC** — scale the worker to 0 for the first boot; a reused Postgres PVC with a half-migrated schema needs the DB reset shown above. A once-only migration Job is the 0.3.5 durable fix.
- **Collection auto-naming** — the app appends the embedding dimension to the collection name (`donna_chunks_4096`); `vector.collection` is a hint only.
- **In-UI ingestion embeds extracted text** — multimodal image retrieval currently runs via the direct VL path (notebook); in-UI image ingestion is future work.
- **Pod images have no shell/curl** (api, web, litellm, qdrant, minio) — use `kubectl port-forward` + curl/aws from your workstation, not `kubectl exec … curl`.
- **Bundled Postgres/Redis are single-replica POC services** (one PVC each, no HA/backup). For production, set `deploy=false` and point at managed/HA instances.
- **Secrets in values** — move `s3.*` keys, `security.encryption_secret`, and the LiteLLM key into a Kubernetes Secret (or the PCAI secret flow) rather than plain values for anything beyond a POC.
- **NetworkPolicy stays disabled** for this demo: MLIS, MinIO, and Qdrant live in other namespaces, and the shipped policy only permits same-namespace + DNS egress. Author explicit cross-namespace rules before enabling it.
- **Demo data is synthetic and fictional** (Matter M-2001, Harbourline ↔ Northbridge).

---

> Chart `0.3.4` (appVersion `0.3.0`). Durable fixes folded in across 0.3.2–0.3.3: pinned encryption secret, web readiness `/`, wait-for-postgres init, `api_host: donna-api`, VirtualService nil-guard. 0.3.4 is a values-only delta: retrieval switched to **Qwen3-VL** (`Qwen3-VL-Embedding-8B` 4096-dim + `Qwen3-VL-Reranker-8B` via LiteLLM) and chat to **`glm-5.3-flash`**; templates unchanged.
