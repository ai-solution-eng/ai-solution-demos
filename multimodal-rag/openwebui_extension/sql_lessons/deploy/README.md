# Deploy — SQL-lessons promotion gate (K8s CronJob)

Runs `promote.py` on the target cluster on a schedule, so the loop's
candidate→curated step happens without a human running it each time.

## Files

- `Dockerfile` — minimal image for `promote.py` (python:3.12-slim + httpx only).
- `cronjob.yaml` — the `CronJob` that runs `--auto` promotion against your
  Toromont RAG server and dataset names.

## Build & push the image (once)

```bash
cd openwebui_extension/sql_lessons
docker build -f deploy/Dockerfile -t <registry>/sql-lessons-promote:0.1.0 .
docker push <registry>/sql-lessons-promote:0.1.0
```

## Deploy the CronJob

1. Edit `deploy/cronjob.yaml`:
   - `image:` → your pushed image
   - `namespace:` → the target namespace (e.g. the one hosting `rag-mcp-server`)
   - `schedule:` → your cadence (default `0 3 * * 1` = Mondays 03:00 UTC)
2. Apply:

```bash
kubectl apply -f openwebui_extension/sql_lessons/deploy/cronjob.yaml -n <namespace>
```

3. **First runs: review before auto.** Change the args to add `--dry-run`
   and watch the log for a couple of runs:
   ```bash
   kubectl get cronjob sql-lessons-promote -n <ns>
   kubectl logs job/<last-job> -n <ns> --tail=50
   ```
   When it looks right, remove `--dry-run`.

## Config

| Item | Default in manifest | Notes |
|---|---|---|
| `RAG_API_URL` | `https://rag-mcp-server.tr7-0211-...` | in-cluster or via ingress; the job needs network to it |
| `--curated-dataset` | `sql-learnings` | matches your dataset name |
| `--candidates-dataset` | `sql-learnings-staging` | matches your staging name |
| `--auto` | on | evidence + near-dup gate; no review LLM needed |
| `RAG_LESSONS_PASSWORD` | none | only if the datasets are password-protected — mount from a Secret, never inline |

## Modes

- `--auto` — promote any candidate that passes the positive-evidence shape and
  the near-dup (semantic ≥ 0.90) check. Best default for cron.
- `--llm-url/--llm-model` — add an LLM review step; requires a reachable
  OpenAI-compatible endpoint and a token budget per run. Optional.
- `--review` — interactive; **not** suitable for cron (blocks waiting for
  input).
- `--demote-stale` — demote lessons whose usage counters show repeated
  failures; safe to leave off until counters are wired server-side.