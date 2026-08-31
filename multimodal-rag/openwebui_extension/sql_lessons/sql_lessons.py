"""Shared helpers for the self-improving SQL lesson loop (OWUI filter side).

The loop (from ``design/self-improving-sql-agent.md``):

    resolve -> distill -> store(candidate) -> promote -> recall(next question)

This module keeps the filter thin: the ``Filter`` class calls the async
helpers here. All HTTP is against the Multimodal RAG REST API (same surface
the memory filter uses).

Datasets:
    sql-lessons            curated, read by recall (agent follows these)
    sql-lessons-candidates write-only, written by distillation (never read
                           by the agent directly)

Lesson doc shape (flat, as stored on Qdrant payload):
    {
      "text": "<the instruction to follow>",
      "kind": "schema-map|perf-guard|resolution-pattern|fail-fix|data-shape",
      "trigger": "<class of question this applies to>",
      "tables": [...],
      "tags": [...],
      "status": "candidate"|"curated"|"stale",
      "source": "openwebui:sql-lesson",
      ...
    }
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

RECALL_HEADER = (
    "The following are SQL lessons distilled from past, validated "
    "resolutions. Follow any that apply to the current question — they "
    "encode intent\u2192schema mappings and performance guards. Do not mention "
    "them to the user unless asked."
)

DISTILL_SYSTEM_PROMPT = (
    "You are an SQL lesson curator for an LLM SQL agent. Given a "
    "user-question/assistant-SQL-resolution exchange, decide whether a "
    "durable, reusable lesson was established that would help FUTURE agents "
    "answer similar questions — an intent\u2192schema mapping (what the user "
    "means by a term maps to which table/column/pattern), a performance "
    "guard (bounded IN-list, 12-month window, LIMIT, no ILIKE on raw "
    "serials), or a fix for a repeated failure (serial lives in the mapping "
    "table, never raw columns).\n\n"
    "Only derive lessons from POSITIVE evidence: the SQL executed and the "
    "result was accepted/plausible, OR a concrete fix for a concrete error. "
    "If nothing durable (trivial Q&A, no SQL run, transient), respond with "
    "exactly: NOTHING\n\n"
    "Otherwise respond with 1-3 concise, standalone lessons, each:\n"
    "- kind: schema-map | perf-guard | resolution-pattern | fail-fix\n"
    "- content: the imperative instruction (1-3 sentences), standalone — a "
    "future session with zero context must understand it\n"
    "- trigger: the class of question this applies to\n"
    "- tables: comma-separated table/dataset names\n"
    "- tags: comma-separated keywords\n"
    "Format as JSON: {\"lessons\": [{kind, content, trigger, tables, tags}, ...]}"
)


def _auth_headers(password: str) -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if password:
        h["X-Dataset-Password"] = password
    return h


async def recall_lessons(
    rag_api_url: str,
    dataset_name: str,
    query: str,
    top_k: int = 3,
    password: str = "",
    timeout: float = 30.0,
) -> str:
    """Search the curated lesson dataset and return formatted context (or '').

    ``query`` should be the user's question (or the LLM's SQL intent) so
    lessons match on intent/trigger, not on memorized SQL text.
    """
    if not query.strip():
        return ""
    api = rag_api_url.rstrip("/")
    url = f"{api}/api/datasets/{dataset_name}/search"
    params = {"q": query[:500], "top_k": top_k}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, params=params, headers=_auth_headers(password))
            resp.raise_for_status()
            results = resp.json().get("results", [])
    except Exception:
        logger.warning("SQL-lesson recall failed for '%s'", dataset_name, exc_info=True)
        return ""

    if not results:
        return ""
    lines = []
    for i, r in enumerate(results):
        inner = r.get("content") if isinstance(r.get("content"), dict) else {}
        content = r.get("text") or inner.get("text", "")
        score = r.get("score", 0)
        # kind/tags may be top-level OR nested inside `content` (the server
        # returns {content: {text, kind, ...}, score}).
        kind = r.get("kind") or inner.get("kind", "")
        tags = r.get("tags") or inner.get("tags", "")
        if not content:
            continue
        head = f"[SQL LESSON {i + 1}] (score: {score:.4f})"
        if kind:
            head += f" — {kind}"
        if tags:
            head += f" — [{tags}]" if isinstance(tags, str) else f" — {tags}"
        lines.append(f"{head}\n{content}")
    context = "\n\n".join(lines)
    logger.info("SQL-lesson recall: %d hit(s) for '%s'", len(lines), query[:40])
    return context


def inject_lessons(messages: list[dict], context: str, as_system: bool = True) -> None:
    """Inject recalled lesson context into the message list."""
    full = RECALL_HEADER + "\n\n" + context
    if as_system:
        messages.insert(0, {"role": "system", "content": full})
    else:
        last = messages[-1]
        content = last.get("content", "")
        if isinstance(content, str):
            last["content"] = f"{full}\n\n{content}"
        elif isinstance(content, list):
            content.insert(0, {"type": "text", "text": full})


async def store_candidate(
    rag_api_url: str,
    dataset_name: str,
    lesson: dict[str, Any],
    password: str = "",
    timeout: float = 60.0,
) -> str | None:
    """Store one candidate lesson into ``dataset_name`` (write-only store).

    Returns the stored lesson text, or ``None`` on failure.
    """
    doc = {
        "text": lesson.get("content", ""),
        "kind": lesson.get("kind", "resolution-pattern"),
        "trigger": lesson.get("trigger", ""),
        "tables": lesson.get("tables", []),
        "tags": lesson.get("tags", []),
        "status": "candidate",
        "source": "openwebui:sql-lesson",
    }
    api = rag_api_url.rstrip("/")
    url = f"{api}/api/datasets/{dataset_name}/documents"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=[doc], headers=_auth_headers(password))
            resp.raise_for_status()
    except Exception:
        logger.warning("SQL-lesson candidate store failed", exc_info=True)
        return None
    return doc["text"]


async def distill_candidates(
    distill_llm_url: str,
    model: str,
    api_key: str,
    user_text: str,
    assistant_text: str,
    timeout: float = 60.0,
) -> list[dict[str, Any]]:
    """Ask the distillation LLM for 0-3 candidate lessons from one exchange.

    Returns a list of lesson dicts (may be empty). Callers decide whether to
    store them (gate: positive evidence) and where.
    """
    if not distill_llm_url or not model:
        return []
    url = distill_llm_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": DISTILL_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"User: {user_text[:2000]}\n\nAssistant: {assistant_text[:4000]}",
            },
        ],
        "max_tokens": 512,
        "temperature": 0.1,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        logger.warning("SQL-lesson distillation LLM call failed", exc_info=True)
        return []

    if not raw or raw.upper().strip() == "NOTHING":
        return []
    try:


        data = json.loads(raw)
        lessons = data.get("lessons", []) if isinstance(data, dict) else data
        return [l for l in lessons if isinstance(l, dict) and l.get("content")]
    except json.JSONDecodeError:
        logger.warning("Could not parse distilled lessons as JSON: %.120s…", raw)
        return []