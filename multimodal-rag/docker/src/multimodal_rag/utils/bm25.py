"""Client-side BM25 sparse vectors for hybrid dense + BM25 retrieval (roadmap feature 2).

Dense embeddings are weakest exactly where this corpus is strongest — code
(identifiers, function names), logs (error codes), JSON/YAML keys.  The
lexical lane fixes that: every document chunk that carries real text also
stores a sparse ``bm25`` vector next to its dense vector, and text queries
fuse both lanes with Reciprocal Rank Fusion (RRF) in a single Qdrant request.

This module owns the *computation* only:

    tokenize(text) → term_counts(text) → bm25_doc_weights / bm25_query_weights
    → to_sparse_vector → qdrant SparseVector(indices, values)

No new model dependency: term extraction reuses the bundled Qwen
``tokenizer.json`` (the same file ``token_text_splitter.py`` chunks with —
present in the production image, absent in dev checkouts, where a stdlib
regex tokenizer keeps the lane alive).  Everything else is stdlib.  The
per-dataset document-frequency (df) stats live in a ``.bm25_stats.json``
sidecar maintained by ``dataset_manager`` / the ingest path; the file I/O
helpers here mirror that module's ``.hashes.json`` pattern (mtime-cached
reads, atomic writes, cross-process lock).

Env knobs (read per call so tests and runtime changes take effect):

    RAG_HYBRID_SEARCH  "1" (default) — build/sparse-search the bm25 lane on
                       bm25-capable collections; "0" forces dense-only.
    RAG_BM25_K1        BM25 term-frequency saturation (default 1.5).
    RAG_BM25_B         BM25 document-length normalisation (default 0.75).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import zlib
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Named-vector layout of a hybrid-capable collection: the dense lane keeps
# the name ``dense`` (the store's ``vector_name``) and the lexical lane is
# the sparse vector ``bm25``.  Both names are schema constants — a collection
# created by an older build has an *unnamed* default vector instead and
# simply never enters the hybrid path.
DENSE_VECTOR_NAME = "dense"
BM25_VECTOR_NAME = "bm25"

BM25_STATS_FILENAME = ".bm25_stats.json"
BM25_LOCK_FILENAME = ".bm25.lock"

__all__ = [
    "BM25_STATS_FILENAME",
    "BM25_VECTOR_NAME",
    "DENSE_VECTOR_NAME",
    "bm25_b",
    "bm25_doc_weights",
    "bm25_query_weights",
    "build_query_sparse_vector",
    "copy_stats",
    "forget_documents",
    "hybrid_search_enabled",
    "idf",
    "load_stats",
    "merge_doc",
    "record_documents",
    "reset_stats",
    "save_stats",
    "term_counts",
    "to_sparse_vector",
    "tokenize",
]


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def hybrid_search_enabled() -> bool:
    """Whether the BM25 lane should be used at ingest and query time.

    On by default for bm25-capable collections; ``RAG_HYBRID_SEARCH=0``
    forces dense-only (no sparse vectors are computed at ingest and no
    fusion request is built at query time).
    """
    return os.environ.get("RAG_HYBRID_SEARCH", "1").strip().lower() not in ("0", "false", "no", "off")


def bm25_k1() -> float:
    """BM25 term-frequency saturation knob (``RAG_BM25_K1``, default 1.5)."""
    try:
        return float(os.environ.get("RAG_BM25_K1", "1.5"))
    except ValueError:
        return 1.5


def bm25_b() -> float:
    """BM25 document-length normalisation knob (``RAG_BM25_B``, default 0.75)."""
    try:
        return float(os.environ.get("RAG_BM25_B", "0.75"))
    except ValueError:
        return 0.75


# ---------------------------------------------------------------------------
# Tokenisation — bundled Qwen tokenizer, stdlib regex fallback
# ---------------------------------------------------------------------------

_TOKENIZER: Any = None
_TOKENIZER_LOCK = threading.Lock()
_TOKENIZER_LOADED = False

# Dev checkouts (and unit tests) may lack the image's bundled tokenizer.json;
# this fallback keeps the lane alive with plain word tokens.  Both sides of a
# dataset always resolve to the SAME tokenizer (ingest and query), which is
# what term-matching consistency requires.
_WORD_RE = re.compile(r"[a-z0-9_]+")


def _get_tokenizer() -> Any:
    """Return the bundled tokenizer (loaded once), or ``None`` when absent."""
    global _TOKENIZER, _TOKENIZER_LOADED
    with _TOKENIZER_LOCK:
        if _TOKENIZER_LOADED:
            return _TOKENIZER
        tok = None
        try:
            from multimodal_rag.utils.token_text_splitter import _find_bundled_tokenizer

            path = _find_bundled_tokenizer("tokenizer.json")
            if path is not None:
                from tokenizers import Tokenizer

                tok = Tokenizer.from_file(str(path))
        except Exception as exc:
            logger.warning("BM25: bundled tokenizer unavailable (%s); using regex tokenizer", exc)
        _TOKENIZER = tok
        _TOKENIZER_LOADED = True
        return _TOKENIZER


def tokenize(text: str) -> list[str]:
    """Segment *text* into lowercase lexical terms.

    With the bundled tokenizer, terms are the tokenizer's surface forms —
    subword pieces mapped back onto the original text via offsets and
    lowercased — so ``getData`` and ``get_data`` share the ``get`` term and
    segmentation is identical at ingest and query time.  The regex fallback
    yields plain ``[a-z0-9_]+`` word tokens.
    """
    if not text:
        return []
    tok = _get_tokenizer()
    if tok is None:
        return _WORD_RE.findall(text.lower())
    out: list[str] = []
    for start, end in tok.encode(text).offsets:
        term = text[start:end].lower().strip()
        if term:
            out.append(term)
    return out


def term_counts(text: str) -> dict[str, int]:
    """Term-frequency map of *text* (document length is ``sum(values)``)."""
    tf: dict[str, int] = {}
    for term in tokenize(text):
        tf[term] = tf.get(term, 0) + 1
    return tf


# ---------------------------------------------------------------------------
# df stats — .bm25_stats.json sidecar I/O (mirrors the .hashes.json pattern)
# ---------------------------------------------------------------------------
# Layout: {"n_docs": int, "total_len": int, "df": {term: doc_frequency}}.
# ``avgdl`` is derived (total_len / n_docs) so the file never holds two views
# of the same number.  Reads are cached per-path and invalidated on mtime, so
# query-side loads skip re-parsing a potentially megabyte-scale vocabulary
# while still picking up writes made by other pods under the lock.

_stats_cache: dict[Path, tuple[int, dict[str, Any]]] = {}
_stats_cache_lock = threading.Lock()


def load_stats(stats_path: Path) -> dict[str, Any]:
    """Return the parsed df stats for *stats_path* (mtime-cached; empty when absent/corrupt)."""
    stats_path = Path(stats_path)
    try:
        mtime = stats_path.stat().st_mtime_ns if stats_path.exists() else 0
    except OSError:
        return _new_stats()
    with _stats_cache_lock:
        cached = _stats_cache.get(stats_path)
        if cached is not None and cached[0] == mtime:
            return cached[1]

    stats = _new_stats()
    if mtime:
        try:
            raw = json.loads(stats_path.read_text(encoding="utf-8"))
            stats["n_docs"] = max(0, int(raw.get("n_docs", 0)))
            stats["total_len"] = max(0, int(raw.get("total_len", 0)))
            df = raw.get("df") or {}
            stats["df"] = {str(k): max(0, int(v)) for k, v in df.items() if int(v) > 0}
        except Exception:
            logger.debug("Unable to parse %s — starting empty", stats_path, exc_info=True)
    with _stats_cache_lock:
        _stats_cache[stats_path] = (mtime, stats)
        if len(_stats_cache) > 100:  # bound across many datasets
            _stats_cache.pop(next(iter(_stats_cache)), None)
    return stats


def save_stats(stats_path: Path, stats: dict[str, Any]) -> None:
    """Atomically persist *stats* and refresh the read cache."""
    stats_path = Path(stats_path)
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = stats_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(stats), encoding="utf-8")
    os.replace(tmp, stats_path)
    with _stats_cache_lock:
        try:
            mtime = stats_path.stat().st_mtime_ns
        except OSError:
            mtime = int(time.time_ns())
        _stats_cache[stats_path] = (mtime, stats)


def reset_stats(stats_path: Path) -> None:
    """Delete the sidecar (recreate starts a fresh collection → fresh stats)."""
    stats_path = Path(stats_path)
    try:
        stats_path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not clear BM25 stats %s", stats_path)
    with _stats_cache_lock:
        _stats_cache.pop(stats_path, None)


def _cross_process_lock(lock_path: Path) -> Any:
    """Reuse dataset_manager's fcntl lock helper (lazy import: that module
    imports this package's siblings at module load — never the reverse)."""
    from multimodal_rag.dataset_manager import _cross_process_lock as _lock

    return _lock(lock_path)


def _new_stats() -> dict[str, Any]:
    return {"n_docs": 0, "total_len": 0, "df": {}}


def copy_stats(stats: dict[str, Any]) -> dict[str, Any]:
    """Independent copy of a stats dict.

    :func:`load_stats` returns the cached object — callers that mutate their
    view (the ingest path folds each sub-batch's term counts in before
    weighting) MUST work on a copy, or the next locked read-modify-write
    re-merges the already-folded counts and double-counts every document.
    """
    return {
        "n_docs": int(stats.get("n_docs", 0)),
        "total_len": int(stats.get("total_len", 0)),
        "df": dict(stats.get("df") or {}),
    }


def merge_doc(stats: dict[str, Any], tf: dict[str, int]) -> None:
    """Fold one document's term counts into *stats* in place.

    In-memory only — the caller persists accumulated docs with
    :func:`record_documents` (once per ingest call, not once per document:
    the whole df map is serialised per write, the O(n²) pattern the
    .hashes.json deferred-write fix removed for hashes).
    """
    stats["n_docs"] = stats.get("n_docs", 0) + 1
    stats["total_len"] = stats.get("total_len", 0) + sum(tf.values())
    df: dict[str, int] = stats.setdefault("df", {})
    for term in tf:
        df[term] = df.get(term, 0) + 1


def record_documents(stats_path: Path, term_maps: list[dict[str, int]]) -> None:
    """Merge *term_maps* into the on-disk stats under the cross-process lock.

    Re-reads the file inside the lock so documents ingested concurrently by
    another pod survive (same discipline as the .hashes.json writes).
    """
    if not term_maps:
        return
    stats_path = Path(stats_path)
    with _cross_process_lock(stats_path.parent / BM25_LOCK_FILENAME):
        stats = load_stats(stats_path)
        for tf in term_maps:
            merge_doc(stats, tf)
        save_stats(stats_path, stats)


def forget_documents(stats_path: Path, term_maps: list[dict[str, int]]) -> None:
    """Decrement the stats by *term_maps* (points that are being deleted).

    Terms the stats never counted (legacy points, a lost sidecar) are
    skipped rather than clamped below zero, so a delete can never corrupt
    the map it does know about.  Best-effort overall: a stale-high df only
    flattens idf slightly.
    """
    if not term_maps:
        return
    stats_path = Path(stats_path)
    with _cross_process_lock(stats_path.parent / BM25_LOCK_FILENAME):
        stats = load_stats(stats_path)
        df: dict[str, int] = stats.setdefault("df", {})
        for tf in term_maps:
            stats["n_docs"] = max(0, stats.get("n_docs", 0) - 1)
            stats["total_len"] = max(0, stats.get("total_len", 0) - sum(tf.values()))
            for term in tf:
                if term in df:
                    df[term] = max(0, df[term] - 1)
                    if df[term] == 0:
                        del df[term]
        save_stats(stats_path, stats)


# ---------------------------------------------------------------------------
# BM25 scoring
# ---------------------------------------------------------------------------


def idf(stats: dict[str, Any], term: str) -> float:
    """Smoothed BM25 inverse document frequency (Lucene variant — always positive).

    Terms absent from the stats (df=0) get the maximum idf the formula
    yields, so a query for a term the sidecar has not seen yet still ranks
    documents that contain it — relevant right after a fresh ingest whose
    df write raced, and for stats reset by a recreate.
    """
    n = max(0, int(stats.get("n_docs", 0)))
    df = max(0, int(stats.get("df", {}).get(term, 0)))
    return math.log(1.0 + (n - df + 0.5) / (df + 0.5))


def _avgdl(stats: dict[str, Any]) -> float:
    return float(stats.get("total_len", 0)) / float(stats["n_docs"]) if stats.get("n_docs") else 0.0


def bm25_doc_weights(
    tf: dict[str, int], stats: dict[str, Any], k1: float | None = None, b: float | None = None
) -> dict[str, float]:
    """BM25 weights for one document's term counts.

    ``w(t) = idf(t) · tf·(k1+1) / (tf + k1·(1 − b + b·dl/avgdl))`` — the
    classic saturation + length-normalisation term.  Unknown terms (df=0)
    still get a positive weight so brand-new identifiers are searchable.
    """
    k1 = bm25_k1() if k1 is None else k1
    b = bm25_b() if b is None else b
    dl = sum(tf.values())
    avgdl = _avgdl(stats) or float(dl) or 1.0
    norm = k1 * (1.0 - b + b * (dl / avgdl))
    out: dict[str, float] = {}
    for term, freq in tf.items():
        weight = idf(stats, term) * freq * (k1 + 1.0) / (freq + norm)
        if weight > 0.0:
            out[term] = weight
    return out


def bm25_query_weights(tf: dict[str, int], stats: dict[str, Any]) -> dict[str, float]:
    """Query-side BM25 weights: idf only.

    The tf-saturation half of the BM25 score lives in the *document*
    vector, so ``dot(query_vec, doc_vec)`` reconstructs the BM25 score —
    the same split the fastembed/Qdrant BM25 recipes use.
    """
    return {term: idf(stats, term) for term in tf}


# ---------------------------------------------------------------------------
# SparseVector construction
# ---------------------------------------------------------------------------


def to_sparse_vector(weights: dict[str, float]) -> Any:
    """Build a Qdrant ``SparseVector`` from a ``{term: weight}`` map.

    Terms are hashed to sparse dimensions with crc32 — deterministic across
    processes and pods (Python's salted ``hash()`` is neither), which is
    what makes ingest-time and query-time indices comparable.  A 32-bit
    collision between two terms merges them into one dimension (weights
    summed): rare, deterministic, and it only blurs those terms' scores.
    """
    from qdrant_client.models import SparseVector

    dims: dict[int, float] = {}
    for term, weight in weights.items():
        idx = zlib.crc32(term.encode("utf-8"))
        dims[idx] = dims.get(idx, 0.0) + weight
    indices = sorted(dims)
    return SparseVector(indices=indices, values=[dims[i] for i in indices])


def build_query_sparse_vector(query_text: str, stats: dict[str, Any]) -> Any | None:
    """Sparse query vector for *query_text*, or ``None`` when it has no terms.

    ``None`` tells the caller to keep the flat dense-only request (e.g. an
    empty query, or a query reduced to nothing by normalisation).
    """
    tf = term_counts(query_text)
    if not tf:
        return None
    return to_sparse_vector(bm25_query_weights(tf, stats))
