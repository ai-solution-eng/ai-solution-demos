# Multimodal RAG Bridge — Open WebUI Extension

A filter function for [Open WebUI](https://docs.openwebui.com/) that hands
unsupported modalities (images, video, audio) to the Multimodal RAG MCP
tool **without** injecting raw media into the LLM context window.

## Variants

This directory ships **two** filters — pick the one that matches your setup:

| File | Media routing | Long-term memory | Use when |
|------|---------------|------------------|----------|
| [`filter.py`](filter.py) | Full (stage → MCP hint, `STRIP_MODELS` per-model) | Yes (recall + distillation + SQL lessons) | You want RAG-backed media routing **and** long-term memory / SQL lessons (valve-gated) |
| [`filter_media_strip.py`](filter_media_strip.py) | **Strip-only** — removes image/video (and optionally audio) parts for text-only LLMs | No | Your text-only LLM errors on media; you don't want RAG staging at all |

> Memory and SQL-lesson features are **valve-gated** in `filter.py`, so a
> dedicated "no memory" fork is no longer shipped — disable
> `MEMORY_ENABLED` / `SQL_LESSONS_ENABLED` / `SQL_LESSONS_DISTILL_ENABLED`
> for a pure media-routing filter. (Formerly `filter_no_memory.py`.)

Both remaining filters declare `file_handler = True` so they take control
of file processing. The rest of this document describes the full
`filter.py`.

## Problem

Open WebUI blocks requests containing modalities (images, video, audio)
that the target LLM doesn't support. Even when uploads succeed with a
capable model, the raw media data (e.g. base64-encoded images) is injected
into the LLM context window, consuming valuable tokens.

This extension solves that by **deferring to the RAG MCP tool** instead of
embedding media (or preemptively retrieved RAG text) in the context:

1. **Allows uploads** of any modality without requiring the LLM to support
   them natively.
2. **Stages the media** on the RAG API's staging endpoint and injects only
   a short `file://` URL hint plus the dataset name — **no base64** in the
   LLM context.
3. Lets the **LLM call the MCP tools itself** with that URL — `describe_media`
   to analyse the media, `transcribe_audio` to transcribe it, or
   `search_dataset` for similar content — so results arrive as tool results
   (not as silently injected context).

If the RAG MCP is not enabled (`DEFER_TO_MCP = false`), the filter simply
warns the user and strips the unsupported modality.

## How It Works

```
User uploads files / pastes image  ──→  Open WebUI Filter (inlet)
                                            │
                              ┌─────────────┴─────────────┐
                              │                           │
                      Files/media found            No files → pass through
                              │
             ┌─────────────────┼──────────────────┐
             │                 │                  │
        Media files        Text files         Binary files
       (img/video/audio)   (.txt/.md/...)     (PDF/docx/...)
             │                 │                  │
             ▼                 ▼                  ▼
   DEFER_TO_MCP?          Read content       Include filename
      │   │               directly           as reference
   true   false                │                  │
      │   └→ warn + strip      │                  │
      │       (no RAG)         │                  │
      ▼                        │                  │
  POST /api/staging            │                  │
  (RAG API server)             │                  │
      │                        │                  │
      ▼                        │                  │
  file:// URL hint             │                  │
  + dataset name               │                  │
      │                        │                  │
      └─────────┬──────────────┘──────────────────┘
                │
                ▼
      Inject text hint + context
      (system or user message)
                │
                ▼
        LLM receives only text
                │
                ▼ (if DEFER_TO_MCP)
   LLM calls search_dataset MCP tool
   with the staged file:// URL
                │
                ▼
   Multimodal RAG API (MCP server reads
   the staged file from the shared PVC)
                │
                ▼
   Retrieval results returned to the LLM
   as a tool result
```

### What happens to different file types

| File type | Behavior |
|-----------|----------|
| **Image** (jpg, png, gif, etc.) | Staged on the RAG API; a `file://` URL hint is injected so the LLM can call `search_dataset` (when `DEFER_TO_MCP=true`). Stripped + warned when `DEFER_TO_MCP=false`. |
| **Video** (mp4, webm, etc.) | Same as image. |
| **Audio** (mp3, wav, flac, etc.) | Same as image. |
| **Text** (txt, md, json, etc.) | Read directly and included as inline context. |
| **Other** (PDF, docx, etc.) | Filename included as a reference (full text extraction requires additional libraries). |

## Prerequisites

- Open WebUI **v0.3.0+** (for Functions/Filter support)
- A running [Multimodal RAG API server](../src/multimodal_rag/api_server.py)
  (for the staging endpoint) — ships with the standard helm chart
- The **Multimodal RAG MCP server** enabled and connected to the Open
  WebUI model, so the LLM can call `search_dataset`.
- The API and MCP containers must share the `DATA_PATH` PVC (they do in
  the default helm deployment: both mount `/data`), so the MCP server can
  read staged `file://` URLs directly.

## Installation

1. Open your Open WebUI admin panel.
2. Navigate to **Admin Panel → Functions**.
3. Click **+** (Add Function).
4. Paste the contents of `filter.py` into the editor.
5. Set the **Name** to `Multimodal RAG Bridge`.
6. Click **Save**.

The filter will appear in the Integrations menu (⚙️) and can be toggled
per-chat via the chip icon.

## Configuration

After installing, click the ⚙️ icon next to the filter to configure:

| Valve | Default | Description |
|-------|---------|-------------|
| `RAG_API_URL` | `http://rag-mcp-server-api.mm-rag-mcp.svc.cluster.local` | Base URL of the Multimodal RAG API (for the staging endpoint + dataset list) |
| `RAG_API_KEY` | `""` | API key sent as `X-RAG-Api-Key` on every RAG-API request; required when the server has `security.apiKey` set (the charts ship a default one) |
| `DATASET_NAME` | `default` | Fallback injected into the hint only if the filter can't fetch the live dataset list |
| `ROUTE_IMAGES` | `true` | Hand images off to the MCP tool (false = leave for a vision LLM) |
| `ROUTE_VIDEO` | `true` | Hand video off to the MCP tool |
| `ROUTE_AUDIO` | `true` | Hand audio off to the MCP tool |
| `STRIP_MODELS` | _(empty)_ | Comma-separated text-only model IDs/names where media is stripped. Models NOT listed keep media in context (vision LLMs). Empty = strip for all. |
| `DEFER_TO_MCP` | `true` | Stage media + inject URL hint so the LLM calls `search_dataset`. **false** = no staging (text-only warns+drops, vision passes through). |
| `STAGING_PATH` | `/api/staging` | Path on the RAG API server used to stage uploaded media |
| `MCP_TOOL_HINT` | _(default text)_ | Header prepended to the staged-media hint |
| `INJECT_AS_SYSTEM` | `true` | Inject as system message (vs. append to user) |
| `MAX_CONTEXT_CHARS` | `4000` | Max characters of injected context (hint + text files) |
| `REPAIR_MEDIA_URLS` | `true` | Repair garbled media URLs in the model's reply: match every `/api/datasets/{name}/files/{file}` URL, re-resolve the real staged/dataset path, and substitute the correct host + short-lived `?token=` so media renders |
| `CONTEXT_HEADER` | _(default text)_ | Header before injected context |
| `PRIORITY` | `0` | Filter priority (lower = runs first) |
| `MEMORY_ENABLED` | `true` | Enable long-term memory (recall + write) |
| `MEMORY_DATASET_PREFIX` | `owui-memory-` | Per-user datasets are named `{PREFIX}{user_id}` (derived from the OWUI `__user__` identity at runtime). Each user gets an isolated dataset with no per-user valve config. |
| `MEMORY_AUTO_CREATE` | `false` | If true, create a user's memory dataset (with their derived password) on first write when it doesn't exist. |
| `MEMORY_PASSWORD` | _(empty)_ | Shared password fallback for all per-user datasets. Used ONLY when `MEMORY_SECRET` is empty (name-based isolation). Server-side, never in LLM context. |
| `MEMORY_SECRET` | _(empty)_ | HMAC key for deriving a UNIQUE per-user password from the SSO-authenticated `__user__` identity. When set: crypto isolation (one leak exposes one user). When empty: falls back to shared `MEMORY_PASSWORD` (one leak exposes all users). See "Per-user isolation" below. |
| `MEMORY_RECALL_TOP_K` | `5` | Number of memories to recall at conversation start |
| `MEMORY_RECALL_FIRST_ONLY` | `true` | Recall only on the first user message (false = every turn) |
| `MEMORY_INJECT_AS_SYSTEM` | `true` | Inject recalled memories as a system message (vs. prepend to user) |
| `DISTILL_LLM_URL` | _(empty)_ | OpenAI-compatible base URL for the distillation LLM. Empty = writes disabled (recall still works). |
| `DISTILL_LLM_MODEL` | _(empty)_ | Model name for distillation (e.g. `deepseek-v4-flash`) |
| `DISTILL_LLM_API_KEY` | _(empty)_ | API key for the distillation LLM |
| `DISTILL_MIN_REPLY_CHARS` | `200` | Skip distillation for shorter assistant replies |
| `SQL_LESSONS_ENABLED` | `false` | Enable the self-improving SQL lesson loop (recall half): at inlet, recall the top-k curated lessons from `SQL_LESSONS_DATASET` and inject them as "follow if applicable" context before the agent writes SQL |
| `SQL_LESSONS_DATASET` | `sql-lessons` | Curated SQL-lesson dataset the agent recalls from. Written ONLY by the promotion gate (candidate → curated); never by the filter. |
| `SQL_LESSONS_PASSWORD` | _(empty)_ | Password for the SQL-lesson datasets (shared, not per-user). Sent as `X-Dataset-Password` to the RAG API; never injected into LLM context. |
| `SQL_LESSONS_RECALL_TOP_K` | `3` | Number of curated SQL lessons to recall at inlet |
| `SQL_LESSONS_INJECT_AS_SYSTEM` | `true` | Inject recalled SQL lessons as a system message (vs. prepend to user) |
| `SQL_LESSONS_DISTILL_ENABLED` | `false` | Enable the write half: after each turn, ask the distillation LLM to extract 0-3 candidate lessons and store them in `SQL_LESSONS_CANDIDATES_DATASET` (NEVER the curated set). |
| `SQL_LESSONS_CANDIDATES_DATASET` | `sql-lessons-candidates` | Write-only staging dataset for distilled SQL lesson candidates. Never read directly by the agent. |
| `SQL_LESSONS_DISTILL_MIN_REPLY_CHARS` | `200` | Skip SQL-lesson distillation for shorter assistant replies |

## Long-term Memory

In addition to multimodal media routing, the filter provides per-user
long-term memory of past conversations:

1. **Recall (inlet):** at the start of each conversation (first user
   message), the filter searches that user's memory dataset via the RAG
   REST API and injects the top-k relevant memories as context — so the
   LLM knows about past decisions, preferences, and gotchas without the
   user having to repeat them. Toggle with `MEMORY_RECALL_FIRST_ONLY`.
2. **Write (outlet):** after the LLM replies, the filter asks a separate
   distillation LLM (`DISTILL_LLM_*` valves) to extract any durable fact
   worth remembering from the exchange. If the LLM produces a memory (and
   doesn't respond `NOTHING`), it's stored in the user's memory dataset
   via the RAG REST API. The user sees nothing — no tool calls in chat,
   no password in context.

### Per-user isolation (multi-user OWUI, SSO)

OWUI filter Valves are **global** (admin-configured once, shared by all
users on the instance), so per-user passwords can't be configured
per-user in Valves. The filter instead derives **two** per-user secrets
from the SSO-authenticated `__user__` identity at runtime:

```
dataset_name = MEMORY_DATASET_PREFIX + sanitised(__user__.id)
             e.g. "owui-memory-a1b2c3d4"

password     = HMAC-SHA256(MEMORY_SECRET, __user__.id)[:18]   (base64url, 24 chars)
             e.g. "c82tY2vCJGCxRjTwr7MDxYxs"
```

Because OWUI populates `__user__` **after** SSO authentication, a user
cannot forge another user's `id` — the derivation is sound. Two isolation
layers, both server-side, neither in the LLM context:

**When `MEMORY_SECRET` is set (recommended):**
Each user gets a **unique, unpredictable** password derived from the
SSO-verified identity. This is **crypto isolation**: if one user's
password leaks, only that user's dataset is exposed. The admin sets one
random `MEMORY_SECRET` (the HMAC key); no per-user provisioning, no
registry.

**When `MEMORY_SECRET` is empty (fallback):**
All per-user datasets share the one `MEMORY_PASSWORD` from the Valves.
Isolation is **dataset-name-based only**: users can't see each other's
memories (different names), but if `MEMORY_PASSWORD` leaks, all users'
datasets are exposed.

| | `MEMORY_SECRET` set | `MEMORY_SECRET` empty |
|---|---|---|
| Dataset name | per-user (from `__user__.id`) | per-user (from `__user__.id`) |
| Dataset password | per-user (HMAC-derived, unpredictable) | shared (`MEMORY_PASSWORD`) |
| If password leaks | one user exposed | all users exposed |
| Admin setup | one `MEMORY_SECRET` string | one `MEMORY_PASSWORD` string |

### Setup (multi-user, SSO)

1. **Generate a random secret** for `MEMORY_SECRET` (e.g.
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`).
2. **Provision each user's dataset** — either:
   - **Automatically (recommended):** set `MEMORY_AUTO_CREATE = true`
     and the filter creates each user's dataset on their first
     memorable reply, using their HMAC-derived password. Zero per-user
     admin work.
   - **Manually:** for each OWUI user, compute their dataset name and
     password (same HMAC formula above), then create the dataset via
     the RAG HTML frontend with that password. Only needed if you
     disable `MEMORY_AUTO_CREATE`.
3. In the filter's ⚙️ settings, set:
   - `MEMORY_DATASET_PREFIX` (default `owui-memory-` is fine)
   - `MEMORY_SECRET` (your random secret from step 1)
   - `MEMORY_AUTO_CREATE = true` (unless provisioning manually)
4. Set `DISTILL_LLM_URL` / `DISTILL_LLM_MODEL` / `DISTILL_LLM_API_KEY`
   to a lightweight OpenAI-compatible LLM for distillation (any small
   fast model works — it just decides "is this worth remembering?").
5. Leave `MEMORY_ENABLED = true`. Recall starts immediately (returns
   empty for users whose dataset doesn't exist yet); writes start once
   the distillation LLM is configured (and create the dataset first if
   `MEMORY_AUTO_CREATE = true`).

> **Secret rotation:** changing `MEMORY_SECRET` re-derives all per-user
> passwords. Existing datasets (hashed with the old derived passwords)
> become inaccessible. To rotate, re-create each user's dataset or
> update each dataset's password via the REST API.

### How recall + write interact with the existing media routing

Memory recall runs **before** media processing in the inlet — the two
are independent. A single user message can trigger both a memory recall
(context injection) and media staging (MCP `search_dataset` hint). The
outlet runs after the reply and is completely separate from the inlet.

## Self-Improving SQL Lessons

In addition to media routing and per-user memory, the filter ships the
**SQL-lesson loop**: an agent that answers questions with SQL improves over
time from how its queries resolve — the same loop that was closed *by hand*
when writing a governed SQL system prompt, but automatic.

```
OWUI turn ─► inlet ─► recall top-k from  sql-lessons (curated) ─► inject into prompt
                 │
                 ▼
           agent resolves via SQL MCP (e.g. SQLhandler / sql-toromont)
                 │
                 ▼
OWUI turn ─► outlet ─► distill → sql-lessons-candidates      (automatic)
                 │
                 ▼
       you run promote.py  → candidate → sql-lessons (curated)  (gated)
```

### Two datasets (the poison guardrail)

| Dataset | Writes | Reads |
|---|---|---|
| `sql-lessons` (**curated**) | promotion gate only (`promote.py`) | the agent (recall) |
| `sql-lessons-candidates` (**staging**) | the filter (distill) | the promotion gate only |

The agent **only ever reads curated**; the loop **only ever writes
candidates**. This is what stops self-improvement from poisoning the prompt
with unvalidated lessons.

### Enabling

1. **Seed the datasets** (one-time, from the repo — creates both datasets and
   uploads the lessons; `--adapter <name>` layers a domain's lessons):
   ```bash
   RAG_API_URL=... python3 seed_sql_lessons.py --adapter toromont
   ```
2. **Filter valves:** `SQL_LESSONS_ENABLED = true` (recall at inlet) and
   `SQL_LESSONS_DISTILL_ENABLED = true` (distill at outlet). Set
   `SQL_LESSONS_PASSWORD` if the datasets are protected.
3. **Promotion** (non-automatic, gated): when candidates accumulate, run
   `promote.py` (see [`sql_lessons/TOROMONT_DEPLOY.md`](sql_lessons/TOROMONT_DEPLOY.md)
   for the full walk-through).

### What the loop does not do

- It does **not** change your system prompt — recalled lessons are injected
  as an extra system message at runtime.
- It does **not** write to the curated set from the filter — only the
  promotion gate does.
- It does **not** modify the SQL server or any container image.

### Full docs

The mechanism, lesson schema, promotion gate, eval harness, staleness, the
end-to-end deployment runbook, and the K8s CronJob deploy artifacts all live
**in this repo** under [`sql_lessons/`](sql_lessons/) (`sql_lessons/README.md`,
`sql_lessons/TOROMONT_DEPLOY.md`, `sql_lessons/deploy/`). The design rationale
is in `design/self-improving-sql-agent.md` in the workspace (not part of this
repo).

## Model Setup in Open WebUI

For the frontend to **allow** media uploads, the selected model must report
that it supports those modalities. You can configure this in:

**Admin Panel → Models → (your model) → Capabilities**

Set `vision: true` even if the actual LLM doesn't support vision. The filter
will intercept the images before they reach the LLM.

To let the LLM call the RAG MCP tool, attach the **Multimodal RAG MCP
server** to the model under **Admin Panel → Models → (your model) →
Connections / Tools** (the exact location depends on your Open WebUI
version). When MCP is attached, keep `DEFER_TO_MCP = true`.

### Text-only vs vision LLMs (`STRIP_MODELS`)

The filter can serve both text-only and vision LLMs from a single
installation. Set `STRIP_MODELS` to a comma-separated list of your
text-only model IDs or names:

| Model type | In `STRIP_MODELS`? | `DEFER_TO_MCP=true` | `DEFER_TO_MCP=false` |
|------------|-----|---------------------|----------------------|
| Text-only (e.g. Deepseek) | yes | Strip image → stage → hint → LLM calls `search_dataset` | Warn + drop image |
| Vision (e.g. Gemma) | no | **Keep image in context** + stage → hint → LLM sees image natively AND can call `search_dataset` | Image passes through to LLM natively (no RAG) |

**Example:** with `STRIP_MODELS = "deepseek-v4"`:
- Deepseek v4 → image stripped, staged, hint injected (text-only RAG)
- Gemma 31b → image stays in context AND is staged with hint (vision + optional RAG)

Both models can be used in the same Open WebUI instance with the same
filter — the filter checks `__model__` at runtime.

If your model natively supports some modality (e.g. text + images) and you
do **not** want to route those through RAG at all, set the matching
`ROUTE_*=false` to let them pass through unchanged.

## Usage

### With RAG MCP enabled (default)

1. **Toggle the filter on** in the chat (click the chip icon).
2. **Upload media** using the paperclip button or paste images directly.
3. **Ask your question** — the filter stages the media and injects a short
   URL hint (no base64). The LLM calls `search_dataset` with that URL when
   it needs context.

### With RAG MCP disabled (`DEFER_TO_MCP = false`)

1. Upload media as usual.
2. The filter warns you that the modality is unsupported and strips it
   from the request; the LLM only sees the text.

### Example (DEFER_TO_MCP = true)

**User uploads:** `photo.jpg` (a picture of a dog in a park)
**User asks:** "What breed is this dog?"

**With filter:**
1. Image bytes are uploaded to `POST /api/staging` on the RAG API.
2. The filter fetches the live dataset list from `GET /api/datasets`
   (6 datasets in the current deployment, e.g. `andrew-test-dataset`,
   `stacks-project`, ...).
3. The image is stripped from the LLM request; only a hint is injected:
   a staged-media block listing the exact `file://` URLs, the live dataset
   list, and the suggested `base_llm_modalities` — telling the LLM it can
   call `describe_media(media_url=...)`, `transcribe_audio(audio_url=...)`,
   or `search_dataset(image=..., dataset_name=...)` with those URLs.
4. The LLM picks the most relevant tool (and dataset) and calls it with
   the staged `file://` URL.
5. The MCP server reads the media from the shared PVC and returns the
   description / transcription / retrieved context (e.g. _"Image shows a
   Golden Retriever in a park..."_).
6. The LLM uses that tool result to answer.

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Media files not uploaded | Model lacks capability flags | Enable `vision` etc. in model config |
| No MCP tool call happens | MCP server not attached to the model | Attach the Multimodal RAG MCP server in model settings, keep `DEFER_TO_MCP=true` |
| "media skipped — DEFER_TO_MCP off" warning | `DEFER_TO_MCP=false` and no native support | Enable MCP + set `DEFER_TO_MCP=true`, or set `ROUTE_*=false` for a vision LLM |
| Staging upload fails | RAG API unreachable | Check `RAG_API_URL` and `STAGING_PATH` |
| MCP tool can't read `file://` URL | PVC not shared between API & MCP | Ensure both containers mount `DATA_PATH` at the same path (default helm chart does) |
| Staged files accumulate | Sweep runs probabilistically (~1 in 10 uploads) in the background | On the **RAG API server** set `STAGING_SWEEP_RATE` higher (or `STAGING_TTL` lower) — these are server env vars, not filter valves; files live under `DATA_PATH/staging/` |
| Hint shows only `DATASET_NAME` instead of full list | Filter couldn't reach `GET /api/datasets` | Check `RAG_API_URL` reachability and RAG API health |
| Upload shows error in UI | `file_handler` conflict with other filters | Check for conflicting filters |

## Architecture

```
┌──────────┐   uploads   ┌──────────────────┐  POST    ┌─────────────────┐
│  Open    │ ──────────→ │  Filter (inlet)  │ ────────→│  Multimodal     │
│  WebUI   │             │                  │ /staging │  RAG API        │
│  Chat    │ ←────────── │  • Strip media   │ ←────────│  (writes to PVC)│
└──────────┘   hint only │  • Read text     │ file://  └─────────────────┘
      │                  │  • Stage media   │               │
      │                  │  • Inject hint   │               ├─ Qdrant (vector store)
      │                  └──────────────────┘               ├─ Embedding Model
      │                       │                             ├─ VLM (vision→text)
      │                       │ tool call                   ├─ ASR (audio→text)
      │                       ▼                             └─ Reranker
   ┌──────────┐         ┌──────────────┐
   │  LLM     │ ──────→ │  RAG MCP     │ ── reads file://──→ (shared PVC)
   │ (text    │ ←────── │  server      │
   │  only)   │ result  │ search_      │
   └──────────┘         │  dataset()   │
                        └──────────────┘
```

The filter uses `file_handler = True` to take full control of file processing,
bypassing Open WebUI's built-in text-only RAG pipeline. This prevents garbage
output from trying to embed images/video/audio as text.
