# Donna — Private Legal Work Assistant

| Owner                 | Name              | Email                              |
| ----------------------|-------------------|------------------------------------|
| Use Case Owner        | Mauro Barberis        | mauro.barberis@hpe.com                        |
| PCAI Deployment Owner | Daniel Cao          | daniel.cao@hpe.com                |

## Abstract

Law firms cannot send client matter files to public LLM APIs — privilege, confidentiality, and data-residency rules forbid it. **Donna** is a local-first, fail-closed legal work platform (a self-hostable Harvey.AI-style assistant) that keeps every document, embedding, and model call inside the customer's trust boundary. This demo ports Donna onto **HPE Private Cloud AI (PCAI)** as an imported (BYOA) Helm framework and wires it entirely to on-cluster services — no public egress, no Ollama, no phone-home.

Outcomes:

- **Accelerates** legal drafting and matter review with a cited assistant that reads the firm's own vault (matters + libraries).
- **Provides** a fully private inference path — the hero LLM is served by **HPE MLIS**, not Ollama or a public vendor.
- **Enables** a repeatable BYOA pattern on PCAI: a custom app composed with **validated external frameworks** (MinIO, Qdrant) plus native platform services (MLIS, Istio, Kyverno).

Features:

- Cited **Legal Assistant** chat over matter vaults and firm libraries (citation chips → source snippet).
- **Two object-store lanes** on external MinIO: a Matter vault (`{matter_id}/outputs/…` write lane) and a read-only Library shelf.
- **Retrieval-augmented** ingestion — embeddings stored/queried in external **Qdrant**.
- **Hero LLM `glm-5.3-flash` on MLIS** for chat + specialist worker; `bge-m3` on MLIS for embeddings.
- **Fail-closed egress** — `inference.base_url` empty by default; public endpoints blocked; `bash` forever forbidden.
- **PCAI-native import** — Helm framework with Istio VirtualService routing and Kyverno `hpe-ezua/type=vendor-service` labeling.

Recordings:

- *(to be recorded — short, customer-focused)*
- *(to be recorded — long, technical walkthrough)*

## Description

### Overview

Donna is a control-plane / data-plane app. The **API (FastAPI)** owns all policy — the UI never talks to models, object storage, or the vector DB directly. A **worker (ARQ)** performs ingest / embed / agent runs. The **web (Next.js)** UI is reached through the cluster Istio gateway. All stateful dependencies are on-cluster: bundled Postgres + Redis (this chart), and validated external frameworks **MinIO** (S3) and **Qdrant** (vectors), with **MLIS** serving the LLMs.

*Architecture diagram (To be added)*

Value-proposition mapping (why this demo exists):

| PCAI value proposition | How Donna proves it |
|---|---|
| LLMs on **HPE MLIS** instead of Ollama | `inference.base_url` → in-cluster MLIS; `glm-5.3-flash` (chat/worker) + `bge-m3` (embed). No Ollama pulled. |
| **External MinIO S3** as a validated framework | `s3.*` → imported MinIO; matter/library blobs land in MinIO buckets. |
| **External Qdrant** as a validated framework | `vector.*` → imported Qdrant; embeddings in the `donna_chunks` collection. |
| Any other validated framework / BYOA | App imported as a Helm framework; Istio VirtualService + Kyverno vendor-service labeling; bundled Postgres/Redis. |

### Workflow

*Workflow diagram (To be added)*

Data landing points: source documents → **MinIO** Matter bucket; embeddings → **Qdrant** `donna_chunks`; metadata/sessions → **Postgres**; job queue → **Redis**; produced work product (DOCX/XLSX/PPTX/CSV/PDF-form) → **MinIO** `{matter_id}/outputs/…` (Library bytes never overwritten).

## Deployment

### Prerequisites

**PCAI / AIE Software:** validated on **AI Essentials Software 1.10.0**.

**GPUs & models (served on MLIS, vLLM / OpenAI-compatible):**

| Role | Model | Notes |
|---|---|---|
| Chat + Specialist Worker | **`glm-5.3-flash`** (hero) | Import on MLIS; record the OpenAI-compatible base URL + model id. |
| Embeddings | **`bge-m3`** | Small footprint, multilingual, 8k context. Served as its own MLIS deployment. |

Target hardware: **4× RTX PRO 6000 (96 GB, 384 GB total)** or **4× H200 (141 GB, 564 GB total)**. Both comfortably serve the hero chat model + `bge-m3` with concurrency headroom; the H200 set leaves room to trial larger MoE models later. Confirm the exact `glm-5.3-flash` weight format / vLLM version and per-GPU footprint from its model card before sizing (see Limitations).

**Validated frameworks to install first (Tools & Frameworks):**

- **MinIO** — create two buckets/lanes: Matter (`donna`) and Library (`library_bucket`/`library_prefix`); capture access key + secret.
- **Qdrant** — reachable in-cluster; Donna auto-creates the `donna_chunks` collection.
- **HPE MLIS** — deploy `glm-5.3-flash` and `bge-m3`; capture the in-cluster OpenAI-compatible base URL.

**Bundled by this chart** (single-replica, POC): **Postgres 16** + **Redis 7** (each with a PVC). Set `postgres.deploy=false` / `redis.deploy=false` to use external/managed instances instead.

**Cluster facts to collect:** Istio gateway name + host, a StorageClass for the PVCs, and the image registry that the 3 Donna images are pushed to.

### Installation and configuration

1. **Build & push images** (no GitHub clone at install):

   ```bash
   docker build -f deploy/Dockerfile.api    -t <registry>/donna-api:0.3.0 .
   docker build -f deploy/Dockerfile.web    -t <registry>/donna-web:0.3.0 .
   docker build -f deploy/Dockerfile.worker -t <registry>/donna-worker:0.3.0 .
   docker push <registry>/donna-api:0.3.0 && docker push <registry>/donna-web:0.3.0 && docker push <registry>/donna-worker:0.3.0
   ```

2. **Import the chart** `deploy/pcai/donna-pcai.tgz` via **Tools & Frameworks → Import (Helm framework)**.

3. **Provide values** (overlay based on `values-pcai.yaml`; use in-cluster DNS, never IPs):

   ```yaml
   pcai:
     image_registry: "<registry>"
     image_tag: "0.3.0"
     istio: { enabled: true, host: "<donna-host>", gateway: "<ns>/<gateway>" }
     mlis:  { base_url: "http://<mlis-svc>.<mlis-ns>.svc.cluster.local/v1", model: "glm-5.3-flash" }
   inference:
     base_url: "http://<mlis-svc>.<mlis-ns>.svc.cluster.local/v1"
     chat_model: "glm-5.3-flash"
     embed_model: "bge-m3"
   s3:
     endpoint: "http://<minio-svc>.<minio-ns>.svc.cluster.local:9000"
     bucket: "donna"
     library_bucket: "donna-library"
     access_key: "<minio-access>"   # move to secret at install
     secret_key: "<minio-secret>"
   vector:
     endpoint: "http://<qdrant-svc>.<qdrant-ns>.svc.cluster.local:6333"
     collection: "donna_chunks"
   postgres: { deploy: true, password: "<strong-password>", storageClass: "<sc>" }
   redis:    { deploy: true, storageClass: "<sc>" }
   ```

4. **Install / verify:**

   ```bash
   helm install donna deploy/pcai/donna-pcai.tgz -n donna --create-namespace -f my-values.yaml
   kubectl -n donna get pods,svc,pvc
   ```

5. Open `https://<donna-host>/`, **Register** the first account (becomes admin), then confirm **Administration → Models** lists the MLIS-served models.

## Running the demo

### Demo 1 — Private cited RAG over a matter (happy path)

1. Create a **Matter**, upload a sample contract/PDF via **(+)** — messages show "uploaded → ingested into matter RAG".
2. In **MinIO**, confirm the blob under the Matter bucket; in **Qdrant**, confirm points in `donna_chunks`.
3. Ask Legal Assistant a question about the document — answer renders with **citation chips**; open a chip to see the quoted span.
4. Point out: the chat came from **`glm-5.3-flash` on MLIS**, embeddings from **`bge-m3` on MLIS**, retrieval from **Qdrant**, bytes from **MinIO** — zero public egress.

### Demo 2 — Fail-closed egress & platform integration

1. Show **Administration → System Tools**: egress tools (`web_search`, `http`, `mcp`) are **Off**; `bash` cannot be enabled.
2. With `inference.base_url` temporarily unset, show the assistant fails closed (no public fallback).
3. Show the imported resources carry `hpe-ezua/type=vendor-service` (`kubectl -n donna get deploy,svc,pvc --show-labels`) and that the Istio **VirtualService** routes `/api` to api and the rest to web.

## Limitations

- **`glm-5.3-flash` specifics are operator-verified.** Exact parameter count, weight/quant format, required vLLM version, and per-GPU VRAM footprint must be confirmed from the model's card at deploy time — they are not asserted here. If MLIS's default vLLM image predates the model's architecture, switch to a custom MLIS model and set parameters manually.
- **Bundled Postgres/Redis are single-replica POC services** (one PVC each, no HA/backup). For production, set `deploy=false` and point at managed/HA instances.
- **NetworkPolicy stays disabled** for this demo: MLIS, MinIO, and Qdrant live in other namespaces, and the shipped policy only permits same-namespace + DNS egress. Author explicit cross-namespace rules before enabling it.
- **Secrets in values** — move `s3.*` keys, `postgres.password`, and MLIS keys into a Kubernetes Secret (or the PCAI secret flow) rather than plain values for anything beyond a POC.
- **Single-node model serving** as scoped here; scale/quantization tuning for many concurrent lawyers is out of scope for the first version.
