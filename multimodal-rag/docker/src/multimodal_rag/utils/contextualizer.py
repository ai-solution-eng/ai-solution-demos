"""Ingest-time contextual retrieval (feature: contextual retrieval, DECISIONS.md 2026-09).

Anthropic-style context injection at INGEST time: for every document chunk
that carries real extracted text, one small LLM call writes 1–2 sentences of
document-level context ("this chunk is from <document>, which …") and the
sentence is prepended to the chunk text as a ``[Document context]:`` line
before embedding.  The line travels with the chunk everywhere the text does —
the dense vector, the BM25 lexical lane (:func:`multimodal_rag.rag_system._bm25_indexable_text`
keeps it — only bare media placeholders are stripped), and the stored payload
where retrieval-time LLM context can quote it (visibly labeled, so answers can
attribute).

Three gate levels make "disabled by default" real:

1. The chart default ``rag.contextual`` (``RAG_CONTEXTUAL_DEFAULT``) only
   pre-checks the create form — NEW datasets stamp ``contextual: false``
   unless the operator or the request body says otherwise.
2. The per-dataset ``meta["contextual"]`` flag gates the pipeline
   (``MultimodalRAG.contextualize``); existing datasets are untouched until
   PATCHed, and adoption of existing CONTENT is the Recreate flow (content
   dedup would otherwise skip re-embedding).
3. This module is a no-op without a VLM (``rag.vlm`` — the text-capable
   ChatModel the caption path already uses; there is deliberately NO new
   model role).

Environment knobs (read per call so tests and runtime changes take effect):

    RAG_CONTEXTUAL_CONCURRENCY  max in-flight context calls (default 8;
                                ``<= 0`` disables bounding entirely — the
                                MODEL_EMBED_MAX_CONCURRENCY semaphore pattern)
    RAG_CONTEXTUAL_DIGEST_CHARS document-excerpt size in the shared preamble
                                (default 512)

Skips (verified interplay gotchas, review 2026-09):

* **pure-media docs** (no ``_has_real_text``) — a context line would newly
  count as real text and flip ``_has_real_text`` semantics, giving
  placeholder-only docs text-only twins; media docs already have the
  caption-twin path;
* **memory documents** (``memory_kind``-tagged) — memory recall wants the
  verbatim memory text, not LLM paraphrase around it;
* pre-made twins (``_twin``) — defensive; twins are created after this stage.

Failure discipline is the Preprocessor caption-skip one: ANY LLM error stores
the plain chunk and records a non-fatal ingest warning — a VLM outage degrades
ingest to un-contextualized, never fails it.

Prefix caching: the prompt is ``system(constant) + user(doc preamble +
chunk)`` and the preamble (filename + first-N-chars excerpt) is STABLE per
document, so a document's chunks issued in order share one growing prefix the
vLLM/SGLang prefix cache absorbs.  Chunks are therefore processed per
document in producer order — never gathered across files — and each
document's first call completes before its remaining chunks fan out (the
first response is what warms the cache).
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import weakref
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from multimodal_rag.utils.logging_utils import logging

logger = logging.getLogger(__name__)

__all__ = [
    "CONTEXTUALIZED_META_KEY",
    "DOC_CONTEXT_MARKER",
    "OUTPUT_TOKENS_PER_CHUNK",
    "acontextualize_docs",
    "concurrency",
    "contextualized_texts",
    "digest_chars",
    "estimate_tokens",
    "sample_preamble_tokens",
]

# Stored-text marker and per-point provenance key.  The marker must NOT match
# _CAPTION_LINE_RE / _MEDIA_PLACEHOLDER_RE (it doesn't — different shape), so
# caption stripping and _has_real_text are unaffected by its presence.
DOC_CONTEXT_MARKER = "[Document context]:"
CONTEXTUALIZED_META_KEY = "contextualized"

# Planning constant for the cost preview (POST /api/admin/datasets/{name}/
# contextual-preview): a 1–2 sentence context is ~80 tokens of output.
OUTPUT_TOKENS_PER_CHUNK = 80

# Chars-per-token heuristic for the estimate math — intentionally rough; the
# preview is labeled an estimate everywhere it surfaces.
_AVG_CHARS_PER_TOKEN = 4.0

# Upper bound on the chunk text placed in ONE prompt.  Chunks from the file
# pipeline are bounded by the embedder chunk_size (~2048 tokens ≈ 8 KB), but
# raw-document drops (POST /documents, add_memory) are unbounded — a 100 KB
# paste must not become a 100 KB LLM prompt.  Only the PROMPT is capped; the
# stored/embedded text is the full chunk regardless.
_MAX_PROMPT_CHARS = 6000

_CONCURRENCY_ENV = "RAG_CONTEXTUAL_CONCURRENCY"
_DIGEST_ENV = "RAG_CONTEXTUAL_DIGEST_CHARS"
_DEFAULT_CONCURRENCY = 8
_DEFAULT_DIGEST_CHARS = 512

# Per-loop semaphores: asyncio primitives bind to their loop on first use, so
# — exactly like MultiModalEmbeddings._embed_sems — one per event loop,
# created lazily (main loop + sync_wrapper_safe's background loop coexist in
# one process).
_SEMAPHORES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()

_SYSTEM_PROMPT = (
    "You write concise document-level context for retrieval-augmented "
    "generation. You are given a document's name, a short excerpt from its "
    "beginning, and one chunk of that document. Write 1-2 sentences "
    "situating the chunk within the document: what the document is and how "
    "this chunk relates to it. Answer with the sentences only - no preamble, "
    "no quotes, no commentary."
)


def concurrency() -> int:
    """Max in-flight context calls (``RAG_CONTEXTUAL_CONCURRENCY``, default 8).

    ``<= 0`` disables bounding entirely (0 requests in flight is still bounded
    by the gather structure, not the semaphore).
    """
    raw = os.environ.get(_CONCURRENCY_ENV, "")
    try:
        return int(raw) if raw.strip() else _DEFAULT_CONCURRENCY
    except (TypeError, ValueError):
        logger.warning("%s=%r is not an int — using the default %d", _CONCURRENCY_ENV, raw, _DEFAULT_CONCURRENCY)
        return _DEFAULT_CONCURRENCY


def digest_chars() -> int:
    """Document-excerpt length in the shared preamble (default 512 chars)."""
    raw = os.environ.get(_DIGEST_ENV, "")
    try:
        n = int(raw) if raw.strip() else _DEFAULT_DIGEST_CHARS
    except (TypeError, ValueError):
        logger.warning("%s=%r is not an int — using the default %d", _DIGEST_ENV, raw, _DEFAULT_DIGEST_CHARS)
        return _DEFAULT_DIGEST_CHARS
    return max(64, min(n, 8192))


def _semaphore() -> asyncio.Semaphore | None:
    """Per-loop semaphore bounding concurrent context calls (may be None)."""
    n = concurrency()
    if n <= 0:
        return None
    loop = asyncio.get_running_loop()
    sem = _SEMAPHORES.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(n)
        _SEMAPHORES[loop] = sem
    return sem


# ---------------------------------------------------------------------------
# Doc inspection
# ---------------------------------------------------------------------------


def _filename_of(doc: dict[str, Any]) -> str:
    """Human filename for the preamble — the most stable identity a chunk carries."""
    for key in ("original_source", "source", "file"):
        val = doc.get(key)
        if isinstance(val, str) and val.strip():
            name = Path(val.replace("\\", "/")).name
            if name:
                return name
    return "(untitled)"


def _is_skipped(doc: Any) -> bool:
    """Whether *doc* must NOT be contextualized (see module docstring)."""
    if not isinstance(doc, dict):
        return True
    if doc.get("_twin"):
        return True
    if doc.get("memory_kind"):
        return True
    # Real-text gate: contextualize only docs whose text carries content
    # beyond captions/placeholders.  Prepared LAZILY below (the regex work is
    # skipped for docs already excluded) — import here to keep the module
    # import graph acyclic (rag_system imports this module).
    from multimodal_rag.rag_system import _has_real_text

    return not _has_real_text(doc.get("text") or "")


def doc_preamble(doc: dict[str, Any]) -> str:
    """Doc-level prompt preamble — IDENTICAL for every chunk of one document.

    Stability is the prefix-caching contract: filename + a fixed-size excerpt
    of the document's first chunk.  Built once per document run and reused
    verbatim, so the tokens ``system + preamble`` are one cacheable prefix.
    """
    digest = (doc.get("text") or "")[: digest_chars()].strip()
    if digest:
        digest += "…" if len(doc.get("text") or "") > digest_chars() else ""
    excerpt = re.sub(r"\s+", " ", digest).strip()
    if not excerpt:
        excerpt = "(no readable excerpt)"
    return f"Document: {_filename_of(doc)}\nExcerpt: {excerpt}"


def _build_user_prompt(preamble: str, chunk_text: str, position: tuple[int, int]) -> str:
    total = position[1]
    where = f"Chunk {position[0]} of {total}." if total > 1 else "Single chunk."
    excerpt = (chunk_text or "").strip()
    if len(excerpt) > _MAX_PROMPT_CHARS:
        excerpt = excerpt[:_MAX_PROMPT_CHARS] + "…"
    return f"{preamble}\n{where}\nChunk:\n{excerpt}"


def _extract_context(response: Any) -> str:
    """Pull the 1–2 sentence context out of a ChatModel completion response."""
    content = response.choices[0].message.content or ""
    text = " ".join(str(content).split())
    # Models occasionally wrap the answer in quotes; the stored line is a
    # labeled fragment, so strip symmetric wrapping quotes.
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'“”":
        text = text[1:-1].strip()
    return text


# ---------------------------------------------------------------------------
# Estimate math (cost preview)
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Rough token count of *text* (chars/4 heuristic — preview-only)."""
    return math.ceil(max(0, len(text or "")) / _AVG_CHARS_PER_TOKEN)


def sample_preamble_tokens(digest_len: int | None = None) -> int:
    """Planning estimate of the STABLE per-document prefix, in tokens.

    Builds a real preamble from a worst-case sample (a 1-char filename and a
    *digest_len*-char excerpt of repeated 'x') and tokenizes it with the
    chars/4 heuristic — the exact shape the ingest path sends, so the preview
    overestimates nothing systematically.
    """
    n = digest_chars() if digest_len is None else max(0, int(digest_len))
    sample = {"source": "a", "text": "x" * n}
    return (
        estimate_tokens(_SYSTEM_PROMPT)
        + estimate_tokens(doc_preamble(sample))
        + estimate_tokens("\nChunk 1 of 1.\nChunk:\n")
    )


# ---------------------------------------------------------------------------
# The contextualizer
# ---------------------------------------------------------------------------


async def _one_context(
    vlm: Any,
    prompt: str,
    sem: asyncio.Semaphore | None,
) -> str:
    """One bounded LLM call.  Raises on any failure (caller fails open)."""
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    if sem is not None:
        async with sem:
            response = await vlm.llm_async_chat_function_call(messages)
    else:
        response = await vlm.llm_async_chat_function_call(messages)
    return _extract_context(response)


async def acontextualize_docs(
    docs: Sequence[str | dict[str, Any]],
    vlm: Any,
) -> list[str | dict[str, Any]]:
    """Prepend a ``[Document context]:`` line to every real-text chunk.

    Returns a NEW list (dicts are copied on write; the caller's dicts are
    never mutated).  Behaviour:

    * ``vlm`` is ``None`` → the input list, untouched (the module's third
      gate: no VLM configured = the feature does not exist);
    * skipped docs (pure media, memory, twins, non-dicts, strings) pass
      through untouched;
    * contextualized chunks get the marker line prepended and
      ``metadata.contextualized = True`` (via ``_to_documents``'s
      every-non-text-key-is-metadata rule);
    * a real-text chunk whose LLM call fails is stored PLAIN with
      ``metadata.contextualized = False`` plus a non-fatal ingest warning —
      fail-open, never fatal;
    * chunks of one document are called in order and share one stable
      preamble (prefix caching); the first chunk of each document completes
      before its siblings fan out under the concurrency semaphore.
    """
    if vlm is None or not docs:
        return list(docs)

    # Stamp the skipped dict docs (mode is ON — introspectability says the
    # point should say so) without copying strings.  Computed BEFORE the
    # early runs check so an all-skipped batch (pure media, memories) still
    # gets the explicit False stamps.
    out: list[str | dict[str, Any]] = list(docs)
    skipped = {i for i in range(len(docs)) if _is_skipped(docs[i]) and isinstance(docs[i], dict)}
    for i in skipped:
        d = dict(docs[i])  # type: ignore[arg-type]
        d[CONTEXTUALIZED_META_KEY] = False
        out[i] = d

    # ── 1. Split the batch into per-document runs (producer order) ─────────
    runs: list[list[int]] = []
    current: list[int] = []
    current_src: str | None = None
    for i in range(len(docs)):
        if i in skipped:
            if current:
                runs.append(current)
                current = []
            current_src = None
            continue
        doc = docs[i]
        src = doc.get("original_source") or doc.get("source") or ""  # type: ignore[union-attr]
        if current and src != current_src:
            runs.append(current)
            current = []
        if not current:
            current_src = src
        current.append(i)
    if current:
        runs.append(current)

    if not runs:
        return out

    sem = _semaphore()

    # ── 2. Per run: one preamble, first call warms the prefix, rest fan out ─
    for run in runs:
        first = docs[run[0]]
        assert isinstance(first, dict)  # _is_skipped guarantees dict
        preamble = doc_preamble(first)
        total = len(run)

        async def _contextualize_at(i: int, doc: dict[str, Any]) -> None:
            prompt = _build_user_prompt(preamble, doc.get("text") or "", (run.index(i) + 1, total))
            try:
                ctx = await _one_context(vlm, prompt, sem)
            except Exception as exc:
                msg = f"Contextual context skipped ({_filename_of(doc)} chunk {run.index(i) + 1}/{total}): {exc}"
                logger.warning(msg)
                from multimodal_rag.rag_system import _record_ingest_warning

                _record_ingest_warning(msg)
                d = dict(doc)
                d[CONTEXTUALIZED_META_KEY] = False
                out[i] = d
                return
            if not ctx:
                # Empty completion — nothing to prepend, nothing to warn about
                # (an outage raises; this is a quirk).  Keep the plain chunk.
                logger.verbose(  # type: ignore[attr-defined]
                    "Contextual context empty (%s chunk %d/%d) — storing plain chunk",
                    _filename_of(doc),
                    run.index(i) + 1,
                    total,
                )
                d = dict(doc)
                d[CONTEXTUALIZED_META_KEY] = False
                out[i] = d
                return
            d = dict(doc)
            d["text"] = f"{DOC_CONTEXT_MARKER} {ctx}\n{doc.get('text') or ''}"
            d[CONTEXTUALIZED_META_KEY] = True
            out[i] = d

        # First chunk strictly first: its response is what inserts the shared
        # `system + preamble` prefix into the serving engine's prefix cache,
        # so every sibling request after it can absorb the prefix.
        first_doc = docs[run[0]]
        assert isinstance(first_doc, dict)
        await _contextualize_at(run[0], first_doc)
        if total > 1:
            results = await asyncio.gather(
                *[_contextualize_at(i, docs[i]) for i in run[1:]],  # type: ignore[arg-type]
                return_exceptions=False,
            )
            del results
    return out


def contextualized_texts(docs: Sequence[str | dict[str, Any]]) -> list[str]:
    """Test helper: the texts a contextualized batch now carries (dicts only)."""
    return [d.get("text", "") for d in docs if isinstance(d, dict)]
