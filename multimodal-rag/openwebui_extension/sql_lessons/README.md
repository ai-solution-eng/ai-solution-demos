# SQL-Lessons — self-improving SQL agent loop (generic)

A **domain-agnostic** loop that makes an LLM SQL agent improve from how its
queries resolve, exactly as the Toromont "who travelled the most" work did by
hand — but automatic.

> **Deploying?** Follow the step-by-step [TOROMONT_DEPLOY.md](TOROMONT_DEPLOY.md)
> (OWUI filter paste + two datasets + promote gate — no new image, no system
> prompt change). Everything below is the mechanism reference.

```
 resolve ──► distill ──► store(candidate) ──► promote ──► recall(next question)
```

- **Mechanism is generic.** Any data domain plugs in by dropping a seed file
  under `adapters/<name>/`; nothing in the loop is Toromont-specific.
- **Toromont is one adapter** (`adapters/toromont/`). Add your own by copying
  that directory and editing the lessons.

## Layout

```
seed/sql-lessons-seed.jsonl      generic core lessons (any domain)
adapters/<name>/<name>.jsonl     domain-specific lessons layered on the core
seed_sql_lessons.py              creates datasets + uploads core and/or adapters
sql_lessons.py                   standalone helpers (recall/inject/store/distill)
promote.py                       promotion gate (candidate -> curated) + staleness
deploy/                          K8s CronJob + Dockerfile to run promote on-cluster
eval/sql-eval.jsonl              held-out eval set (adapter-specific)
eval/replay.py                   replay harness to measure improvement
```

## The two datasets (guardrail)

| Dataset | Writes | Reads |
|---|---|---|
| `sql-lessons` (curated) | promotion gate only | agent recall |
| `sql-lessons-candidates` | distill step only | promotion gate |

The agent **only ever reads curated**; the loop **only ever writes
candidates**. This is the guardrail that stops self-improvement from poisoning
the prompt with unvalidated lessons.

## Seed it

```bash
# generic core only
RAG_API_URL=... python3 seed_sql_lessons.py

# generic core + Toromont domain lessons
RAG_API_URL=... python3 seed_sql_lessons.py --adapter toromont

# a different dataset name (for a different domain)
RAG_API_URL=... python3 seed_sql_lessons.py --dataset my-co-lessons --adapter myco
```

## Wire the OWUI filter

In the Open WebUI filter (MultimodalRAG/openwebui_extension/filter.py), set:

- `SQL_LESSONS_ENABLED = true` → inlet recalls top-k from `sql-lessons`
- `SQL_LESSONS_DATASET = <your dataset>` (default `sql-lessons`)
- `SQL_LESSONS_DISTILL_ENABLED = true` → outlet stores candidates
- `DISTILL_LLM_URL / DISTILL_LLM_MODEL` → the distillation LLM
- `SQL_LESSONS_PASSWORD` → password for the lesson datasets

## Add a new domain (adapter pattern)

1. `mkdir adapters/<name>`
2. Write `adapters/<name>/<name>.jsonl` — one JSON per line:
   `{text: <instruction>, kind, trigger, tables, tags, status: "curated"}`
3. Seed with `--adapter <name>`.
4. Point `SQL_LESSONS_DATASET` at it (or reuse `sql-lessons` for a shared store).

Lesson `kind` values: `schema-map` (intent→table/column mapping),
`perf-guard` (bounded windows/joins/limits), `resolution-pattern` (how to
resolve a class of question), `fail-fix` (concrete fix for a concrete error).

## Distillation gate (the poison check)

The outlet only emits lessons on **positive evidence**: the SQL executed and
the result was accepted/plausible, or a concrete fix for a concrete error.
Trivial exchanges emit `NOTHING`. Promotion candidate→curated is a separate
gated step, so candidates never reach the agent until validated.

## Promote (candidate → curated)

```bash
RAG_API_URL=... [RAG_LESSONS_PASSWORD=...] python3 promote.py \
    [--review]                # interactive y/N per candidate
    [--llm-url ... --llm-model ...]   # LLM review gate
    [--auto]                  # auto-promote (evidence gate + near-dup only)
    [--dup-threshold 0.90]    # near-dup vs curated semantic score
    [--dry-run]               # show what would happen, change nothing
    [--demote-stale]          # also demote curated lessons whose usage
                              # counters show repeated failures (staleness)
```

The gate: every candidate is checked for (a) the positive-evidence shape, then
(b) a near-dup search against the curated set (score ≥ threshold → skip), then
(c) a review decision (auto / LLM / human). Promoted docs are written to the
curated set with `status: curated`; processed candidates are deleted.

## Measure improvement (replay)

```bash
# baseline BEFORE lessons are seeded (or after each promotion batch):
python3 eval/replay.py --eval adapters/toromont/eval.jsonl \
    --executor "python3 run_agent.py --model X" \
    --judge-llm --llm-url ... --llm-model ... > eval/report-before.txt
# repeat AFTER promotions -> eval/report-after.txt, compare PASS rate.
```

Each adapter supplies its own eval set (the checks reference real tables). The
harness accepts `--executor` (a shell command that runs the agent on a
question) and judges with either `--judge-llm` or `--judge-manual`.

## Staleness / decay

`promote.py --demote-stale` reviews curated lessons by their `hits` /
`successes` counters (a lesson with ≥ `--min-hits` samples and
success-rate < `--min-success-rate` is demoted — schema drift, or a lesson
that stopped helping). Note: the recall path is read-only today; wiring the
filter to increment those counters needs a small RAG-server document-PATCH
endpoint (not yet implemented) — the demotion pass itself works whenever the
counters are present.

## Reference

Design doc: `../self-improving-sql-agent.md`.