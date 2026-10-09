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
    RAG_BM25_FLUSH_CALLS    deferred df-stats writes: flush after this many
                       ``mark_dirty`` calls for one sidecar (default 50; set
                       1 for immediate/historical writes, 0 or less to
                       disable the count trigger).
    RAG_BM25_FLUSH_SECONDS  deferred df-stats writes: flush after this many
                       seconds with unflushed deltas (default 30; 0 or less
                       disables the age trigger).
"""

from __future__ import annotations

import atexit
import json
import logging
import math
import os
import re
import signal
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
    "flush_if_dirty",
    "forget_documents",
    "hybrid_search_enabled",
    "idf",
    "load_stats",
    "mark_dirty",
    "merge_doc",
    "record_documents",
    "reset_stats",
    "save_stats",
    "stats_fully_absent",
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
    # Pending deferred deltas belong to the OLD collection: flushing them
    # after a recreate would resurrect df counts the reset just dropped.
    with _dirty_lock:
        _dirty_deltas.pop(stats_path, None)
        _dirty_calls.pop(stats_path, None)
        _dirty_since.pop(stats_path, None)


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
    :func:`record_documents` (immediate mode) or :func:`mark_dirty` +
    :func:`flush_if_dirty` (deferred mode).  The whole df map is serialised
    per write, so batch ingests MUST defer: rewriting it once per file is
    the O(n²) pattern the .hashes.json deferred-write fix removed for hashes.
    """
    stats["n_docs"] = stats.get("n_docs", 0) + 1
    stats["total_len"] = stats.get("total_len", 0) + sum(tf.values())
    df: dict[str, int] = stats.setdefault("df", {})
    for term in tf:
        df[term] = df.get(term, 0) + 1


def record_documents(stats_path: Path, term_maps: list[dict[str, int]]) -> None:
    """Merge *term_maps* into the on-disk stats under the cross-process lock.

    Re-reads the file inside the lock so documents ingested concurrently by
    another pod survive (same discipline as the .hashes.json writes).  The
    merge runs on a COPY of the cached object and the cache is only replaced
    once the write has actually landed: mutating the cached dict in place
    would leave phantom counts behind if the write raised (the next flush
    would then double-count them).
    """
    if not term_maps:
        return
    stats_path = Path(stats_path)
    with _cross_process_lock(stats_path.parent / BM25_LOCK_FILENAME):
        stats = copy_stats(load_stats(stats_path))
        for tf in term_maps:
            merge_doc(stats, tf)
        save_stats(stats_path, stats)


def forget_documents(stats_path: Path, term_maps: list[dict[str, int]]) -> None:
    """Decrement the stats by *term_maps* (points that are being deleted).

    Terms the stats never counted (legacy points, a lost sidecar) are
    skipped rather than clamped below zero, so a delete can never corrupt
    the map it does know about.  Best-effort overall: a stale-high df only
    flattens idf slightly.  Like :func:`record_documents`, the decrement
    works on a copy so a failed write cannot leave the cache out of sync with
    the file.
    """
    if not term_maps:
        return
    stats_path = Path(stats_path)
    with _cross_process_lock(stats_path.parent / BM25_LOCK_FILENAME):
        stats = copy_stats(load_stats(stats_path))
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
# Deferred df-stats writes (batch-ingest optimisation)
# ---------------------------------------------------------------------------
# Batch ingests call add_to_vector_store once per FILE (dataset_manager's
# consumer loop) and it used to persist the whole sidecar at the end of
# every call: a 5000-file batch = 5000 full multi-MB JSON rewrites of the df
# map over NFS.  Deferred mode instead accumulates the per-call term-count
# deltas here (mark_dirty) and merges them into the on-disk sidecar once
# (flush_if_dirty) when a cheap trigger fires:
#
#   * every RAG_BM25_FLUSH_CALLS mark_dirty calls for one sidecar (default
#     50), or
#   * RAG_BM25_FLUSH_SECONDS after the first unflushed delta (default 30),
#
# plus an atexit / SIGTERM hook so a clean process exit never loses them.
#
# CRASH SEMANTICS (verified against the read paths, 2026-10): a missing or
# torn sidecar degrades gracefully everywhere — load_stats() treats it as
# empty (and save_stats is atomic via os.replace, so a torn file cannot
# exist), vector_store._bm25_stats() returns None for an empty index and the
# query stays flat dense, and idf() gives df=0 terms the maximum positive idf
# so any sparse vector already stored stays searchable.  What is NOT true is
# the stronger claim that the map is re-derived from the stored points: there
# is no rebuild-from-.hashes/scroll path in this repo (verified by grepping
# every bm25/df site — nothing re-counts terms from a collection).  So losing
# the sidecar entirely would quietly switch the lane off.  Losing a BATCH of
# deltas is therefore bounded: a sidecar that already exists stays usable and
# only drifts (df slightly low → idf slightly high, never corrupt, never
# negative), and :func:`stats_fully_absent` lets the ingest path force an
# immediate write whenever the sidecar is missing altogether, so the map can
# never be *absent* while points with sparse vectors exist.
#
# The deltas are (term-count maps, not a merged dict): merging them into the
# on-disk view must re-read the file under the cross-process lock, exactly
# like record_documents, so another pod's concurrent writes survive.

_dirty_lock = threading.Lock()
# stats_path -> list of per-call term-count delta sets awaiting a flush
_dirty_deltas: dict[Path, list[list[dict[str, int]]]] = {}
# stats_path -> mark_dirty calls accumulated since the last flush
_dirty_calls: dict[Path, int] = {}
# stats_path -> monotonic timestamp of that sidecar's oldest unflushed delta
_dirty_since: dict[Path, float] = {}

_FLUSH_HOOKS_REGISTERED = False
_flush_hook_lock = threading.Lock()
# Serialises flushes in-process (a signal/atexit flush racing a normal one
# must not pop the same deltas twice).  Never held across the file lock.
_flush_serialize = threading.Lock()
# The SIGTERM handler this module replaced (captured at install time so the
# hook can chain to it instead of re-reading its own handler).
_PREV_SIGTERM: Any = None


def bm25_flush_calls() -> int:
    """``mark_dirty`` calls per sidecar before a flush (``RAG_BM25_FLUSH_CALLS``).

    Default 50.  Set ``1`` for the historical write-every-call behaviour;
    ``0`` or less disables the count trigger.  With BOTH triggers disabled
    (``RAG_BM25_FLUSH_CALLS<=0`` AND ``RAG_BM25_FLUSH_SECONDS<=0``) every
    ``mark_dirty`` reports due, i.e. immediate writes — "defer forever" is
    deliberately not expressible.
    """
    try:
        return int(os.environ.get("RAG_BM25_FLUSH_CALLS", "50"))
    except ValueError:
        return 50


def bm25_flush_seconds() -> float:
    """Max age of unflushed deltas before a flush (``RAG_BM25_FLUSH_SECONDS``).

    Default 30.0 seconds.  ``0`` or less disables the age trigger.
    """
    try:
        return float(os.environ.get("RAG_BM25_FLUSH_SECONDS", "30"))
    except ValueError:
        return 30.0


def _register_flush_hooks() -> None:
    """Install the atexit + SIGTERM safety nets (once per process).

    atexit covers a normal interpreter exit (including an unhandled exception
    that unwinds the main thread).  SIGTERM covers the container stop path:
    Python's default SIGTERM handler kills the process WITHOUT running
    atexit, so the hook flushes and then chains to the handler that was
    installed when we took over.  SIGINT is deliberately left alone — its
    default handler raises KeyboardInterrupt and the interpreter still runs
    atexit on the way out.

    Best-effort throughout: a process that cannot install a handler (signal
    API restricted, non-main thread) simply keeps the atexit net.
    """
    global _FLUSH_HOOKS_REGISTERED, _PREV_SIGTERM
    with _flush_hook_lock:
        if _FLUSH_HOOKS_REGISTERED:
            return
        _FLUSH_HOOKS_REGISTERED = True

    atexit.register(flush_if_dirty)

    signum = getattr(signal, "SIGTERM", None)
    if signum is None:  # pragma: no cover - non-unix only
        return

    def _signal_flush(signum: int, frame: Any) -> None:
        try:
            # Non-blocking: the interrupted frame may itself hold the dirty
            # lock in this very thread (handlers run in the main thread).
            flush_if_dirty(signal_safe=True)
        except Exception:  # never let a shutdown flush mask the signal
            logger.debug("BM25 df-stats flush during signal %s failed", signum, exc_info=True)
        prev = _PREV_SIGTERM
        try:
            if callable(prev) and prev is not _signal_flush:
                prev(signum, frame)
            else:
                # Stock default: re-raise with the default disposition so the
                # process still dies of SIGTERM (correct exit status).
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)
        except Exception:
            logger.debug("BM25 signal re-raise for %s failed", signum, exc_info=True)

    try:
        _PREV_SIGTERM = signal.getsignal(signum)
        signal.signal(signum, _signal_flush)
    except (ValueError, OSError):  # pragma: no cover - non-main thread / restricted
        logger.debug("Could not install BM25 flush handler for SIGTERM", exc_info=True)


def stats_fully_absent(stats_path: Path) -> bool:
    """True when *stats_path* does not exist on disk at all.

    The ingest path uses this to force the FIRST persist through: a lost
    sidecar is not re-derived from the stored points (see the crash-semantics
    note above), so an entirely absent map is the one case deferral must not
    gamble on.
    """
    try:
        return not Path(stats_path).exists()
    except OSError:  # pragma: no cover - stat failure is treated as absent
        return True


def effective_stats(stats_path: Path) -> dict[str, Any]:
    """An independent stats copy INCLUDING this process's unflushed deltas.

    Deferred writes would otherwise make each ingest call weigh its documents
    against a sidecar that lags by every file of the current batch (df low →
    idf high on exactly the terms just ingested, i.e. a quality regression
    rather than the bounded crash drift).  Folding the pending deltas in
    keeps the weighting view identical to what the next flush will write,
    without performing the write.  Returns a fresh copy — never the cached
    object — so callers can mutate it freely (see :func:`copy_stats`).
    """
    stats_path = Path(stats_path)
    stats = copy_stats(load_stats(stats_path))
    with _dirty_lock:
        deltas = _dirty_deltas.get(stats_path)
        if deltas:
            for group in deltas:
                for tf in group:
                    merge_doc(stats, tf)
    return stats


def mark_dirty(stats_path: Path, term_maps: list[dict[str, int]]) -> bool:
    """Record df deltas for a deferred flush; return True when a flush is due.

    The deltas are NOT written here — they accumulate until
    :func:`flush_if_dirty` (or the count/age trigger recorded by this call)
    persists them.  Returns ``True`` when the caller should flush NOW: the
    per-sidecar call count reached ``RAG_BM25_FLUSH_CALLS``, the oldest
    unflushed delta is older than ``RAG_BM25_FLUSH_SECONDS``, or the sidecar
    is missing entirely (``stats_fully_absent`` — the map must never be
    absent while points carrying sparse vectors exist).  Returning a verdict
    instead of flushing keeps the I/O decision at the call site, where it can
    be offloaded/monkeypatched, and lets the count threshold be checked
    without a second lock.
    """
    if not term_maps:
        return False
    stats_path = Path(stats_path)
    _register_flush_hooks()
    now = time.monotonic()
    with _dirty_lock:
        _dirty_deltas.setdefault(stats_path, []).append(list(term_maps))
        calls = _dirty_calls.get(stats_path, 0) + 1
        _dirty_calls[stats_path] = calls
        first = _dirty_since.setdefault(stats_path, now)
        max_calls = bm25_flush_calls()
        max_age = bm25_flush_seconds()
        if max_calls > 0 and calls >= max_calls or max_age > 0 and (now - first) >= max_age:
            due = True
        else:
            # Both triggers disabled is the documented immediate mode (never
            # "defer forever" — that would be silent data loss at scale).
            due = max_calls <= 0 and max_age <= 0
    return bool(due or stats_fully_absent(stats_path))


def flush_if_dirty(stats_path: Path | None = None, *, signal_safe: bool = False) -> int:
    """Persist any deferred df deltas (once per sidecar); return sidecars flushed.

    With *stats_path*, flush only that sidecar; without it, flush everything
    (the atexit / signal path).  Each sidecar's deltas are merged into a
    fresh locked read of the file — the record_documents discipline — so
    concurrent writers are never clobbered.  Deltas are cleared BEFORE the
    write is attempted: a failed flush must not re-merge the same counts
    later (a double-count corrupts idf; a lost count only drifts it), and the
    caller already logs the failure.

    ``signal_safe=True`` (used by the SIGTERM hook) never BLOCKS on the
    in-process locks: a Python signal handler runs in the main thread, so
    waiting for a lock the interrupted frame itself holds would deadlock the
    shutdown.  If a lock is busy the flush is skipped and the deltas stay for
    the next flush — a bounded loss, never a hang.
    """
    if signal_safe:
        if not _flush_serialize.acquire(blocking=False):
            return 0
    else:
        _flush_serialize.acquire()
    try:
        if signal_safe:
            if not _dirty_lock.acquire(blocking=False):
                return 0
        else:
            _dirty_lock.acquire()
        try:
            if stats_path is None:
                targets = list(_dirty_deltas)
            else:
                targets = [Path(stats_path)]
            batch: list[tuple[Path, list[dict[str, int]]]] = []
            for target in targets:
                deltas = _dirty_deltas.pop(target, None)
                if not deltas:
                    continue
                batch.append((target, [tf for group in deltas for tf in group]))
                _dirty_calls.pop(target, None)
                _dirty_since.pop(target, None)
        finally:
            _dirty_lock.release()
    finally:
        _flush_serialize.release()
    flushed = 0
    for target, term_maps in batch:
        if not term_maps:
            continue
        try:
            record_documents(target, term_maps)
            flushed += 1
        except Exception:
            logger.warning(
                "Could not flush deferred BM25 df stats (%s) — idf weighting may drift", target, exc_info=True
            )
    return flushed


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
