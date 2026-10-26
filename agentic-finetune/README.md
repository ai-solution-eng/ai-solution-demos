# agentic-finetune

Agentic LLM fine-tuning on HPE Private Cloud AI. Register customer training
data, prep it, train a LoRA adapter, evaluate, merge, export, and run batch
inference — all through chat.

**Current version: 0.4.3** (chart + orchestrator + MCP server)

---

## What it does

The framework exposes **19 MCP tools** that let an LLM agent drive the whole
fine-tuning pipeline from a conversational UI (Open-WebUI). A customer or SE
describes what they want in plain language; the agent chains the right tools
to make it happen.

Backing stack:
- **LlamaFactory** for training (LoRA SFT, built-in evals, dataset discovery)
- **MinIO** for artifact storage (adapters, merged models, uploaded datasets)
- **MLIS** for serving merged models as endpoints
- **Orchestrator** FastAPI service that wraps everything into a stable REST API
- **MCP server** thin protocol adapter so Open-WebUI (or any MCP client) can
  reach the orchestrator

Everything runs on a single Kubernetes namespace on an AIE cluster with H100 /
H200 GPUs. Scheduled through standard K8s Jobs; no bespoke scheduler.

---

## Prerequisites

- HPE AI Essentials Software **1.10.0** or compatible
- Kubernetes **v1.32.x** (tested with v1.32.5-hpe1)
- One or more GPU nodes (H100 or H200)
- A ReadWriteMany StorageClass (tested with `gl4f-filesystem`)
- MinIO reachable from the orchestrator pod, with a bucket (default:
  `agentic-adapters`) and a `minio-credentials` Secret in the install namespace
- MLIS running on the same cluster if you want to serve merged models or run
  endpoint batch inference
- Open-WebUI (0.8.12 tested) with Admin access to register tool servers
- HPE corporate proxy env vars **NOT** inherited by in-cluster HTTPS calls —
  the chart handles this for its own pods, but your other workloads may need
  similar treatment

---

## Install

**1. Build and push images** (replace `caovd` with your Docker Hub user):

```bash
tar -xzf agentic-finetune-images-0.4.3.tar.gz
cd images
DOCKERHUB_USER=caovd VERSION=0.4.3 make build-orchestrator push-orchestrator
DOCKERHUB_USER=caovd VERSION=0.4.3 make build-mcp-server push-mcp-server
```

**2. Upload the chart** `agentic-finetune-0.4.3.tgz` through the AIE UI
(Import Framework → Upload → Install into a dedicated namespace, typically
`agentic-finetune`).

**3. Wait for rollout:**

```bash
kubectl rollout status deploy/agentic-finetune \
  -n agentic-finetune --timeout=180s
```

**4. Register the MCP tool server in Open-WebUI** (Admin Panel → Settings →
Integrations → Manage Tool Servers → `+`) pointing at the service URL from
the chart (something like
`http://agentic-finetune.agentic-finetune.svc.cluster.local:8080/mcp`).
Enable the server.

**5. Paste the system prompt** into the model that will drive the agent
(see "System prompt" below for the current 0.4.3 version).

**6. Verify** with a trivial chat:

> Hi — list the available datasets.

The agent should call `list_datasets` and report the built-ins
(`alpaca_en_demo`, `alpaca_zh_demo`, `glaive_toolcall_en_demo`) plus any
custom datasets.

---

## The 19 tools

### Training (4)
| Tool | What it does |
|---|---|
| `list_datasets` | Lists all datasets LlamaFactory can see, annotated with `source: "built-in" \| "custom"` |
| `dataset_prep` | Partitions a dataset into train/eval splits. Accepts `n_samples`, `eval_pct`, `overwrite` (0.4.2+) |
| `launch_training` | Starts a LoRA SFT training Job against a prepared dataset |
| `get_job_status` | Polls a training Job's state and recent log tail |

### Evaluation (4)
| Tool | What it does |
|---|---|
| `run_eval` | In-training eval (perplexity on held-out split) |
| `launch_custom_eval` | Task-specific eval probe set (e.g. `tool_calling_basic`) |
| `get_custom_eval_status` | Polls a custom eval |
| `read_custom_eval_results` | Returns the probe-level pass/fail breakdown |

### Model registry (3)
| Tool | What it does |
|---|---|
| `register_model` | Registers a trained adapter under an alias |
| `resolve_model_alias` | Resolves an alias to the underlying job/path/base model |
| `list_registered_models` | Lists aliases known to the framework |

### Export (3)
| Tool | What it does |
|---|---|
| `export_adapter_to_s3` | Pushes the raw adapter files to MinIO (not deployable to MLIS as-is) |
| `export_merged_model_to_s3` | Merges adapter + base and pushes a deployable model directory to MinIO |
| `get_merge_export_status` | Polls the merge+export Job |

### Batch inference (3)
| Tool | What it does |
|---|---|
| `run_batch_inference` | Runs a model (adapter or MLIS endpoint) over a dataset. Discriminated-union source |
| `get_batch_inference_status` | Polls a batch run |
| `read_batch_inference_results` | Returns the output JSONL |

### Dataset registration (2, new in 0.4.0)
| Tool | What it does |
|---|---|
| `register_dataset` | Registers a customer-supplied JSON dataset. Pattern A inline (≤10MB) or Pattern B via MinIO `source_uri` (≤500MB in 0.4.2+) |
| `deregister_dataset` | Removes a custom dataset entirely |

---

## Canonical pipeline

Chain these when the user says "do the whole loop":

```
[optional] register_dataset(name, content | source_uri)
   ↓
list_datasets
   ↓
dataset_prep(name, [n_samples], [eval_pct])
   ↓
launch_training → get_job_status (poll)
   ↓
run_eval + register_model(alias)
   ↓
launch_custom_eval → get_custom_eval_status (poll) → read_custom_eval_results
   ↓
export_merged_model_to_s3 → get_merge_export_status (poll)
   ↓
tell user the s3_uri for MLIS deployment
   ↓
[optional] after MLIS deploy + per-deployment Secret created:
  run_batch_inference(endpoint, token_secret_name=...)
```

---

## System prompt

The system prompt that drives the agent lives in Open-WebUI's model
configuration (Workspace → Models → edit the model → System Prompt). The
current 0.4.3 version covers:

- Tool discipline (do NOT use k8s/kubectl tools to work around framework
  gaps; always relay framework errors verbatim)
- `REGISTERING CUSTOMER DATA` (Pattern A vs Pattern B, naming conventions,
  format auto-detection, `transform_applied` surfacing)
- `DATASET PREP — FLEXIBLE SUBSETTING` (`n_samples`, `eval_pct`, `overwrite`
  semantics)
- `MLIS TOKEN HANDLING` (default Secret vs per-deployment via
  `token_secret_name`)
- `BATCH INFERENCE SOURCE MODES` (adapter vs endpoint)
- `CANONICAL PIPELINE`

A reference copy ships at `docs/system-prompt.md` in the chart tarball.
After every framework upgrade that changes tool signatures (any 0.x.y bump
touching the MCP layer), **toggle the tool server off/on in Open-WebUI's
Admin → Settings → Integrations** to force a schema refresh. Chat sessions
started before the refresh will still see the old signatures.

---

## Validated demo — xLAM function-calling

End-to-end validated against **Beryex/xlam-function-calling-60k-sharegpt**
(60 000 single-turn tool-calling records derived from Salesforce/xLAM).

**Preparation** (one-time, on any machine with HuggingFace access):

```bash
# Produces xlam-function-calling-60k-sharegpt.json (~180 MB)
python3 prepare-xlam-demo-dataset.py --n 60000 \
  --out xlam-function-calling-60k-sharegpt.json

# Upload to MinIO
mc cp xlam-function-calling-60k-sharegpt.json \
  minio/agentic-adapters/uploads/
```

**Chat flow:**

> Register the xLAM dataset at
> `s3://agentic-adapters/uploads/xlam-function-calling-60k-sharegpt.json` as
> `xlam-full`. Prep 2000 samples with 15% eval. Train Qwen2.5-3B-Instruct,
> LoRA rank 8, 50 steps.

Expected agent behavior:
1. `register_dataset(source_uri=..., name="xlam-full")` → response includes
   `transform_applied: "xlam_function_call_to_gpt: rewrote 60000
   function_call turns to gpt for LlamaFactory compatibility"`. **This note
   must be surfaced to the user verbatim.**
2. `dataset_prep(name="xlam-full", n_samples=2000, eval_pct=0.15)` →
   `train_size: 1700, eval_size: 300`
3. `launch_training(...)` → ~2 minute training on H200
4. Final: `train_loss ≈ 0.033`, `eval_loss ≈ 0.034`, `perplexity ≈ 1.035`

Reference numbers from one validation run (2026-09-03):

| Metric | 1700 train / 300 eval | 54k train / 6k eval |
|---|---|---|
| Train loss | 0.033 | 0.157 |
| Eval loss | 0.034 | 0.036 |
| Runtime | 1 m 52 s | 17 m 07 s |
| Trainable params | 14.97 M | 14.97 M |

Low perplexity on the training-format eval is a **convergence signal, not a
quality guarantee**. Run `launch_custom_eval(eval_set="tool_calling_basic")`
afterward for an answer to "does this adapter emit valid tool calls on
unseen prompts?" — that's the number to quote.

---

## Troubleshooting

**Dataset trains with "Cannot find valid samples"** — usually a stale prep
artifact. In 0.4.1 and earlier, `dataset_prep` didn't detect source-file
changes and silently reused old splits. 0.4.2+ regenerates when the source
file's mtime is newer than the splits. If you suspect staleness on an older
install, pass `overwrite=true` to `dataset_prep` or delete
`/workspace/data/<name>_train.json` and `/workspace/data/<name>_eval.json`
manually.

**Agent doesn't pass new tool parameters** — Open-WebUI's MCP schema cache
is stale. Admin → Settings → Integrations → toggle the tool server off,
save, on, save. New chats will pick up the new signatures. Existing chats
need to be restarted.

**`register_dataset` rejects a legitimate file with "inline cap exceeded"**
— the file is over 10 MB. Upload it to MinIO instead and use `source_uri`
(500 MB cap).

**Training logs show `[SSL: CERTIFICATE_VERIFY_FAILED]` retrying 5×** — the
training container hits huggingface.co for processor config even when the
model is cached locally. 0.4.2+ sets `HF_HUB_ETAG_TIMEOUT=1` to cap the
retry loop. Harmless; training continues with cached config.

**MLIS endpoint batch inference returns `Connection reset by peer`** — HPE
proxy env vars are being inherited into the batch inference pod. 0.3.5+
strips `HTTPS_PROXY` / `HTTP_PROXY` in the Job template. Verify your
installed version with `curl /healthz`.

**MLIS token 403s** — "failed to verify token" means the Secret is stale
(refresh it). "not allowed to access the resource" means the Secret has the
wrong scope (regenerate from MLIS). Per-deployment Secrets rotate
independently of the default `mlis-token`.

For deeper design notes and the full version history, see **porting.md**.

---

## Compatibility notes

- Chart version strictly `MAJOR.MINOR.PATCH` (AIE Import Framework
  requirement — no metadata suffixes like `0.4.3-rc1`)
- Chart version always matches both orchestrator and MCP server image tags
  (they ship as a unit; do not mix versions across the three)
- Backward compatible back to 0.3.5 for datasets registered via
  `register_dataset`; older sidecar files (version 1) are auto-regenerated
  on first prep under 0.4.2+
