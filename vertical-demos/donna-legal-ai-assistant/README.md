# Donna — Private Legal AI Assistant on HPE Private Cloud AI

A reference demo of **Donna**, a local-first legal work platform, running entirely on
**HPE Private Cloud AI (PCAI / AI Essentials)** as a BYOA / Import Framework app. Every
document, embedding, vector and model call stays inside the cluster — positioned as a
**private, self-hosted alternative to SaaS legal AI** (e.g. Harvey.ai), with no per-seat
licensing and multimodal retrieval over scans, tables and signatures.

## What it shows

- **Cited RAG over legal documents** — ask questions, get answers with citations back to the
  source file.
- **Multimodal retrieval with Qwen3-VL** — retrieve by *page image* (signatures, fee tables,
  flow diagrams) that text-only pipelines miss.
- **Fully in-cluster & auditable** — source files in MinIO, vectors in Qdrant, all inference on
  MLIS via LiteLLM. Nothing leaves PCAI (`log_prompts: false`, no egress).

## Architecture

```
Browser ──> donna-web (Next.js) ──> donna-api (FastAPI) ──┬─> MinIO   (s3://donna, original files)
                                                          ├─> Qdrant  (donna_chunks_4096, 4096-dim)
                                                          ├─> Postgres (metadata)
                                                          └─> LiteLLM ─> MLIS: chat glm-5.3-flash
                                                                               embed Qwen3-VL-Embedding-8B
                                                                               rerank Qwen3-VL-Reranker-8B
```

## Contents

| Path | What |
|---|---|
| `donna-0.3.4-pcai.tgz` | Helm chart (BYOA Import Framework) |
| `values.yaml` | Example values (MLIS/LiteLLM, Qdrant, MinIO, Istio) |
| `porting.md` | **Deploy guide** — prerequisites, config, import, and all known gotchas |
| `demo-script.md` | Timed demo run-of-show with backend-evidence commands |
| `demo-dataset/` | Synthetic matter M-2001 — text docs (`text/`) + image pages (`multimodal/`) |
| `notebooks/` | Smoke-test + VL multimodal retrieval cells |

## Quickstart

1. **Prereqs:** PCAI/AIE 1.10.x with LiteLLM (chat + `Qwen3-VL-Embedding-8B` + `Qwen3-VL-Reranker-8B`
   registered), Qdrant, MinIO. See `porting.md` §2.
2. **Configure** `values.yaml` — in-cluster DNS, `ezua.virtualService`, and pin
   `security.encryption_secret`.
3. **Import** via the PCAI dashboard → **Tools & Frameworks → Import Framework**: upload
   `donna-0.3.4-pcai.tgz`, paste your `values.yaml` overrides, and submit. No CLI. See `porting.md` §4.
4. **Two required post-install steps** (see `porting.md` §5): add the `api` alias Service, and
   do a clean DB migrate+seed. Then register a user and add the `demo-dataset/` files.
5. **Verify** the file → MinIO → Qdrant → cited-answer path with the commands in `porting.md` §6.

## Models

| Role | Model | Served via |
|---|---|---|
| Chat | `glm-5.3-flash` (or `deepseek-v4.1-flash`) | LiteLLM → MLIS |
| Embeddings | `Qwen/Qwen3-VL-Embedding-8B` (4096-dim, multimodal) | LiteLLM → MLIS |
| Reranker | `Qwen/Qwen3-VL-Reranker-8B` | LiteLLM → MLIS |

## Notes

- Multimodal image retrieval runs via the **direct VL path** (notebook) — Donna's UI ingestion
  embeds extracted *text*; productionising in-UI image retrieval is an ingestion change.
- All demo data is **synthetic and fictional** (Matter M-2001, Harbourline ↔ Northbridge).

> Chart `0.3.4`. See `porting.md` for the full deploy guide and the fixes folded in across 0.3.2–0.3.4.
