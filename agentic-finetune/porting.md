# porting.md

Design journal for the agentic-finetune-v2 framework. Each entry captures
what shipped, what broke, and why we chose what we chose. New entries at the
top. For install and user-facing docs see **README.md**.

Current: **0.4.3** (chart + orchestrator + MCP server, all three move together)

---

## §25 — 0.4.3: inline size cap decoupled from parse

**Shipped:** chart-only patch over 0.4.2. Fixes a bug introduced during the
0.4.1 Pattern B addition.

### The bug

`register_dataset(source_uri="s3://agentic-adapters/uploads/xlam-60k.json")`
on a 125 MB file returned:

```
content is 125308940 bytes; the inline cap is 10485760 bytes (~10MB).
For larger datasets, use Pattern B (S3 URI) — coming in 0.4.1
```

Problem: Pattern B had already fetched the file under its own 500 MB cap
(0.4.2 bump). The S3-sourced content then flowed into `_parse_and_detect`,
which carried an unconditional 10 MB check copied from 0.4.0 — plus error
text referring to a feature that already shipped two versions ago.

### Root cause

`_parse_and_detect` was written in 0.4.0 as the only entry point for
content. 0.4.1 added Pattern B via `_fetch_from_s3` without noticing that
`_parse_and_detect` still enforced the inline cap. The misleading error
text ("Pattern B — coming in 0.4.1") further proved the function hadn't
been touched since 0.4.0.

### Fix

Move size enforcement out of `_parse_and_detect` and into the input
boundary where each path knows its own cap:

```python
# dataset_registry.py:register_dataset
if source_uri is not None:
    resolved_content = _fetch_from_s3(source_uri)  # own 500 MB cap
    source_type = "s3"
else:
    content_bytes = content.encode("utf-8")
    if len(content_bytes) > MAX_INLINE_BYTES:
        raise ValueError(
            f"inline content is {len(content_bytes)} bytes; the inline "
            f"cap is {MAX_INLINE_BYTES} bytes (~10MB). For larger "
            "datasets, upload to MinIO and pass source_uri instead — "
            "that path accepts up to 500MB."
        )
    source_type = "inline"
```

`_parse_and_detect` now does one thing (parse) and takes bytes of any size.

### Tests

- Rewrote `test_parse_too_big` → `test_parse_no_longer_enforces_size_cap`:
  passes a 500 000-record blob through `_parse_and_detect`, expects
  success. Documents the new contract.
- Added `test_register_dataset_enforces_inline_cap`: proves the boundary
  check still rejects oversized inline content with a message pointing to
  `source_uri`.

Total: 148 tests (0.4.2 was 147).

### Meta note

The 0.4.2 test suite had `test_parse_too_big` passing — because the
check was testing the (then-correct) behavior of 0.4.0 code that 0.4.1
had made obsolete. Lesson for future refactors: when a function's
responsibility narrows, audit tests of the removed responsibility, not
just tests of the new one.

---

## §24 — 0.4.2: xLAM auto-transform, dataset_prep enhancements, HF SSL noise

**Shipped:** chart + both images. Four changes to close out the xLAM demo
work.

### Change 1 — xLAM auto-transform at registration

The problem: xLAM-style sharegpt data uses `function_call` as the
assistant role. LlamaFactory's SFT converter treats it as a "function"
role (even position), passes `broken_data` check, but the downstream
tokenization produces `_prompt=[], _response=[]` for every example.
Supervised processor drops every one with "Dropped invalid example: []"
and training aborts with "Cannot find valid samples."

Debugged over five rounds (role tags → assistant tag → data transform →
HF cache → split file discovery). The actual fix is a data transform:
rewrite `function_call → gpt` so the data looks like standard sharegpt.
Content stays as a JSON string; the model learns to emit JSON as text,
which is what tool-calling models do anyway. The inference-time harness
(vLLM's tool-call extraction) parses the JSON from the text output.

Framework now detects xLAM shape (`function_call` present, `gpt` absent)
in the first 100 records and auto-transforms:

```python
def _needs_xlam_transform(records: list[dict]) -> bool:
    has_function_call = False
    has_gpt = False
    for record in records[:100]:
        for turn in record.get("conversations", []):
            role = turn.get("from")
            if role == "function_call": has_function_call = True
            elif role == "gpt": has_gpt = True
    return has_function_call and not has_gpt
```

Response gains `transform_applied: str | None`. Agent surfaces it to the
customer verbatim: *"Registered 60000 records. Note: auto-rewrote
function_call turns to gpt for LlamaFactory compatibility."*

### Change 2 — dataset_prep idempotency via mtime check

The trap from the xLAM debug: `dataset_prep` created
`<name>_train.json` and `<name>_eval.json` at prep time. The sidecar
`<name>_split.meta.json` recorded `(eval_fraction, seed)`. If those
matched, prep no-op'd — even if the source file had been modified since.

Hours were lost patching `/workspace/data/custom/xlam-full.json`
in-place and relaunching training that still read stale split files.

Fix: add mtime check to the idempotency chain. Precedence (highest
first):
1. `overwrite=True` → regenerate (explicit)
2. Params in sidecar don't match this call → regenerate (`params_changed`)
3. Any artifact missing → regenerate (`not_prepared`)
4. Source file mtime > min(split files mtime) → regenerate
   (`source_newer`)
5. Otherwise → no-op (`already_prepared: true`)

Sidecar version bumped 1 → 2 to carry `n_samples`. Older sidecars
regenerate automatically via (2).

Response gains `regenerated_reason` so the agent can tell the user
what actually happened and why.

### Change 3 — flexible N-sample subsetting

User-visible: `dataset_prep` now takes optional `n_samples: int` and
`eval_pct: float`. Deterministic subset (first N records), configurable
split. Enables the "register once, prep many sizes" workflow:

```
register_dataset(name="xlam-full", source_uri=...)   # 60k records
dataset_prep(name="xlam-full", n_samples=1000)       # quick demo
dataset_prep(name="xlam-full", n_samples=5000, eval_pct=0.2)  # bigger run
```

The second call auto-regenerates (`params_changed`), no manual
intervention. Response includes `n_samples_used` and `eval_pct_used`
echoing the actual values applied (useful when `n_samples > len(rows)`
and the framework silently clamps).

Random sampling was considered and dropped. First-N is deterministic
across runs and across machines, which matters for reproducibility more
than it loses to sample bias (and customers who care about the
distinction can shuffle their input).

### Change 4 — HF Hub SSL retry noise

Training logs filled with:

```
'[SSL: CERTIFICATE_VERIFY_FAILED]' thrown while requesting HEAD
https://huggingface.co/Qwen/Qwen2.5-3B-Instruct/resolve/main/processor_config.json
Retrying in 1s [Retry 1/5].
```

HF Hub does a HEAD check against each referenced file even when the
model is fully cached locally. The HPE cert bundle in the training
image can't verify huggingface.co, so each retry eats ~1 s × 5. Set
`HF_HUB_ETAG_TIMEOUT=1` to cap the retry attempts and
`HF_HUB_DISABLE_TELEMETRY=1` to suppress the analytics ping that also
hits the network. Baked into the training Job template env.

Didn't set `HF_HUB_OFFLINE=1` because a customer trying a model that
isn't yet cached locally would hit a hard fail; soft-fail with a
one-second ETag timeout is kinder.

### Install context

Chart 0.4.1 → 0.4.2. Orchestrator and MCP server images both bump to
0.4.2 (new tool signatures = agent needs refreshed cache in Open-WebUI).

Tests: 126 → 147 (+21).

---

## §23 — 0.4.1: Pattern B (S3 URI) for register_dataset

**Shipped:** chart + both images. Open-WebUI 0.8.12 rejected JSON file
uploads with "File type json is not allowed" — forced Pattern B earlier
than planned.

### What changed

`register_dataset` becomes a discriminated union: exactly one of
`content` or `source_uri`. The agent passes `source_uri` when the file
is already in MinIO; the framework fetches it under its own cap (100 MB
in 0.4.1, bumped to 500 MB in 0.4.2).

Accepted shapes:
- `s3://agentic-adapters/<key>` (explicit bucket)
- `<key>` (bare key, bucket defaults to the framework's configured one)

Rejected shapes:
- `s3://<empty-bucket>/<key>` → `empty bucket segment`
- Keys containing `..` segments → `path traversal rejected`
- Keys that resolve to a missing or inaccessible object → specific error
  pointing at bucket + key (so the user knows what to fix)

Reuses the existing `minio-credentials` Secret and the boto3 client
pattern from `s3_export.py`. No new K8s resources, no new Secret
plumbing. Response gains `source_type: "inline" | "s3"` and a longer
`source_ref` audit field.

### Why not just fix Open-WebUI

Open-WebUI's file-type allowlist is version-dependent and sometimes
locked at the deployment level. Pattern B works regardless of UI
version. Also sidesteps a separate class of problem: even when uploads
work, some LLM backing the agent will try to summarize or truncate a
3 MB JSON attachment instead of passing it verbatim to the tool. S3 URI
is three dozen characters — no model garbles that.

### MinIO prefix convention

Customers upload under `uploads/`:
`s3://agentic-adapters/uploads/<file>.json`. This keeps customer data
visually separated from the framework's own artifacts
(`adapters/`, `merged/`). Not enforced by the code; just the
documented convention.

Tests: 117 → 126 (+9).

---

## §22 — 0.4.0: BYO datasets

**Shipped:** chart + both images. First release with customer-data
registration. Tools 18 and 19 (`register_dataset`, `deregister_dataset`)
plus an extension to `list_datasets`.

### Design decisions

1. **Unified registry.** Custom datasets land in the same
   `/workspace/data/dataset_info.json` that LlamaFactory reads for
   built-ins. `launch_training(dataset="<custom-name>")` works with
   zero special-casing. A separate
   `/workspace/data/custom/registry.json` tracks which entries came
   from `register_dataset` (safe to deregister) vs which are
   LF built-ins (protected).

2. **Format auto-detection.** First record's keys drive it. Alpaca if
   it has `instruction` + `output`; sharegpt if it has `conversations`
   (a list). Alpaca wins when both signals present. Everything else is
   a 400 that lists both expected schemas.

3. **Sharegpt extras auto-included.** If the first record has `system`
   or `tools` fields, they're added to the LF column mapping and
   `tags` block. Critical for tool-calling datasets like xLAM where
   the tools schema is what the model must condition on. (Note: the
   `tags` additions here didn't solve the xLAM training trap; that
   needed the data transform in 0.4.2.)

4. **10 MB inline cap.** Comfortably holds ~3000 xLAM-shape records.
   Enough for first-shot demos. Pattern B followed in 0.4.1 for the
   bigger scenarios.

5. **Explicit deregister only.** No TTLs, no auto-cleanup, no quotas.
   Operators retain full control; registered datasets persist across
   framework upgrades.

### What was NOT in scope

- Tenant isolation (single-namespace framework, no per-user data)
- Dataset versioning (customers can register `foo-v1`, `foo-v2`
  manually)
- Streaming / incremental appends (full replace only; dataset is a
  JSON array)
- Multi-file datasets (one JSON file per registered name)
- Validation beyond "parses as array of objects + format signal
  present"

Tests: 95 → 117 (+22).

---

## §21 — 0.3.5: endpoint batch inference, proxy fix

**Shipped:** chart-only.

### The debug saga

0.3.4 shipped `run_batch_inference` with the endpoint source mode.
Testing showed every endpoint call returning `Connection reset by peer`.
Debugged ten wrong hypotheses — MLIS token scope, Istio sidecar
timeouts, httpx client lifecycle, DNS resolution, TLS verification,
authority header, cluster-IP routing, service-mesh circuit breaker,
HPAs. All red herrings.

Fix found by diffing the full Job YAML of a failing pod against a
working one-liner curl pod:

```diff
-        - name: HTTPS_PROXY
-          value: http://hpeproxy.its.hpecorp.net:8080
-        - name: HTTP_PROXY
-          value: http://hpeproxy.its.hpecorp.net:8080
+        [none]
```

The chart's `trainingJob.env` inherited HPE corporate proxy env vars
for HuggingFace downloads during training. The batch-inference Job
template inherited the same env block. Those proxies can't route to
in-cluster service IPs (10.96.3.118 range), so every endpoint call was
tunneling through corp proxy and getting dropped.

### Fix

Explicit clear of HTTPS_PROXY / HTTP_PROXY / etc. in the
batch-inference endpoint Job template, plus defensive `os.environ.pop`
in the container entrypoint. The training Job keeps the proxy env vars
(it needs them to reach huggingface.co).

### Methodology lesson

Documented in learnings.md: **when two "similar" pods behave
differently, diff full YAML specs FIRST**. Everything else — theories
about Istio, token lifecycle, httpx — is downstream of actual pod
state. Corp proxy env vars were visible immediately in the diff; the
ten hypothesis rounds all cost time I could have skipped by looking at
the ground truth.

---

## §20 — 0.3.4: enhanced error-chain capture

**Shipped:** chart-only. Added error-chain propagation in
`run_batch_inference` endpoint mode so the Connection reset error
actually surfaced in the response instead of being swallowed as a
generic 500. This is what made §21 debuggable.

---

## §19 — 0.3.3: per-deployment token Secret

**Shipped:** chart + orchestrator image.

Previously the chart's single `mlis-token` Secret backed every endpoint
call. In practice MLIS deployments rotate tokens independently, so
teams want per-deployment Secrets like `mlis-token-qwen2.5-3b-alpaca`.

Added `token_secret_name` param to `run_batch_inference` endpoint
source. If present, orchestrator reads the Secret via K8s API at
request time and injects it into the batch-inference Job's config.
Falls back to the default `mlis-token` Secret via secretKeyRef env if
omitted. RBAC: orchestrator granted `get` on secrets in its namespace.

Documented quirk: MLIS token 403 error has two distinct messages:
- "failed to verify token" → stale, refresh it
- "not allowed to access the resource" → wrong scope, regenerate from
  MLIS with the right permissions

Design gap called out for 0.5.x: no deployment registry. Operators
still have to track which `token_secret_name` goes with which
deployment manually. A future `register_deployment` tool could unify
this.

---

## §18 — 0.3.2: run_batch_inference

**Shipped:** chart + both images. Tools 15-17.

`run_batch_inference` with a discriminated-union source:
- `source: "adapter"` + `adapter_path` → runs the adapter against local
  Qwen weights
- `source: "endpoint"` + `endpoint_url` → calls an MLIS-served endpoint

Three tools total: launch, status, read-results. Standard
launch/poll/read triplet that mirrors `launch_custom_eval`.

Output JSONL has one line per prompt: id, prompt, response, latency.
Summary JSON records the config and aggregate timing.

---

## §17 — 0.3.1: resolve_model_alias + merged-model export

**Shipped:** chart + both images.

Four new tools (`resolve_model_alias`, `list_registered_models`,
`export_merged_model_to_s3`, `get_merge_export_status`).

The important one: `export_merged_model_to_s3`. The 0.3.0
`export_adapter_to_s3` only pushed the LoRA adapter files, which MLIS
cannot deploy directly — vLLM wants a full model directory. 0.3.1 adds
the merge+export Job that runs
`llamafactory-cli export --model_name_or_path <base>
--adapter_name_or_path <adapter> --export_dir <merged>`, then uploads
the resulting ~6 GB directory to MinIO under a stable prefix.

Validated on cluster: Qwen2.5-3B merge in 117.5 s + 139.3 s upload. MLIS
can then deploy via Packaged Model with the S3 URI.

---

## §16 — 0.3.0: export_adapter_to_s3

**Shipped:** adapter-to-MinIO push. Discovered mid-release that this
isn't actually deployable to MLIS; MLIS wants a merged model. Kept the
tool as a reference / debugging aid; shipped the real path (merged
export) one release later.

Lesson: validate the deployment target BEFORE shipping an export tool
that references it. Would have saved a 0.3.0 → 0.3.1 iteration.

---

## §15 — 0.2.3 → 0.2.5: custom eval

**Shipped:** three closely-spaced iterations on custom eval. Tools
`launch_custom_eval`, `get_custom_eval_status`, `read_custom_eval_results`.

Three probe sets: `instruction_following_basic` (20 probes),
`tool_calling_basic` (20 probes), `reasoning_basic` (20 probes).
Final/aggregated: 60 probes, 19/20 pass on the Alpaca-tuned Qwen
baseline.

Permission bug in 0.2.3 caused `PermissionError: [Errno 13]`
on the probe-set output dir (orchestrator ran as `app`, couldn't write
to `/workspace/jobs/<job_id>/output/custom_eval` because the training
Job had created the parent dir with root ownership). Fixed by moving
custom eval output to `/workspace/custom_eval/<job_id>/<probe_set>/`
in 0.2.4 — orchestrator-owned path from the start.

---

## §14 — 0.2.2: perplexity in-training eval

**Shipped:** `run_eval` tool. Reads LlamaFactory's `eval_loss` from
checkpoint metadata and returns perplexity.

Caveats surfaced to the agent: perplexity on the held-out training
split is a convergence signal, not a quality metric. Especially for
narrowly-scoped data (xLAM function-call JSON), low perplexity can
reflect memorization. Always suggest `launch_custom_eval` afterward
for the quality question.

---

## Earlier (0.1.x → 0.2.1)

Covered in the pre-port design doc. Short version:
- 0.1.0: six-tool framework, inline Jobs, no idempotency
- 0.2.0: standardize on Job-per-task pattern, run-id scheme, PVC race
  fix (busybox pre-install Job with 30 s sleep for RWX StorageClass
  mount timing)
- 0.2.1: alias system for registered models

Three persistent principles from the early releases that still hold:
1. Never claim victory from `job.status.state: succeeded` alone —
   always read `outputs.jsonl` or equivalent artifact
2. Chart version strict `MAJOR.MINOR.PATCH` (AIE Import Framework
   requirement)
3. HPE corp proxy env vars must not leak into in-cluster HTTPS calls

---

## Open design questions for 0.5.x

- Deployment registry (unify MLIS deployment metadata + per-deployment
  Secrets)
- Multi-tenant isolation (currently single namespace, no per-user
  scoping)
- Dataset versioning (currently each name is a flat replace)
- Streaming large datasets (full-array JSON doesn't scale past a few
  hundred MB)
- Non-Qwen base models in evals (currently all probe sets assume Qwen
  chat template)
