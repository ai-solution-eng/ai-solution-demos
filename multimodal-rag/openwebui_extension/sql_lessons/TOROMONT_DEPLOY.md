# Toromont — End-to-End Enablement Runbook

Enables the self-improving SQL agent loop on the existing Toromont deployment.
**This is a deploy runbook — the code it references already exists; nothing
here requires rebuilding an image or changing the system prompt.**

Scope: the loop wraps the existing `sql-toromont` MCP server (unchanged) and
adds recall + distillation via the **Open WebUI filter** and two **RAG
datasets**. The deployed system prompt (`agent-system-prompt.md`) is **not
modified** — lessons are injected as an extra system message at runtime.

Time: ~15 minutes. Prerequisites: admin access to Open WebUI, the Toromont
RAG server URL (the one your `RAG_API_URL` already points at), and network
reachability to that RAG API from your machine.

---

## 0. What you're about to do (mental model)

```
OWUI turn ─► filter inlet ─► recall top-k from  sql-lessons (curated) ─► inject into prompt
                 │
                 ▼
           agent resolves via sql-toromont
                 │
                 ▼
OWUI turn ─► filter outlet ─► distill → sql-lessons-candidates   (automatic)
                 │
                 ▼
       you run promote.py  → candidate → sql-lessons (curated)  (gated)
```

Two datasets, because it's the poison guardrail:
- `sql-lessons` — **curated**; only the agent reads; only `promote.py` writes.
- `sql-lessons-candidates` — **staging**; only the filter writes; never read
  by the agent.

---

## 1. Pre-flight

1. Confirm the OWUI filter you already run has **memory distillation
   working** (i.e. `DISTILL_LLM_URL` / `DISTILL_LLM_MODEL` valves are set and
   memories are being written). The SQL-lesson distill reuses the **same**
   LLM config — if memory works, you're done with LLM wiring.
2. Find the RAG API base URL the filter uses. In OWUI → ⚙️ on the filter,
   read the `RAG_API_URL` value. That is the server the lessons will live on
   and the seeder must target.
3. From a shell where you can reach that URL, confirm reachability:
   ```bash
   curl -s "$RAG_API_URL/api/datasets" | head -c 200
   ```
   (You'll see the JSON dataset list, e.g. memory + test datasets.)

---

## 2. Create the datasets + seed (one-time)

From this repo (in `openwebui_extension/sql_lessons/`):

```bash
cd openwebui_extension/sql_lessons
RAG_API_URL=<the value from step 1.2> \
  python3 seed_sql_lessons.py --adapter toromont
```

What this does:
- creates `sql-lessons` (curated) and `sql-lessons-candidates` (staging) if missing,
- uploads 6 generic-core + 8 Toromont lessons into `sql-lessons`.

**Verify (must see):**
```
  created dataset 'sql-lessons'
  created dataset 'sql-lessons-candidates'
  uploaded 14 docs -> 'sql-lessons'
```
If it says `dataset already exists`, that's fine (idempotent) — but you may
want to re-run after manually confirming you want the seed appended.

Optional: protect the datasets with a password via `RAG_LESSONS_PASSWORD=...`
(also set `SQL_LESSONS_PASSWORD` in the filter to match).

---

## 3. Update the OWUI filter (the only code change)

1. OWUI → Admin Panel → Functions → open `Multimodal RAG Bridge`.
2. Replace the contents with the new `filter.py`
   (`MultimodalRAG/openwebui_extension/filter.py` — the copy to paste lives
   in the checkout; both repo mirrors are in sync).
3. Save.

**Set the valves** (⚙️ on the filter):

| Valve | Value |
|---|---|
| `SQL_LESSONS_ENABLED` | `true` (recall at inlet) |
| `SQL_LESSONS_DATASET` | `sql-lessons` |
| `SQL_LESSONS_DISTILL_ENABLED` | `true` (distill at outlet) |
| `SQL_LESSONS_CANDIDATES_DATASET` | `sql-lessons-candidates` |
| `SQL_LESSONS_PASSWORD` | only if you set one in step 2 |
| `DISTILL_LLM_URL` / `DISTILL_LLM_MODEL` | already set (memory) — verify |
| `SQL_LESSONS_RECALL_TOP_K` | `3` (fine as-is) |

**Verify:** in a chat with the Toromont model, ask a question that should
recall a lesson (e.g. "which technician travelled the most last year?").
The assistant's reply should be routed as usual — you won't see the lesson in
the visible chat (it's a system message). To confirm recall is firing, check
the filter logs for `SQL-lesson recall: N hit(s)`.

---

## 4. Verify distillation → candidates

Ask the model a genuinely new, SQL-resolving question (ideally one where it
runs a query and reports a number). Then confirm a candidate was written:

```bash
RAG_API_URL=<the value from step 1.2> python3 - <<'PY'
import httpx, json, os
url = os.environ["RAG_API_URL"].rstrip("/")
r = httpx.get(f"{url}/api/datasets/sql-lessons-candidates/documents")
print(json.dumps(r.json(), indent=2)[:800])
PY
```
(or just open the dataset in the RAG UI and look at `sql-lessons-candidates`.)

**Expected:** at least one doc with `"status": "candidate"` and
`"source": "openwebui:sql-lesson"`. If empty: the exchange was trivial
(under `SQL_LESSONS_DISTILL_MIN_REPLY_CHARS`), the distill LLM judged
"NOTHING", or the exchange had no positive evidence — all expected behavior.

---

## 5. Promote candidates → curated (gated, ongoing)

When candidates accumulate, run the promotion gate. **This is the only
non-automatic step** — it's the safety review.

```bash
# dry-run first (see what would promote / skip):
RAG_API_URL=... python3 promote.py --dry-run --auto

# real run, auto (evidence + near-dup gate):
RAG_API_URL=... python3 promote.py --auto

# or with LLM review:
RAG_API_URL=... python3 promote.py \
  --llm-url <distill-url> --llm-model <model>

# or interactive:
RAG_API_URL=... python3 promote.py --review
```

Schedule it (cron) if you trust the gate, or run it weekly manually. A ready-to-apply
K8s `CronJob` (+ tiny `Dockerfile`) lives in [`deploy/`](deploy/README.md) — see
that README for the build/apply commands and the `--dry-run`-first recommendation.

---

## 6. Measure improvement (optional but recommended)

Before you enable distillation, take a **baseline** on the held-out set, then
re-run after promotions and compare:

```bash
python3 eval/replay.py \
  --eval eval/sql-eval.jsonl \
  --executor "<cmd that runs your agent on a question>" \
  --judge-llm --llm-url <distill-url> --llm-model <model> \
  > eval/report-before.txt
# ...after promotions...
python3 eval/replay.py ... > eval/report-after.txt
```
Compare the `PASS` line. That's your "it's actually improving" number.

---

## 7. Rollback / turn off

- **Immediate stop:** set `SQL_LESSONS_ENABLED=false` and
  `SQL_LESSONS_DISTILL_ENABLED=false` in the filter valves → the loop stops
  reading and writing. Your system prompt, SQL server, and RAG server are
  unaffected.
- **To fully remove:** also delete the two datasets from the RAG dashboard,
  and (optionally) restore the old filter content.

Nothing else changes — no image, no helm, no system prompt.

---

## What you do NOT touch

- `sql-toromont` / SQLserver — unchanged.
- The RAG server image — unchanged (datasets are data, not code).
- `agent-system-prompt.md` (the deployed one) — unchanged.
- Any helm release — unchanged.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `dataset already exists` on seed | ran twice | fine; it's idempotent |
| No recall log line | `SQL_LESSONS_ENABLED=false` or wrong `RAG_API_URL` | check valve + url |
| No candidates appear | trivial exchange / distill "NOTHING" / distill LLM unreachable | check `DISTILL_LLM_URL`; try a long query answer; look at filter logs for `SQL-lesson distillation LLM call failed` |
| Recall returns stale data | lessons promoted to curated | just wait for next promote |
| `--demote-stale` no-op | counters not populated (no document-PATCH yet) | out of scope v1; safe to leave off |