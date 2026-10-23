# Porting Donna to HPE Private Cloud AI (PCAI / AI Essentials) — chart 0.3.4

How to import **Donna** (a local-first legal work platform) as a **BYOA / Import
Framework** app on PCAI, wire it to native in-cluster model serving (MLIS via
LiteLLM), Qdrant and MinIO, and avoid the gotchas we hit bringing it up.

- **Chart:** `donna-0.3.4-pcai.tgz` (chart version `0.3.4`, appVersion `0.3.0`)
- **Images (3):** `caovd/donna-api`, `caovd/donna-web`, `caovd/donna-worker`
- **Validated against:** AIE Software 1.10.x, Istio `ezaf-gateway`, Kyverno
  `hpe-ezua/type=vendor-service`

---

## 1. Architecture on PCAI

Donna is three app pods plus three data stores, with all model inference on MLIS:

| Component | Runs as | Role |
|---|---|---|
| `donna-api` | Deployment (FastAPI) | REST API, retrieval, agentic chat, migrations/seed |
| `donna-web` | Deployment (Next.js) | UI; **server-side proxies `/v1/*` to the api** |
| `donna-worker` | Deployment (ARQ) | background jobs |
| Postgres | bundled (POC) | metadata: files ↔ matters ↔ users ↔ providers |
| Qdrant | in-cluster framework | embeddings/chunks (**collection auto-named by dim**) |
| MinIO | in-cluster framework | object store — original uploaded files (bucket `donna`) |
| Chat / embed / rerank | **MLIS via LiteLLM** | OpenAI-compatible `/v1/*` |

**Data path on upload:** file → object in `s3://donna` → text extracted + chunked
→ embedded (`/v1/embeddings`) → vectors in Qdrant; Postgres row ties them together.
Citations link back to the MinIO object.

---

## 2. Prerequisites on the cluster

1. **Images** built and pushed to a registry the cluster can pull (`pcai.image_registry`).
2. **LiteLLM** deployed and reachable at `http://litellm-helm.litellm.svc.cluster.local:4000/v1`
   with these models **registered** (names must match the chart values):
   - chat — e.g. `glm-5.3-flash` (or `deepseek-v4.1-flash`)
   - embeddings — `Qwen/Qwen3-VL-Embedding-8B` → must answer `POST /v1/embeddings`
   - rerank — `Qwen/Qwen3-VL-Reranker-8B` → must answer `POST /v1/rerank` (Cohere/Jina shape)
3. **Qdrant** at `http://qdrant.qdrant.svc.cluster.local:6333`.
4. **MinIO** at `http://minio.minio.svc.cluster.local:9000`, a `donna` bucket, access/secret keys.
5. Postgres/Redis: bundled by the chart for a POC (`postgres.deploy/redis.deploy: true`).

> **Serving note:** `Qwen3-VL-Embedding-8B` has no "official" OpenAI embeddings route in its
> model card, but served through LiteLLM it **does** answer `/v1/embeddings` (verified). If your
> LiteLLM returns 404/400 on embeddings or rerank, the model isn't registered for that route —
> fix it in LiteLLM config, not the chart.

---

## 3. Configure `values.yaml` (the blocks that matter)

```yaml
images: { api: caovd/donna-api, web: caovd/donna-web, worker: caovd/donna-worker }

app:
  api_host: donna-api            # must match the <release>-api Service
  public_url: "https://donna.${DOMAIN_NAME}"

security:
  encryption_secret: ""          # SEE §5.1 — pin this; blank = generated once & preserved on upgrade

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

The `pcai.*` block holds **import-UI hints only** — keep each mirror key in sync with its
top-level value.

---

## 4. Import via the PCAI Import Framework

Deploy Donna through the PCAI dashboard — **no CLI required**. The chart package
(`donna-0.3.4-pcai.tgz`) is pre-built; you only upload and configure it.

1. In the PCAI dashboard, open **Tools & Frameworks → Frameworks → Import Framework**.
2. Set **Framework name** = `donna`, **Namespace** = `donna`, pick a category and (optionally) a logo.
3. **Upload the Helm package:** `donna-0.3.4-pcai.tgz`.
4. **Provide the values** — paste/upload your configured `values.yaml` overrides from §3:
   in-cluster DNS (never IPs), `ezua.virtualService.endpoint` / `istioGateway`, the LiteLLM/Qdrant/
   MinIO endpoints, and the pinned `security.encryption_secret`.
5. Submit **Import**.

AIE then substitutes `${DOMAIN_NAME}`, applies the Kyverno `hpe-ezua/type=vendor-service` label,
and creates: 3 Deployments + Services, bundled Postgres/Redis, Secret, ConfigMap, and the Istio
VirtualService. The app appears under **Tools & Frameworks** with its endpoint
`https://donna.${DOMAIN_NAME}`.

**To update** (e.g. a new values change or a 0.3.5 package): re-import / use the framework's
**Upgrade** action in the same screen with the new `.tgz` — again, no CLI.

---

## 5. Required post-install steps & known gotchas

### 5.1 Pin the encryption secret (critical)
Stored model-provider API keys are encrypted with `USER_API_KEYS_ENCRYPTION_SECRET`. If it
changes, **every saved key becomes undecryptable and chat 500s**. 0.3.4 injects it from
`donna-secrets` and **preserves it across re-imports/upgrades** (lookup). For reproducibility across
reinstalls, set `security.encryption_secret` to a fixed 32+ char string. After a first deploy on
a fresh secret, **re-save the provider key once** in the UI.

### 5.2 Add the `api` alias Service (web proxy hardcodes `api`)
The Next.js web tier proxies to host **`api`**, ignoring `APP__API_*`. The api Service is
`donna-api`, so registration/login fail with `ENOTFOUND api` until you add an alias:
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

### 5.3 Migrations: one migrator, clean DB
On boot the api creates schema + seeds. Two gotchas:
- **api and worker can race** the migration → `duplicate key … alembic_version`. Scale the worker
  to 0 for the first clean boot.
- A **reused Postgres PVC** with a half-migrated schema aborts the seed ("serving health without
  seed") → registration 500. Reset for a clean run:
  ```bash
  kubectl -n donna scale deploy/donna-api deploy/donna-worker --replicas=0
  kubectl -n donna exec deploy/postgres -- psql -U donna -d postgres -c \
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='donna' AND pid<>pg_backend_pid();"
  kubectl -n donna exec deploy/postgres -- psql -U donna -d postgres -c "DROP DATABASE donna;"
  kubectl -n donna exec deploy/postgres -- psql -U donna -d postgres -c "CREATE DATABASE donna OWNER donna;"
  kubectl -n donna scale deploy/donna-api --replicas=1   # clean migrate+seed, then scale worker back to 1
  ```
  *(0.3.4 adds wait-for-postgres initContainers; a once-only migration Job is the 0.3.5 durable fix.)*

### 5.4 Collection is auto-named by dimension
The app ignores `vector.collection` and names the collection `donna_chunks_<dim>` — with
`Qwen3-VL-Embedding-8B` that's **`donna_chunks_4096`**. Confirm the live name:
```bash
curl -s localhost:6333/collections | jq -r '.result.collections[].name'
```

### 5.5 Model-call gotchas (LiteLLM → MLIS)
- **Reasoning models** (GLM/Qwen3) burn tokens thinking first — use `max_tokens >= 256` and read
  `content or reasoning_content`. `chat_template_kwargs:{enable_thinking:false}` at top level 500s
  on some stacks; pass via `extra_body` or omit.
- **Tool-call parser must match the model's output.** If the model emits XML
  `<function=…><parameter=…>`, use `--tool-call-parser qwen3_coder`, not `hermes` (which JSON-parses
  and throws, leaking the call as text).

### 5.6 Operational
- **Pod images have no shell/curl** (api, web, litellm, qdrant, minio). Use `kubectl port-forward` +
  curl/aws from your workstation, not `kubectl exec … curl`.
- **MinIO** needs **path-style + s3v4** addressing.

---

## 6. Verify the pipeline

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

A healthy run: upload a file → object appears in `s3://donna` → `POST /v1/embeddings 200` →
`donna_chunks_4096` point count rises → a chat question yields `POST /v1/matters/<id>/chat 200`
with a citation.

---

## 7. What changed in 0.3.4

- **Retrieval switched to Qwen3-VL** — `embed_model: Qwen/Qwen3-VL-Embedding-8B` (4096-dim) and
  `rerank_enabled: true` with `Qwen/Qwen3-VL-Reranker-8B`, both via LiteLLM.
- **Chat** → `glm-5.3-flash` (cleaner tool/response behavior than the earlier Qwen chat model).
- Values-only delta over 0.3.3; **templates unchanged**. 0.3.3 carried the durable fixes
  (pinned encryption secret, web readiness `/`, wait-for-postgres init, `api_host: donna-api`,
  VirtualService nil-guard). See §5 for the manual steps still outstanding for 0.3.5.
