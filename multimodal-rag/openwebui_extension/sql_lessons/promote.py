#!/usr/bin/env python3
"""Promotion gate for the self-improving SQL agent lesson loop.

Moves reviewed, validated lessons from the write-only **candidates** dataset
into the **curated** dataset the agent actually recalls. This is the only
path that writes to the curated set — the distillation loop never writes
there directly.

Flow:
1. Pull candidate lessons from `sql-lessons-candidates` (documents list).
2. Gate each candidate: positive evidence (ran + accepted, or a concrete fix)
   AND either auto-LLM-review (if `--llm-url`) or explicit `--accept`.
3. Near-dup check against the curated set (semantic search, score >= thresh).
4. Promote survivors -> curated (status: curated), delete from candidates.
5. Optionally demote stale curated lessons (hits/successes counters).

Usage:
    RAG_API_URL=... [RAG_LESSONS_PASSWORD=...] python promote.py \
        [--candidates-dataset sql-lessons-candidates] \
        [--curated-dataset sql-lessons] \
        [--llm-url https://vllm/v1 --llm-model model]   # auto-review
        [--dry-run] [--promote-all] [--dup-threshold 0.90]

Endpoints used (Multimodal RAG api_server.py):
  GET    /api/datasets/{name}/documents            list candidates
  POST   /api/datasets/{name}/documents            add curated docs
  DELETE /api/datasets/{name}/documents/{id}       drop candidate after promote
  GET    /api/datasets/{name}/search               near-dup check
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import httpx

CURATED = "sql-lessons"
CANDIDATES = "sql-lessons-candidates"
DEFAULT_DUP_THRESHOLD = 0.90

REVIEW_SYSTEM_PROMPT = (
    "You are the promotion gate for an SQL-lesson store. Given a candidate "
    "lesson distilled from a real agent resolution, decide whether it is "
    "safe and durable enough to promote into the curated instruction set "
    "that agents recall before writing SQL.\n\n"
    "Promote when it is: a durable intent-to-schema mapping, a performance "
    "guard, or a concrete fix for a concrete failure — stated as a "
    "standalone instruction that a future agent with no context would "
    "understand and follow correctly.\n\n"
    "Reject when it is: schema-specific in a way that would mislead other "
    "questions, too vague to act on, a one-off (not reusable), or would "
    "conflict with the existing curated lessons.\n\n"
    'Respond with exactly one line: "PROMOTE" or "REJECT". If you are '
    "uncertain, respond REJECT."
)


def api_base() -> str:
    url = os.environ.get("RAG_API_URL", "").rstrip("/")
    if not url:
        sys.exit("Set RAG_API_URL (e.g. https://rag.example.com) — required.")
    return url


def headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    pw = os.environ.get("RAG_LESSONS_PASSWORD", "").strip()
    if pw:
        h["X-Dataset-Password"] = pw
    return h


def list_candidates(client: httpx.Client, api: str, dataset: str) -> list[dict[str, Any]]:
    """Return [{id, text, kind, trigger, tables, tags, status, ...}]."""
    r = client.get(f"{api}/api/datasets/{dataset}/documents", params={"limit": 1000}, headers=headers())
    r.raise_for_status()
    body = r.json()
    out: list[dict[str, Any]] = []
    for entry in body.get("documents", []):
        doc = entry.get("payload", {}) or {}
        rec = {"id": entry.get("id"), "text": doc.get("text", "")}
        for key in ("kind", "trigger", "tables", "tags", "status", "source"):
            rec[key] = doc.get(key, "")
        out.append(rec)
    return out


def upload_curated(client: httpx.Client, api: str, dataset: str, docs: list[dict[str, Any]]) -> int:
    if not docs:
        return 0
    r = client.post(f"{api}/api/datasets/{dataset}/documents", json=docs, headers=headers())
    r.raise_for_status()
    return r.json().get("count", len(docs))


def delete_candidate(client: httpx.Client, api: str, dataset: str, doc_id: str) -> None:
    r = client.delete(f"{api}/api/datasets/{dataset}/documents/{doc_id}", headers=headers())
    r.raise_for_status()


def search_curated(client: httpx.Client, api: str, dataset: str, text: str, top_k: int = 5) -> list[dict[str, Any]]:
    r = client.get(
        f"{api}/api/datasets/{dataset}/search",
        params={"q": text[:500], "top_k": top_k},
        headers=headers(),
    )
    r.raise_for_status()
    return r.json().get("results", [])


def near_dup_score(client: httpx.Client, api: str, curated_ds: str, text: str) -> float:
    results = search_curated(client, api, curated_ds, text)
    if not results:
        return 0.0
    return max(float(r.get("score", 0.0)) for r in results)


def llm_review(
    url: str, model: str, api_key: str, candidate: dict[str, Any]
) -> bool:
    """Ask the review LLM whether to promote; True = PROMOTE."""
    content = (
        f"kind: {candidate.get('kind', '')}\n"
        f"content: {candidate.get('text', '')}\n"
        f"trigger: {candidate.get('trigger', '')}\n"
        f"tables: {candidate.get('tables', '')}\n"
        f"tags: {candidate.get('tags', '')}"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "max_tokens": 16,
        "temperature": 0.0,
    }
    hdrs = {"Content-Type": "application/json"}
    if api_key:
        hdrs["Authorization"] = f"Bearer {api_key}"
    try:
        r = httpx.post(
            f"{url.rstrip('/')}/chat/completions",
            json=payload,
            headers=hdrs,
            timeout=30.0,
        )
        r.raise_for_status()
        verdict = r.json()["choices"][0]["message"]["content"].strip().upper()
        return verdict.startswith("PROMOTE")
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] LLM review failed for candidate: {exc}")
        return False


def is_candidate_positive(candidate: dict[str, Any]) -> bool:
    """Positive evidence gate (mirrors the filter). Candidates from the
    distillation step are positive by construction (the prompt only emits on
    evidence), so this is a defensive check on the stored shape."""
    return bool(candidate.get("text")) and candidate.get("kind") in (
        "schema-map",
        "perf-guard",
        "resolution-pattern",
        "fail-fix",
        "data-shape",
    )


def demote_stale(
    client: httpx.Client,
    api: str,
    dataset: str,
    min_hits: int = 3,
    min_success_rate: float = 0.5,
    dry_run: bool = True,
) -> list[str]:
    """Find curated lessons whose usage counters suggest staleness and delete
    them (schema drifted, or the lesson stopped helping).

    Reads `hits`/`successes` counters stored on curated docs. A lesson is
    demoted when it has enough samples (hits >= min_hits) but a low success
    rate (successes/hits < min_success_rate). Returns the list of deleted
    doc ids. Always require evidence, never delete an untested lesson.

    NOTE: the OWUI filter currently only stores candidates; usage counters are
    NOT yet incremented on recall (that is a filter change in increment 4).
    This function is the demotion *pass* — wire counter increments first.
    """
    r = client.get(f"{api}/api/datasets/{dataset}/documents", params={"limit": 1000}, headers=headers())
    r.raise_for_status()
    to_demote: list[tuple[str, str, int, int]] = []
    for entry in r.json().get("documents", []):
        doc = entry.get("payload", {}) or {}
        if doc.get("status") != "curated":
            continue
        hits = int(doc.get("hits", 0) or 0)
        successes = int(doc.get("successes", 0) or 0)
        if hits < min_hits:
            continue  # not enough signal — leave it alone
        success_rate = successes / hits
        if success_rate < min_success_rate:
            summary = doc.get("text", "")[:80]
            to_demote.append((entry.get("id"), summary, hits, successes))

    if not to_demote:
        print(f"  no stale lessons found (need >= {min_hits} hits with < {min_success_rate:.0%} success)")
        return []
    for doc_id, summary, hits, successes in to_demote:
        if dry_run:
            print(f"  [dry-run] would demote '{summary}…' (hits={hits}, success={successes}/{hits})")
        else:
            delete_candidate(client, api, dataset, doc_id)
            print(f"  demoted '{summary}…' (hits={hits}, success={successes}/{hits})")
    return [t[0] for t in to_demote]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--curated-dataset", default=CURATED)
    parser.add_argument("--candidates-dataset", default=CANDIDATES)
    parser.add_argument("--dup-threshold", type=float, default=DEFAULT_DUP_THRESHOLD)
    parser.add_argument("--dry-run", action="store_true", help="print what would happen, change nothing")
    parser.add_argument(
        "--review",
        action="store_true",
        help="interactively confirm each candidate (stdin y/n) instead of auto-promoting",
    )
    parser.add_argument("--llm-url", default=os.environ.get("DISTILL_LLM_URL", ""))
    parser.add_argument("--llm-model", default=os.environ.get("DISTILL_LLM_MODEL", ""))
    parser.add_argument(
        "--llm-api-key",
        default=os.environ.get("DISTILL_LLM_API_KEY", ""),
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="promote candidates automatically (positive-evidence gate + near-dup only; "
        "use --llm-* for LLM review)",
    )
    parser.add_argument(
        "--demote-stale",
        action="store_true",
        help="also run the staleness pass: demote curated lessons whose usage "
        "counters show repeated failures (hits>=3 and success rate low)",
    )
    parser.add_argument("--min-hits", type=int, default=3, help="min hits before a lesson can be demoted")
    parser.add_argument(
        "--min-success-rate",
        type=float,
        default=0.5,
        help="lessons with successes/hits below this are demoted (default 0.5)",
    )
    args = parser.parse_args()

    api = api_base()
    curated_ds = args.curated_dataset.strip() or CURATED
    cand_ds = args.candidates_dataset.strip() or CANDIDATES

    with httpx.Client(timeout=60.0) as client:
        candidates = list_candidates(client, api, cand_ds)
        if not candidates:
            print("No candidates to promote.")
            if args.demote_stale:
                print("\nStaleness pass on curated set:")
                demote_stale(client, api, curated_ds, args.min_hits, args.min_success_rate, args.dry_run)
            return 0

        print(f"Reviewing {len(candidates)} candidate(s) against '{curated_ds}'…\n")
        promoted: list[dict[str, Any]] = []
        deleted: list[str] = []

        for i, cand in enumerate(candidates, 1):
            print(f"--- candidate {i}/{len(candidates)} ---")
            print(f"  kind: {cand.get('kind', '')} | trigger: {cand.get('trigger', '')}")
            print(f"  text: {cand.get('text', '')[:220]}")
            if not is_candidate_positive(cand):
                print("  -> REJECT (not a valid lesson shape)")
                continue

            # near-dup against curated
            dup = near_dup_score(client, api, curated_ds, cand.get("text", ""))
            if dup >= args.dup_threshold:
                print(f"  -> SKIP (near-dup with curated, score {dup:.2f})")
                deleted.append(cand["id"])
                continue

            # decide promote vs reject
            promote = False
            if args.review:
                answer = input("  promote? [y/N] ").strip().lower()
                promote = answer in ("y", "yes")
            elif args.llm_url and args.llm_model:
                promote = llm_review(args.llm_url, args.llm_model, args.llm_api_key, cand)
                print(f"  -> LLM verdict: {'PROMOTE' if promote else 'REJECT'}")
            elif args.auto:
                promote = True
            else:
                print("  (no --review / --llm-url / --auto -> treating as SKIP)")

            if promote:
                promoted.append(
                    {
                        "text": cand.get("text", ""),
                        "kind": cand.get("kind", "resolution-pattern"),
                        "trigger": cand.get("trigger", ""),
                        "tables": cand.get("tables", []),
                        "tags": cand.get("tags", []),
                        "status": "curated",
                        "source": cand.get("source") or "promote:gate",
                    }
                )
                deleted.append(cand["id"])
            else:
                print("  -> not promoted")

        if promoted:
            if args.dry_run:
                print(f"\n[dry-run] would promote {len(promoted)} to '{curated_ds}':")
                for p in promoted:
                    print(f"  - [{p['kind']}] {p['text'][:100]}")
            else:
                count = upload_curated(client, api, curated_ds, promoted)
                print(f"\nPromoted {count} lesson(s) to '{curated_ds}'")
                for cid in deleted:
                    delete_candidate(client, api, cand_ds, cid)
                print(f"Deleted {len(deleted)} processed candidate(s) from '{cand_ds}'")
        elif deleted:
            print(f"\n(no promotions — would delete {len(deleted)} processed candidates)")

        if args.demote_stale:
            print("\nStaleness pass on curated set:")
            demote_stale(client, api, curated_ds, args.min_hits, args.min_success_rate, args.dry_run)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())