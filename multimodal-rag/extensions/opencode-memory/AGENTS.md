# Agent Instructions — Multimodal RAG memory integration

This repo ships a Multimodal RAG MCP server. When connected to opencode it provides **long-term memory** of past interactions via MCP tools:

- **Memory tools** — personal, per-user long-term memory: `add_memory`, `search_memory`, `delete_memory`, `list_memories`, `forget_session`. The memory dataset and its password are resolved **server-side from your identity** (the /access ★ binding + saved selection), so you do NOT pass `dataset_name` or `password` to these tools.
- **Knowledge tools** — access to the shared project/knowledge datasets: `search_dataset`, `search_datasets`, `list_datasets`, `get_dataset_files`, `get_dataset_info`, `unlock_dataset`, `dataset_add_documents`, `dataset_delete_documents`, `dataset_replace_document`, `describe_media`, `transcribe_audio`. For these you DO pass `dataset_name` explicitly; pass `password` only for protected datasets.

Tools arrive prefixed with the MCP connection name (e.g. `rag_add_memory` under the unified 2026-10 config; `rag-memory_add_memory` / `rag-knowledge_search_dataset` under the legacy two-connection pattern). Adapt the prefixes below to whatever `opencode mcp list` shows.

> If the memory tools are not connected, ignore this file — memory is unavailable and you should proceed normally.

## Long-term memory behavior (memory tools)

### When to RECALL — `search_memory`
- At the **start of any non-trivial task** (a task likely to span multiple steps or touch existing code), call `search_memory` with a concise summary of the task. This surfaces relevant past decisions, preferences, gotchas, and prior work before you act.
- Whenever the **user references prior work** ("remember when…", "like we did before", "last time"), call `search_memory` with the described topic.
- Keep `top_k` at the default (5). Turn the reranker on only if the first recall feels off.

### When to WRITE — `add_memory`
A full record of this session (prompts, responses, tool calls, file changes) is captured automatically by the `session-memory-logger` plugin (`kind: session_history`), so don't reproduce the session here — only durable, distilled facts.

After completing a non-trivial task, store a memory **only if** something durable was learned. Worth remembering:
- A **decision** and its rationale ("chose tabs over spaces for repo style", "auth uses oauth2-proxy bearer tokens, not cookies").
- A confirmed **preference** of the user.
- A **gotcha / fix** that took real effort to find and could recur.
- A non-obvious **architectural fact** about this codebase.

Do NOT store: transient debugging steps, trivial Q&A, restatements of what is already in committed docs/code, or anything that would be obvious from reading the repo. When in doubt, don't write — noise degrades recall.

### How to WRITE a good memory
- Make it **standalone**: a future session with zero other context must understand it. "User prefers tabs over spaces because of repo style" beats "prefers tabs".
- Be **specific and concrete**: names of files, commands, error messages.
- Pass `metadata={"kind": "decision"|"preference"|"gotcha"|"fact", "tags": [...], "session_id": "<this session>"}` so memories are attributable. `kind` and tags help future you reason about hits.
- One memory per call; call multiple times if several distinct facts deserve saving.

### Rules
- Never mention the memory dataset name or password to the user — they are resolved silently server-side and are none of the user's concern.
- Near-duplicate memories are auto-skipped at cosine ≥ 0.995, so re-saving a learned fact in a later session is a harmless no-op.
- Wrong or outdated memories can be removed with `delete_memory` (by explicit `memory_id` from `search_memory`/`list_memories` results) — prefer correcting with a new memory when the fact is still useful, and mention the supersession. `forget_session` wipes one session's stored history.
- Memory is per-user (per dataset). Do not assume a teammate's memory is present; recall only ever searches your own store.

## Knowledge datasets (knowledge tools)

Use `search_dataset` to retrieve from a named project dataset (the RAG knowledge base). Use `list_datasets` to discover named datasets when the user asks about available corpora. Prefer `search_dataset` over `get_dataset_files` for finding content — datasets can contain tens of thousands of files and listing them wastes context.
