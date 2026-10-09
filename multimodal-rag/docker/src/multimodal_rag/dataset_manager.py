"""
Dataset manager for multimodal RAG.

Handles dataset creation, file uploads, text input, and Qdrant vector storage.
Each dataset is a Qdrant collection on a shared Qdrant instance, with
uploaded files stored on a PVC at ``/data/datasets/<name>/files/``.
"""

import base64
import bisect
import contextlib
import contextvars
import copy
import hashlib
import json
import math
import mimetypes
import os
import re
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast, overload

import httpx2

try:
    import fcntl  # Unix-only; k8s pods are Linux.

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - Windows dev only
    fcntl = None  # type: ignore[assignment]
    _HAS_FCNTL = False

import multimodal_rag.utils.bm25 as bm25_lane
from multimodal_rag.input_processing import (
    ArchiveProcessor,
    CodeProcessor,
    EbookProcessor,
    HTMLProcessor,
    ImageProcessor,
    JSONProcessor,
    LogProcessor,
    NotebookProcessor,
    OfficeProcessor,
    PDFProcessor,
    TableProcessor,
    TextProcessor,
    VideoProcessor,
    XMLProcessor,
    YAMLProcessor,
)
from multimodal_rag.rag_system import MultimodalRAG, _record_ingest_warning, _store_write_guard
from multimodal_rag.utils.general_tools import retry_call
from multimodal_rag.utils.logging_utils import logging
from multimodal_rag.vector_store import RrfParams

# Per-dataset weighted-RRF defaults (feature: weighted RRF, dataset-defaults
# slice).  Stored in meta.json as ``meta["rrf"] = {"dense_weight": …,
# "sparse_weight": …, "k": …}``.  Resolution order at search time is
# per-call override > dataset meta default > global default (1.0/1.0 with no
# pinned k).  The clamp discipline mirrors the API slices exactly: weights
# clamp to ``[0.0, 10.0]`` rounded to 3 decimals, ``k`` to ``[1, 1000]``.
_RRF_WEIGHT_MAX = 10.0

logger = logging.getLogger(__name__)

# Collection/dataset schema version recorded in meta.json.  v2 (current) is
# the hybrid layout: a *named* "dense" vector plus a named "bm25" sparse
# vector (roadmap feature 2).  Datasets created before the field existed are
# implicitly v1 — their collections hold one unnamed default vector, which
# cannot host a second lane, so they keep dense-only retrieval until
# recreated.  The fingerprint guard nudges that with a warning (see
# _nudge_schema_upgrade); it is deliberately NOT an error — v1 retrieval is
# fully functional.
DATASET_SCHEMA_VERSION = 2


class EmbedderMismatchError(ValueError):
    """Raised when a dataset's stored vectors were built with a different
    embedder than the one currently configured.

    Existing vectors are incompatible with the current embedder (different
    model and/or dimension), so the dataset must be recreated before it can
    be ingested into or searched again.
    """


# When True (the default since Wave-4 / D12), get_dataset()/list_datasets()
# skip the per-call Qdrant count sync (which cost N sequential round-trips on
# the hottest endpoint and wrote meta.json on every read). Counts are still
# maintained incrementally via _increment_count/_decrement_count, so listed
# counts are exact for anything ingested through this process — they may lag
# by the defer window after out-of-band changes (points added/removed directly
# in Qdrant, or by another replica without a count increment).  The scale
# chart already pinned this on; it is now the fleet default to cut Qdrant
# load and cross-replica meta.json write races everywhere.
# Escape hatch: RAG_DEFER_COUNT_SYNC=false restores live per-request counting
# (exact counts, N sequential Qdrant round-trips per list call).
_DEFER_COUNT_SYNC = os.environ.get("RAG_DEFER_COUNT_SYNC", "true").lower() in (
    "true",
    "1",
    "yes",
)


# ---------------------------------------------------------------------------
# Additive cursor pagination (Wave-4) — opaque continuation tokens for the
# list surfaces.  A versioned URL-safe base64 JSON blob, deliberately not a
# bare offset/index, so a token survives ordering-representation changes.
# ---------------------------------------------------------------------------

_LIST_CURSOR_VERSION = 1


def _encode_list_cursor(after: Any) -> str:
    """Build the continuation token resuming *after* the given key."""
    blob = json.dumps({"v": _LIST_CURSOR_VERSION, "o": after}).encode("utf-8")
    return base64.urlsafe_b64encode(blob).decode("ascii")


def _decode_list_cursor(cursor: str) -> Any:
    """Decode a continuation token; raises ``ValueError`` on garbage."""
    try:
        blob = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        if not isinstance(blob, dict) or blob.get("v") != _LIST_CURSOR_VERSION or "o" not in blob:
            raise ValueError("unsupported cursor version or shape")
        return blob["o"]
    except Exception as exc:
        raise ValueError(f"Invalid cursor: {str(cursor)[:64]!r}") from exc


def _dataset_order_key(name: str) -> tuple[str, str]:
    """Listing order key: alphabetical regardless of case.

    The primary component is the lowercased name, so ``Zebra`` files under
    ``apple`` instead of jumping ahead of it (a plain ASCII sort puts all
    uppercase names first).  The raw name is the tie-break, so two datasets
    differing only by case (``MLS`` vs ``mls``) stay adjacent in a
    deterministic order (uppercase first) instead of inheriting the
    directory-read order.  Names are validated ``[A-Za-z0-9._-]``, so a
    plain ``lower()`` is an exact case fold here.
    """
    return (name.lower(), name)


# Bounded parallel preprocessing fan-out for batch ingests (Wave-4): up to
# this many files inside one ``add_files_batch`` call are preprocessed
# concurrently (store/copy/hash/classify/extract).  Document order, batch
# composition and results stay deterministic — workers feed an ordered
# sequencer.  ``RAG_INGEST_CONCURRENCY=1`` restores the strictly sequential
# pre-Wave-4 behaviour.
try:
    _INGEST_CONCURRENCY = max(1, int(os.environ.get("RAG_INGEST_CONCURRENCY", "4")))
except ValueError:
    _INGEST_CONCURRENCY = 4

# Optional Redis backend for cross-pod dataset existence caching.
# When a dataset is created on pod A, pod B's NFS client cache may not
# see meta.json for several seconds.  Redis provides an immediate
# existence signal so pod B can retry _read_meta instead of returning 404.
_REDIS_URL = os.environ.get("REDIS_URL", "")
_redis_client: Any = None
_redis_client_lock = threading.Lock()

# How many times to retry _read_meta when Redis says the dataset exists
# but NFS hasn't propagated yet.
_DATASET_EXIST_RETRY_COUNT = 4
_DATASET_EXIST_RETRY_DELAY = 0.5  # seconds


def _get_redis() -> Any:
    """Lazily build a Redis client, cached for the process lifetime."""
    global _redis_client
    if not _REDIS_URL:
        return None
    if _redis_client is not None:
        return _redis_client
    with _redis_client_lock:
        if _redis_client is not None:
            return _redis_client
        try:
            import redis  # type: ignore[import-untyped]

            _redis_client = redis.from_url(_REDIS_URL, socket_timeout=2.0, socket_connect_timeout=2.0)
            _redis_client.ping()
            logger.info("Dataset existence cache: Redis backend connected at %s", _REDIS_URL)
        except Exception as exc:
            logger.warning("Dataset existence cache: Redis unavailable (%s); falling back to NFS-only", exc)
            _redis_client = None
    return _redis_client


def _dataset_exist_key(name: str) -> str:
    return f"dataset_exists:{name}"


def _dataset_exist_set(name: str) -> None:
    r = _get_redis()
    if r is not None:
        try:
            r.set(_dataset_exist_key(name), "1", ex=86400)  # 24h TTL
        except Exception:
            pass


def _dataset_exist_delete(name: str) -> None:
    r = _get_redis()
    if r is not None:
        try:
            r.delete(_dataset_exist_key(name))
        except Exception:
            pass


def _dataset_exist_check(name: str) -> bool:
    """Return True if Redis says the dataset exists (NFS may lag)."""
    r = _get_redis()
    if r is None:
        return False
    try:
        return r.exists(_dataset_exist_key(name)) > 0
    except Exception:
        return False


# Fallback thread locks for platforms without fcntl (Windows dev only).
_fallback_locks: dict[str, threading.Lock] = {}
_fallback_locks_guard = threading.Lock()


@contextlib.contextmanager
def _cross_process_lock(lock_path: Path):
    """Cross-process + cross-thread advisory file lock.

    Uses ``fcntl.flock(LOCK_EX)`` on a sidecar ``.lock`` file so that
    multiple pods sharing the RWX PVC serialize read→modify→write cycles
    on dataset metadata and the dedup hash index. On non-unix platforms
    falls back to a per-path ``threading.Lock`` (in-process only).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if _HAS_FCNTL:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    else:  # pragma: no cover - Windows dev only
        key = str(lock_path)
        with _fallback_locks_guard:
            lk = _fallback_locks.get(key)
            if lk is None:
                lk = threading.Lock()
                _fallback_locks[key] = lk
        with lk:
            yield


# ---------------------------------------------------------------------------
# Upload history (persisted per-file log shown on the Manage page)
# ---------------------------------------------------------------------------
# Each completed upload/ingestion job appends one entry per processed file so
# the Manage page can show a table of what was uploaded and when.  The log
# lives under DATA_PATH (the shared RWX PVC) so it survives restarts and is
# visible across pods; a cross-process fcntl lock serializes appends.
#
# Wave-5 (F2): these helpers moved here from api_server so the MCP
# document-management tools record the SAME events as their REST twins
# (single source of truth — api_server re-imports the names, so
# ``api_server._record_upload_history`` keeps working for callers/monkeypatches).


def _upload_history_path() -> Path:
    return Path(os.environ.get("DATA_PATH", "/data")) / "upload_history.json"


def _load_upload_history() -> list[dict[str, Any]]:
    p = _upload_history_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        logger.debug("Unable to parse upload history — starting empty", exc_info=True)
        return []


def _save_upload_history(entries: list[dict[str, Any]]) -> None:
    p = _upload_history_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: temp file + os.replace() so a crash mid-write never leaves
    # a truncated history file.
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, p)


def _record_upload_history(dataset_name: str, files: list[dict[str, Any]], source: str) -> None:
    """Persist one entry per processed file (name, outcome, timestamp)."""
    if not files:
        return
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with _cross_process_lock(_upload_history_path().with_suffix(".lock")):
        entries = _load_upload_history()
        for f in files:
            err = f.get("error")
            chunks = f.get("chunks") or 0
            status = "error" if err else ("ok" if chunks > 0 else "skipped")
            entries.append(
                {
                    "timestamp": now,
                    "dataset": dataset_name,
                    "file": f.get("file") or "unknown",
                    "chunks": chunks,
                    "status": status,
                    "source": source,
                    "error": err,
                }
            )
        # Bound the file size — keep only the newest 2000 entries.
        if len(entries) > 2000:
            entries = entries[-2000:]
        _save_upload_history(entries)


# ---------------------------------------------------------------------------
# Ingest webhook (Wave-5 F2, opt-in — RAG_WEBHOOK_URL)
# ---------------------------------------------------------------------------
# When RAG_WEBHOOK_URL is set, every completed ingest fires one small JSON
# event (dataset, doc_count, status) via utils/webhook.notify_ingest —
# timeout-capped, failures logged-not-fatal, X-RAG-Webhook-Secret header when
# RAG_WEBHOOK_SECRET is set.  Unset (the default) = zero behaviour: no
# request, no latency.  Bulk replays (dataset restore/import) are muted.


def _stored_points_count(files: list[dict[str, Any]] | None) -> int:
    """Points added by a batch ingest result (failed and dedup-skipped
    files add 0; legacy shapes without stored_ids fall back to chunks)."""
    total = 0
    for f in files or []:
        if f.get("error"):
            continue
        ids = f.get("stored_ids")
        if isinstance(ids, list):
            total += len(ids)
        else:
            total += f.get("chunks") or 0
    return total


def _notify_ingest_hook(dataset_name: str, doc_count: int, status: str = "ok") -> None:
    """Fire the opt-in ingest webhook for *dataset_name* (never raises)."""
    if doc_count <= 0 and status == "ok":
        # Nothing was ingested (all files dedup-skipped / empty batch) — the
        # receiver cares about completed ingests, not no-ops.
        return
    try:
        from multimodal_rag.utils.webhook import notify_ingest

        notify_ingest(dataset_name, doc_count, status)
    except Exception:  # pragma: no cover - defensive: never fail an ingest
        logger.debug("Ingest webhook dispatch failed", exc_info=True)


def _webhook_muted():
    """Context manager suppressing ingest webhooks (dataset restore replays)."""
    from multimodal_rag.utils.webhook import muted

    return muted()


# ---------------------------------------------------------------------------
# .hashes.json read/write helpers with an in-process cache
# ---------------------------------------------------------------------------
# The index maps a file SHA-256 to its stored path and can grow large; caching
# it per-Path (invalidated on mtime change) avoids re-reading + re-parsing the
# whole JSON for every uploaded file while preserving cross-pod correctness
# (the mtime check picks up writes made by other pods under the fcntl lock).
_hash_index_cache: dict[Path, tuple[int, dict[str, str]]] = {}
_hash_index_cache_lock = threading.Lock()


def _load_hash_index(hashes_path: Path) -> dict[str, str]:
    """Return the parsed hash index for *hashes_path* (mtime-cached)."""
    mtime = hashes_path.stat().st_mtime_ns if hashes_path.exists() else 0
    with _hash_index_cache_lock:
        cached = _hash_index_cache.get(hashes_path)
        if cached is not None and cached[0] == mtime:
            return cached[1]

    index: dict[str, str] = {}
    if mtime:
        try:
            index = json.loads(hashes_path.read_text())
        except Exception:
            logger.debug("Unable to parse %s — starting empty", hashes_path, exc_info=True)
            index = {}
    with _hash_index_cache_lock:
        _hash_index_cache[hashes_path] = (mtime, index)
        if len(_hash_index_cache) > 100:  # bound across many datasets
            _hash_index_cache.pop(next(iter(_hash_index_cache)), None)
    return index


def _write_hash_index(hashes_path: Path, index: dict[str, str]) -> None:
    """Atomically persist *index* to *hashes_path* and refresh the cache."""
    tmp = hashes_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, indent=2))
    os.replace(tmp, hashes_path)
    with _hash_index_cache_lock:
        _hash_index_cache[hashes_path] = (hashes_path.stat().st_mtime_ns, dict(index))


# Deferred hash-index writes: batch ingests used to rewrite the ENTIRE
# .hashes.json once per file (O(n²) serialization + NFS writes across a
# batch).  _store_file(..., defer_write=True) instead records its new
# entries here; _flush_hash_index_writes() merges them into the on-disk
# index under the cross-process lock once per batch.  If the process dies
# before the flush the files are still on disk — only the dedup entries are
# missing, so a retry re-copies them (benign duplicates, never corruption).
_hash_index_dirty: dict[Path, dict[str, str]] = {}
_hash_index_dirty_lock = threading.Lock()


def _flush_hash_index_writes() -> None:
    """Persist all deferred hash-index entries (merge, not clobber).

    Re-reads the on-disk index under the dataset's cross-process lock so
    entries written by another pod during our batch survive.
    """
    with _hash_index_dirty_lock:
        dirty = dict(_hash_index_dirty)
        _hash_index_dirty.clear()
    for hashes_path, updates in dirty.items():
        if not updates:
            continue
        with _cross_process_lock(hashes_path.parent / ".hashes.lock"):
            fresh = _load_hash_index(hashes_path)
            fresh.update(updates)
            _write_hash_index(hashes_path, fresh)


# ── Source-only payload projections (perf quick-win #3, audit perf P1-5) ──
# Source scans (S3 sync's "which sources does this prefix hold?") need two
# small payload keys — the canonical ``metadata.source`` and, for objects
# that carry it, ``original_source`` / ``page_content``.  Asking Qdrant for
# the whole ``metadata`` object instead drags the multi-MB base64
# ``metadata.image`` / ``metadata.video`` / ``metadata.audio`` values over
# the wire on every scan (the same waste ``vector_store._lightweight_payload_selector``
# exists to avoid on the search path).
#
# Nested keys DO work with ``PayloadSelectorInclude`` (verified against the
# pinned qdrant-client 1.19.x: the filter is applied server-side by Qdrant's
# payload projection, and the local in-memory client behaves identically) —
# ``include=["metadata.source"]`` returns exactly ``{"metadata": {"source": …}}``.
#
# ``_prune_sources`` uses the include form too (with page_content): its
# scroll projects exactly ``_source_scan_payload_keys(True)``.
_SOURCE_SCAN_PAYLOAD_KEYS: tuple[str, ...] = ("metadata.source", "metadata.original_source")
# ``page_content`` is included only for callers that actually consume it
# (prune's BM25 df decrement) — ``_stored_sources`` never touches it, so it
# keeps the projection to the two source keys.
_SOURCE_SCAN_TEXT_PAYLOAD_KEY = "page_content"


def _source_scan_payload_keys(include_page_content: bool = False) -> list[str]:
    """Key list for ``scroll_documents(..., payload_keys=...)`` source scans.

    Nested keys are supported by ``PayloadSelectorInclude`` (verified against
    the pinned qdrant-client 1.19.x — the projection is Qdrant-side, and the
    local in-memory client returns exactly ``{"metadata": {"source": …}}``),
    so only the requested leaves cross the wire: the multi-MB base64
    ``metadata.image`` / ``metadata.video`` / ``metadata.audio`` values stay
    on the server.
    """
    keys = list(_SOURCE_SCAN_PAYLOAD_KEYS)
    if include_page_content:
        keys.append(_SOURCE_SCAN_TEXT_PAYLOAD_KEY)
    return keys


# Ingest-dedup (.ingested_hashes.json) read cache — the batch loop used to
# re-read and re-parse the whole file for every file (same O(n²) pattern).
_ingested_hashes_cache: dict[Path, tuple[int, set[str]]] = {}
_ingested_hashes_cache_lock = threading.Lock()


def _load_ingested_hashes(p: Path) -> set[str]:
    """Return the parsed ingest-dedup hash set for *p* (mtime-cached)."""
    mtime = p.stat().st_mtime_ns if p.exists() else 0
    with _ingested_hashes_cache_lock:
        cached = _ingested_hashes_cache.get(p)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    hashes: set[str] = set()
    if mtime:
        try:
            hashes = set(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            hashes = set()
    with _ingested_hashes_cache_lock:
        _ingested_hashes_cache[p] = (mtime, hashes)
        if len(_ingested_hashes_cache) > 100:  # bound across many datasets
            _ingested_hashes_cache.pop(next(iter(_ingested_hashes_cache)), None)
    return hashes


# ---------------------------------------------------------------------------
# meta.json parsed-read cache (perf P1-2)
# ---------------------------------------------------------------------------
# ``_read_meta`` sits on the hottest search path (once per search via
# ``_effective_rrf``, plus once per federated target) and used to cost two NFS
# syscalls (``exists()`` + ``read_text()``) AND a full JSON parse on every
# call.  The parsed dict is cached per meta path under an mtime/size stamp —
# the read discipline of ``clients_registry.is_public_dataset`` /
# ``created_by_dataset`` and this module's own ``_load_hash_index``: stat →
# (mtime_ns, size) compare → parse only on a miss.
#
# Deliberately NO TTL/max-age floor.  A stamp check is what the NFS
# close-to-open visibility contract wants: the stat() that decides hit/miss is
# the same syscall that observes another pod's write, so a write is picked up
# on the very next read, whereas any TTL would serve a pre-change dict for its
# whole window.  The win does not depend on a TTL — a hit still removes the
# JSON parse and the exists()/open/read/close round trips, leaving one cheap
# stat() per call.
#
# Every in-process writer invalidates explicitly (``_write_meta`` plus the
# password/delete paths) so correctness never rides on timestamp granularity:
# a rewrite landing with an identical (mtime_ns, size) — same-length value,
# coarse clock — would otherwise match the stale entry.
_META_CACHE: dict[str, tuple[int, int, dict[str, Any] | None]] = {}
_meta_cache_lock = threading.Lock()
_META_CACHE_MAX = 100  # bound across many datasets (same cap as _hash_index_cache)
# Stamp for a stat() failure (missing/unstat-able meta): a real file always
# carries mtime_ns >= 0 and size >= 0, so this can never collide with one.
_META_CACHE_MISSING = (-1, -1)
# Sentinel for "no usable cache hit" — distinct from a cached None (a missing
# or corrupt meta, which IS a legitimate cached value).
_META_CACHE_HIT = object()


def _invalidate_meta_cache(path: Path) -> None:
    """Drop the parsed-meta cache entry for *path* (call after any write)."""
    with _meta_cache_lock:
        _META_CACHE.pop(str(path), None)


# Supported file extensions mapped to a media type label
_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff"})
_VIDEO_EXTS = frozenset({".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v"})
_AUDIO_EXTS = frozenset({".mp3", ".wav", ".flac", ".ogg", ".m4a", ".wma"})
_TEXT_EXTS = frozenset({".txt", ".md"})
_TABLE_EXTS = frozenset({".csv", ".tsv", ".xlsx", ".xls", ".ods"})
_CODE_EXTS = frozenset(
    {
        ".py",
        ".pyw",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".java",
        ".cpp",
        ".cxx",
        ".cc",
        ".c",
        ".h",
        ".hpp",
        ".hxx",
        ".cs",
        ".rb",
        ".go",
        ".rs",
        ".swift",
        ".kt",
        ".kts",
        ".scala",
        ".php",
        ".r",
        ".R",
        ".sh",
        ".bash",
        ".zsh",
    }
)
_OFFICE_EXTS = frozenset({".docx", ".pptx", ".odt", ".odp"})
_HTML_EXTS = frozenset({".html", ".htm"})
_XML_EXTS = frozenset({".xml"})
_YAML_EXTS = frozenset({".yaml", ".yml"})
_NOTEBOOK_EXTS = frozenset({".ipynb"})
_EBOOK_EXTS = frozenset({".epub"})
_LOG_EXTS = frozenset({".log", ".txt.log"})
_ARCHIVE_EXTS = frozenset({".zip", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".tbz2", ".txz", ".rar"})
_INGESTED_HASHES_FILE = ".ingested_hashes.json"

# Watched S3 sources (feature: watched sources): per-dataset skip-state sidecar
# next to .hashes.json / .bm25_stats.json.  Key = the object's canonical S3 URL
# (``s3://bucket/key``), value = ``{"etag": ..., "size": ...}`` — the fingerprint
# of the last successfully ingested version of that object.  On the next sync,
# an object whose listed ETag+Size still matches is dropped from the download
# set entirely (no re-download, no re-embed attempt); a changed ETag+Size (the
# content-changed-under-same-key case) or a new key re-ingests.  ETag+Size is
# the fingerprint because S3 ETags of multipart uploads are opaque (not MD5) —
# together the pair catches every realistic mutation.  No ETag use existed in
# the repo before this feature (verified).
_WATCHED_STATE_FILE = ".watched_state.json"


def _sha256_file(path: str | Path) -> str:
    """Return the SHA-256 hex digest of a file's contents (streamed)."""
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Watched S3 sources: per-dataset ETag/Size skip state (.watched_state.json)
# ---------------------------------------------------------------------------
# The reconciler CronJob re-lists its prefixes every tick and ingests what it
# finds.  Without state, every tick re-downloads every object (the content-hash
# ingest dedup then discards the identical bytes — network + disk I/O for
# nothing, and the skip hides would-be "changed" objects until after the
# download).  This sidecar mirrors the ``.bm25_stats.json`` discipline: a JSON
# file in the dataset's files/ directory, read/write under the same
# cross-process fcntl lock, atomically promoted so a crash mid-write never
# truncates it.  Written ONLY after a successful ingest of the object.

# module-level read cache (mtime-keyed) — the same shape as _load_hash_index's
_watched_state_cache: dict[Path, tuple[int, dict[str, dict[str, Any]]]] = {}
_watched_state_cache_lock = threading.Lock()


def _canonical_s3_url(url: str) -> str:
    """Canonical state key for an S3 URL: query stripped, no trailing slash.

    Matches how ``metadata.source`` records URL ingests
    (``url.split("?")[0].rstrip("/")``), so a state key and a stored source
    are the same string.
    """
    return url.split("?")[0].rstrip("/")


def _load_watched_state(p: Path) -> dict[str, dict[str, Any]]:
    """Return the parsed watched-source state for *p* (mtime-cached).

    Corrupt/missing file → empty state (a re-ingest of everything is the safe
    recovery; the state is an optimisation, never a correctness gate).
    """
    try:
        mtime = p.stat().st_mtime_ns if p.exists() else 0
    except OSError:
        return {}
    with _watched_state_cache_lock:
        cached = _watched_state_cache.get(p)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    state: dict[str, dict[str, Any]] = {}
    if mtime:
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state = {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}
        except (json.JSONDecodeError, OSError, ValueError):
            logger.debug("Unable to parse watched-source state %s — starting empty", p, exc_info=True)
    with _watched_state_cache_lock:
        _watched_state_cache[p] = (mtime, state)
        if len(_watched_state_cache) > 100:  # bound across many datasets
            _watched_state_cache.pop(next(iter(_watched_state_cache)), None)
    return state


def _save_watched_state(p: Path, state: dict[str, dict[str, Any]]) -> None:
    """Atomically persist *state* and refresh the read cache.

    Caller holds the cross-process lock (the read→modify→write must be
    serialised across pods sharing the RWX PVC).
    """
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)
    with _watched_state_cache_lock:
        _watched_state_cache[p] = (p.stat().st_mtime_ns, state)


# ── Preprocessing limits for files stored on PVC ─────────────────────────
_PVC_IMAGE_MAX_PIXELS: int = 1920 * 1080  # 2,073,600
_PVC_VIDEO_MAX_PIXELS: int = 1280 * 720  # 921,600
_PVC_VIDEO_MAX_FPS: float = 24.0

# Target frame count per video segment for *short* clips.  Short clips are
# sampled above the configured fps (toward this many frames per segment)
# instead of always falling back to ~1 frame/s; the configured fps remains a
# floor.  Set ``VIDEO_TARGET_FRAMES=0`` to restore the legacy fixed-fps
# behaviour.  Kept out of ``mm_processor_kwargs`` (which is forwarded to the
# embedding server) so the server never sees this client-side knob.
_VIDEO_TARGET_FRAMES: int = int(os.environ.get("VIDEO_TARGET_FRAMES", "30"))

# Audio files above this raw size (bytes) are truncated to avoid exceeding
# the embedding endpoint's JSON payload limit (~32 MiB). Base64 adds ~33%
# overhead, so ~20 MiB raw → ~27 MiB payload, leaving comfortable margin.
# Duration to truncate oversized audio files to (seconds).
_PVC_AUDIO_MAX_BYTES: int = 5 * 1024 * 1024

# Type-specific chunk size overrides for structured data (code, JSON, XML, YAML).
# These benefit from larger context windows to keep definitions intact.
# The actual chunk_size/chunk_overlap values come from the embedder model's
# ``code_chunk_size`` / ``code_chunk_overlap`` fields.
_STRUCTURED_TYPES: frozenset[str] = frozenset({"code", "json", "xml", "yaml"})


def _classify_file(path: str) -> str:
    """Return file type: ``'pdf'``, ``'image'``, ``'video'``, ``'audio'``,
    ``'text'``, ``'json'``, ``'table'``, ``'code'``, ``'office'``,
    ``'html'``, ``'xml'``, ``'yaml'``, ``'notebook'``, ``'ebook'``,
    ``'log'``, or ``'unknown'``."""
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _AUDIO_EXTS:
        return "audio"
    if ext in _TEXT_EXTS:
        return "text"
    if ext == ".json":
        return "json"
    if ext in _TABLE_EXTS:
        return "table"
    if ext in _CODE_EXTS:
        return "code"
    if ext in _OFFICE_EXTS:
        return "office"
    if ext in _HTML_EXTS:
        return "html"
    if ext in _XML_EXTS:
        return "xml"
    if ext in _YAML_EXTS:
        return "yaml"
    if ext in _NOTEBOOK_EXTS:
        return "notebook"
    if ext in _EBOOK_EXTS:
        return "ebook"
    if ext in _LOG_EXTS:
        return "log"
    return "unknown"


def _is_url(path: str) -> bool:
    return path.startswith(("http://", "https://", "s3://"))


def _get_s3_client() -> Any:
    """Return a configured boto3 S3 client.

    Reads connection details from environment variables so the same code
    works against AWS S3 or an on-cluster MinIO instance::

        S3_ENDPOINT_URL       — custom endpoint (e.g. http://minio.minio.svc.cluster.local:9000)
        S3_ACCESS_KEY_ID      — access key
        S3_SECRET_ACCESS_KEY  — secret key

    When ``S3_ENDPOINT_URL`` is unset the default boto3 credential chain
    (IAM roles, ~/.aws/credentials, etc.) is used.
    """
    try:
        import boto3
    except ImportError:
        raise ValueError("boto3 is required for S3 URLs. Install with: pip install boto3")

    endpoint = os.environ.get("S3_ENDPOINT_URL") or None
    access_key = os.environ.get("S3_ACCESS_KEY_ID") or None
    secret_key = os.environ.get("S3_SECRET_ACCESS_KEY") or None

    kwargs: dict[str, Any] = {}
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    if access_key and secret_key:
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key

    return boto3.client("s3", **kwargs)


def _download_s3(s3_url: str, timeout: int = 120) -> str:
    """Download a file from S3 to a temp location and return the local path.

    The object body is streamed to disk in 1 MiB chunks with the same
    size-cap abort as the HTTP download path, so a large object is never
    buffered fully in RAM.
    """
    import tempfile
    from urllib.parse import urlparse

    parsed = urlparse(s3_url)
    bucket = parsed.hostname  # s3://bucket/key → hostname = bucket
    key = parsed.path.lstrip("/")
    _check_s3_bucket(bucket)

    basename = Path(key).name or "download"
    suffix = Path(basename).suffix or ""
    stem = Path(basename).stem or "file"

    tmp = tempfile.NamedTemporaryFile(prefix=f"{stem}_", suffix=suffix, delete=False)
    tmp_path = ""
    try:

        def _fetch_to() -> None:
            # Each retry restarts the download from scratch.
            tmp.seek(0)
            tmp.truncate()
            s3 = _get_s3_client()
            resp = s3.get_object(Bucket=bucket, Key=key)
            body = resp["Body"]
            total = 0
            while True:
                buf = body.read(1 << 20)
                if not buf:
                    break
                total += len(buf)
                if _MAX_REMOTE_DOWNLOAD_BYTES > 0 and total > _MAX_REMOTE_DOWNLOAD_BYTES:
                    raise ValueError(
                        f"Download of {s3_url} exceeds MAX_REMOTE_DOWNLOAD_BYTES ({_MAX_REMOTE_DOWNLOAD_BYTES} bytes)"
                    )
                tmp.write(buf)

        try:
            retry_call(_fetch_to, max_attempts=3, base_delay=2.0, connection_delay=10.0)
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Failed to download {s3_url}: {e}")
        tmp_path = tmp.name
    finally:
        tmp.close()

    return tmp_path


def _is_s3_directory_url(url: str) -> bool:
    """Return ``True`` if *url* is an S3 URL that should be treated as a
    prefix (directory) rather than a single file.

    The heuristic: ``s3://bucket/key/`` (trailing slash) or
    ``s3://bucket/prefix`` (no detectable file extension).
    """
    if not url.startswith("s3://"):
        return False
    from urllib.parse import urlparse

    parsed = urlparse(url)
    key = parsed.path.lstrip("/")
    if not key:
        return True  # just s3://bucket — list the whole bucket
    # Trailing slash → directory
    if key.endswith("/"):
        return True
    # No extension → likely a prefix
    return "." not in key.rsplit("/", 1)[-1]


def _sync_prefixes(urls: list[str]) -> list[str]:
    """Normalize the S3 URLs an S3 sync reconciles against.

    One prefix per directory-like S3 URL: query stripped, exactly one
    trailing slash.  Single-object S3 URLs raise — syncing one object has no
    meaningful prune scope (use a prefix).
    """
    out: list[str] = []
    for u in urls:
        if not u.startswith("s3://"):
            continue
        if not _is_s3_directory_url(u):
            raise ValueError(f"sync requires an s3:// prefix (directory) URL, got a single-object URL: {u}")
        out.append(u.split("?")[0].rstrip("/") + "/")
    return out


# -- Prune mass-deletion guards (audit 2026-10-02, P1-4) ---------------------
# The empty-listing refusal needs no constant (it is NOT env-overridable —
# it is the one that protects a whole corpus).  The ratio guard is tunable:
# pruning more than max(min, ratio × in-scope points) points under one
# prefix is refused.  The floor keeps small corpora from tripping on a
# normal one- or two-object deletion; the ratio catches a listing that lost
# most of a large corpus.  Both are env-overridable for operators who
# deliberately want an aggressive prune (PRUNE_STALE_RATIO=0 disables the
# ratio guard entirely).
_PRUNE_STALE_RATIO: float = max(0.0, float(os.environ.get("PRUNE_STALE_RATIO", "0.2")))
_PRUNE_STALE_MIN: int = max(0, int(os.environ.get("PRUNE_STALE_MIN", "50")))


def _list_s3_prefix(s3_url: str) -> list[str]:
    """List all supported-file-type objects under an S3 prefix, **recursively**.

    Returns full ``s3://bucket/key`` URLs for every object whose extension
    is recognised by :func:`_classify_file`.  The listing paginates
    ``list_objects_v2`` with the ``Prefix`` filter only — that is a recursive
    walk of the whole subtree, not just the immediate level (directories are
    not recursed *as directories*; every object under the prefix shows up).
    Directory marker keys (``key/``) are skipped.

    (Historical bug fixed 2026-09: the docstring used to claim only the
    immediate level was listed — the code has always been recursive, which is
    exactly the behaviour S3 sync and watched sources rely on.)
    """
    from urllib.parse import urlparse

    parsed = urlparse(s3_url)
    bucket = parsed.hostname
    prefix = parsed.path.lstrip("/")
    _check_s3_bucket(bucket)

    def _list() -> list[str]:
        s3 = _get_s3_client()
        paginator = s3.get_paginator("list_objects_v2")
        result: list[str] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key: str = obj["Key"]
                # Skip "directory" markers
                if key.endswith("/"):
                    continue
                # Only include supported file types
                if _classify_file(key) != "unknown":
                    result.append(f"s3://{bucket}/{key}")
        return result

    try:
        urls = retry_call(_list, max_attempts=3, base_delay=2.0, connection_delay=10.0)
    except Exception as e:
        raise ValueError(f"Failed to list S3 prefix {s3_url}: {e}")

    return urls


def _list_s3_prefix_fingerprints(s3_url: str) -> dict[str, dict[str, Any]]:
    """List an S3 prefix (same recursive walk as :func:`_list_s3_prefix`) and
    return a per-object fingerprint map: ``s3://bucket/key → {etag, size}``.

    Watched-sources helper: the ETag+Size pair is the object fingerprint —
    ETag alone is not comparable across multipart uploads (S3 renders it as
    ``<md5>-N`` there) and Size alone misses same-length edits.  Bucket
    allowlist and the supported-file-type filter apply exactly as in the
    plain listing.  Objects without an ETag (shouldn't happen on real S3/
    MinIO listings) are still returned with ``etag: None`` so a missing
    fingerprint can never falsely "unchanged"-match.
    """
    from urllib.parse import urlparse

    parsed = urlparse(s3_url)
    bucket = parsed.hostname
    prefix = parsed.path.lstrip("/")
    _check_s3_bucket(bucket)

    def _list() -> dict[str, dict[str, Any]]:
        s3 = _get_s3_client()
        paginator = s3.get_paginator("list_objects_v2")
        out: dict[str, dict[str, Any]] = {}
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key: str = obj["Key"]
                if key.endswith("/"):
                    continue
                if _classify_file(key) != "unknown":
                    out[f"s3://{bucket}/{key}"] = {
                        "etag": obj.get("ETag"),
                        "size": obj.get("Size"),
                    }
        return out

    try:
        return retry_call(_list, max_attempts=3, base_delay=2.0, connection_delay=10.0)
    except Exception as e:
        raise ValueError(f"Failed to list S3 prefix {s3_url}: {e}")


def _expand_urls(raw_urls: list[str]) -> list[str]:
    """Expand a list of URLs, resolving S3 directory prefixes to
    individual file URLs.

    Non-S3 URLs and S3 file URLs are passed through unchanged.
    """
    expanded: list[str] = []
    for url in raw_urls:
        if url.startswith("s3://") and _is_s3_directory_url(url):
            expanded.extend(_list_s3_prefix(url))
        else:
            expanded.append(url)
    return expanded


# ---------------------------------------------------------------------------
# Remote-URL download guards (SSRF + size)
# ---------------------------------------------------------------------------

# Hard cap on a single remote/S3 download in bytes.  Streams are aborted past
# this bound so a mislabelled/malicious URL cannot exhaust memory or fill the
# PVC.  ``0`` disables the cap.
_MAX_REMOTE_DOWNLOAD_BYTES = max(0, int(os.environ.get("MAX_REMOTE_DOWNLOAD_BYTES", str(512 * 1024 * 1024))))

# Max http(s) redirect hops followed during URL ingest (each hop is re-checked
# against the URL policy).
_MAX_URL_REDIRECTS = max(1, int(os.environ.get("MAX_URL_REDIRECTS", "5")))

# Optional comma-separated allowlist of S3 buckets for ingest/sync.  Empty
# = all buckets the configured credentials can access (default, backward
# compatible).  When set, _download_s3 / _list_s3_prefix refuse any bucket
# not on the list — on a shared MinIO this stops one tenant's ingest token
# from reading another tenant's buckets.
_INGEST_ALLOW_S3_BUCKETS = tuple(
    b.strip().lower() for b in os.environ.get("INGEST_ALLOW_S3_BUCKETS", "").split(",") if b.strip()
)


def _check_s3_bucket(bucket: str | None) -> None:
    """Raise :class:`ValueError` when *bucket* violates INGEST_ALLOW_S3_BUCKETS."""
    if not _INGEST_ALLOW_S3_BUCKETS:
        return
    if (bucket or "").lower() not in _INGEST_ALLOW_S3_BUCKETS:
        raise ValueError(
            f"S3 bucket '{bucket}' is not allowed by INGEST_ALLOW_S3_BUCKETS"
            + (f"={','.join(_INGEST_ALLOW_S3_BUCKETS)}" if _INGEST_ALLOW_S3_BUCKETS else "")
        )


# URL-fetch policy (SSRF guards) lives in utils/url_policy.py — re-exported
# here because mcp_server (and older callers) import these names from
# dataset_manager.  rag_system imports the module directly (this module
# imports rag_system, so the reverse would be circular).
from multimodal_rag.utils.url_policy import (  # noqa: E402, F401 — re-exported
    _check_media_url_policy,
    _check_url_policy,
    _host_is_private,
    _host_matches_allowlist,
)


def _download_url(url: str, timeout: int = 120) -> str:
    """Download a remote file to a temp location and return the local path.

    Supports HTTP(S) and S3 URLs.  The temp file retains the URL's basename
    and extension so that ``_classify_file`` can identify the type correctly.
    Bytes are streamed to disk and aborted once
    ``MAX_REMOTE_DOWNLOAD_BYTES`` is exceeded.
    """
    if url.startswith("s3://"):
        return _download_s3(url, timeout=timeout)

    from multimodal_rag.utils.url_policy import validate_fetch_url

    _check_url_policy(url)
    import tempfile
    from urllib.parse import urlparse

    import httpx2

    parsed = urlparse(url)
    basename = Path(parsed.path).name or "download"
    suffix = Path(basename).suffix or ""
    stem = Path(basename).stem or "file"

    tmp = tempfile.NamedTemporaryFile(prefix=f"{stem}_", suffix=suffix, delete=False)
    try:
        try:
            # Follow redirects manually so every hop is re-checked against the
            # URL policy (a public URL must not be able to redirect into an
            # internal/private address), and connect each hop to the IP the
            # policy just validated — the DNS-rebinding pin (check-time DNS =
            # fetch-time DNS; utils/url_policy.validate_fetch_url).  A pinned
            # hop bypasses proxy envs on purpose (a proxy would re-resolve);
            # when a proxy is configured the pin is inactive and trust_env
            # keeps corporate egress working (documented residual).
            from urllib.parse import urljoin

            current = url
            for _ in range(_MAX_URL_REDIRECTS):
                pinned = validate_fetch_url(current, allow_loopback=False)
                with httpx2.Client(
                    timeout=httpx2.Timeout(timeout, connect=30.0),
                    follow_redirects=False,
                    trust_env=not pinned.pin_active,
                ) as client:
                    request = client.build_request(
                        "GET",
                        pinned.pinned_url,
                        headers={"Host": pinned.host_header} if pinned.host_header else None,
                        extensions=({"sni_hostname": pinned.sni_hostname} if pinned.sni_hostname else None),
                    )
                    response = client.send(request, stream=True, follow_redirects=False)
                    try:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                raise ValueError(f"Redirect from {current} has no Location header")
                            current = urljoin(current, location)
                            continue
                        response.raise_for_status()
                        content_length = int(response.headers.get("content-length") or 0)
                        if _MAX_REMOTE_DOWNLOAD_BYTES > 0 and content_length > _MAX_REMOTE_DOWNLOAD_BYTES:
                            raise ValueError(
                                f"Download of {url} exceeds MAX_REMOTE_DOWNLOAD_BYTES "
                                f"({content_length} > {_MAX_REMOTE_DOWNLOAD_BYTES} bytes)"
                            )
                        written = 0
                        for chunk in response.iter_bytes():
                            written += len(chunk)
                            if _MAX_REMOTE_DOWNLOAD_BYTES > 0 and written > _MAX_REMOTE_DOWNLOAD_BYTES:
                                raise ValueError(
                                    f"Download of {url} exceeds MAX_REMOTE_DOWNLOAD_BYTES ({_MAX_REMOTE_DOWNLOAD_BYTES} bytes)"
                                )
                            tmp.write(chunk)
                        break
                    finally:
                        response.close()
            else:
                raise ValueError(f"Too many redirects downloading {url}")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Failed to download {url}: {e}")
    except BaseException:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise
    else:
        return tmp.name
    finally:
        tmp.close()


# ---------------------------------------------------------------------------
# Password helpers (PBKDF2-SHA256 with random salt)
# ---------------------------------------------------------------------------


_PBKDF2_ITERATIONS = 600_000  # OWASP 2023 minimum for PBKDF2-SHA256


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS)
    return f"{salt}${h.hex()}"


def _check_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
        computed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS).hex()
        return secrets.compare_digest(h, computed)
    except (ValueError, AttributeError, TypeError):
        # Malformed stored hash / wrong argument types verify to False — the
        # stored value comes from meta.json and a corrupted (or hand-edited)
        # entry must fail CLOSED, never raise into the caller.
        return False


def _fix_source(chunks: list, orig_name: str, stored_path: str) -> None:
    """Keep ``source`` as the stored path and add ``original_source`` with
    the original basename, for every dict chunk that has a ``source`` key."""
    for chunk in chunks:
        if isinstance(chunk, dict) and "source" in chunk:
            chunk.setdefault("original_source", orig_name)
            chunk["source"] = stored_path


# ---------------------------------------------------------------------------
# PVC preprocessing helpers
# ---------------------------------------------------------------------------


def _mp4_boxes_broken(path: Path) -> bool:
    """Walk the top-level MP4 atoms; True on truncation or duplicate ``moov``.

    Cheap (header bytes only) but catches exactly the damage two concurrent
    ffmpeg finalizations on one path leave behind: a second ``moov`` spliced
    into the file and/or an ``mdat`` whose declared size overruns EOF.
    """
    try:
        size = path.stat().st_size
        seen_moov = False
        with path.open("rb") as fh:
            offset = 0
            while offset + 8 <= size:
                header = fh.read(8)
                if len(header) < 8:
                    return True
                box_size = int.from_bytes(header[:4], "big")
                box_type = header[4:8].decode("latin-1", "replace")
                if box_size == 1:
                    largesize = fh.read(8)
                    if len(largesize) < 8:
                        return True
                    box_size = int.from_bytes(largesize, "big")
                elif box_size == 0:
                    box_size = size - offset
                if box_size < 8 or offset + box_size > size:
                    return True  # box overruns EOF → truncated file
                if box_type == "moov":
                    if seen_moov:
                        return True  # two finalizations raced on this path
                    seen_moov = True
                offset += box_size
                fh.seek(offset)
            return offset != size  # stray trailing bytes
    except OSError:
        return True


def _media_file_ok(path: Path) -> bool:
    """True when *path* is a fully-formed media file ffprobe can parse.

    A file truncated mid-mux — or with a spliced-together trailer from two
    concurrent ffmpeg runs sharing one output path — often still has a
    positive size and a parseable leading ``moov``, so a naive
    ``size > 0`` check happily promotes unplayable output.  Validation here
    is cheap but real: ffprobe must exit 0 with at least one stream and a
    positive duration, and for MP4-family containers the top-level atom
    walk must find no truncation and no duplicated ``moov``.
    """
    import json
    import subprocess as sp

    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        probe = sp.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=index",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if probe.returncode != 0:
            return False
        info = json.loads(probe.stdout or "{}")
        streams = info.get("streams") or []
        duration = float((info.get("format") or {}).get("duration") or 0)
        if not streams or duration <= 0:
            return False
    except Exception:
        return False

    if path.suffix.lower() not in (".mp4", ".mov", ".m4a", ".m4v"):
        return True

    return not _mp4_boxes_broken(path)


def _image_file_ok(path: Path) -> bool:
    """True when PIL can fully decode *path* (guards torn image writes)."""
    from io import BytesIO

    from PIL import Image

    try:
        with Image.open(BytesIO(path.read_bytes())) as img:
            img.verify()
        return True
    except Exception:
        return False


def _unique_tmp_sibling(final: Path) -> Path:
    """A unique hidden temp path next to *final* (same filesystem, rename-safe).

    Keeps the final file's extension — ffmpeg infers the output muxer from
    the file extension, so an extension-less ``.tmp`` name would abort the
    transcode with "Unable to find a suitable output format".
    """
    return final.with_name(f".{final.stem}.{os.getpid()}.{uuid.uuid4().hex}.tmp{final.suffix}")


def _preprocess_image_file(path: Path, max_pixels: int = _PVC_IMAGE_MAX_PIXELS) -> Path:
    """Downscale an image on the PVC so width × height ≤ *max_pixels*.

    Aspect ratio is preserved.  Images already within the limit are
    returned unchanged.  A new ``*_preprocessed`` file is created
    alongside the original when resizing is needed.
    """
    from io import BytesIO

    from PIL import Image

    from multimodal_rag.input_processing.image_processor import _resize_image

    raw = path.read_bytes()
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"

    # Open under a context manager so the underlying file handle is released
    # immediately after the size check (raw bytes were already read above).
    with Image.open(BytesIO(raw)) as img:
        needs_resize = img.width * img.height > max_pixels
    if not needs_resize:
        return path

    resized = _resize_image(raw, mime, max_pixels)
    preproc = path.with_stem(path.stem + "_preprocessed")

    # Idempotency: another pod may have created a valid preprocessed copy
    # while this resize was in flight — don't clobber it.
    if preproc.exists() and _image_file_ok(preproc):
        return preproc

    tmp = _unique_tmp_sibling(preproc)
    try:
        resized = _resize_image(raw, mime, max_pixels)
        tmp.write_bytes(resized)
        if not _image_file_ok(tmp):
            tmp.unlink(missing_ok=True)
            return path  # write produced a broken file — keep the original
        tmp.replace(preproc)  # atomic on POSIX
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        logger.warning("Image preprocess write failed for %s; keeping original", path.name, exc_info=True)
        return path
    return preproc


def _preprocess_video_file(
    path: Path,
    max_pixels: int = _PVC_VIDEO_MAX_PIXELS,
    max_fps: float = _PVC_VIDEO_MAX_FPS,
) -> Path:
    """Transcode a video on the PVC to at most *max_pixels* per frame @ *max_fps*.

    Aspect ratio is preserved.  Videos already within both limits are
    returned unchanged.  A new ``*_preprocessed`` file is created
    alongside the original when any transcode is needed.
    """
    import json
    import subprocess as sp

    # Container/codec compatibility: the transcode always emits H.264
    # (libx264) + AAC, and ffmpeg picks the output muxer from the file
    # extension.  Some containers — `.webm` above all — cannot legally
    # carry that pair, so every transcode into them is doomed before it
    # starts: ffmpeg writes the container header, then fails on the first
    # packet (or burns the full timeout on a large file), leaving a tiny
    # stub that a naive size check would promote as "preprocessed".  Only
    # transcode into containers known to accept the codec pair; everything
    # else keeps its original (embedding decodes frames regardless of the
    # container, so retrieval is unaffected).
    if path.suffix.lower() not in (".mp4", ".m4v", ".mov", ".mkv"):
        logger.info(
            "Skipping video preprocess for %s: %s container cannot hold the libx264/aac transcode output",
            path.name,
            path.suffix.lower() or "(unknown)",
        )
        return path

    # Probe source dimensions and frame rate
    vw = vh = 0
    src_fps = 0.0
    try:
        probe = sp.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,r_frame_rate",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        pinfo = json.loads(probe.stdout)
        for s in pinfo.get("streams", []):
            vw = int(s.get("width", 0))
            vh = int(s.get("height", 0))
            r_frac = s.get("r_frame_rate", "0")
            if "/" in r_frac:
                num, den = r_frac.split("/", 1)
                src_fps = float(num) / float(den) if float(den) > 0 else 0.0
            else:
                src_fps = float(r_frac) if r_frac else 0.0
            break
    except Exception:
        logger.debug("Suppressed exception", exc_info=True)

    if vw <= 0 or vh <= 0:
        return path

    needs_scale = vw * vh > max_pixels
    needs_fps = max_fps > 0 and src_fps > max_fps

    if not needs_scale and not needs_fps:
        return path

    filters: list[str] = []
    if needs_scale:
        scale = (max_pixels / (vw * vh)) ** 0.5
        new_w = max(2, (int(vw * scale) // 2) * 2)
        new_h = max(2, (int(vh * scale) // 2) * 2)
        filters.append(f"scale={new_w}:{new_h}")
    if needs_fps:
        filters.append(f"fps={max_fps}")

    preproc = path.with_stem(path.stem + "_preprocessed")

    # Idempotency: another pod may have produced a valid preprocessed copy
    # while this transcode was in flight — don't clobber it.
    if preproc.exists() and _media_file_ok(preproc):
        return preproc

    tmp = _unique_tmp_sibling(preproc)
    cmd: list[str] = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(path),
        "-vf",
        ",".join(filters),
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "28",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        "-y",
        str(tmp),
    ]
    try:
        sp.run(cmd, capture_output=True, timeout=300)
        # Size > 0 is not enough: a raced or truncated mux leaves a file that
        # parses at the head but is unplayable.  Validate before promoting.
        if not _media_file_ok(tmp):
            tmp.unlink(missing_ok=True)
            logger.warning("Video preprocess produced invalid output for %s; keeping original", path.name)
            return path
        tmp.replace(preproc)  # atomic on POSIX
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        logger.debug("Video preprocess failed for %s", path.name, exc_info=True)
        return path

    return preproc


def _truncate_audio_file(path: Path, max_seconds: float = 60.0) -> Path:
    """Truncate audio to at most *max_seconds* duration.

    Audio already within the limit is returned unchanged.  A new
    ``*_truncated`` file is created alongside the original when
    truncation is needed.  Used at staging time so audio queries don't
    exceed the ASR endpoint's payload cap.
    """
    import subprocess as sp

    try:
        probe = sp.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        info = json.loads(probe.stdout)
        duration = float(info.get("format", {}).get("duration", 0))
    except Exception:
        logger.debug("Audio duration probe failed", exc_info=True)
        return path

    if duration <= max_seconds or duration <= 0:
        return path

    truncated = path.with_stem(path.stem + "_truncated")

    # Idempotency: don't redo (or clobber) another pod's valid truncation.
    if truncated.exists() and _media_file_ok(truncated):
        return truncated

    tmp = _unique_tmp_sibling(truncated)
    try:
        sp.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-t",
                str(max_seconds),
                "-i",
                str(path),
                "-c",
                "copy",
                "-y",
                str(tmp),
            ],
            capture_output=True,
            timeout=60,
        )
        # Stream copy may fail for some containers — re-encode as fallback
        if not _media_file_ok(tmp):
            sp.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-t",
                    str(max_seconds),
                    "-i",
                    str(path),
                    "-y",
                    str(tmp),
                ],
                capture_output=True,
                timeout=60,
            )
        if not _media_file_ok(tmp):
            tmp.unlink(missing_ok=True)
            logger.warning("Audio truncation produced invalid output for %s; keeping original", path.name)
            return path
        tmp.replace(truncated)  # atomic on POSIX
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        logger.debug("Audio truncation failed", exc_info=True)

    return path


def _split_audio_segments(
    path: Path,
    max_bytes: int = _PVC_AUDIO_MAX_BYTES,
) -> list[Path]:
    """Split an audio file into segments each roughly *max_bytes* in size.

    Files already within *max_bytes* are returned as a single-element list.
    Oversized files are split using ffmpeg's segment muxer so that each
    segment can be transcribed individually without hitting API payload
    limits.  Segment files are created alongside the original with a
    ``_segment_NNN`` suffix.
    """
    import json
    import math
    import subprocess as sp

    if path.stat().st_size <= max_bytes:
        return [path]

    # Get total duration via ffprobe
    try:
        probe = sp.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        info = json.loads(probe.stdout)
        total_duration = float(info.get("format", {}).get("duration", 0))
    except Exception:
        return [path]

    if total_duration <= 0:
        return [path]

    # Number of segments so each is roughly max_bytes
    num_segments = max(1, math.ceil(path.stat().st_size / max_bytes))
    segment_duration = total_duration / num_segments

    stem = path.stem
    suffix = path.suffix

    # Idempotency: another pod may have split this file already — reuse its
    # segments when the expected number of valid ones exists.
    import glob

    existing = sorted(Path(p) for p in glob.glob(str(path.parent / f"{stem}_segment_*{suffix}")))
    if len(existing) == num_segments and all(_media_file_ok(s) for s in existing):
        return existing

    # The segment muxer writes deterministic ``{stem}_segment_NNN`` names, so
    # concurrent writers would interleave exactly like the preprocessed-MP4
    # corruption (the fix in the *_preprocessed writers).  Split into a unique
    # temp directory, validate, then move each segment into place atomically
    # under the files-dir lock so the resulting SET is from one writer.
    import shutil

    tmp_dir = path.parent / f".{stem}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    output_pattern = str(tmp_dir / f"{stem}_segment_%03d{suffix}")

    cmd: list[str] = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(path),
        "-f",
        "segment",
        "-segment_time",
        str(segment_duration),
        "-c",
        "copy",
        "-y",
        output_pattern,
    ]
    try:
        with _cross_process_lock(path.parent / ".preprocess.lock"):
            tmp_dir.mkdir(parents=True, exist_ok=False)
            try:
                sp.run(cmd, capture_output=True, timeout=300)
            except Exception:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return [path]

            segments = sorted(Path(p) for p in glob.glob(str(tmp_dir / f"{stem}_segment_*{suffix}")))
            if not segments or not all(_media_file_ok(s) for s in segments):
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return [path]

            moved: list[Path] = []
            for seg in segments:
                final_seg = path.parent / seg.name
                seg.replace(final_seg)  # atomic on POSIX
                moved.append(final_seg)
            shutil.rmtree(tmp_dir, ignore_errors=True)
    except OSError:
        return [path]
    return moved if moved else [path]


class DatasetManager:
    """Create, populate, query and delete multimodal RAG datasets.

    Parameters
    ----------
    base_path:
        Root directory for dataset storage on the PVC.
        Default ``/data`` → datasets live under ``/data/datasets/``.
    qdrant_host:
        Qdrant hostname (in-cluster service DNS).
        Empty string (default) uses local ``qdrant_path`` instead.
    qdrant_port:
        Qdrant gRPC port.
    embedder, reranker, vlm, asr:
        Model instances.  A missing optional model (``None``) disables its
        feature (VLM captioning / ASR transcription / reranking) — no
        hardcoded fallback is applied.
    caption_with_asr:
        Whether to transcribe audio tracks from videos via ASR.
    remote:
        Whether to use remote (cluster) model endpoints.
    """

    def __init__(
        self,
        base_path: str = "/data",
        qdrant_host: str = "",
        qdrant_port: int = 6333,
        embedder=None,
        reranker=None,
        vlm=None,
        asr=None,
        caption_with_asr: bool = False,
        caption_with_vlm: bool = False,
        remote: bool = True,
        dedup_threshold: float = 0.995,
        contextualize: bool = False,
    ):
        self.base_path = Path(base_path)
        self.datasets_path = self.base_path / "datasets"
        self.datasets_path.mkdir(parents=True, exist_ok=True)

        self.qdrant_host = qdrant_host
        self.qdrant_port = qdrant_port

        # No hardcoded fallbacks: models come from the environment
        # (model_config.build_all, env MODEL_*_URL).  A missing optional
        # model stays None and simply disables its feature (VLM captioning /
        # ASR transcription / reranking) rather than silently pointing at a
        # baked-in endpoint.
        if embedder is None:
            raise RuntimeError("Embedder model is required — set MODEL_EMBEDDER_NAME and MODEL_EMBEDDER_URL.")

        self.embedder = embedder
        self.reranker = reranker
        self.vlm = vlm
        self.asr = asr
        self.caption_with_asr = caption_with_asr
        self.caption_with_vlm = caption_with_vlm
        self.remote = remote
        self.dedup_threshold = dedup_threshold
        # Per-dataset default for the contextual flag on CREATE when the
        # request body omits it (RAG_CONTEXTUAL_DEFAULT, chart values
        # rag.contextual — disabled by default).  Also what the fingerprint
        # guard reads as "the mode the manager would ingest with" before
        # meta stamps anything.  Feature module: utils/contextualizer.py.
        self.contextualize = bool(contextualize)

        self._verify_endpoints()

        # Cache: dataset_name → MultimodalRAG instance
        self._has_password_cache: dict[str, tuple[float, bool]] = {}
        self._has_password_lock = threading.Lock()
        self._rag_cache: OrderedDict[str, MultimodalRAG] = OrderedDict()
        self._rag_cache_lock = threading.Lock()
        # LRU cap: each cached MultimodalRAG holds its own QdrantClient
        # (connection pool + sockets).  Unbounded growth — one client per
        # dataset ever touched, for the process lifetime — matters on
        # servers serving many datasets.  Eviction closes the client.
        self._rag_cache_max = max(1, int(os.environ.get("RAG_CACHE_MAX", "64")))

        # Embedder fingerprint support: current embedder's vector dimension
        # (probed once per embedder object) and the set of datasets whose
        # collection has already been verified compatible this process.
        self._embedder_dim_cache: dict[int, int | None] = {}
        self._embedder_verified: set[str] = set()

        # Note: per-dataset meta.json locking is handled by
        # _get_meta_lock() which returns a cross-process file lock
        # (fcntl.flock on a .meta.lock sidecar file).  No in-process
        # lock dict is needed.

    # ------------------------------------------------------------------
    # Model endpoint verification
    # ------------------------------------------------------------------

    @staticmethod
    def _verify_endpoint(model: Any, role: str) -> None:
        """Verify a model endpoint is reachable via its OpenAI-compatible
        ``/v1/models`` endpoint."""
        if model is None:
            return
        url = model.url_remote.rstrip("/")
        headers: dict[str, str] = {}
        if model.api_key:
            headers["Authorization"] = f"Bearer {model.api_key}"
        try:
            with httpx2.Client(verify=False, timeout=5.0) as client:
                resp = client.get(url + "/v1/models", headers=headers)
                resp.raise_for_status()
                logger.info(
                    "✓ %s endpoint verified via /v1/models (%s)",
                    role,
                    url,
                )
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Model '{role}' ({model.model_name}) at {url} is not reachable: {exc}")

    def _verify_endpoints(self) -> None:
        """Run endpoint verification for all configured models.

        The embedder is required — if it's unreachable, raise.  All other
        models (reranker, vlm, asr) are optional: a warning is logged but
        startup proceeds so the system can degrade gracefully without them.
        """
        self._verify_endpoint(self.embedder, "embedder")
        for role, model in (
            ("reranker", self.reranker),
            ("vlm", self.vlm),
            ("asr", self.asr),
        ):
            try:
                self._verify_endpoint(model, role)
            except RuntimeError as exc:
                logger.warning(
                    "Optional model '%s' endpoint not reachable — system will work without %s capabilities: %s",
                    role,
                    role,
                    exc,
                )

    def check_model_connections(self) -> list[dict[str, Any]]:
        """Live-check every configured model endpoint via ``/v1/models``.

        Returns one entry per role with a three-state status:
        ``healthy`` (reachable), ``not_provided`` (not configured), or
        ``unhealthy`` (configured but unreachable).  Never raises.
        """
        results: list[dict[str, Any]] = []
        for role, model in (
            ("embedder", self.embedder),
            ("reranker", self.reranker),
            ("vlm", self.vlm),
            ("asr", self.asr),
        ):
            entry: dict[str, Any] = {
                "role": role,
                "status": "not_provided",
                "model_name": None,
                "url": None,
                "error": None,
            }
            if model is None:
                results.append(entry)
                continue
            entry["model_name"] = model.model_name
            entry["url"] = model.url_remote.rstrip("/")
            try:
                self._verify_endpoint(model, role)
                entry["status"] = "healthy"
            except Exception as exc:
                entry["status"] = "unhealthy"
                entry["error"] = str(exc)
            results.append(entry)
        return results

    # ------------------------------------------------------------------
    # Dataset lifecycle
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Dataset name validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_name(name: str) -> None:
        """Reject dataset names that could escape the datasets directory.

        Names must start with an alphanumeric character and contain only
        ``[A-Za-z0-9._-]``.  This prevents path-traversal via ``../`` in
        dataset names reaching ``mkdir`` / ``shutil.rmtree``.
        """
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", name):
            raise ValueError(
                f"Invalid dataset name {name!r}. Names must start with an "
                "alphanumeric character and contain only [A-Za-z0-9._-]."
            )

    # ------------------------------------------------------------------
    # Dataset lifecycle
    # ------------------------------------------------------------------

    def create_dataset(
        self,
        name: str,
        description: str = "",
        caption_with_asr: bool = False,
        caption_with_vlm: bool = False,
        keep_originals: bool = True,
        password: str | None = None,
        ocr: bool = False,
        rrf: "dict[str, Any] | None" = None,
        contextual: bool = False,
        created_by: "str | None" = None,
    ) -> dict[str, Any]:
        """Create a new dataset.

        Parameters
        ----------
        caption_with_asr:
            Whether to transcribe audio tracks from videos during
            preprocessing.  Set per-dataset — applies to all files and
            documents added to this dataset.
        caption_with_vlm:
            Whether to generate VLM descriptions of images/videos during
            preprocessing (even when the embedder supports them natively).
            Descriptions enrich the text and are reused at retrieval time
            for generic queries, avoiding a VLM call.
        keep_originals:
            Whether to keep original (full-quality) files on disk after
            preprocessing.  When False, the original is deleted once a
            preprocessed copy exists, saving disk space.  The Qdrant
            ``original_image``/``original_video`` references are also
            removed.  Default True.
        password:
            Optional password to protect read access to the dataset.
            Stored as a PBKDF2-SHA256 hash.
        ocr:
            Whether to OCR scanned PDF pages (pages carrying images but no
            text layer) through the tesseract CLI during preprocessing.
            Off by default — OCR on a large scan is expensive and must be
            opted into.  Requires the tesseract binary in the image (shipped
            in the Dockerfile); without it the flag is a no-op.
        rrf:
            Optional per-dataset weighted-RRF defaults (feature: weighted
            RRF, dataset-defaults slice): ``{"dense_weight": …,
            "sparse_weight": …, "k": …}``, all optional keys.  Stored in
            meta.json; validated with the same clamp discipline as the
            REST/MCP search surfaces (weights 0.0–10.0 rounded to 3
            decimals, k 1–1000).  ``None``/empty stores nothing — the
            dataset then simply inherits the global default (1.0/1.0).
            Query-time only: no recreate, no schema change; applies to
            single-dataset searches when the caller passes no explicit
            override (federated search ignores per-dataset defaults).
        contextual:
            Whether ingest-time contextual retrieval runs for this dataset
            (feature: contextual retrieval, DECISIONS.md 2026-09): one small
            LLM call per real-text chunk writes 1–2 sentences of
            document-level context, prepended as a ``[Document context]:``
            line before embedding.  Off by default — one LLM call per chunk
            is real cost and must be opted into (the
            ``/api/admin/datasets/{name}/contextual-preview`` endpoint makes
            it visible before the flag flips).  Requires a VLM model; without
            one the flag is a no-op.  Affects NEW ingests only — enabling it
            on an existing dataset does NOT re-contextualize stored content
            (content-hash dedup would skip the re-embed anyway); run Recreate
            to re-contextualize existing files.
        created_by:
            D23 dataset ownership: the CREATOR identity name stamped into
            meta.json at create time.  ``None`` stores nothing (the dataset
            is then "pre-D23" — deletable by admins only, never toggleable
            public by a non-admin).  The REST surface passes the request's
            resolved identity: a registry/JWT/SSO name, ``"admin"`` (admins
            act as the deployment), or ``"anonymous"``.
        """
        self._validate_name(name)
        dataset_dir = self.datasets_path / name
        if dataset_dir.exists():
            raise FileExistsError(f"Dataset '{name}' already exists")
        dataset_dir.mkdir(parents=True)
        (dataset_dir / "files").mkdir()

        meta: dict[str, Any] = {
            "name": name,
            "description": description,
            "caption_with_asr": caption_with_asr,
            "caption_with_vlm": caption_with_vlm,
            "keep_originals": keep_originals,
            "ocr": bool(ocr),
            "contextual": bool(contextual),
            "created": datetime.now().isoformat(),
            "document_count": 0,
            # The collection _get_rag creates below uses the current schema
            # (hybrid dense+BM25, roadmap feature 2) — record it so the
            # fingerprint guard can tell v1 datasets apart when nudging a
            # recreate.
            "schema_version": DATASET_SCHEMA_VERSION,
        }
        rrf_meta = self._sanitize_rrf_meta(rrf)
        if rrf_meta is not None:
            meta["rrf"] = rrf_meta
        # D23 ownership: the creator's identity name — the delete/public
        # surfaces read it back.  Empty/None stores nothing (pre-D23 shape).
        creator = str(created_by or "").strip()
        if creator:
            meta["created_by"] = creator
        if password:
            meta["password_hash"] = _hash_password(password)
        self._write_meta(name, meta)
        # A same-name recreate (delete + create, e.g. an import overwrite) on
        # one pod must not inherit another pod's stale has_password negative
        # — the import-overwrite gate trusts this value.
        self._invalidate_has_password(name)

        # Signal dataset existence to other pods via Redis (NFS cache bypass)
        _dataset_exist_set(name)

        # Pre-create the Qdrant collection by initialising the RAG instance
        self._get_rag(name)
        logger.info(
            "Created dataset '%s' (caption_with_asr=%s, rrf_defaults=%s, contextual=%s)",
            name,
            caption_with_asr,
            rrf_meta is not None,
            bool(contextual),
        )
        return self._strip_password(meta)

    def has_password(self, name: str) -> bool:
        # TTL-cached: every search/unlock called this, i.e. an NFS stat+read
        # + JSON parse of meta.json PER REQUEST for a value that changes only
        # when a password is set/removed.  Once the read was parallelised
        # (async offload), 4 replicas' worth of concurrent metadata reads
        # degraded the NFS server and search throughput declined within a
        # single benchmark run.  30s TTL; invalidated on password change and
        # dataset delete.
        now = time.monotonic()
        with self._has_password_lock:
            entry = self._has_password_cache.get(name)
            if entry is not None and now - entry[0] < 30.0:
                return entry[1]
        meta = self._read_meta(name)
        result = bool(meta and meta.get("password_hash"))
        with self._has_password_lock:
            self._has_password_cache[name] = (now, result)
        return result

    def has_password_fresh(self, name: str) -> bool:
        """Uncached protection check that tolerates NFS visibility delays.

        Use for SECURITY DECISIONS, not hot paths: the import-overwrite gate
        consulted the TTL-cached :meth:`has_password`, and on the 4-replica
        rollout a pod that had recently cached a negative (dataset not yet
        visible / not yet carrying a hash on this pod) skipped the gate and
        REPLACED a password-protected dataset without demanding its password.
        A stale negative is harmless for search throughput but fatal for an
        authz check.

        Mirrors :meth:`get_dataset`'s bridge over the NFS close-to-open gap:
        when Redis confirms the dataset exists but ``meta.json`` isn't
        visible (or is stale) on this pod yet, retry briefly before
        answering.  The result is deliberately NOT written to the TTL cache.
        """
        meta = self._read_meta(name)
        if not meta and _dataset_exist_check(name):
            for _ in range(_DATASET_EXIST_RETRY_COUNT):
                time.sleep(_DATASET_EXIST_RETRY_DELAY)
                meta = self._read_meta(name)
                if meta:
                    break
        return bool(meta and meta.get("password_hash"))

    def _invalidate_has_password(self, name: str) -> None:
        with self._has_password_lock:
            self._has_password_cache.pop(name, None)
        # Both caches describe the same file and must move together: every
        # password set/remove (and every dataset delete/recreate, which call
        # this) drops the parsed-meta entry too, so a has_password decision
        # can never be served from a meta read that predates the change.
        _invalidate_meta_cache(self._meta_path(name))

    def verify_password(self, name: str, password: str) -> bool:
        meta = self._read_meta(name)
        if not meta:
            raise FileNotFoundError(f"Dataset '{name}' not found")
        stored = meta.get("password_hash")
        if not stored:
            return True  # no password set → always passes
        return _check_password(password, stored)

    def set_password(self, name: str, password: str | None) -> None:
        self._invalidate_has_password(name)
        meta = self._read_meta(name)
        if not meta:
            raise FileNotFoundError(f"Dataset '{name}' not found")
        if password:
            meta["password_hash"] = _hash_password(password)
        else:
            meta.pop("password_hash", None)
        self._write_meta(name, meta)

    def set_public(self, name: str, public: bool) -> dict[str, Any]:
        """Set the dataset's "public" flag (feature: public-to-all-keys).

        A public dataset is readable by every AUTHENTICATED registry-client
        key (D15/D16/D17 identities) without an explicit grant - list, read
        and search. Guardrails:

          * ADMIN-ONLY operation: the REST surface is
            POST /api/admin/datasets/{name}/public (the middleware 403s
            client keys on /api/admin/*) - this method trusts its caller
            to have resolved an admin identity.
          * A password-protected dataset can NEVER be made public: the flag
            would silently publish password-gated content to every minted
            key. ValueError - remove the password first
            (set_password(name, None)) if publication is really intended.
            The read side (clients_registry.is_public_dataset) enforces
            the same rule, so even a hand-edited meta.json cannot publish a
            protected dataset.

        The flag is unstamped (key removed) when *public* is false, so
        datasets created before the feature - and recreated datasets - are
        private by default. Query-time only: no RAG rebuild, no re-ingest;
        effective on the next request from any replica (meta.json is
        re-read per check, mtime-cached).
        """
        with self._get_meta_lock(name):
            meta = self._read_meta(name)
            if not meta:
                raise FileNotFoundError(f"Dataset '{name}' not found")
            if public and meta.get("password_hash"):
                raise ValueError(
                    "Refusing to make a password-protected dataset public - "
                    "remove the password first if you really mean to publish it."
                )
            if public:
                meta["public"] = True
            else:
                meta.pop("public", None)
            self._write_meta(name, meta)
            return {
                "name": name,
                "public": "public" in meta,
                "has_password": "password_hash" in meta,
            }

    @staticmethod
    def _strip_password(meta: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of *meta* with the password hash removed and a
        ``has_password`` boolean added."""
        m = dict(meta)
        m["has_password"] = "password_hash" in m
        m.pop("password_hash", None)
        return m

    def delete_dataset(self, name: str) -> None:
        """Delete a dataset and its Qdrant collection."""
        self._validate_name(name)
        self._invalidate_has_password(name)
        rag = self._get_rag(name, check_embedder=False)
        try:
            vs = rag.vector_store
            assert vs is not None and not isinstance(vs, dict)
            client = vs._client  # type: ignore[attr-defined]
            client.delete_collection(vs.collection_name)  # type: ignore[attr-defined]
        except Exception:
            logger.debug("Suppressed exception", exc_info=True)
        if name in self._rag_cache:
            del self._rag_cache[name]

        dataset_dir = self.datasets_path / name
        if dataset_dir.exists():
            import shutil

            shutil.rmtree(dataset_dir)

        # Drop any cached hash index for the deleted files dir.
        with _hash_index_cache_lock:
            _hash_index_cache.pop(self._dataset_dir(name) / "files" / ".hashes.json", None)

        # …and the cached meta parse: the file is gone (mtime/size would miss
        # anyway, but a same-stamp recreate must not inherit the dead entry).
        _invalidate_meta_cache(self._meta_path(name))

        # Invalidate Redis existence cache so other pods don't retry
        _dataset_exist_delete(name)

        logger.info("Deleted dataset '%s'", name)

    def _list_recreate_files(self, dataset_name: str) -> list[tuple[str, str]]:
        """Return ``(path, name)`` pairs of the original files to rebuild.

        Rebuild candidates are the files recorded in ``.hashes.json`` (the
        dedup index of uploaded originals), falling back to any regular file
        in the dataset's ``files/`` directory that is not a derived artifact
        (``*_preprocessed*``, ``*_segment_NNN*``, ``{hash}_image`` /
        ``{hash}_audio`` media, dotfiles).  Returns an empty list when the
        dataset has nothing to rebuild.
        """
        files_dir = self._dataset_dir(dataset_name) / "files"
        if not files_dir.is_dir():
            return []
        seen: set[str] = set()
        entries: list[tuple[str, str]] = []

        # 1. Originals recorded in the upload dedup index
        hash_index_path = files_dir / ".hashes.json"
        if hash_index_path.exists():
            for dest in (_load_hash_index(hash_index_path) or {}).values():
                p = Path(dest)
                if p.exists() and str(p) not in seen:
                    seen.add(str(p))
                    entries.append((str(p), p.name))

        # 2. Fallback: any plausible original file
        derived = re.compile(r"^[0-9a-f]{32}_(?:image|audio|video)\b")
        for f in sorted(files_dir.iterdir()):
            if not f.is_file() or f.name.startswith("."):
                continue
            name = f.name
            if name in (".hashes.json",) or name.endswith(".lock"):
                continue
            if "_preprocessed" in name or re.search(r"_segment_\d+", name):
                continue
            if derived.search(name):
                continue
            if str(f) not in seen:
                seen.add(str(f))
                entries.append((str(f), name))
        return entries

    def recreate_dataset(
        self,
        dataset_name: str,
        file_entries: list[tuple[str, str]] | None = None,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        """Rebuild a dataset's Qdrant collection from its on-disk originals.

        Drops the existing collection (old vectors are incompatible with the
        currently configured embedder), resets the document counters and
        embedder fingerprint, then re-ingests the dataset's original files
        with the **current** embedder.  Useful after an embedder swap.

        ``file_entries`` is an optional ``[(path, name)]`` list; when omitted
        it is derived from :meth:`_list_recreate_files`.  Returns the batch
        result from :meth:`add_files_batch`.
        """
        if file_entries is None:
            file_entries = self._list_recreate_files(dataset_name)
        if not file_entries:
            return {"status": "ok", "file_count": 0, "files": []}

        # Drop the old collection + cached RAG instance (bypass the embedder
        # guard — this is the recovery path for a mismatch).
        try:
            rag = self._get_rag(dataset_name, check_embedder=False)
            vs = rag.vector_store
            if vs is not None and not isinstance(vs, dict):
                client = vs._client  # type: ignore[attr-defined]
                client.delete_collection(vs.collection_name)  # type: ignore[attr-defined]
        except Exception:
            logger.debug("Suppressed exception while dropping collection", exc_info=True)
        self._rag_cache.pop(dataset_name, None)
        self._embedder_verified.discard(dataset_name)

        # Reset counters + fingerprint so the fresh collection re-records.
        with self._get_meta_lock(dataset_name):
            meta = self._read_meta(dataset_name) or {}
            meta["document_count"] = 0
            meta.pop("file_type_counts", None)
            meta.pop("embedder_model", None)
            meta.pop("embedder_dim", None)
            meta.pop("embedder_updated_at", None)
            # The rebuilt collection carries the CURRENT schema (named dense
            # + bm25 sparse) — record it so the schema-upgrade nudge stops
            # firing for this dataset.
            meta["schema_version"] = DATASET_SCHEMA_VERSION
            self._write_meta(dataset_name, meta)

        # The BM25 df stats belong to the old collection — re-ingesting on
        # top of them would double every document frequency and flatten the
        # idf weighting.
        self.reset_bm25_stats(dataset_name)

        logger.info(
            "Recreating dataset '%s' from %d file(s)",
            dataset_name,
            len(file_entries),
        )
        # Drop the ingest-dedup index too: recreate deliberately re-embeds the
        # same on-disk files, but add_files_batch would otherwise treat every
        # previously-ingested content hash as "already ingested" and skip the
        # file — leaving a freshly-dropped, empty collection.
        self._clear_ingested_hashes(dataset_name)
        # Same reasoning for the watched-source skip state: the collection was
        # dropped, so every watched prefix must re-ingest on the next sync.
        self._watched_state_clear(dataset_name)
        return self.add_files_batch(dataset_name, file_entries, progress_callback=progress_callback)

    # ------------------------------------------------------------------
    # Backup restore / import (roadmap feature 4)
    # ------------------------------------------------------------------

    # Payload keys that reference media stored *outside* documents.jsonl —
    # dropped in text-replay mode where the referenced files were never
    # exported (raw-documents datasets have no files/).
    _REPLAY_STRIP_KEYS = (
        "image",
        "video",
        "audio",
        "original_image",
        "original_video",
        "preprocessed_image",
        "preprocessed_video",
    )

    # Import bounds (M-1 hardening).  Unlike ingested archives (which go
    # through archive_processor's pre-extraction audit), a backup .tar.gz
    # used to be extracted with no cap on member count or total unpacked
    # size — a ≤MAX_UPLOAD_BYTES tar.gz can expand ~1000× (gzip bomb) and
    # fill the PVC.  Declared tar sizes are authoritative, so the audit is
    # cheap.  ``0`` disables a cap.
    _MAX_IMPORT_EXTRACT_BYTES = max(0, int(os.environ.get("MAX_IMPORT_EXTRACT_BYTES", str(8 * 1024 * 1024 * 1024))))
    _MAX_IMPORT_MEMBERS = max(0, int(os.environ.get("MAX_IMPORT_MEMBERS", "20000")))
    _MAX_IMPORT_META_BYTES = 4 * 1024 * 1024

    @staticmethod
    def peek_backup_meta(tar_path: str | Path) -> dict[str, Any]:
        """Read just the ``meta.json`` of a dataset export without extracting.

        Used by the REST import endpoint to learn the *target* dataset name
        (``new_name`` fallback: the archive's original name) BEFORE
        :meth:`prepare_import` runs, so an ``overwrite=true`` import of an
        existing password-protected dataset can demand that dataset's
        password first (destructive routes are password-gated — deleting
        must not be easier than reading).
        """
        import tarfile

        with tarfile.open(Path(tar_path), "r:*") as tar:
            info = tar.getmember("meta.json")  # raises KeyError if absent
            if info.size > DatasetManager._MAX_IMPORT_META_BYTES:
                raise ValueError("Backup meta.json is implausibly large — not a dataset export")
            member = tar.extractfile(info)
            if member is None:
                raise ValueError("Backup archive has no meta.json — not a dataset export")
            meta = json.loads(member.read().decode("utf-8"))
        if not isinstance(meta, dict):
            raise TypeError("Backup meta.json is malformed")
        return meta

    def prepare_import(
        self,
        tar_path: str | Path,
        *,
        new_name: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Validate and unpack a dataset export; return the restore plan.

        Fast, side-effect-only-up-to-meta step of a restore: reads the
        archive (structure-checked), restores ``files/`` under the target
        dataset dir and writes the restored ``meta.json`` — everything the
        slow re-embed phase needs.  The caller then jobifies either
        :meth:`recreate_dataset` (files mode) or
        :meth:`replay_imported_documents` (text-replay mode).

        Raises ``ValueError`` for non-export archives, unsafe members, bad
        target names, or existing targets without ``overwrite=True``.
        """
        import shutil
        import tarfile

        src = Path(tar_path)
        if not src.is_file():
            raise FileNotFoundError(f"Backup archive not found: {src}")

        with tarfile.open(src, "r:*") as tar:
            members = tar.getmembers()
            if self._MAX_IMPORT_MEMBERS and len(members) > self._MAX_IMPORT_MEMBERS:
                raise ValueError(
                    f"Backup archive has too many members ({len(members)} > {self._MAX_IMPORT_MEMBERS}; "
                    f"tune MAX_IMPORT_MEMBERS)"
                )
            total_declared = sum(m.size for m in members)
            if self._MAX_IMPORT_EXTRACT_BYTES and total_declared > self._MAX_IMPORT_EXTRACT_BYTES:
                raise ValueError(
                    f"Backup archive unpacks to {total_declared} bytes — exceeds "
                    f"MAX_IMPORT_EXTRACT_BYTES ({self._MAX_IMPORT_EXTRACT_BYTES})"
                )
            for m in members:
                name = m.name
                if name not in ("meta.json", "documents.jsonl") and not name.startswith("files/"):
                    raise ValueError(f"Unexpected member in backup archive: {name!r}")
                if name.startswith(("/", "..")) or ".." in Path(name).parts:
                    raise ValueError(f"Unsafe path in backup archive: {name!r}")

            meta_member = tar.extractfile("meta.json")
            if meta_member is None:
                raise ValueError("Backup archive has no meta.json — not a dataset export")
            meta_size = next(m.size for m in members if m.name == "meta.json")
            if meta_size > self._MAX_IMPORT_META_BYTES:
                raise ValueError("Backup meta.json is implausibly large — not a dataset export")
            backup_meta = json.loads(meta_member.read().decode("utf-8"))
            if not isinstance(backup_meta, dict):
                raise TypeError("Backup meta.json is malformed")

            original_name = str(backup_meta.get("name") or src.stem)

            docs_member = None
            try:
                docs_member = tar.extractfile("documents.jsonl")
            except KeyError:
                docs_member = None  # tolerate archives without a documents member
            row_count = 0
            if docs_member is not None:
                # Stream the count — documents.jsonl can be hundreds of MB and
                # is only needed line-by-line (never buffered whole).
                for raw_line in docs_member:
                    if raw_line.strip():
                        row_count += 1

            target = new_name or original_name
            self._validate_name(target)

            dataset_dir = self.datasets_path / target
            if dataset_dir.exists() and not overwrite:
                raise FileExistsError(f"Dataset '{target}' already exists; pass overwrite=true to replace it.")

            # Stage the restore in a hidden sibling dir first: the existing
            # dataset is replaced only AFTER the archive is fully unpacked and
            # a restorable mode is determined — a bad/partial archive must
            # never destroy the dataset it was meant to replace.
            staging = self.datasets_path / f".{target}.importing-{os.getpid()}-{int(time.time())}"
            try:
                (staging / "files").mkdir(parents=True)
                file_members = [m for m in members if m.name.startswith("files/") and m.isfile()]
                if file_members:
                    # filter="data" refuses traversal, absolute paths, devices.
                    tar.extractall(staging, members=file_members, filter="data")

                has_files = any(p.is_file() for p in (staging / "files").rglob("*"))
                mode = "re-embed-files" if has_files else ("text-replay" if row_count else None)
                if mode is None:
                    raise ValueError("Backup archive contains no files and no documents — nothing to restore")
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise

            # Swap: drop the existing dataset (dir + old collection), then move
            # the staged files into place.  Past this point the archive is
            # known-good; a failure in the later re-embed phase leaves a
            # dataset that a re-run of the import repairs.
            if dataset_dir.exists():
                self.delete_dataset(target)
            dataset_dir.mkdir(parents=True, exist_ok=True)
            (staging / "files").rename(dataset_dir / "files")
            shutil.rmtree(staging, ignore_errors=True)

        restored_meta = dict(backup_meta)
        restored_meta["name"] = target
        restored_meta["document_count"] = 0
        # both restore modes rebuild the collection with the current schema -
        # stamp it so the hybrid-capability nudge does not fire spuriously
        restored_meta["schema_version"] = DATASET_SCHEMA_VERSION
        for stale in (
            "password_hash",
            "embedder_model",
            "embedder_dim",
            "embedder_updated_at",
            "file_type_counts",
        ):
            restored_meta.pop(stale, None)
        self._write_meta(target, restored_meta)

        return {
            "dataset": target,
            "original_name": original_name,
            "mode": mode,
            "file_count": len(file_members),
            "document_rows": row_count,
            "had_password": "password_hash" in backup_meta,
        }

    def replay_imported_documents(
        self,
        dataset_name: str,
        tar_path: str | Path,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        """Text-replay half of a restore: re-embed ``documents.jsonl`` rows.

        For backups of raw-documents datasets (no ``files/``).  Each row's
        ``page_content`` is re-embedded as a raw document with its lightweight
        metadata (media payload keys are stripped — they reference files the
        backup never contained).  Rows whose text is a bare media placeholder
        are skipped.  ``metadata.source`` values keep pointing at the
        original PVC paths and may dangle; ``file_type`` is re-stamped from
        the source extension.  Run under a job — it embeds everything.
        """
        import re as _re
        import tarfile

        placeholder = _re.compile(r"^\s*\[(?:Image|Video|Audio)[^\]]*\][\d\s–—-]*$")
        counted = 0
        skipped = 0
        batch: list[str | dict[str, Any]] = []

        def _flush() -> None:
            nonlocal counted
            if batch:
                # Restore replay: one logical ingest, not hundreds of
                # per-batch webhook events — mute the webhook inside.
                with _webhook_muted():
                    self.add_documents(dataset_name, batch)
                counted += len(batch)
                batch.clear()
                if progress_callback:
                    try:
                        progress_callback({"event": "embedding", "chunks": counted})
                    except Exception:
                        logger.debug("import progress callback failed", exc_info=True)

        with tarfile.open(Path(tar_path), "r:*") as tar:
            member = tar.extractfile("documents.jsonl")
            if member is None:
                raise ValueError("Backup archive has no documents.jsonl")
            for raw in member.read().decode("utf-8").splitlines():
                line = raw.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                payload = row.get("payload") or {}
                text = str(payload.get("page_content") or "")
                meta = payload.get("metadata") or {}
                if not text.strip() or placeholder.match(text):
                    skipped += 1
                    continue
                doc: dict[str, Any] = {"text": text}
                for k, v in meta.items():
                    if k not in self._REPLAY_STRIP_KEYS:
                        doc[k] = v
                batch.append(doc)
                if len(batch) >= 64:
                    _flush()
            _flush()

        return {
            "status": "ok",
            "dataset": dataset_name,
            "mode": "text-replay",
            "documents": counted,
            "skipped": skipped,
        }

    def import_dataset(
        self,
        tar_path: str | Path,
        *,
        new_name: str | None = None,
        overwrite: bool = False,
        password: str | None = None,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        """Synchronous end-to-end restore (prepare + re-embed).

        Convenience wrapper running :meth:`prepare_import` and then the
        appropriate slow phase inline.  The REST endpoint jobifies the two
        phases separately instead; tests and scripts use this.
        """
        plan = self.prepare_import(tar_path, new_name=new_name, overwrite=overwrite)
        target = plan["dataset"]
        if password:
            self.set_password(target, password)
        if plan["mode"] == "re-embed-files":
            result = self.recreate_dataset(target, progress_callback=progress_callback)
            return {**plan, "result": result, "password_set": bool(password)}
        result = self.replay_imported_documents(target, tar_path, progress_callback=progress_callback)
        return {**plan, "result": result, "password_set": bool(password)}

    # The return shape depends on the call mode: no cursor/limit → the
    # historical full listing (list); either given → a page dict.  The
    # overloads let callers get the shape they actually receive back.
    @overload
    def list_datasets(self, cursor: None = None, limit: None = None) -> list[dict[str, Any]]: ...

    @overload
    def list_datasets(self, cursor: str | None = ..., limit: int | None = ...) -> dict[str, Any]: ...

    def list_datasets(
        self, cursor: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Return metadata for all existing datasets (password hash stripped).

        Additive cursor pagination (Wave-4): with **no** *cursor* and **no**
        *limit* the full listing is returned as a plain list (the historical
        shape).  Datasets are ordered alphabetically regardless of case (an
        ASCII sort would file ``Zebra`` ahead of ``apple``); names differing
        only by case tie-break on their raw spelling (``MLS`` before
        ``mls``).  Passing either parameter switches the return to
        ``{"datasets": [...], "next_cursor": str | None}`` — *next_cursor* is
        an opaque token that resumes the listing after the last returned
        dataset, and ``None`` once the listing is exhausted.  Counts follow
        ``RAG_DEFER_COUNT_SYNC`` on every page, exactly as for the full list.
        """
        paginated = cursor is not None or limit is not None
        if not paginated:
            datasets: list[dict[str, Any]] = []
            if not self.datasets_path.exists():
                return datasets
            for child in sorted(self.datasets_path.iterdir(), key=lambda p: _dataset_order_key(p.name)):
                if child.is_dir():
                    meta = self._read_meta(child.name)
                    if meta:
                        if not _DEFER_COUNT_SYNC:
                            self._sync_count_from_qdrant(child.name, meta)
                        datasets.append(self._strip_password(meta))
            return datasets

        if limit is not None and limit < 1:
            raise ValueError("limit must be >= 1")
        names: list[str] = []
        if self.datasets_path.exists():
            names = [
                child.name
                for child in sorted(self.datasets_path.iterdir(), key=lambda p: _dataset_order_key(p.name))
                if child.is_dir()
            ]
        if cursor:
            # Empty string = start of the listing (a client's first page);
            # only a real token is decoded.  bisect_LEFT: the token names the
            # first item of the NEXT page (it was never returned), so the
            # resume position includes it.  The probe is pre-keyed because
            # bisect's ``key`` applies to the elements, not to the probe.
            after = _decode_list_cursor(cursor)
            names = names[bisect.bisect_left(names, _dataset_order_key(after), key=_dataset_order_key) :]
        if limit is not None:
            page_names = names[:limit]
            overflow = names[limit:]
        else:
            page_names, overflow = names, []
        datasets = []
        for name in page_names:
            meta = self._read_meta(name)
            if meta:
                if not _DEFER_COUNT_SYNC:
                    self._sync_count_from_qdrant(name, meta)
                datasets.append(self._strip_password(meta))
        next_cursor = _encode_list_cursor(overflow[0]) if overflow else None
        return {"datasets": datasets, "next_cursor": next_cursor}

    def get_dataset(self, name: str, sync_count: bool = True) -> dict[str, Any]:
        """Return metadata for a single dataset (password hash stripped).

        Raises ``FileNotFoundError`` if it does not exist.

        ``sync_count`` is ignored when ``RAG_DEFER_COUNT_SYNC`` is set, so
        reads never trigger a Qdrant round-trip + meta.json write under the
        scale deployment.

        When Redis is available and signals that the dataset exists, this
        method retries ``_read_meta`` a few times with short delays to
        bridge the NFS close-to-open cache propagation gap between pods.
        """
        meta = self._read_meta(name)
        if not meta:
            # NFS cache may not have propagated meta.json from another pod.
            # If Redis says the dataset exists, retry with short delays.
            if _dataset_exist_check(name):
                logger.debug(
                    "Dataset '%s' not visible on NFS yet but Redis confirms existence — retrying",
                    name,
                )
                for _ in range(_DATASET_EXIST_RETRY_COUNT):
                    time.sleep(_DATASET_EXIST_RETRY_DELAY)
                    meta = self._read_meta(name)
                    if meta:
                        break
            if not meta:
                raise FileNotFoundError(f"Dataset '{name}' not found")
        if sync_count and not _DEFER_COUNT_SYNC:
            self._sync_count_from_qdrant(name, meta)
        return self._strip_password(meta)

    def update_dataset(self, name: str, updates: dict[str, Any]) -> None:
        """Update metadata fields for an existing dataset.

        Captioning flags can be changed at any time — the cached RAG is
        rebuilt so the new settings apply to subsequent ingests and
        retrievals without a restart. Only affects future operations;
        already-ingested content keeps its stored captions.

        The weighted-RRF defaults (``{"rrf": {…}}``) are query-time only —
        they are read per search, so no RAG rebuild or re-ingest is needed;
        the change takes effect on the next search of this dataset from any
        replica (meta.json is re-read per query).
        """
        with self._get_meta_lock(name):
            meta = self._read_meta(name)
            if not meta:
                raise FileNotFoundError(f"Dataset '{name}' not found")
            caption_changed = False
            for key in ("description", "caption_with_asr", "caption_with_vlm", "keep_originals", "ocr"):
                if key in updates:
                    if key in ("caption_with_asr", "caption_with_vlm", "ocr"):
                        meta[key] = bool(updates[key])
                        caption_changed = key != "ocr"  # ocr needs no RAG rebuild (read per file at ingest)
                    else:
                        meta[key] = updates[key]
            # Contextual retrieval (feature: contextual retrieval): a plain
            # boolean that rides the SAME cached-RAG invalidation as the
            # caption flags — the flag is baked into the MultimodalRAG
            # instance (contextualize=), so a flip must rebuild the cached
            # RAG to apply to subsequent ingests.  Affects NEW ingests only;
            # already-ingested content keeps its stored form (content-hash
            # dedup would skip the re-embed anyway) — Recreate is the
            # adoption path for existing files.
            if "contextual" in updates:
                meta["contextual"] = bool(updates["contextual"])
                caption_changed = True
            # The public flag (feature: public-to-all-keys) NEVER rides the
            # generic PATCH: this endpoint is reachable by registry clients,
            # and a client must never be able to publish a dataset
            # (capabilities travel with the credential - D15). The single
            # admin surface is POST /api/admin/datasets/{name}/public.
            if "public" in updates:
                raise ValueError(
                    "The 'public' flag is admin-only: POST /api/admin/datasets/{name}/public"
                )  # Weighted-RRF defaults (feature: weighted RRF, dataset-defaults
            # slice): PATCH ``{"rrf": {"dense_weight": …, "sparse_weight": …,
            # "k": …}}`` with any subset of keys.  Clamped/validated like the
            # create-time surface; an empty object or a payload that reduces
            # to the global default (1.0/1.0, no k) REMOVES the stored
            # default entirely, so ``meta["rrf"]`` never claims a default
            # that behaves like "no default".
            if "rrf" in updates:
                rrf_meta = self._sanitize_rrf_meta(updates["rrf"])
                if rrf_meta is None:
                    meta.pop("rrf", None)
                else:
                    meta["rrf"] = rrf_meta
            self._write_meta(name, meta)
        # Rebuild the cached RAG so the new caption flags take effect now.
        if caption_changed:
            with self._rag_cache_lock:
                self._rag_cache.pop(name, None)
            logger.info("Dataset '%s' caption settings updated; RAG cache invalidated", name)

    # ------------------------------------------------------------------
    # Adding content
    # ------------------------------------------------------------------

    def add_documents(self, dataset_name: str, documents: Sequence[str | dict[str, Any]]) -> list[str]:
        """Add raw documents (strings or multimodal dicts) to a dataset.

        Documents are embedded and stored in the dataset's Qdrant collection.
        Returns the list of stored point IDs.
        """
        rag = self._get_rag(dataset_name)
        ids = rag.add_to_vector_store(documents)
        self._increment_count(dataset_name, len(ids))
        # Opt-in ingest webhook (RAG_WEBHOOK_URL) — logged-not-fatal, no-op
        # when unset.
        _notify_ingest_hook(dataset_name, len(ids))
        return ids

    def add_file(self, dataset_name: str, file_path: str, original_name: str | None = None) -> dict[str, Any]:
        """Process and store a single file into a dataset.

        The file is first copied to the dataset's PVC-backed ``files/``
        directory, then processed according to its type and stored as one
        or more vector entries.

        After embedding, large media payloads (``image``, ``video``,
        ``audio`` data URLs) are stripped from the Qdrant stored payload
        and replaced with ``file://`` PVC paths.  The embedding step still
        sees the full media — only the persisted payload is kept lean.

        Returns
        -------
        dict
            ``{"type": …, "chunks": N, "stored_ids": …}``
        """
        rag = self._get_rag(dataset_name)
        mpk = rag.embedder.mm_processor_kwargs
        chunk_size = rag.embedder.chunk_size
        chunk_overlap = rag.embedder.chunk_overlap
        text_splitter = rag.embedder.text_splitter

        # Download remote URLs (HTTP or S3) to a temporary file — they are
        # processed directly without a separate PVC copy.
        _tmp_cleanup: list[str] = []
        source_url: str | None = None
        if _is_url(file_path):
            source_url = file_path
            file_path = _download_url(file_path)
            _tmp_cleanup.append(file_path)

        try:
            result = self._add_file_processed(
                dataset_name,
                file_path,
                rag,
                mpk,
                chunk_size,
                chunk_overlap,
                text_splitter=text_splitter,
                original_name=original_name,
                source_url=source_url,
            )
            # Opt-in ingest webhook (RAG_WEBHOOK_URL) — logged-not-fatal.
            _notify_ingest_hook(dataset_name, len(result.get("stored_ids") or []))
            return result
        finally:
            for tmp_path in _tmp_cleanup:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    logger.debug("Suppressed exception", exc_info=True)

    def add_files_batch(
        self,
        dataset_name: str,
        file_entries: list[tuple[str, str]],
        progress_callback: Any | None = None,
        batch_score: float = 128.0,
        force_names: set[str] | None = None,
        source_urls: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Process multiple files and store dedup-aware chunk counts.

        Producer–consumer pipeline:

        **Producer** (main thread): preprocesses files sequentially,
        accumulating chunks.  When *batch_score* is reached the batch is
        handed off to a background consumer thread and the producer
        immediately starts on the next file.

        ``force_names`` (S3 sync): original file names whose content-hash
        dedup skip is bypassed — used for URLs that have no stored points
        (a file pruned after an upstream delete and re-appeared), so the
        content is re-embedded instead of silently skipped.

        ``source_urls`` (S3 sync / URL ingest, audit 2026-10-02 P1-3 fix):
        ``tmp_path → canonical s3:// URL`` (keyed by tmp_path — unique per
        download; a basename key would collide when one sync spans prefixes
        holding the same object name).  When provided, the point
        payload's ``metadata.source`` is stamped with the canonical URL
        instead of the stored PVC path — prune matches ``s3://`` prefixes
        against ``metadata.source``, so a PVC-path stamp makes the prune
        structurally dead (upstream deletions never propagate).  Mirrors
        how the single-file ``add_file`` path stamps ``source_url``.
        Files whose name has no entry keep the stored-path stamp.

        **Consumer** (background thread): calls the embedding API and
        stores results in Qdrant.  This overlap hides the embedding
        latency behind preprocessing of subsequent files.

        Scoring (2.56 MB of data ≈ 1.0 score):

        * Standalone media files (image, video, audio) → ``file_bytes / 2.56 MB``
        * PDF chunks → ``1.0`` per chunk + ``image_data_url_bytes / 2.56 MB``
        * Other text-type files are processed individually inside
          :meth:`_add_file_processed` and are not batched.
        """
        import queue
        import threading

        rag = self._get_rag(dataset_name)
        mpk = rag.embedder.mm_processor_kwargs
        chunk_size = rag.embedder.chunk_size
        chunk_overlap = rag.embedder.chunk_overlap
        max_pixels = mpk.get("max_pixels", 720 * 720)
        fps = mpk.get("fps", 1.0)
        total_pixels = mpk.get("total_pixels", 0)
        target_frames = _VIDEO_TARGET_FRAMES

        file_results: list[dict[str, Any]] = []
        _MB = 2.56 * 1024 * 1024
        # Byte cap for the idle "keep the consumer fed" handoff: at most this
        # many bytes of in-memory payload (base64 data URLs) per batch, so a
        # stack of huge media files can't be pushed into one multi-GB batch.
        _IDLE_SEND_BYTES = 256 * 1024 * 1024

        def _doc_payload_bytes(doc: Any) -> int:
            """Estimate the in-memory bytes a doc contributes to a batch."""
            if isinstance(doc, str):
                return len(doc)
            if isinstance(doc, dict):
                total = len(doc.get("text") or "")
                for k in ("image", "video", "audio"):
                    v = doc.get(k)
                    if isinstance(v, str):
                        total += len(v)
                    elif isinstance(v, list):
                        total += sum(len(x) for x in v if isinstance(x, str))
                return total
            return len(str(doc))

        # -- Queues for producer-consumer handoff -------------------------------
        # Bounded so a slow embedder can never buffer an unbounded number of
        # multi-GB batches in RAM: the producer blocks once maxsize are queued.
        _BATCH_QUEUE_MAX = 2
        batch_queue: queue.Queue = queue.Queue(maxsize=_BATCH_QUEUE_MAX)
        result_queue: queue.Queue = queue.Queue()
        consumer_busy = threading.Event()
        consumer_error: list[str | None] = [None]

        def _consumer_alive() -> bool:
            return consumer_error[0] is None

        def _queue_batch(batch_docs_: list, batch_files_list_: list) -> None:
            """Hand a batch to the consumer, blocking only until it drains.

            If the consumer thread has died, emit terminal error results for
            the batch's files so the frontend never hangs waiting on them.
            """
            if not batch_docs_:
                return
            while _consumer_alive():
                try:
                    batch_queue.put((batch_docs_, batch_files_list_), timeout=0.5)
                    return
                except queue.Full:
                    continue
            # Consumer died before this batch was queued.
            err = consumer_error[0] or "consumer failed"
            logger.error("Dropping batch (%d files) — consumer thread dead: %s", len(batch_files_list_), err)
            for fname, _, _, _, _ in batch_files_list_:
                result_queue.put((fname, 0, err))
                if progress_callback:
                    progress_callback({"file": fname, "status": "error", "error": err})

        def consumer() -> None:
            """Background thread: embed + store batches from the producer."""
            try:
                while True:
                    batch = batch_queue.get()
                    if batch is None:
                        batch_queue.task_done()
                        break
                    consumer_busy.set()
                    docs, batch_files_list = batch
                    try:
                        # Mark all files in this batch as embedding (with chunk
                        # count).  Inside the try so a callback failure cannot
                        # kill the consumer thread.
                        for fname, count, _, _, _ in batch_files_list:
                            if progress_callback:
                                progress_callback({"file": fname, "status": "embedding", "chunks": count})

                        # Embed + store each file independently so one bad file
                        # cannot fail (and then mis-report) every other file in
                        # the batch.  Per-file retries, per-file outcomes.
                        per_file: list[tuple[str, int, str | None]] = []
                        offset = 0
                        for fname, count, file_type_, content_hash_, stored_path in batch_files_list:
                            file_docs = docs[offset : offset + count]
                            offset += count
                            try:
                                ids = retry_call(
                                    lambda fd=file_docs: rag.add_to_vector_store(fd),
                                    max_attempts=3,
                                    base_delay=2.0,
                                    connection_delay=10.0,
                                )
                                self._strip_media_payloads(rag, ids)
                                self._increment_count(dataset_name, len(ids), file_type=file_type_)
                                per_file.append((fname, len(ids), None))
                                if content_hash_ and ids:
                                    self._mark_ingested(dataset_name, content_hash_)
                                elif not ids and stored_path:
                                    # Every doc for this file was dropped (unconvertible
                                    # media, or vector-dedup with no existing ref) — the
                                    # stored copy would sit on the PVC forever unused.
                                    self._delete_unreferenced_file(dataset_name, stored_path, content_hash_, rag)
                            except Exception as exc:
                                logger.warning("Embedding failed for '%s' after 3 attempts: %s", fname, exc)
                                per_file.append((fname, 0, str(exc)))

                        for fname, chunk_count, err in per_file:
                            result_queue.put((fname, chunk_count, err))
                            if progress_callback:
                                if err:
                                    progress_callback({"file": fname, "status": "error", "error": err})
                                else:
                                    progress_callback(
                                        {
                                            "file": fname,
                                            "status": "complete",
                                            "chunks": chunk_count,
                                        }
                                    )
                    except Exception as exc:
                        logger.error("Batch processing failed unexpectedly: %s", exc)
                        for fname, _, _, _, _ in batch_files_list:
                            result_queue.put((fname, 0, str(exc)))
                            if progress_callback:
                                progress_callback({"file": fname, "status": "error", "error": str(exc)})
                    finally:
                        consumer_busy.clear()
                        batch_queue.task_done()
            except Exception as exc:
                logger.error("Consumer thread crashed unexpectedly: %s", exc)
                consumer_error[0] = str(exc)
                # Drain the queue: every still-queued batch gets a terminal
                # error so the frontend never hangs waiting on it.
                while True:
                    try:
                        batch = batch_queue.get_nowait()
                    except queue.Empty:
                        break
                    if batch is None:
                        batch_queue.task_done()
                        break
                    _, pending_files = batch
                    for fname, _, _, _, _ in pending_files:
                        result_queue.put((fname, 0, str(exc)))
                        if progress_callback:
                            progress_callback({"file": fname, "status": "error", "error": str(exc)})
                    batch_queue.task_done()

        # Propagate the caller's context (incl. the ingest-warning collector
        # from rag_system._ingest_warnings) into the consumer thread.
        consumer_thread = threading.Thread(target=lambda: contextvars.copy_context().run(consumer), daemon=True)
        consumer_thread.start()

        # -- Producer: preprocess files, queue batches --------------------------
        # Parallel preprocessing fan-out (Wave-4): up to ``RAG_INGEST_CONCURRENCY``
        # (default 4) files are preprocessed CONCURRENTLY in worker threads —
        # store/copy, classify, hash, dedup check, PDF/image/video/audio
        # extraction.  Deterministic ordering is preserved by design: each
        # worker emits its output as ordered events on a per-file bounded
        # queue, and the sequencer below absorbs them strictly in file order,
        # so batch composition, document order, ``file_results`` order and
        # progress-callback semantics match the sequential implementation
        # regardless of worker completion order.  ``RAG_INGEST_CONCURRENCY=1``
        # degenerates to the strictly sequential pre-Wave-4 behaviour.
        batch_docs: list[str | dict[str, Any]] = []
        # (fname, chunk_count, file_type, content_hash) — the content hash lets
        # the consumer mark successfully-embedded files for ingest dedup.
        batch_files_list: list[tuple[str, int, str, str, str]] = []
        current_score = 0.0

        def _drain_results() -> None:
            """Pull completed results from the consumer into *file_results*.

            Progress callbacks are now sent directly from the consumer thread so
            the frontend gets live updates even after the producer has finished
            preprocessing all files.  This function only maintains the
            ``file_results`` accumulator used for the final return value.
            """
            while not result_queue.empty():
                fname, count, err = result_queue.get_nowait()
                if err:
                    file_results.append({"file": fname, "chunks": 0, "error": err})
                else:
                    file_results.append({"file": fname, "chunks": count})

        # Bounded per-file event queues: a worker blocks once its queue is
        # full, so in-flight preprocessing memory stays bounded (the same
        # reason the batch handoff is bounded below).
        _EVENT_QUEUE_MAX = 8
        ingest_concurrency = _INGEST_CONCURRENCY
        evqs: list[queue.Queue] = []
        worker_threads: list[threading.Thread] = []

        def _preprocess_worker(evq: queue.Queue, tmp_path_: str, orig_name_: str) -> None:
            """Preprocess ONE file, emitting ordered events for the sequencer.

            Event shapes (positional):
              ("progress", payload)                       — progress-callback event
              ("docs", docs, file_entry, score_delta, reset, flush_after)
                                                          — batch-buffer append
              ("skip", fname)                             — content-hash dedup skip
              ("direct_result", fname, chunks)            — inline text-file result
              ("progress_error", fname, err)              — per-file failure
              ("base_error", exc)                         — BaseException; re-raised
              ("score_reset",)                            — current_score = 0.0
              ("done",)                                   — terminal; run tail logic
            """
            fname = orig_name_

            # Canonical-source stamp (audit 2026-10-02 P1-3 fix): URL ingests
            # carry their s3:// URL as metadata.source so prune's MatchPrefix
            # filters can actually match them (a stored-PVC-path stamp made
            # the prune structurally dead).  The map is keyed by THIS file's
            # tmp_path (unique per download — a basename key collides when
            # one sync spans prefixes holding the same object name,
            # cross-validation finding 1) and falls through to the stored
            # path for files with no entry.
            def _source_for(_target_fname: str, stored: str) -> str:
                if source_urls:
                    return source_urls.get(tmp_path_, stored)
                return stored

            try:
                try:
                    evq.put(("progress", {"file": fname, "status": "preprocessing"}))

                    dst = self._store_file(dataset_name, tmp_path_, original_name=fname, defer_write=True)
                    dst_str = str(dst)
                    stored_path = str(dst)  # tier-0 stored path (before preprocessing)
                    file_type = _classify_file(dst_str)
                    content_hash = _sha256_file(dst)
                    if self._is_ingested(dataset_name, content_hash) and not (force_names and fname in force_names):
                        evq.put(("skip", fname))
                        return
                    file_bytes = dst.stat().st_size
                    file_total = 0  # estimated total chunks (0 if unknown)

                    if file_type == "pdf":
                        pdf_proc = PDFProcessor()
                        embed_batch = getattr(rag.embed, "chunk_size", 64) or 64
                        chunk_count = 0
                        pdf_batch: list[dict[str, Any]] = []
                        for chunk in pdf_proc.extract_chunks_iter(
                            dst_str,
                            chunk_size=chunk_size,
                            chunk_overlap=chunk_overlap,
                            text_splitter=rag.embedder.text_splitter,
                            ocr=self._get_ocr_enabled(dataset_name),
                        ):
                            pdf_batch.append(chunk)
                            chunk_count += 1

                            # Hand off to consumer mid-PDF for concurrent
                            # extraction + embedding.  This lets the embedder
                            # start processing the first pages while later
                            # pages are still being extracted.
                            if len(pdf_batch) >= embed_batch:
                                _fix_source(pdf_batch, fname, _source_for(fname, dst_str))
                                self._save_doc_media(dataset_name, pdf_batch, model_max_pixels=max_pixels)
                                evq.put(
                                    (
                                        "docs",
                                        pdf_batch,
                                        (fname, len(pdf_batch), file_type, content_hash, stored_path),
                                        0.0,
                                        True,
                                        True,
                                    )
                                )
                                pdf_batch = []

                        # Handle remaining chunks from the generator
                        if pdf_batch:
                            _fix_source(pdf_batch, fname, _source_for(fname, dst_str))
                            self._save_doc_media(dataset_name, pdf_batch, model_max_pixels=max_pixels)
                            evq.put(
                                (
                                    "docs",
                                    pdf_batch,
                                    (fname, len(pdf_batch), file_type, content_hash, stored_path),
                                    0.0,
                                    True,
                                    False,
                                )
                            )
                        file_total = chunk_count

                    elif file_type == "image":
                        original_dst = dst_str
                        dst = _preprocess_image_file(dst)
                        dst_str = str(dst)
                        img_proc = ImageProcessor(max_pixels=max_pixels)
                        doc = img_proc.process(dst_str)
                        _fix_source([doc], fname, _source_for(fname, dst_str))
                        doc["preprocessed_image"] = f"file://{dst_str}"
                        if dst_str != original_dst:
                            doc["original_image"] = f"file://{original_dst}"
                        file_total = 1

                        # Delete original if keep_originals=False (mutates only
                        # this worker's own doc — emitted afterwards).
                        if dst_str != original_dst and not self._get_keep_originals(dataset_name):
                            self._delete_original_file(dataset_name, original_dst, [], "original_image")
                            doc.pop("original_image", None)

                        evq.put(
                            (
                                "docs",
                                [doc],
                                (fname, 1, file_type, content_hash, stored_path),
                                file_bytes / _MB,
                                False,
                                False,
                            )
                        )

                    elif file_type == "video":
                        original_dst = dst_str
                        dst = _preprocess_video_file(dst)
                        dst_str = str(dst)
                        vid_proc = VideoProcessor(
                            fps=fps,
                            max_pixels=max_pixels,
                            total_pixels=total_pixels,
                            target_frames=target_frames,
                        )
                        embed_batch = getattr(rag.embed, "chunk_size", 64) or 64
                        vid_batch: list[dict[str, Any]] = []
                        vid_chunk_count = 0
                        store_url = f"file://{dst_str}"
                        original_url = f"file://{original_dst}" if dst_str != original_dst else None
                        for doc in vid_proc.process_iter(dst_str):
                            doc["preprocessed_video"] = store_url
                            if original_url:
                                doc["original_video"] = original_url
                            vid_batch.append(doc)
                            vid_chunk_count += 1

                            if len(vid_batch) >= embed_batch:
                                _fix_source(vid_batch, fname, _source_for(fname, dst_str))
                                self._save_doc_media(dataset_name, vid_batch)
                                evq.put(
                                    (
                                        "docs",
                                        vid_batch,
                                        (fname, len(vid_batch), file_type, content_hash, stored_path),
                                        0.0,
                                        True,
                                        True,
                                    )
                                )
                                vid_batch = []

                        delete_original = dst_str != original_dst and not self._get_keep_originals(dataset_name)
                        if vid_batch:
                            _fix_source(vid_batch, fname, _source_for(fname, dst_str))
                            self._save_doc_media(dataset_name, vid_batch)
                            if delete_original:
                                # Mirrors the sequential pop over the un-flushed
                                # batch remainder — these docs are emitted after
                                # the pop, so their metadata is identical.
                                for vd in vid_batch:
                                    if isinstance(vd, dict):
                                        vd.pop("original_video", None)
                            evq.put(
                                (
                                    "docs",
                                    vid_batch,
                                    (fname, len(vid_batch), file_type, content_hash, stored_path),
                                    0.0,
                                    True,
                                    False,
                                )
                            )
                        if delete_original:
                            self._delete_original_file(dataset_name, original_dst, [], "original_video")
                        file_total = vid_chunk_count

                    elif file_type == "audio":
                        segments = _split_audio_segments(dst)
                        audio_batch: list[dict[str, Any]] = []
                        for seg_idx, seg_path in enumerate(segments):
                            raw = seg_path.read_bytes()
                            mime = mimetypes.guess_type(str(seg_path))[0] or "audio/mpeg"
                            b64 = base64.b64encode(raw).decode("utf-8")
                            seg_name = (
                                f"{dst.name} — segment {seg_idx + 1}/{len(segments)}" if len(segments) > 1 else dst.name
                            )
                            doc = {
                                "text": f"[Audio: {seg_name}]",
                                "audio": f"data:{mime};base64,{b64}",
                                "source": dst_str,
                                "segment_index": seg_idx,
                            }
                            _fix_source([doc], fname, _source_for(fname, dst_str))
                            audio_batch.append(doc)
                        # Save each segment to disk so _strip_media_payloads
                        # doesn't replace the segment data URL with the full
                        # source file (which could be tens of MB and exceed
                        # the ASR endpoint's size cap at query time).
                        self._save_doc_media(dataset_name, audio_batch)
                        evq.put(
                            (
                                "docs",
                                audio_batch,
                                (fname, len(segments), file_type, content_hash, stored_path),
                                file_bytes / _MB,
                                False,
                                False,
                            )
                        )
                        file_total = len(segments)

                    else:
                        result = self._add_file_processed(
                            dataset_name,
                            dst_str,
                            rag,
                            mpk,
                            chunk_size,
                            chunk_overlap,
                            original_name=fname,
                        )
                        evq.put(("direct_result", fname, result.get("chunks", 0)))

                    # Mark as preprocessed (waiting in batch buffer).
                    # Skip for the else branch — those files are already fully
                    # processed (embedded + stored) and have sent "complete".
                    in_batch_buffer = file_type in ("pdf", "image", "video", "audio")
                    if in_batch_buffer:
                        evq.put(("progress", {"file": fname, "status": "preprocessed", "total": file_total}))

                    # pdf/video always zero the score after their streaming
                    # windows (the sequential code did this unconditionally).
                    if file_type in ("pdf", "video"):
                        evq.put(("score_reset",))

                except Exception as exc:
                    logger.warning("File '%s' failed: %s", fname, exc)
                    evq.put(("progress_error", fname, str(exc)))
                except BaseException as exc:
                    # KeyboardInterrupt/SystemExit must abort the whole batch
                    # exactly as the sequential producer did — re-raised by the
                    # sequencer in the calling thread.
                    evq.put(("base_error", exc))
                    raise
            finally:
                evq.put(("done",))

        # Launch the first ``ingest_concurrency`` workers; each subsequent
        # worker starts only after an earlier file's events have been fully
        # absorbed — this bounds both the thread count and the in-flight
        # preprocessing memory without starving the pipeline.
        n_entries = len(file_entries)
        for idx0 in range(min(ingest_concurrency, n_entries)):
            tmp_path, orig_name = file_entries[idx0]
            evq: queue.Queue = queue.Queue(maxsize=_EVENT_QUEUE_MAX)
            evqs.append(evq)
            t = threading.Thread(
                target=contextvars.copy_context().run,
                args=(_preprocess_worker, evq, tmp_path, orig_name),
                daemon=True,
            )
            worker_threads.append(t)
            t.start()

        for k in range(n_entries):
            evq = evqs[k]
            while True:
                ev = evq.get()
                kind = ev[0]
                if kind == "progress":
                    if progress_callback:
                        progress_callback(ev[1])
                elif kind == "docs":
                    _, docs_, entry_, score_delta_, reset_, flush_ = ev
                    batch_docs.extend(docs_)
                    batch_files_list.append(entry_)
                    if reset_:
                        current_score = 0.0
                    else:
                        current_score += score_delta_
                    if flush_:
                        _queue_batch(batch_docs, batch_files_list)
                        batch_docs = []
                        batch_files_list = []
                        current_score = 0.0
                        _drain_results()
                elif kind == "skip":
                    file_results.append({"file": ev[1], "chunks": 0, "deduplicated": True})
                    if progress_callback:
                        progress_callback({"file": ev[1], "status": "skipped", "chunks": 0, "deduplicated": True})
                    break
                elif kind == "direct_result":
                    file_results.append({"file": ev[1], "chunks": ev[2]})
                    if progress_callback:
                        progress_callback(
                            {
                                "file": ev[1],
                                "chunks": ev[2],
                                "status": "complete",
                            }
                        )
                    # Falls through to the tail logic on "done", exactly like
                    # the sequential else-branch did.
                elif kind == "progress_error":
                    if progress_callback:
                        progress_callback({"file": ev[1], "status": "error", "error": ev[2]})
                    break
                elif kind == "base_error":
                    raise ev[1]
                elif kind == "score_reset":
                    current_score = 0.0
                elif kind == "done":
                    # Hand off to consumer when the batch reaches the score
                    # threshold.  Also send a reasonable chunk when the consumer
                    # is idle so it never sits around waiting.
                    if current_score >= batch_score:
                        _queue_batch(batch_docs, batch_files_list)
                        batch_docs = []
                        batch_files_list = []
                        current_score = 0.0
                    elif batch_docs and not consumer_busy.is_set():
                        # Consumer idle → send a chunk to keep it fed, bounded to
                        # at most 10 files AND ~_IDLE_SEND_BYTES of in-memory
                        # payload so huge media files can't form one multi-GB batch.
                        n = min(len(batch_files_list), 10)
                        send_doc_count = 0
                        send_bytes = 0
                        send_files: list[tuple[str, int, str, str, str]] = []
                        for fname_, cnt, file_type_, content_hash, stored_path in batch_files_list[:n]:
                            file_payload = sum(
                                _doc_payload_bytes(d) for d in batch_docs[send_doc_count : send_doc_count + cnt]
                            )
                            if send_files and send_bytes + file_payload > _IDLE_SEND_BYTES:
                                break
                            send_files.append((fname_, cnt, file_type_, content_hash, stored_path))
                            send_bytes += file_payload
                            send_doc_count += cnt
                        if not send_files:
                            # Even one file is over the cap — send it anyway so the
                            # pipeline cannot deadlock on a single huge file.
                            send_files = batch_files_list[:1]
                            send_doc_count = sum(c for _, c, _, _, _ in send_files)
                        keep_files = batch_files_list[len(send_files) :]
                        _queue_batch(batch_docs[:send_doc_count], send_files)
                        batch_docs = batch_docs[send_doc_count:]
                        batch_files_list = keep_files
                        current_score = 0.0

                    # Drain any completed results so the frontend gets
                    # "complete" callbacks promptly rather than all at the end.
                    _drain_results()
                    break

            # Keep the pipeline full: replace the absorbed worker with the
            # next file's worker.
            nxt = k + ingest_concurrency
            if nxt < n_entries:
                tmp_path, orig_name = file_entries[nxt]
                nxt_evq: queue.Queue = queue.Queue(maxsize=_EVENT_QUEUE_MAX)
                evqs.append(nxt_evq)
                t = threading.Thread(
                    target=contextvars.copy_context().run,
                    args=(_preprocess_worker, nxt_evq, tmp_path, orig_name),
                    daemon=True,
                )
                worker_threads.append(t)
                t.start()

        for t in worker_threads:
            t.join(timeout=30)

        # -- Flush remaining batch and shut down consumer -----------------------
        if batch_docs:
            _queue_batch(batch_docs, batch_files_list)
        batch_queue.put(None)  # sentinel
        consumer_thread.join()

        # Persist the batch's deferred .hashes.json entries — one merged
        # write under the cross-process lock instead of a full index rewrite
        # per stored file (O(n²) NFS I/O on large batches).
        _flush_hash_index_writes()

        # If the consumer thread crashed, propagate the error
        if consumer_error[0] is not None:
            logger.error("Consumer thread crashed: %s", consumer_error[0])
            return {
                "status": "error",
                "error": f"Consumer thread crashed: {consumer_error[0]}",
                "file_count": len(file_entries),
                "files": file_results,
            }

        # -- Collect results from consumer --------------------------------------
        while not result_queue.empty():
            fname, count, err = result_queue.get_nowait()
            if err:
                file_results.append({"file": fname, "chunks": 0, "error": err})
            else:
                file_results.append({"file": fname, "chunks": count})

        # Metrics: one line summarising file outcomes + embedded chunks.
        from multimodal_rag.utils.metrics import observe_ingest_results

        observe_ingest_results(file_results, dataset_name)
        # Opt-in ingest webhook (RAG_WEBHOOK_URL) — one event per completed
        # batch. add_urls_batch delegates here, so URL ingests fire exactly
        # one event too (not one per add path).
        _notify_ingest_hook(dataset_name, _stored_points_count(file_results))
        return {"status": "ok", "file_count": len(file_entries), "files": file_results}

    def add_urls_batch(
        self,
        dataset_name: str,
        urls: list[str],
        progress_callback: Any | None = None,
        batch_score: float = 128.0,
        sync: bool = False,
        sync_dry_run: bool = False,
    ) -> dict[str, Any]:
        """Download files from URLs (``s3://``, ``http://``, ``https://``)
        and process them as a batch.

        S3 URLs that look like directories (trailing slash or no file
        extension) are automatically expanded — each object under the
        prefix is listed and ingested individually.  Other URLs are
        processed as single files.

        Each file URL is downloaded to a temporary file, then delegated to
        :meth:`add_files_batch` for chunking, embedding and storage.
        Temporary files are cleaned up after ingestion.

        **Sync mode** (S3 prefix URLs only): ``sync=True`` makes the ingest a
        two-way reconciliation with the bucket — after the normal ingest, any
        stored document whose ``metadata.source`` starts with one of the
        synced prefixes but is **not** in the current listing is pruned (its
        Qdrant points are deleted), so objects deleted upstream disappear
        from the dataset instead of lingering forever.  URLs that have no
        stored points yet are force-re-ingested even when their content hash
        was recorded before (heals a file that was pruned and re-appeared).
        ``sync_dry_run=True`` reports the planned ingest/prune sets without
        touching anything.  Known limitation: an object whose *content*
        changed under the same key is re-ingested (its hash changes) but the
        old version's points stay — source-keyed pruning cannot see versions.

        **Watched-source skip state** (feature: watched sources): sync paths
        consult the per-dataset ``files/.watched_state.json`` sidecar —
        ``s3://bucket/key → {etag, size}`` of the last successfully ingested
        version of each object.  A listed object whose ETag+Size still
        matches its state entry is dropped from the download set before any
        bytes move (no re-download, no re-embed attempt); the state is written
        back after the batch for every object whose ingest demonstrably
        succeeded.  The fingerprint is ETag+Size together, so content changed
        under the same key re-ingests (the ETag flips even when the size
        doesn't).  Consequences worth knowing: an empty state changes nothing
        (the first sync of a dataset behaves exactly as before and populates
        the state), and — once state exists — deleting a source's documents
        out-of-band (``dataset_delete_documents`` / the prune filter) sticks:
        the reconciler no longer resurrects them on the next tick.  The state
        is an optimisation, never a correctness gate: any parse/IO failure
        degrades to re-downloading everything.  ``sync_dry_run`` reports the
        split as ``would_download`` (objects that would move) vs ``unchanged``
        (objects the state would skip).
        """
        sync_mode = sync or sync_dry_run
        # Expand S3 directory prefixes to individual file URLs.  On sync
        # paths the per-prefix listing is the *fingerprinting* variant — the
        # same recursive walk and filter as _expand_urls uses (identical URL
        # set, same order), but it also carries each object's ETag/Size, so
        # the watched-state skip costs zero extra S3 calls.  Non-sync paths
        # keep the plain expansion byte-identical.
        expanded: list[str] = []
        listed_fingerprints: dict[str, dict[str, Any]] = {}
        if sync_mode:
            for u in urls:
                if u.startswith("s3://") and _is_s3_directory_url(u):
                    fps = _list_s3_prefix_fingerprints(u)
                    listed_fingerprints.update(fps)
                    expanded.extend(fps.keys())
                else:
                    expanded.append(u)
        else:
            expanded = _expand_urls(urls)
        if not expanded:
            if sync_mode:
                return {
                    "status": "dry-run" if sync_dry_run else "ok",
                    "file_count": 0,
                    "files": [],
                    "sync": {"pruned_points": 0, "pruned_sources": [], "scope": []},
                }
            return {"status": "ok", "file_count": 0, "files": []}

        sync_prefixes = _sync_prefixes(urls)
        stored_sources: set[str] = set()
        if sync_mode:
            non_s3 = [u for u in expanded if not u.startswith("s3://")]
            if non_s3:
                raise ValueError(
                    "sync / sync_dry_run require s3:// prefix URLs only — got non-S3 URL(s): " + ", ".join(non_s3[:3])
                )
            stored_sources = self._stored_sources(dataset_name, sync_prefixes)

        # Watched-state skip: unchanged = listed object whose ETag+Size still
        # matches the recorded fingerprint of a successful ingest.  Only
        # fingerprint-listed objects (S3 prefix expansions) can match; plain
        # URLs are always attempted.
        unchanged: set[str] = set()
        if sync_mode:
            state = self._watched_state_load(dataset_name)
            if state:
                for u in expanded:
                    key = _canonical_s3_url(u)
                    fp = listed_fingerprints.get(key)
                    st = state.get(key)
                    if fp is not None and st and st.get("etag") == fp.get("etag") and st.get("size") == fp.get("size"):
                        unchanged.add(key)

        if sync_dry_run:
            expected = {_canonical_s3_url(u) for u in expanded}
            download = [u for u in expanded if _canonical_s3_url(u) not in unchanged]
            return {
                "status": "dry-run",
                "scope": sync_prefixes,
                "listed": sorted(expected),
                "would_ingest": sorted({_canonical_s3_url(u) for u in download} - stored_sources),
                "would_prune": sorted(stored_sources - expected),
                # Watched-source skip split (feature: watched sources).
                "would_download": sorted({_canonical_s3_url(u) for u in download}),
                "unchanged": sorted(unchanged),
            }

        # Objects the state says are unchanged are NOT downloaded and NOT
        # force-re-ingested — their recorded ingest is still current.
        download = [u for u in expanded if _canonical_s3_url(u) not in unchanged]
        # Force-re-ingest URLs with no stored points (see docstring): keyed by
        # the original file name the producer loop sees.
        force_names = {Path(_canonical_s3_url(u)).name for u in download if u not in stored_sources}

        file_entries: list[tuple[str, str]] = []
        tmp_paths: list[str] = []
        try:
            # Notify the frontend about each expanded file before downloading
            if progress_callback and len(expanded) > len(urls):
                for url in expanded:
                    clean = _canonical_s3_url(url)
                    orig_name = Path(clean).name or "download"
                    if clean in unchanged:
                        progress_callback({"file": orig_name, "status": "unchanged"})
                    else:
                        progress_callback({"file": orig_name, "status": "listed"})

            for url in download:
                # Derive a human-readable original name from the URL
                clean = _canonical_s3_url(url)
                orig_name = Path(clean).name or "download"

                tmp_path = _download_url(url)
                tmp_paths.append(tmp_path)
                file_entries.append((tmp_path, orig_name))

            # Canonical-source stamping (audit 2026-10-02 P1-3): every
            # ingested URL object carries its canonical s3:// URL as
            # metadata.source so prune's MatchPrefix(scope) can match it —
            # a stored-PVC-path stamp makes the prune structurally dead.
            # Keyed by tmp_path (NOT basename — cross-validation finding 1,
            # 2026-10-02: one sync may span several prefixes and two of them
            # can hold the SAME object name; a basename key would stamp one
            # file with the other's canonical URL and make per-object prune
            # reconciliation impossible. tmp_path is unique per download).
            canonical_by_tmp = {tmp: _canonical_s3_url(u) for (tmp, _), u in zip(file_entries, download)}
            result = self.add_files_batch(
                dataset_name,
                file_entries,
                progress_callback=progress_callback,
                batch_score=batch_score,
                force_names=force_names or None,
                source_urls=canonical_by_tmp,
            )
            if sync:
                result["sync"] = self._prune_sources(
                    dataset_name, sync_prefixes, expected={_canonical_s3_url(u) for u in expanded}
                )
                # Sources the prune deleted upstream are gone: forget their
                # fingerprints so a pruned-and-reappeared object (even with an
                # identical ETag) re-ingests instead of state-skipping — sync's
                # documented heal path must keep working.
                pruned = (result["sync"] or {}).get("pruned_sources") or []
                if pruned:
                    self._watched_state_forget(dataset_name, pruned)
            if sync_mode:
                self._record_watched_ingest_state(dataset_name, download, listed_fingerprints, result)
            return result
        finally:
            for tmp_path in tmp_paths:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    logger.debug("Suppressed exception", exc_info=True)

    def _record_watched_ingest_state(
        self,
        dataset_name: str,
        attempted_urls: list[str],
        fingerprints: dict[str, dict[str, Any]],
        batch_result: dict[str, Any],
    ) -> None:
        """Record watched-state fingerprints for URLs whose ingest succeeded.

        Success = the file shows up in the batch result without an error AND
        with either stored chunks or a content-hash dedup skip (its content is
        in the dataset either way).  Files that failed, produced no chunks, or
        are missing from the result stay unrecorded — the next reconcile
        re-downloads and re-attempts them (fail-soft: a wasted download, never
        a silently skipped hole).  Basename collisions inside one batch (two
        prefixes holding the same file name) cannot be resolved per-file and
        stay unrecorded for the same reason.  Pruned (upstream-deleted) sources
        were already forgotten by the caller.
        """
        if not fingerprints or not attempted_urls:
            return
        by_name: dict[str, list[str]] = {}
        for u in attempted_urls:
            by_name.setdefault(Path(_canonical_s3_url(u)).name or "download", []).append(u)
        succeeded: set[str] = set()
        for f in batch_result.get("files") or []:
            if not isinstance(f, dict) or f.get("error"):
                continue
            urls_for_name = by_name.get(str(f.get("file") or "")) or []
            if len(urls_for_name) != 1:
                continue
            if (f.get("chunks") or 0) > 0 or f.get("deduplicated"):
                succeeded.add(_canonical_s3_url(urls_for_name[0]))
        if succeeded:
            self._watched_state_update(dataset_name, fingerprints, only_urls=succeeded)

    def _add_file_processed(
        self,
        dataset_name: str,
        file_path: str,
        rag: MultimodalRAG,
        mpk: dict[str, Any],
        chunk_size: int,
        chunk_overlap: int,
        text_splitter=None,
        original_name: str | None = None,
        source_url: str | None = None,
    ) -> dict[str, Any]:
        """Core file processing logic (shared by ``add_file`` and URL ingestion).

        If *source_url* is set (HTTP/S3 URL), the file is processed from the
        temp download and **not** copied to the PVC files directory.  The
        original URL is used as the canonical ``source``.
        """
        fname = original_name or Path(file_path).name
        # Archives: extract and process each contained file through the same
        # per-file handler used for standalone uploads (_add_file_processed), so
        # images / videos / audio / PDFs inside an archive behave identically to
        # the same file uploaded directly — same preprocessing, segmentation,
        # model params, metadata and per-type chunk counts.  ArchiveProcessor now
        # only handles extraction + bounds checks; per-member processing is
        # delegated to the standalone handler.
        if Path(file_path).suffix.lower() in _ARCHIVE_EXTS:
            if source_url:
                # URL archives extract directly from the temp file; members are
                # then stored and processed as ordinary local files.
                archive_path = file_path
            else:
                archive_path = str(self._store_file(dataset_name, file_path, original_name=fname))

            def _process_member(member_path: str, member_name: str) -> list[str]:
                try:
                    result = self._add_file_processed(
                        dataset_name,
                        member_path,
                        rag,
                        mpk,
                        chunk_size,
                        chunk_overlap,
                        text_splitter=text_splitter,
                        original_name=member_name,
                    )
                    return list(result.get("stored_ids") or [])
                except ValueError as exc:
                    # Unsupported member type — skip it, matching the old
                    # behaviour of silently ignoring such files.
                    logger.debug("Skipping unsupported archive member %s: %s", member_name, exc)
                    return []
                except Exception as exc:
                    logger.warning("Failed to process archive member %s: %s", member_name, exc)
                    return []

            archive_proc = ArchiveProcessor(process_member=_process_member)
            ids = archive_proc.process(archive_path)
            return {"type": "archive", "chunks": len(ids), "stored_ids": ids}

        file_type = _classify_file(file_path)
        if file_type == "unknown":
            raise ValueError(f"Unsupported file type: {Path(file_path).suffix}")

        # Override chunk size for structured types (code, JSON, XML, YAML).
        # Uses the model's ``code_chunk_size`` / ``code_chunk_overlap``
        # (typically 8192/512) so that functions and document structures
        # stay intact.  A matching ``code_text_splitter`` is used when
        # available.
        if file_type in _STRUCTURED_TYPES:
            chunk_size = rag.embedder.code_chunk_size
            chunk_overlap = rag.embedder.code_chunk_overlap
            text_splitter = rag.embedder.code_text_splitter

        if source_url:
            # URL-based file: process from temp, use original URL as source, no PVC copy
            dst_path = Path(file_path)
            source_str = source_url
            store_url = source_url
        else:
            dst_path = self._store_file(dataset_name, file_path, original_name=fname)
            source_str = str(dst_path)
            store_url = f"file://{source_str}"

        # Content-hash ingest dedup: identical bytes already successfully
        # ingested into this dataset are skipped (no duplicate vectors).
        content_hash: str | None = None
        if not source_url:
            content_hash = _sha256_file(dst_path)
            if self._is_ingested(dataset_name, content_hash):
                logger.info("Skipping %s: identical content already ingested in %s", fname, dataset_name)
                return {"type": file_type, "chunks": 0, "stored_ids": [], "deduplicated": True}

        if file_type == "pdf":
            pdf_proc = PDFProcessor()
            chunks = pdf_proc.extract_chunks(
                source_str,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                text_splitter=text_splitter,
            )
            _fix_source(chunks, fname, source_str)
            self._save_doc_media(dataset_name, chunks, model_max_pixels=mpk.get("max_pixels", 720 * 720))
            ids = rag.add_to_vector_store(chunks)
            self._strip_media_payloads(rag, ids)
            self._increment_count(dataset_name, len(ids), file_type=file_type)
            self._cleanup_dropped_file(dataset_name, rag, source_url, str(dst_path), content_hash, ids)
            return self._finish_ingest(
                dataset_name, content_hash, {"type": "pdf", "chunks": len(ids), "stored_ids": ids}
            )

        elif file_type == "image":
            original_path = str(dst_path)
            dst_path = _preprocess_image_file(dst_path)
            source_str = str(dst_path)
            store_url = f"file://{source_str}"
            img_proc = ImageProcessor(max_pixels=mpk.get("max_pixels", 720 * 720))
            doc = img_proc.process(source_str)
            _fix_source([doc], fname, source_str)
            doc["preprocessed_image"] = store_url
            if not source_url and source_str != original_path:
                doc["original_image"] = f"file://{original_path}"
            ids = rag.add_to_vector_store([doc])
            self._strip_media_payloads(rag, ids, store_url)
            self._increment_count(dataset_name, len(ids), file_type=file_type)
            self._cleanup_dropped_file(dataset_name, rag, source_url, original_path, content_hash, ids)

            # Delete original if keep_originals=False
            if not source_url and source_str != original_path and not self._get_keep_originals(dataset_name):
                self._delete_original_file(dataset_name, original_path, ids, "original_image")

            return self._finish_ingest(
                dataset_name, content_hash, {"type": "image", "chunks": len(ids), "stored_ids": ids}
            )

        elif file_type == "video":
            original_path = str(dst_path)
            dst_path = _preprocess_video_file(dst_path)
            source_str = str(dst_path)
            store_url = f"file://{source_str}"
            vid_proc = VideoProcessor(
                fps=mpk.get("fps", 1.0),
                max_pixels=mpk.get("max_pixels", 720 * 720),
                total_pixels=mpk.get("total_pixels", 0),
                target_frames=_VIDEO_TARGET_FRAMES,
            )
            docs = vid_proc.process(source_str)
            original_url = f"file://{original_path}" if not source_url and source_str != original_path else None
            for doc in docs:
                doc["preprocessed_video"] = store_url
                if original_url:
                    doc["original_video"] = original_url
            _fix_source(docs, fname, source_str)
            if docs:
                ids = rag.add_to_vector_store(docs)
                self._strip_media_payloads(rag, ids, store_url)
                self._increment_count(dataset_name, len(ids), file_type=file_type)
            else:
                ids = []
            self._cleanup_dropped_file(dataset_name, rag, source_url, original_path, content_hash, ids)

            # Delete original if keep_originals=False
            if original_url and not self._get_keep_originals(dataset_name):
                self._delete_original_file(dataset_name, original_path, ids, "original_video")

            return self._finish_ingest(
                dataset_name, content_hash, {"type": "video", "chunks": len(ids), "stored_ids": ids}
            )

        elif file_type == "audio":
            import base64

            segments = _split_audio_segments(dst_path)
            audio_docs: list[dict[str, Any]] = []
            for seg_idx, seg_path in enumerate(segments):
                raw = seg_path.read_bytes()
                mime = mimetypes.guess_type(str(seg_path))[0] or "audio/mpeg"
                b64 = base64.b64encode(raw).decode("utf-8")
                seg_name = (
                    f"{dst_path.name} — segment {seg_idx + 1}/{len(segments)}" if len(segments) > 1 else dst_path.name
                )
                doc = {
                    "text": f"[Audio: {seg_name}]",
                    "audio": f"data:{mime};base64,{b64}",
                    "source": source_str,
                    "segment_index": seg_idx,
                }
                _fix_source([doc], fname, source_str)
                audio_docs.append(doc)
            # Save each segment to disk so _strip_media_payloads
            # doesn't replace the segment data URL with the full source
            # file (which could be tens of MB and exceed the ASR endpoint's
            # size cap at query time).
            self._save_doc_media(dataset_name, audio_docs)
            ids = rag.add_to_vector_store(audio_docs)
            self._strip_media_payloads(rag, ids, store_url)
            self._increment_count(dataset_name, len(ids), file_type=file_type)
            self._cleanup_dropped_file(dataset_name, rag, source_url, str(dst_path), content_hash, ids)
            return self._finish_ingest(
                dataset_name, content_hash, {"type": "audio", "chunks": len(ids), "stored_ids": ids}
            )

        elif file_type == "json":
            json_proc = JSONProcessor(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                text_splitter=text_splitter,
            )
            chunks = json_proc.process(source_str)
            _fix_source(chunks, fname, source_str)
            ids = rag.add_to_vector_store(chunks) if chunks else []
            self._increment_count(dataset_name, len(ids), file_type=file_type)
            return self._finish_ingest(
                dataset_name, content_hash, {"type": "json", "chunks": len(ids), "stored_ids": ids}
            )

        elif file_type == "table":
            table_proc = TableProcessor(chunk_size=chunk_size, text_splitter=text_splitter)
            chunks = table_proc.process(source_str)
            _fix_source(chunks, fname, source_str)
            ids = rag.add_to_vector_store(chunks) if chunks else []
            self._increment_count(dataset_name, len(ids), file_type=file_type)
            return self._finish_ingest(
                dataset_name, content_hash, {"type": "table", "chunks": len(ids), "stored_ids": ids}
            )

        elif file_type == "code":
            code_proc = CodeProcessor(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                text_splitter=text_splitter,
            )
            chunks = code_proc.process(source_str)
            _fix_source(chunks, fname, source_str)
            ids = rag.add_to_vector_store(chunks)
            self._increment_count(dataset_name, len(ids), file_type=file_type)
            return self._finish_ingest(
                dataset_name, content_hash, {"type": "code", "chunks": len(ids), "stored_ids": ids}
            )

        elif file_type == "office":
            office_proc = OfficeProcessor(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                text_splitter=text_splitter,
            )
            chunks = office_proc.process(source_str)
            _fix_source(chunks, fname, source_str)
            if chunks:
                self._save_doc_media(dataset_name, chunks)
                ids = rag.add_to_vector_store(chunks)
                self._strip_media_payloads(rag, ids, store_url)
                self._increment_count(dataset_name, len(ids), file_type=file_type)
            else:
                ids = []
            return self._finish_ingest(
                dataset_name, content_hash, {"type": "office", "chunks": len(ids), "stored_ids": ids}
            )

        elif file_type == "html":
            html_proc = HTMLProcessor(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                text_splitter=text_splitter,
            )
            chunks = html_proc.process(source_str)
            _fix_source(chunks, fname, source_str)
            ids = rag.add_to_vector_store(chunks)
            self._increment_count(dataset_name, len(ids), file_type=file_type)
            return self._finish_ingest(
                dataset_name, content_hash, {"type": "html", "chunks": len(ids), "stored_ids": ids}
            )

        elif file_type == "xml":
            xml_proc = XMLProcessor(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                text_splitter=text_splitter,
            )
            chunks = xml_proc.process(source_str)
            _fix_source(chunks, fname, source_str)
            ids = rag.add_to_vector_store(chunks) if chunks else []
            self._increment_count(dataset_name, len(ids), file_type=file_type)
            return self._finish_ingest(
                dataset_name, content_hash, {"type": "xml", "chunks": len(ids), "stored_ids": ids}
            )

        elif file_type == "yaml":
            yaml_proc = YAMLProcessor(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                text_splitter=text_splitter,
            )
            chunks = yaml_proc.process(source_str)
            _fix_source(chunks, fname, source_str)
            ids = rag.add_to_vector_store(chunks) if chunks else []
            self._increment_count(dataset_name, len(ids), file_type=file_type)
            return self._finish_ingest(
                dataset_name, content_hash, {"type": "yaml", "chunks": len(ids), "stored_ids": ids}
            )

        elif file_type == "notebook":
            nb_proc = NotebookProcessor(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                text_splitter=text_splitter,
            )
            chunks = nb_proc.process(source_str)
            _fix_source(chunks, fname, source_str)
            if chunks:
                self._save_doc_media(dataset_name, chunks)
                ids = rag.add_to_vector_store(chunks)
                self._strip_media_payloads(rag, ids)
                self._increment_count(dataset_name, len(ids), file_type=file_type)
            else:
                ids = []
            return self._finish_ingest(
                dataset_name, content_hash, {"type": "notebook", "chunks": len(ids), "stored_ids": ids}
            )

        elif file_type == "ebook":
            epub_proc = EbookProcessor(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                text_splitter=text_splitter,
            )
            chunks = epub_proc.process(source_str)
            _fix_source(chunks, fname, source_str)
            if chunks:
                self._save_doc_media(dataset_name, chunks)
                ids = rag.add_to_vector_store(chunks)
                self._strip_media_payloads(rag, ids)
                self._increment_count(dataset_name, len(ids), file_type=file_type)
            else:
                ids = []
            return self._finish_ingest(
                dataset_name, content_hash, {"type": "ebook", "chunks": len(ids), "stored_ids": ids}
            )

        elif file_type == "log":
            log_proc = LogProcessor(chunk_size=chunk_size, text_splitter=text_splitter)
            chunks = log_proc.process(source_str)
            _fix_source(chunks, fname, source_str)
            ids = rag.add_to_vector_store(chunks)
            self._increment_count(dataset_name, len(ids), file_type=file_type)
            return self._finish_ingest(
                dataset_name, content_hash, {"type": "log", "chunks": len(ids), "stored_ids": ids}
            )

        else:  # text
            text_proc = TextProcessor(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                text_splitter=text_splitter,
            )
            chunks = text_proc.process(source_str)
            _fix_source(chunks, fname, source_str)
            ids = rag.add_to_vector_store(chunks)
            self._increment_count(dataset_name, len(ids), file_type=file_type)
            return self._finish_ingest(
                dataset_name, content_hash, {"type": "text", "chunks": len(ids), "stored_ids": ids}
            )

    # ------------------------------------------------------------------
    # Media payload minimisation
    # ------------------------------------------------------------------

    def _strip_media_payloads(self, rag: MultimodalRAG, point_ids: list[str], source_url: str | None = None) -> None:
        """Replace heavy data-URL payloads in Qdrant with lightweight ``file://`` refs.

        **Tier 3 keys** (``image``, ``video``) are **kept as data URLs** — they
        hold model-ready base64 that the reranker and VLM consume directly at
        retrieval time, avoiding redundant file reads and re-resizing.

        **Tier 1/2 keys** (``preprocessed_*``, ``original_*``) and ``audio``
        are stripped if they still contain data URLs (they should normally be
        ``file://`` refs by this point, but this is a safety net).
        """
        if not point_ids:
            return
        vs = rag.vector_store
        assert vs is not None and not isinstance(vs, dict)
        client = vs._client  # type: ignore[attr-defined]
        coll = vs.collection_name  # type: ignore[attr-defined]

        # Batch retrieve all points in a single request.  The local-write
        # guard serializes against concurrent ingest workers on the
        # in-process local backend (no internal locking there); remote
        # clients and in-memory stores are unaffected.
        _guard = _store_write_guard(vs)
        try:
            with _guard:
                points = client.retrieve(coll, ids=point_ids, with_payload=True, with_vectors=False)
        except Exception as exc:
            # Non-fatal: heavy data URLs stay in Qdrant and the next ingest
            # batch retries the strip.  Log so the silence isn't total.
            logger.warning(
                "Media payload strip: could not retrieve %d point(s) from %s — skipping: %s",
                len(point_ids),
                coll,
                exc,
            )
            return

        # Group modified point IDs by their new payload (JSON key for hashability)
        updates: dict[str, list[str]] = {}
        for pt in points:
            payload = pt.payload or {}
            meta = payload.get("metadata", {})
            if not isinstance(meta, dict):
                continue

            modified = False
            # NOTE: "image" and "video" are intentionally excluded — their
            # tier-3 data URLs are consumed directly by the reranker/VLM.
            for key in (
                "audio",
                "preprocessed_image",
                "preprocessed_video",
                "preprocessed_audio",
                "original_image",
                "original_video",
                "original_audio",
            ):
                val = meta.get(key)
                if val is None:
                    continue

                # Already a valid local file reference — nothing to strip
                def _is_valid_file_ref(v: Any) -> bool:
                    return isinstance(v, str) and v.startswith("file://") and os.path.exists(v[7:])

                if _is_valid_file_ref(val) or (
                    isinstance(val, list) and len(val) > 0 and all(_is_valid_file_ref(v) for v in val)
                ):
                    continue

                # Determine the replacement source reference
                src = source_url
                if src is None:
                    src = meta.get("source")

                if src is None:
                    # No source at all — remove the heavy payload
                    del meta[key]
                    modified = True
                elif src.startswith(("http://", "https://", "s3://")):
                    # Remote URL — keep as-is, no local copy to reference
                    pass
                elif os.path.exists(src if not src.startswith("file://") else src[7:]):
                    # Local file exists — replace heavy payload with file:// ref
                    ref = f"file://{src}" if not src.startswith("file://") else src
                    meta[key] = ref
                    modified = True
                else:
                    # No accessible source — remove the heavy payload key
                    del meta[key]
                    modified = True

            if modified:
                new_payload = {
                    "page_content": payload.get("page_content", ""),
                    "metadata": meta,
                }
                key = json.dumps(new_payload, sort_keys=True, default=str)
                updates.setdefault(key, []).append(str(pt.id))

        # Batch set_payload — one call per unique payload group
        for payload_json, ids in updates.items():
            try:
                with _guard:
                    client.set_payload(
                        coll,
                        payload=json.loads(payload_json),
                        points=ids,
                    )
            except Exception as exc:
                logger.warning(
                    "Media payload strip: set_payload for %d point(s) on %s failed: %s",
                    len(ids),
                    coll,
                    exc,
                )

    def _save_doc_media(
        self,
        dataset_name: str,
        docs: list[dict[str, Any]],
        model_max_pixels: int = 720 * 720,
    ) -> None:
        """Save inline data-URL media to files and swap the reference.

        **Tier 3 keys** (``image``, ``video``) are **kept as data URLs** so the
        reranker and VLM can consume them directly from Qdrant without file
        I/O or re-resizing.

        For ``image`` data URLs that arrive at a resolution above tier 3
        (e.g. PDF-extracted images), the data URL is resized down to
        *model_max_pixels* and a tier-2 ``preprocessed_image`` ``file://``
        ref is produced alongside.

        ``audio`` data URLs are saved to PVC files (existing behaviour).

        ``preprocessed_*`` / ``original_*`` data URLs, if any, are saved to
        PVC files as a safety net.
        """
        files_dir = self._dataset_dir(dataset_name) / "files"
        files_dir.mkdir(parents=True, exist_ok=True)

        for doc in docs:
            if not isinstance(doc, dict):
                continue

            # -- image: keep tier-3 data URL, produce tier-2 file ref ------
            val = doc.get("image")
            if val:
                urls = val if isinstance(val, list) else [val]
                new_urls: list[str] = []
                preproc_urls: list[str] = []
                for url in urls:
                    if not url.startswith("data:"):
                        new_urls.append(url)
                        continue
                    m = re.match(r"data:([^;]+);base64,(.+)", url)
                    if not m:
                        new_urls.append(url)
                        continue
                    mime = m.group(1)
                    raw = base64.b64decode(m.group(2))

                    from multimodal_rag.input_processing.image_processor import (
                        _resize_image,
                    )

                    # Tier 2: save to PVC file
                    tier2_raw = _resize_image(raw, mime, _PVC_IMAGE_MAX_PIXELS)
                    ext = mimetypes.guess_extension(mime) or ".bin"
                    fname = f"{uuid.uuid4().hex}_image{ext}"
                    dest = files_dir / fname
                    dest.write_bytes(tier2_raw)
                    preproc_urls.append(f"file://{dest}")

                    # Tier 3: keep as data URL (resized to model-ready)
                    tier3_raw = _resize_image(raw, mime, model_max_pixels)
                    b64 = base64.b64encode(tier3_raw).decode("utf-8")
                    new_urls.append(f"data:{mime};base64,{b64}")

                doc["image"] = new_urls if isinstance(val, list) else new_urls[0]
                if preproc_urls:
                    doc["preprocessed_image"] = preproc_urls if isinstance(val, list) else preproc_urls[0]

            # -- video: keep as tier-3 data URL (no file save) --------------
            # VideoProcessor already produces model-ready segments.
            # Don't save to file — the data URL stays in Qdrant for the
            # reranker/VLM to consume directly.
            # (No action needed for "video" key.)

            # -- audio: save to file (existing behaviour) ------------------
            for key in ("audio",):
                val = doc.get(key)
                if not val:
                    continue
                urls = val if isinstance(val, list) else [val]
                new_urls = []
                for url in urls:
                    if not url.startswith("data:"):
                        new_urls.append(url)
                        continue
                    m = re.match(r"data:([^;]+);base64,(.+)", url)
                    if not m:
                        new_urls.append(url)
                        continue
                    mime = m.group(1)
                    raw = base64.b64decode(m.group(2))
                    ext = mimetypes.guess_extension(mime) or ".bin"
                    fname = f"{uuid.uuid4().hex}_{key}{ext}"
                    dest = files_dir / fname
                    dest.write_bytes(raw)
                    new_urls.append(f"file://{dest}")
                doc[key] = new_urls if isinstance(val, list) else new_urls[0]

            # -- preprocessed_*/original_*: save data URLs to file ----------
            # Safety net — these should normally be file:// refs already.
            for key in (
                "preprocessed_image",
                "preprocessed_video",
                "preprocessed_audio",
                "original_image",
                "original_video",
                "original_audio",
            ):
                val = doc.get(key)
                if not val:
                    continue
                urls = val if isinstance(val, list) else [val]
                new_urls = []
                for url in urls:
                    if not url.startswith("data:"):
                        new_urls.append(url)
                        continue
                    m = re.match(r"data:([^;]+);base64,(.+)", url)
                    if not m:
                        new_urls.append(url)
                        continue
                    mime = m.group(1)
                    raw = base64.b64decode(m.group(2))
                    ext = mimetypes.guess_extension(mime) or ".bin"
                    fname = f"{uuid.uuid4().hex}_{key}{ext}"
                    dest = files_dir / fname
                    dest.write_bytes(raw)
                    new_urls.append(f"file://{dest}")
                doc[key] = new_urls if isinstance(val, list) else new_urls[0]

    # ------------------------------------------------------------------
    # Tier-schema migration
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_tier1_path(tier2_path: str) -> str | None:
        """Reverse-engineer the tier-1 (full-quality) path from a tier-2
        ``_preprocessed`` path.

        ``_preprocess_image_file`` / ``_preprocess_video_file`` create a
        sibling whose stem ends with ``_preprocessed``.  Stripping that
        suffix yields the original file.  Returns ``None`` when the tier-1
        file does not exist on disk (e.g. it was never preprocessed, or the
        original was deleted).
        """
        p = Path(tier2_path)
        if not p.stem.endswith("_preprocessed"):
            return None
        tier1 = p.with_name(p.stem[: -len("_preprocessed")] + p.suffix)
        if tier1.exists():
            return str(tier1)
        return None

    def _delete_unreferenced_file(
        self,
        dataset_name: str,
        stored_path: str,
        content_hash: str | None,
        rag: MultimodalRAG,
    ) -> None:
        """Delete a stored file whose every document was dropped.

        When a file's media can't be embedded (the embedder doesn't support
        the modality) and can't be converted to caption text (no VLM/ASR), all
        docs it produced are omitted and nothing in the vector store references
        it — the copy in ``files/`` (and its ``*_preprocessed`` sibling) would
        sit on the PVC forever.  This removes them and forgets the dedup hash
        so a later upload (e.g. once an ASR/VLM is configured) is re-ingested.
        """
        p = Path(stored_path)
        # Only ever delete files inside this dataset's own files directory.
        files_dir = self._dataset_dir(dataset_name) / "files"
        try:
            files_res = str(files_dir.resolve())
        except OSError:
            return
        try:
            if not str(p.resolve()).startswith(files_res):
                return
        except OSError:
            return

        sibling = p.with_stem(p.stem + "_preprocessed")
        artifacts = [p, sibling]
        srcs = [str(a) for a in artifacts]
        if not any(a.exists() for a in artifacts):
            return

        # Safety guard: if any stored doc still references these paths (e.g.
        # from an earlier ingest that lost its hash entry), keep the file.
        if self._file_referenced(rag, srcs):
            return

        deleted: list[str] = []
        for a in artifacts:
            try:
                if a.exists():
                    a.unlink()
                    deleted.append(str(a))
            except OSError as exc:
                logger.warning("Could not delete unreferenced file %s: %s", a, exc)
        if deleted:
            logger.info("Removed unreferenced file(s): %s", ", ".join(deleted))

        # Forget the dedup hash so the same bytes can be re-ingested later.
        if content_hash:
            hashes_path = files_dir / ".hashes.json"
            if hashes_path.exists():
                with _cross_process_lock(files_dir / ".hashes.lock"):
                    try:
                        hash_index = _load_hash_index(hashes_path)
                        if hash_index.get(content_hash) == stored_path:
                            del hash_index[content_hash]
                            _write_hash_index(hashes_path, hash_index)
                    except Exception:
                        logger.debug("Failed to update .hashes.json", exc_info=True)

        if deleted:
            _record_ingest_warning(
                f"Removed unreferenced file (not embeddable by the embedder; no caption): {stored_path}"
            )

    def repair_preprocessed_media(
        self,
        dataset_name: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Regenerate broken ``*_preprocessed`` media siblings from their originals.

        Concurrent preprocessing of one file (multiple server replicas share
        the dataset PVC) can leave an interleaved, unplayable ``*_preprocessed``
        file behind even though the original is intact.  For every
        ``*_preprocessed.*`` file that fails :func:`_media_file_ok` /
        :func:`_image_file_ok`, deletes the broken copy and re-runs the
        corresponding preprocess function on the tier-1 original.

        Pass ``dry_run=True`` to only report which files would be repaired.
        """
        files_dir = self._dataset_dir(dataset_name) / "files"
        if not files_dir.is_dir():
            return {"dataset": dataset_name, "dry_run": dry_run, "checked": 0, "repaired": [], "failed": []}

        # Registry of candidates: iterate the files dir directly — a broken
        # preprocessed sibling exists even when the hash index is stale.
        candidates: dict[Path, Path] = {}
        for f in sorted(files_dir.iterdir()):
            if not f.is_file() or not f.stem.endswith("_preprocessed"):
                continue
            tier1 = f.with_name(f.stem[: -len("_preprocessed")] + f.suffix)
            if tier1.exists():
                candidates[f] = tier1

        repaired: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        checked = 0
        for preproc, tier1 in candidates.items():
            checked += 1
            suffix = preproc.suffix.lower()
            ok = _image_file_ok if suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp") else _media_file_ok
            if ok(preproc):
                continue
            record: dict[str, Any] = {"preprocessed": str(preproc), "original": str(tier1)}
            if dry_run:
                record["action"] = "would-regenerate"
                repaired.append(record)
                continue
            # The writers create tmp siblings next to the final name, so
            # serialise under the files-dir lock; unlink first so the writer
            # cannot skip-regenerate (its exists+valid check would pass on
            # the old broken file otherwise).
            with _cross_process_lock(files_dir / ".preprocess.lock"):
                try:
                    preproc.unlink()
                except OSError:
                    pass
                file_type = _classify_file(str(tier1))
                if file_type == "image":
                    result = _preprocess_image_file(tier1)
                elif file_type == "video":
                    result = _preprocess_video_file(tier1)
                elif file_type == "audio":
                    result = _truncate_audio_file(tier1, max_seconds=60.0)
                else:
                    result = tier1
                if result != tier1 and result == preproc and ok(preproc):
                    record["regenerated_bytes"] = preproc.stat().st_size
                    repaired.append(record)
                    logger.info("Repaired broken preprocessed media: %s", preproc.name)
                else:
                    record["reason"] = (
                        "preprocess returned the original (no transcode needed, or the original's "
                        "container cannot hold the libx264/aac output — broken sibling was removed)"
                        if result == tier1
                        else "regenerated file failed validation"
                    )
                    failed.append(record)
                    logger.warning("Could not repair %s: %s", preproc.name, record["reason"])

        return {
            "dataset": dataset_name,
            "dry_run": dry_run,
            "checked": checked,
            "repaired": repaired,
            "failed": failed,
        }

    def _file_referenced(self, rag: MultimodalRAG, sources: list[str]) -> bool:
        """True if the vector store holds a doc referencing one of *sources*.

        Safety guard used before deleting an on-disk file.  Conservative: on
        any store error it returns True so the file is kept.
        """
        vs = rag.vector_store
        assert vs is not None and not isinstance(vs, dict)
        if hasattr(vs, "store"):
            # InMemoryVectorStore
            candidates = set(sources)
            for entry in vs.store.values():
                doc = entry.get("document")
                meta = getattr(doc, "metadata", None) or {}
                if meta.get("source") in candidates:
                    return True
                for k in (
                    "preprocessed_image",
                    "preprocessed_video",
                    "preprocessed_audio",
                    "original_image",
                    "original_video",
                    "original_audio",
                ):
                    v = meta.get(k)
                    if isinstance(v, str) and v.startswith("file://") and v[7:] in candidates:
                        return True
            return False
        # QdrantVectorStore — one bounded scroll per source (limit 1).
        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue

            client = vs._client  # type: ignore[attr-defined]
            coll = vs.collection_name  # type: ignore[attr-defined]
            for s in sources:
                points, _ = client.scroll(
                    coll,
                    limit=1,
                    with_payload=False,
                    with_vectors=False,
                    filter=Filter(must=[FieldCondition(key="metadata.source", match=MatchValue(value=s))]),
                )
                if points:
                    return True
            return False
        except Exception:
            logger.warning("Could not verify file references — keeping file (conservative)")
            return True

    def _cleanup_dropped_file(
        self,
        dataset_name: str,
        rag: MultimodalRAG,
        source_url: str | None,
        stored_path: str,
        content_hash: str | None,
        ids: list[str],
    ) -> None:
        """After ingesting a file, remove its on-disk copy if every doc dropped.

        Used by the single-file/URL ingest path (``_add_file_processed``).
        Remote (URL) ingests never delete anything — there is no PVC copy.
        """
        if ids or source_url or not stored_path:
            return
        self._delete_unreferenced_file(dataset_name, stored_path, content_hash, rag)

    def _delete_original_file(
        self,
        dataset_name: str,
        original_path: str,
        doc_ids: list[str],
        tier1_key: str,
    ) -> None:
        """Delete a tier-1 original file and remove its Qdrant reference.

        Called after preprocessing succeeds when ``keep_originals=False``.
        Only deletes the file if it exists and is distinct from the
        preprocessed copy.  Also removes the ``original_image`` /
        ``original_video`` key from the Qdrant payloads and updates
        the dedup hash index.
        """
        p = Path(original_path)
        if not p.exists():
            return

        # Delete the file
        try:
            p.unlink()
            logger.info("Deleted original file (keep_originals=False): %s", original_path)
        except Exception:
            logger.debug("Failed to delete original file %s", original_path, exc_info=True)
            return

        # Remove from dedup hash index
        files_dir = self._dataset_dir(dataset_name) / "files"
        hashes_path = files_dir / ".hashes.json"
        if hashes_path.exists():
            with _cross_process_lock(files_dir / ".hashes.lock"):
                try:
                    hash_index = _load_hash_index(hashes_path)
                    # Remove any entry pointing at this file
                    stale = [k for k, v in hash_index.items() if v == original_path]
                    for k in stale:
                        del hash_index[k]
                    _write_hash_index(hashes_path, hash_index)
                except Exception:
                    logger.debug("Failed to update .hashes.json", exc_info=True)

        # Remove original_* key from Qdrant payloads
        try:
            rag = self._get_rag(dataset_name)
            vs = rag.vector_store
            assert vs is not None and not isinstance(vs, dict)
            client = vs._client  # type: ignore[attr-defined]
            coll = vs.collection_name  # type: ignore[attr-defined]

            for doc_id in doc_ids:
                client.set_payload(
                    collection_name=coll,
                    payload={tier1_key: None},
                    points=[doc_id],
                )
        except Exception:
            logger.debug("Failed to remove %s from Qdrant payloads", tier1_key, exc_info=True)

    def migrate_tier_schema(
        self,
        dataset_name: str,
        model_max_pixels: int = 720 * 720,
        batch_size: int = 200,
    ) -> dict[str, Any]:
        """Migrate existing Qdrant points to the three-tier media schema.

        * ``original_video`` (old tier-2 key) → renamed to ``preprocessed_video``
        * ``preprocessed_image`` / ``preprocessed_video`` derived from
          ``source`` when missing
        * ``original_image`` / ``original_video`` (tier 1) reverse-engineered
          from ``preprocessed_*`` paths
        * ``image`` / ``video`` ``file://`` refs converted to tier-3 base64
          data URLs (images are resized; videos are read as-is when small
          enough)

        The migration is **idempotent** — points that already have
        ``preprocessed_*`` keys and data-URL ``image``/``video`` are skipped.

        Returns a summary dict with ``migrated``, ``skipped``, and ``errors``
        counts.
        """
        import base64 as _b64

        from multimodal_rag.input_processing.image_processor import _resize_image

        rag = self._get_rag(dataset_name)
        vs = rag.vector_store
        if vs is None or isinstance(vs, dict):
            return {"error": "no vector store"}
        client = vs._client  # type: ignore[attr-defined]
        coll = vs.collection_name  # type: ignore[attr-defined]

        migrated = 0
        skipped = 0
        errors = 0

        scroll_offset: str | None = None
        while True:
            pts, scroll_offset = client.scroll(
                coll,
                limit=batch_size,
                offset=scroll_offset,
                with_payload=True,
                with_vectors=False,
            )
            if not pts:
                break

            updates: dict[str, list[str]] = {}
            for pt in pts:
                payload = pt.payload or {}
                meta = payload.get("metadata", {})
                if not isinstance(meta, dict):
                    continue

                changed = False
                pt_id = str(pt.id)

                # 1. Rename original_video → preprocessed_video (old tier 2)
                ov = meta.get("original_video")
                pv = meta.get("preprocessed_video")
                if ov and not pv:
                    meta["preprocessed_video"] = ov
                    del meta["original_video"]
                    changed = True
                elif ov and pv:
                    # Both exist — just drop the old key
                    del meta["original_video"]
                    changed = True

                # 2. Derive preprocessed_image from source if missing
                pi = meta.get("preprocessed_image")
                src = meta.get("source")
                if not pi and src and isinstance(src, str) and (src.startswith("file://") or os.path.exists(src)):
                    meta["preprocessed_image"] = f"file://{src}" if not src.startswith("file://") else src
                    changed = True

                # 3. Derive original_image / original_video (tier 1)
                for modality in ("image", "video"):
                    preproc_key = f"preprocessed_{modality}"
                    orig_key = f"original_{modality}"
                    if meta.get(orig_key):
                        continue  # already set
                    preproc_val = meta.get(preproc_key)
                    if not preproc_val:
                        # Fall back to source for images
                        if modality == "image" and src:
                            preproc_val = f"file://{src}" if not src.startswith("file://") else src
                        else:
                            continue
                    path_str = preproc_val.removeprefix("file://")
                    tier1 = self._derive_tier1_path(path_str)
                    if tier1:
                        meta[orig_key] = f"file://{tier1}"
                        changed = True

                # 4. Convert image file:// ref → tier-3 data URL
                img_val = meta.get("image")
                if img_val and isinstance(img_val, str) and img_val.startswith("file://"):
                    img_path = img_val[7:]
                    if os.path.exists(img_path):
                        try:
                            raw = Path(img_path).read_bytes()
                            mime = mimetypes.guess_type(img_path)[0] or "image/jpeg"
                            tier3 = _resize_image(raw, mime, model_max_pixels)
                            b64 = _b64.b64encode(tier3).decode("utf-8")
                            meta["image"] = f"data:{mime};base64,{b64}"
                            changed = True
                        except Exception:
                            errors += 1
                    # If file doesn't exist, leave as-is

                # 5. Convert video file:// ref → tier-3 data URL
                #    Skip if the file is too large (> 8 MB) to avoid
                #    bloating Qdrant — those points will still work via
                #    file:// refs (reranker/VLM read the file).
                vid_val = meta.get("video")
                if vid_val and isinstance(vid_val, str) and vid_val.startswith("file://"):
                    vid_path = vid_val[7:]
                    if os.path.exists(vid_path):
                        try:
                            fsize = os.path.getsize(vid_path)
                            if fsize <= 8 * 1024 * 1024:
                                raw = Path(vid_path).read_bytes()
                                mime = mimetypes.guess_type(vid_path)[0] or "video/mp4"
                                b64 = _b64.b64encode(raw).decode("utf-8")
                                meta["video"] = f"data:{mime};base64,{b64}"
                                changed = True
                        except Exception:
                            errors += 1

                if not changed:
                    skipped += 1
                    continue

                new_payload = {
                    "page_content": payload.get("page_content", ""),
                    "metadata": meta,
                }
                key = json.dumps(new_payload, sort_keys=True, default=str)
                updates.setdefault(key, []).append(pt_id)
                migrated += 1

            # Batch set_payload
            for payload_json, ids in updates.items():
                try:
                    client.set_payload(
                        coll,
                        payload=json.loads(payload_json),
                        points=ids,
                    )
                except Exception:
                    errors += len(ids)

            if scroll_offset is None:
                break

        return {
            "migrated": migrated,
            "skipped": skipped,
            "errors": errors,
        }

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        dataset_name: str,
        query: str | dict[str, Any],
        top_k: int = 10,
        use_reranker: bool = False,
        reranker_top_k: int = 3,
        filters: dict[str, Any] | None = None,
        rrf: "RrfParams | None" = None,
    ) -> list[dict[str, Any]]:
        """Search a dataset and return ranked results.

        Returns a list of ``{"content": …, "score": …}`` dicts.

        *filters* is the public metadata-filter dict (see
        ``vector_store.build_payload_filter``): ``file_types``, ``severities``,
        ``source_prefix``, ``date_from``/``date_to`` — all optional,
        AND-combined, applied server-side in Qdrant before ranking.

        *rrf* is the optional weighted-RRF override (feature: weighted RRF):
        when set, the hybrid lane runs ``RrfQuery(rrf=Rrf(k=…,
        weights=[dense, sparse]))`` and every fused entry gains an ``"rrf"``
        block recording ``{dense, sparse, k, applied}`` — the exact
        parameters the executed request used.  The override only ever
        applies to a fusion request: dense-only results (hybrid disabled,
        no BM25 stats, no query text) stay unstamped here and the block
        reports ``applied=false`` rather than labelling dense results with
        weights.  Default path (no override) returns results exactly as
        before, with no ``"rrf"`` key.

        When *rrf* is ``None`` the dataset's stored weighted-RRF defaults
        (``meta["rrf"]``, settable at create time and via PATCH) apply —
        per-call overrides always win over them, and both lose to nothing:
        when the effective parameters equal the global default (1.0/1.0,
        no pinned k) the request stays byte-identical to the historical
        fusion form.  The ``"rrf"`` block reports the EFFECTIVE parameters.
        Per-dataset defaults are IGNORED by federated multi-dataset search
        by ruling — one caller-facing merged pool cannot honour per-dataset
        weights; only an explicit caller override may apply there.
        """
        rag = self._get_rag(dataset_name)
        # Effective weights: per-call override > dataset meta default >
        # global default (None → byte-identical default fusion request).
        effective_rrf = self._effective_rrf(dataset_name, rrf)
        results = rag.retrieve(
            query,
            top_k=top_k,
            use_reranker=use_reranker,
            reranker_top_k=reranker_top_k,
            need_media=use_reranker and rag.reranker is not None,
            filters=filters,
            rrf=effective_rrf,
        )
        # The rrf block rides on the FIRST entry only (same convention as the
        # hybrid score-kind stamping: it describes the executed request, not
        # any individual hit).  applied=false when the lane degraded to
        # dense-only — never label a dense result with weights.  The block
        # reports the EFFECTIVE parameters (override or dataset default),
        # which is exactly what the executed request used.
        rrf_block: dict[str, Any] | None = None
        if isinstance(effective_rrf, RrfParams):
            applied = bool(results) and all(isinstance(doc, dict) and "_rrf" in doc for doc, _ in results)
            rrf_block = {
                "dense": effective_rrf.dense_weight,
                "sparse": effective_rrf.sparse_weight,
                "k": effective_rrf.k,
                "applied": applied,
            }
        output: list[dict[str, Any]] = []
        for doc, score in results:
            entry: dict[str, Any] = {"content": doc, "score": round(score, 4)}
            if isinstance(doc, dict):
                emb = doc.pop("_embedding_score", None)
                rerank = doc.pop("_reranker_score", None)
                if emb is not None:
                    entry["embedding_score"] = emb
                if rerank is not None:
                    entry["reranker_score"] = rerank
                # Pop the private per-doc stamp after folding it into the
                # first entry's request-level block (it must not leak into
                # the content payload).
                stamp = doc.pop("_rrf", None)
            else:
                stamp = None
            if rrf_block is not None and not any("rrf" in e for e in output):
                first_stamp: dict[str, Any] | None = stamp if isinstance(stamp, dict) else None
                entry["rrf"] = {
                    "dense": first_stamp["dense"] if first_stamp else rrf_block["dense"],
                    "sparse": first_stamp["sparse"] if first_stamp else rrf_block["sparse"],
                    "k": first_stamp["k"] if first_stamp else rrf_block["k"],
                    "applied": bool(first_stamp),
                }
            output.append(entry)
        return output

    # Return shape depends on the call mode: no cursor → the historical
    # first-page pair list; cursor given → a page dict.  The overloads let
    # callers get the shape they actually receive back.
    @overload
    def list_documents(
        self, dataset_name: str, limit: int = ..., cursor: None = None, check_embedder: bool = ...
    ) -> list[tuple[str, dict[str, Any]]]: ...

    @overload
    def list_documents(
        self, dataset_name: str, limit: int = ..., cursor: str = ..., check_embedder: bool = ...
    ) -> dict[str, Any]: ...

    def list_documents(
        self,
        dataset_name: str,
        limit: int = 50,
        cursor: str | None = None,
        check_embedder: bool = True,
    ) -> list[tuple[str, dict[str, Any]]] | dict[str, Any]:
        """List stored document payloads for a dataset.

        Additive cursor pagination (Wave-4): without *cursor* the first
        *limit* documents are returned exactly as before.  With a *cursor*
        (the ``next_cursor`` token from a previous page) the return switches
        to ``{"documents": [...], "next_cursor": str | None}`` and the Qdrant
        scroll resumes at the token instead of re-reading from the top.

        ``check_embedder=False`` skips the embedder fingerprint guard — for
        payload-only reads (listing / export) that never embed anything, so
        a dataset indexed by a since-swapped embedder stays readable/exportable
        (the documented export→import recovery path depends on this).
        """
        rag = self._get_rag(dataset_name, check_embedder=check_embedder)
        if cursor is None:
            return rag.list_documents(limit=limit)
        return rag.list_documents_page(limit=limit, cursor=cursor)

    def stream_all_documents(
        self,
        dataset_name: str,
        emit: Callable[[dict[str, Any]], None],
        batch_size: int = 500,
        check_embedder: bool = True,
    ) -> None:
        """Stream every document in a dataset (id + payload) to *emit*.

        Uses paginated Qdrant scroll so arbitrarily large collections are
        exported without holding all points in memory.  Each call emits a
        ``{"id": …, "payload": …}`` dict.

        ``check_embedder=False`` skips the embedder fingerprint guard — the
        export path must stay usable across an embedder swap (it embeds
        nothing; re-embedding happens on the import side).
        """
        rag = self._get_rag(dataset_name, check_embedder=check_embedder)
        vs = rag.vector_store
        if vs is None or isinstance(vs, dict):
            return
        client = vs._client  # type: ignore[attr-defined]
        coll = vs.collection_name  # type: ignore[attr-defined]
        offset = None
        while True:
            pts, offset = client.scroll(
                coll,
                limit=batch_size,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for pt in pts:
                emit({"id": str(pt.id), "payload": pt.payload})
            if offset is None:
                break

    def delete_document(self, dataset_name: str, doc_id: str) -> None:
        """Delete a single document from a dataset by its point ID."""
        rag = self._get_rag(dataset_name)
        vs = rag.vector_store
        assert vs is not None and not isinstance(vs, dict)
        client = vs._client  # type: ignore[attr-defined]
        from qdrant_client.models import PointIdsList

        # Fetch the point's terms for the BM25 df decrement BEFORE deletion.
        self.forget_bm25_documents(dataset_name, [doc_id])
        client.delete(
            collection_name=vs.collection_name,  # type: ignore[attr-defined]
            points_selector=PointIdsList(points=[doc_id]),
            wait=True,
        )
        self._decrement_count(dataset_name, 1)

    def delete_session_history(self, dataset_name: str, session_id: str) -> int:
        """Delete every ``session_history`` document for *session_id*.

        Used by ``add_memory`` so a session's history is **replaced in
        place**: when a session is re-flushed (e.g. it grew on a later
        restart), the new chunks supersede the old ones instead of
        accumulating copies in the store.

        Returns the number of points deleted.
        """
        rag = self._get_rag(dataset_name)
        vs = rag.vector_store
        assert vs is not None and not isinstance(vs, dict)
        client = vs._client  # type: ignore[attr-defined]
        coll = vs.collection_name  # type: ignore[attr-defined]

        from qdrant_client.models import (
            FieldCondition,
            Filter,
            MatchValue,
            PointIdsList,
        )

        scroll_filter = Filter(
            must=[
                FieldCondition(key="metadata.memory_kind", match=MatchValue(value="session_history")),
                FieldCondition(key="metadata.session_id", match=MatchValue(value=session_id)),
            ]
        )

        ids: list[Any] = []
        offset: int | str | None = None
        while True:
            pts, offset = client.scroll(
                coll,
                limit=100,
                offset=offset,
                scroll_filter=scroll_filter,
                with_payload=False,
                with_vectors=False,
            )
            for p in pts:
                ids.append(str(p.id))
            if not offset:
                break

        if ids:
            # Fetch terms for the BM25 df decrement BEFORE deletion.
            self.forget_bm25_documents(dataset_name, ids)
            client.delete(
                collection_name=coll,
                points_selector=PointIdsList(points=ids),
                wait=True,
            )
            self._decrement_count(dataset_name, len(ids))
        return len(ids)

    def delete_documents(self, dataset_name: str, doc_ids: list[str]) -> int:
        """Delete multiple documents by Qdrant point ID in one batched call.

        Returns the number of IDs actually submitted (blank entries are
        dropped).  Used by the MCP ``delete_memory`` tool; unlike
        :meth:`delete_session_history` the caller supplies exact point IDs,
        so nothing is deleted by similarity or by filter.
        """
        # PointIdsList.points is list[ExtendedPointId] (int | str | UUID);
        # str ids are a member, the annotation just needs the union type.
        clean: list[int | str | uuid.UUID] = [str(d).strip() for d in (doc_ids or []) if str(d).strip()]
        if not clean:
            return 0
        rag = self._get_rag(dataset_name)
        vs = rag.vector_store
        assert vs is not None and not isinstance(vs, dict)
        client = vs._client  # type: ignore[attr-defined]
        from qdrant_client.models import PointIdsList

        # Fetch terms for the BM25 df decrement BEFORE deletion.
        self.forget_bm25_documents(dataset_name, clean)
        client.delete(
            collection_name=vs.collection_name,  # type: ignore[attr-defined]
            points_selector=PointIdsList(points=clean),
            wait=True,
        )
        self._decrement_count(dataset_name, len(clean))
        return len(clean)

    def scroll_documents(
        self,
        dataset_name: str,
        scroll_filter: Any = None,
        limit: int = 500,
        batch_size: int = 256,
        payload_keys: list[str] | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Scroll documents (id, payload) up to *limit*, optionally filtered.

        A paginated, payload-filtered counterpart to :meth:`list_documents`
        (which is unfiltered).  *scroll_filter* is a
        ``qdrant_client.models.Filter`` applied server-side.  *payload_keys*
        restricts the transferred payload to the given top-level keys
        (``PayloadSelectorInclude``) — e.g. ``["metadata"]`` for source
        scans over large collections.  Used by the MCP ``list_memories``
        tool and the S3 sync / metadata backfill migrations.
        """
        rag = self._get_rag(dataset_name)
        vs = rag.vector_store
        if vs is None or isinstance(vs, dict):
            return []
        client = vs._client  # type: ignore[attr-defined]
        coll = vs.collection_name  # type: ignore[attr-defined]
        with_payload: Any = True
        if payload_keys:
            from qdrant_client.models import PayloadSelectorInclude

            with_payload = PayloadSelectorInclude(include=payload_keys)
        out: list[tuple[str, dict[str, Any]]] = []
        offset: int | str | None = None
        while len(out) < limit:
            pts, offset = client.scroll(
                coll,
                limit=min(batch_size, limit - len(out)),
                offset=offset,
                scroll_filter=scroll_filter,
                with_payload=with_payload,
                with_vectors=False,
            )
            for pt in pts:
                out.append((str(pt.id), dict(pt.payload or {})))
            if offset is None:
                break
        return out

    def _stored_sources(self, dataset_name: str, prefixes: list[str]) -> set[str]:
        """Distinct stored ``metadata.source`` values under any of *prefixes*.

        S3 sync helper: the sources a prefix currently has points for (URL
        ingestion records the original URL as the canonical source).
        """
        if not prefixes:
            return set()
        from qdrant_client.models import FieldCondition, Filter, MatchPrefix

        scroll_filter = Filter(
            should=[FieldCondition(key="metadata.source", match=MatchPrefix(prefix=p)) for p in prefixes]
        )
        out: set[str] = set()
        # Source-only projection (perf quick-win #3): ``["metadata"]`` used to
        # pull every key under it — including the multi-MB base64 media
        # values — for each of up to a million points.  Nested keys are
        # supported by ``PayloadSelectorInclude``, so ask for just the
        # canonical source.  ``_prune_sources`` has the same waste with
        # ``with_payload=True``; it should follow (not edited here).
        for _, payload in self.scroll_documents(
            dataset_name,
            scroll_filter,
            limit=1_000_000,
            payload_keys=_source_scan_payload_keys(),
        ):
            src = (payload.get("metadata") or {}).get("source")
            if src:
                out.add(str(src).rstrip("/"))
        return out

    def _prune_sources(self, dataset_name: str, prefixes: list[str], expected: set[str]) -> dict[str, Any]:
        """Delete stored documents whose source is under *prefixes* but not in *expected*.

        S3 sync's prune half: objects deleted upstream disappear from the
        dataset instead of lingering.  Deletes are batched by point ID and
        the document counter is decremented accordingly.

        Mass-deletion guards (audit 2026-10-02, per-prefix — a refusal skips
        that prefix and never blocks the others, reported under
        ``refused_prefixes`` so the CronJob log shows it):

        * **empty-listing** (NOT env-overridable): the listing for a prefix
          returned zero objects while stored sources exist under it — the
          classic mis-typed / renamed-prefix / narrowed-bucket-policy case.
          A listing *error* already aborts upstream; a syntactically-valid
          EMPTY listing must not silently delete the corpus.
        * **stale-ratio**: stale points exceed ``max(_PRUNE_STALE_MIN,
          _PRUNE_STALE_RATIO × in-scope points)``.  ``PRUNE_STALE_RATIO``
          (default 0.2; 0 disables) / ``PRUNE_STALE_MIN`` (default 50) are
          env-overridable for operators who deliberately want an aggressive
          prune.
        """
        from qdrant_client.models import FieldCondition, Filter, MatchPrefix, PayloadSelectorInclude, PointIdsList

        rag = self._get_rag(dataset_name)
        vs = rag.vector_store
        if vs is None or isinstance(vs, dict) or not prefixes:
            return {"pruned_points": 0, "pruned_sources": [], "scope": prefixes, "refused_prefixes": []}
        client = vs._client  # type: ignore[attr-defined]
        coll = vs.collection_name  # type: ignore[attr-defined]

        scroll_filter = Filter(
            should=[FieldCondition(key="metadata.source", match=MatchPrefix(prefix=p)) for p in prefixes]
        )
        stale_triples: list[tuple[Any, str, str]] = []  # (point_id, source, text) — per-prefix guards need the pairing
        stale_sources: set[str] = set()
        scope_srcs: set[str] = set()  # every in-scope stored source (built in THIS pass — no second scroll)
        offset: int | str | None = None
        while True:
            pts, offset = client.scroll(
                coll,
                limit=256,
                offset=offset,
                scroll_filter=scroll_filter,
                # Payload projection (perf P1-5 follow-up): this scroll used
                # with_payload=True, pulling metadata.image/video — the
                # multi-MB base64 keys — on every prune tick.  The include
                # form projects exactly what the guards consume
                # (metadata.source + page_content; nested keys verified
                # against qdrant-client 1.19 by the _stored_sources fix).
                with_payload=PayloadSelectorInclude(include=_source_scan_payload_keys(include_page_content=True)),
                with_vectors=False,
            )
            for pt in pts:
                payload = pt.payload or {}
                meta = payload.get("metadata") or {}
                src = str(meta.get("source") or "").rstrip("/")
                if not src:
                    continue
                scope_srcs.add(src)
                if src not in expected:
                    stale_triples.append((pt.id, src, payload.get("page_content") or ""))
                    stale_sources.add(src)
            if offset is None:
                break

        # -- Per-prefix guards: split the stale set into deletable vs refused --
        # The scroll yields (id, src, text) triples; keep them paired so a
        # per-prefix refusal drops exactly that prefix's ids/sources/texts.
        stale_by_prefix: dict[str, set[str]] = {p: set() for p in prefixes}

        def _prefix_of(src: str) -> str | None:
            # Prefixes from _sync_prefixes already end with exactly one "/",
            # so startswith(p) is the accurate test — rstrip("/") would make
            # prefix "s3://b/a/" also match sibling "s3://b/ab/..." sources
            # (cross-validation finding 2, 2026-10-02).
            for p in prefixes:
                if src.startswith(p):
                    return p
            return None

        for src in stale_sources:
            pp = _prefix_of(src)
            if pp is not None:
                stale_by_prefix[pp].add(src)
        # In-scope point estimate per prefix: stored sources under it (the
        # expected set is canonical-URL-shaped, so only the stored side is
        # counted — good enough for a ratio guard).
        in_scope_by_prefix: dict[str, int] = {}
        for s2 in scope_srcs:
            pp = _prefix_of(s2)
            if pp is not None:
                in_scope_by_prefix[pp] = in_scope_by_prefix.get(pp, 0) + 1

        refused: list[dict[str, Any]] = []
        delete_srcs: set[str] = set()
        for p in prefixes:
            stale_p = stale_by_prefix.get(p, set())
            # Empty-listing guard (cross-validation finding 3, rewritten):
            # refuse ONLY when the caller's listing produced NOTHING at all
            # (``expected`` empty) while stored sources exist under this
            # prefix — the mis-typed/renamed-prefix/narrowed-policy case.  A
            # legitimately EMPTIED prefix (expected == ∅ too, but the listing
            # SUCCEEDED) also lands here; that refusal is loud and the
            # operator clears it by confirming the prefix (or env-disabling
            # nothing — the guard is intentionally not overridable).  A
            # partial listing (some objects still exist upstream) never
            # trips this branch — the ratio guard handles that case.
            if not expected and stale_p and in_scope_by_prefix.get(p, 0) > 0:
                refused.append(
                    {
                        "prefix": p,
                        "reason": "empty-listing",
                        "stale_points": len(stale_p),
                        "scope_points": in_scope_by_prefix.get(p, 0),
                        "stale_sources": sorted(stale_p),
                    }
                )
                logger.warning(
                    "PRUNE REFUSED for prefix %s: the listing returned no objects but %d stored point source(s) "
                    "exist under it — nothing deleted for this prefix. Verify the prefix/listing (mis-typed or "
                    "renamed prefix, narrowed bucket policy?) before pruning.",
                    p,
                    len(stale_p),
                )
                continue
            cap = max(_PRUNE_STALE_MIN, int(in_scope_by_prefix.get(p, 0) * _PRUNE_STALE_RATIO))
            if len(stale_p) > cap:
                refused.append(
                    {
                        "prefix": p,
                        "reason": "stale-ratio",
                        "stale_points": len(stale_p),
                        "scope_points": in_scope_by_prefix.get(p, 0),
                        "cap": cap,
                        "stale_sources": sorted(stale_p),
                    }
                )
                logger.warning(
                    "PRUNE REFUSED for prefix %s: %d stale point source(s) exceed the mass-deletion cap (%d of %d "
                    "in scope) — nothing deleted for this prefix. Verify the listing is complete before pruning.",
                    p,
                    len(stale_p),
                    cap,
                    in_scope_by_prefix.get(p, 0),
                )
                continue
            delete_srcs |= stale_p

        delete_ids = [pid for pid, src, _ in stale_triples if src in delete_srcs]
        delete_sources = delete_srcs
        delete_texts = [txt for pid, src, txt in stale_triples if src in delete_srcs]

        deleted = 0
        if delete_ids:
            # Terms for the BM25 df decrement — the payloads were already
            # fetched by the scroll above, so no extra round trip.
            self._forget_bm25_texts(dataset_name, delete_texts)
        for i in range(0, len(delete_ids), 500):
            chunk = delete_ids[i : i + 500]
            client.delete(coll, points_selector=PointIdsList(points=chunk), wait=True)
            deleted += len(chunk)
        if deleted:
            self._decrement_count(dataset_name, deleted)
        return {
            "pruned_points": deleted,
            "pruned_sources": sorted(delete_sources),
            "scope": prefixes,
            "refused_prefixes": refused,
        }

    def backfill_search_metadata(self, dataset_name: str) -> dict[str, Any]:
        """Backfill ``metadata.file_type`` and search payload indexes.

        Metadata-filtered search (roadmap feature 1) keys off
        ``metadata.file_type``, which only docs ingested after the feature
        carry.  This idempotent migration scrolls every point, derives the
        file type from the stored ``metadata.source`` extension, and writes
        the full metadata object back (grouped by identical metadata so a
        batch touches Qdrant once per unique payload — the same pattern the
        media payload-stripping pass uses).  It also creates the payload
        indexes filtered search benefits from (best-effort, idempotent).

        Safe to run on any dataset, any time; points that already carry
        ``file_type`` are left untouched.  Runs on the caller's thread —
        the API endpoint offloads it to ``sync_pool``.

        Note: the read-scroll / write-set_payload window is not transactional;
        docs ingested *while* the backfill runs may be missed (re-run later —
        it is idempotent).
        """
        from qdrant_client.models import PayloadSelectorInclude

        from multimodal_rag.vector_store import ensure_search_payload_indexes

        rag = self._get_rag(dataset_name)
        vs = rag.vector_store
        if vs is None or isinstance(vs, dict):
            return {"scanned": 0, "updated": 0, "indexes": []}
        client = vs._client  # type: ignore[attr-defined]
        coll = vs.collection_name  # type: ignore[attr-defined]

        indexes = ensure_search_payload_indexes(client, coll)

        scanned = 0
        updated = 0
        offset: int | str | None = None
        while True:
            pts, offset = client.scroll(
                coll,
                limit=256,
                offset=offset,
                # Payload projection (cross-validation P2-5): the write-back
                # touches ONLY metadata — projecting ["metadata"] instead of
                # with_payload=True keeps page_content (often the largest
                # key on text corpora) and the twin media keys off the wire
                # for this collection-wide pass.
                with_payload=PayloadSelectorInclude(include=["metadata"]),
                with_vectors=False,
            )
            # group point ids by their complete new metadata so each unique
            # payload costs one set_payload call
            by_meta: dict[str, tuple[dict[str, Any], list[Any]]] = {}
            for pt in pts:
                scanned += 1
                payload = pt.payload or {}
                meta = dict(payload.get("metadata", {}) or {})
                if meta.get("file_type"):
                    continue
                src = str(meta.get("source") or meta.get("original_source") or "")
                if not src:
                    continue
                meta["file_type"] = _classify_file(src)
                key = json.dumps(meta, sort_keys=True, default=str)
                by_meta.setdefault(key, (meta, []))[1].append(pt.id)
            for meta, point_ids in by_meta.values():
                client.set_payload(
                    coll,
                    payload={"metadata": meta},
                    points=point_ids,
                    wait=True,
                )
                updated += len(point_ids)
            if offset is None:
                break
        return {"scanned": scanned, "updated": updated, "indexes": indexes}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_keep_originals(self, dataset_name: str) -> bool:
        """Read the keep_originals flag from dataset metadata (default True)."""
        meta = self._read_meta(dataset_name) or {}
        return meta.get("keep_originals", True)

    def _get_ocr_enabled(self, dataset_name: str) -> bool:
        """Read the OCR fallback flag from dataset metadata (default False)."""
        meta = self._read_meta(dataset_name) or {}
        return bool(meta.get("ocr", False))

    # ------------------------------------------------------------------
    # Per-dataset weighted-RRF defaults (feature: weighted RRF, dataset-
    # defaults slice).  Resolution order at search time: per-call override >
    # dataset meta default > global default (1.0/1.0, no pinned k).  A meta
    # default that EQUALS the global default must behave exactly like "no
    # default at all" — no override reaches the store, so the fusion request
    # stays byte-identical (the k-default trap rule from the wave-1 slice).
    # ------------------------------------------------------------------

    @staticmethod
    def _clamp_rrf_weight(value: Any, name: str) -> float:
        """Clamp a meta weight to ``[0.0, 10.0]``, rounded to 3 decimals.

        Non-numeric values raise ``ValueError`` (the PATCH/create caller
        surfaces it as HTTP 400).  ``None`` never reaches here — callers
        gate on key presence so an omitted key keeps the global default.
        """
        try:
            w = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"rrf.{name} must be a number") from None
        if math.isnan(w) or math.isinf(w):
            raise ValueError(f"rrf.{name} must be a finite number")
        return round(min(max(w, 0.0), _RRF_WEIGHT_MAX), 3)

    @staticmethod
    def _clamp_rrf_k(value: Any) -> int:
        """Clamp the meta RRF ranking constant to ``[1, 1000]``.

        Same clamp-not-reject discipline as the REST/MCP surfaces: a bad
        ``k: 0`` degrades to the minimum sane constant instead of failing.
        """
        try:
            k = int(value)
        except (TypeError, ValueError):
            raise ValueError("rrf.k must be an integer") from None
        return max(1, min(k, 1000))

    @classmethod
    def _sanitize_rrf_meta(cls, rrf: Any) -> "dict[str, Any] | None":
        """Normalize a create/PATCH ``rrf`` payload to the stored meta shape.

        Accepts ``None`` / non-dict → ``None`` (nothing stored).  A dict is
        cleaned key by key: unknown keys are dropped (forward compatibility,
        same tolerance as the public filter dict), present keys are clamped
        with the API-slice discipline.  A payload that clamps to pure
        defaults (1.0/1.0, no k) returns ``None`` — storing it would make
        ``meta["rrf"]`` LOOK set while behaving like the global default,
        which would then need default-identity logic on every read.
        """
        if not isinstance(rrf, dict):
            return None
        out: dict[str, Any] = {}
        if "dense_weight" in rrf and rrf["dense_weight"] is not None:
            out["dense_weight"] = cls._clamp_rrf_weight(rrf["dense_weight"], "dense_weight")
        if "sparse_weight" in rrf and rrf["sparse_weight"] is not None:
            out["sparse_weight"] = cls._clamp_rrf_weight(rrf["sparse_weight"], "sparse_weight")
        if "k" in rrf and rrf["k"] is not None:
            out["k"] = cls._clamp_rrf_k(rrf["k"])
        if not out:
            return None
        if out.get("dense_weight", 1.0) == 1.0 and out.get("sparse_weight", 1.0) == 1.0 and out.get("k") is None:
            # Rank-arithmetic-identical to the global default — store nothing.
            return None
        return out

    def _effective_rrf(self, dataset_name: str, rrf: "RrfParams | None" = None) -> "RrfParams | None":
        """Resolve the effective weighted-RRF params for one search.

        Order: the per-call override wins after its existence check (it was
        already clamped at the API boundary); otherwise the dataset's
        ``meta["rrf"]`` default; otherwise ``None`` — the global default,
        which keeps the fusion request byte-identical (no ``RrfQuery``).

        A meta default that equals the global default (1.0/1.0, no k) also
        returns ``None``: emitting an ``RrfQuery`` for it would pin the
        implicit client ``k`` and silently change rank arithmetic on every
        default query (the k-default trap from the wave-1 slice).
        """
        if isinstance(rrf, RrfParams):
            return rrf
        meta = self._read_meta(dataset_name) or {}
        stored = meta.get("rrf")
        if not isinstance(stored, dict) or not stored:
            return None
        dense = self._clamp_rrf_weight(stored.get("dense_weight", 1.0), "dense_weight")
        sparse = self._clamp_rrf_weight(stored.get("sparse_weight", 1.0), "sparse_weight")
        k = self._clamp_rrf_k(stored["k"]) if stored.get("k") is not None else None
        if dense == 1.0 and sparse == 1.0 and k is None:
            return None
        return RrfParams(dense_weight=dense, sparse_weight=sparse, k=k)

    def _get_rag(self, dataset_name: str, check_embedder: bool = True) -> MultimodalRAG:
        """Return (or create and cache) a MultimodalRAG for *dataset_name*.

        With ``check_embedder=True`` (default), the dataset's stored
        embedder fingerprint is verified against the currently configured
        embedder once per process; a mismatch raises :class:`EmbedderMismatchError`.
        Pass ``False`` for operations that must work regardless (e.g. delete,
        recreate).
        """
        # Fast path — no lock needed once cached
        rag = self._rag_cache.get(dataset_name)
        if rag is None:
            with self._rag_cache_lock:
                # Double-check after acquiring the lock
                rag = self._rag_cache.get(dataset_name)
                if rag is None:
                    meta = self._read_meta(dataset_name) or {}
                    ds_caption_with_asr = meta.get("caption_with_asr", self.caption_with_asr)
                    ds_caption_with_vlm = meta.get("caption_with_vlm", self.caption_with_vlm)
                    rag = MultimodalRAG(
                        embedder=self.embedder,
                        reranker=self.reranker,
                        vlm=self.vlm,
                        asr=self.asr,
                        caption_with_asr=ds_caption_with_asr,
                        caption_with_vlm=ds_caption_with_vlm,
                        contextualize=bool(meta.get("contextual", False)),
                        remote=self.remote,
                        dedup_threshold=self.dedup_threshold,
                        vector_store={
                            "qdrant_host": self.qdrant_host,
                            "qdrant_port": self.qdrant_port,
                            "collection_name": dataset_name,
                            # Where the hybrid BM25 lane reads/writes the
                            # dataset's document-frequency stats (roadmap
                            # feature 2).  Harmless for legacy collections —
                            # they never take the hybrid path.
                            "bm25_stats_path": str(self._bm25_stats_path(dataset_name)),
                        },
                    )
                    self._rag_cache[dataset_name] = rag
                    # LRU eviction: drop the least-recently-used RAG (and
                    # close its Qdrant client) past the cap.
                    while len(self._rag_cache) > self._rag_cache_max:
                        _, evicted = self._rag_cache.popitem(last=False)
                        try:
                            vs = getattr(evicted, "vector_store", None)
                            client = getattr(vs, "_client", None)
                            if client is not None:
                                client.close()
                        except Exception:
                            logger.debug("Evicted RAG client close failed", exc_info=True)
                        from multimodal_rag.utils.metrics import CACHE_EVENTS

                        CACHE_EVENTS.labels(cache="rag", event="eviction").inc()
                else:
                    self._rag_cache.move_to_end(dataset_name)
        else:
            with self._rag_cache_lock:
                if dataset_name in self._rag_cache:
                    self._rag_cache.move_to_end(dataset_name)
        if check_embedder and dataset_name not in self._embedder_verified:
            self._assert_embedder_compatible(dataset_name)
            self._nudge_schema_upgrade(dataset_name)
            self._embedder_verified.add(dataset_name)
        return rag

    # ------------------------------------------------------------------
    # Embedder fingerprint
    # ------------------------------------------------------------------

    def _embedder_dimension(self) -> int | None:
        """Vector dimension produced by the current embedder (probed once)."""
        if self.embedder is None:
            return None
        key = id(self.embedder)
        if key not in self._embedder_dim_cache:
            try:
                vec = self.embedder.model.embed_query("")
                self._embedder_dim_cache[key] = len(vec) if isinstance(vec, (list, tuple)) else None
            except Exception:
                self._embedder_dim_cache[key] = None
        return self._embedder_dim_cache[key]

    def _assert_embedder_compatible(self, dataset_name: str) -> None:
        """Record the embedder fingerprint for a dataset and, when one
        already exists, fail loudly if the current embedder no longer matches.

        The fingerprint is recorded the first time a dataset is touched so
        that a later embedder swap is detected.  No embedding call is made
        when a matching fingerprint already exists (verified once per
        dataset per process).

        The contextual-retrieval mode rides the same fingerprint: a new
        ingest whose ``contextualize`` setting differs from the stored one
        is allowed but loudly warned — per the decision, mixed-embedding
        datasets (contextualized + plain chunks) are explicitly permitted,
        so this is an introspectability nudge (per-point
        ``metadata.contextualized`` says which is which), never an error.
        """
        stored_dim = None
        stored_name = ""
        stored_contextual: bool | None = None
        meta = self._read_meta(dataset_name) or {}
        if meta:
            stored_dim = meta.get("embedder_dim")
            stored_name = meta.get("embedder_model") or ""
            if "contextual" in meta:
                stored_contextual = bool(meta.get("contextual"))

        cur_name = (getattr(self.embedder, "model_name", "") or "") if self.embedder else ""
        cur_dim = self._embedder_dimension()
        cur_contextual = bool(getattr(self, "contextualize", False))
        # Alias group: ids the operator declared equivalent to the configured
        # embedder (e.g. a quantized redeploy of the same base model).  A
        # dataset fingerprinted under any of them passes the name check.
        cur_aliases = tuple(getattr(self.embedder, "alias_names", ()) or ()) if self.embedder else ()

        if not stored_dim and not stored_name:
            # First time — record the fingerprint (dataset newly created or
            # predating this guard).
            self._write_embedder_fingerprint(dataset_name)
            return

        if stored_dim and cur_dim is not None and int(stored_dim) != cur_dim:
            raise EmbedderMismatchError(
                f"Dataset '{dataset_name}' was indexed with embedder "
                f"{stored_name!r} (dim {stored_dim}); the currently configured "
                f"embedder is {cur_name!r} (dim {cur_dim}). Existing vectors "
                "are incompatible — recreate the dataset to rebuild it "
                "(POST /api/admin/datasets/{name}/recreate)."
            )
        if stored_name and cur_name and stored_name != cur_name and stored_name not in cur_aliases:
            # Same dimension, different model — semantically incompatible
            # (unless the configured embedder declares the stored name as an
            # alias — e.g. a quantized variant of the same base model).
            alias_hint = f" (aliases accepted: {', '.join(sorted(cur_aliases))})" if cur_aliases else ""
            raise EmbedderMismatchError(
                f"Dataset '{dataset_name}' was indexed with embedder "
                f"{stored_name!r} but the currently configured embedder is "
                f"{cur_name!r}{alias_hint} (same dim {cur_dim}). Existing vectors are "
                "semantically incompatible — recreate the dataset to rebuild it "
                "(POST /api/admin/datasets/{name}/recreate)."
            )
        # Contextual-mode mismatch: allowed (the decision explicitly permits
        # mixed-embedding datasets — the per-point ``contextualized`` stamp
        # keeps them introspectable), so WARN rather than raise.  Recreate
        # remains the way to make a dataset uniformly (un-)contextualized.
        if stored_contextual is not None and stored_contextual != cur_contextual:
            logger.warning(
                "Dataset '%s' was indexed with contextual retrieval %s; ingesting now with "
                "contextual retrieval %s. Mixed context modes are allowed (per-point "
                "metadata.contextualized records which is which); Recreate the dataset "
                "(POST /api/admin/datasets/%s/recreate) to make it uniformly %s.",
                dataset_name,
                "ENABLED" if stored_contextual else "disabled",
                "enabled" if cur_contextual else "DISABLED",
                dataset_name,
                "contextualized" if cur_contextual else "plain",
            )

    def _nudge_schema_upgrade(self, dataset_name: str) -> None:
        """Warn once per process when a dataset predates the current schema.

        meta.json lacking (or carrying an older) ``schema_version`` means the
        collection holds one unnamed default vector: it cannot host the
        hybrid ``bm25`` sparse lane (roadmap feature 2), so retrieval stays
        dense-only until the dataset is recreated.  A warning, not an error —
        dense retrieval is fully functional; the existing Recreate flow is
        the adoption path.
        """
        meta = self._read_meta(dataset_name) or {}
        if not meta:
            return
        stored = int(meta.get("schema_version", 1) or 1)
        if stored < DATASET_SCHEMA_VERSION:
            logger.warning(
                "Dataset '%s' uses collection schema v%s; v%s adds hybrid dense+BM25 retrieval. "
                "Recreate it (POST /api/admin/datasets/%s/recreate) to enable the BM25 lane — "
                "dense-only search continues to work in the meantime.",
                dataset_name,
                stored,
                DATASET_SCHEMA_VERSION,
                dataset_name,
            )

    def _write_embedder_fingerprint(self, dataset_name: str) -> None:
        """Persist the current embedder's model name + dimension to meta.json.

        The dataset's contextual-retrieval mode rides along (``contextual``
        next to ``embedder_model``/``embedder_dim``): a later mode flip is
        detected and warned about (never an error — mixed-embedding datasets
        are allowed per the decision) instead of being invisible.  The value
        recorded is the DATASET's own meta flag (create/PATCH set it), not
        the manager's deployment-wide default — the fingerprint describes
        this dataset.
        """
        with self._get_meta_lock(dataset_name):
            meta = self._read_meta(dataset_name)
            if not meta:
                return
            meta["embedder_model"] = (getattr(self.embedder, "model_name", "") or "") if self.embedder else ""
            meta["embedder_dim"] = self._embedder_dimension()
            meta["contextual"] = bool(meta.get("contextual", getattr(self, "contextualize", False)))
            meta["embedder_updated_at"] = datetime.now().isoformat()
            self._write_meta(dataset_name, meta)

    def _dataset_dir(self, name: str) -> Path:
        return self.datasets_path / name

    def _meta_path(self, name: str) -> Path:
        return self._dataset_dir(name) / "meta.json"

    def _read_meta(self, name: str) -> dict[str, Any] | None:
        """Return the parsed meta.json for *name*, or ``None`` (fail-closed).

        Cached per meta path under an mtime/size stamp (perf P1-2) — see
        :data:`_META_CACHE`.  Missing/corrupt meta caches the ``None`` under
        the same stamp discipline, preserving the historical behavior exactly:
        no meta dir -> None, unparseable meta -> None, never an exception.

        Returns a defensive copy: callers mutate the returned dict (pop
        ``password_hash``, bump ``document_count``, …) and a shared cached
        dict would silently corrupt the cache for every later reader.
        ``copy.deepcopy`` keeps nested structures (``rrf``,
        ``file_type_counts``) private too, so an in-place edit of a returned
        sub-dict cannot poison the cache either.
        """
        p = self._meta_path(name)
        key = str(p)
        try:
            st = p.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = _META_CACHE_MISSING  # missing (or unstat-able) meta
        with _meta_cache_lock:
            cached = _META_CACHE.get(key)
            if cached is not None and (cached[0], cached[1]) == stamp:
                hit: dict[str, Any] | None | object = cached[2]
                # Move-to-back on hit (cross-validation P3-12): makes the
                # eviction FIFO shape an actual LRU so a >100-dataset
                # deployment with a hot working set stops thrashing.
                _META_CACHE[key] = _META_CACHE.pop(key)
            else:
                hit = _META_CACHE_HIT
        # Copy OUTSIDE the lock: cached entries are never mutated in place (a
        # refresh always stores a fresh object), so handing the reference out
        # of the critical section is safe and readers do not serialise on the
        # deep copy.
        if hit is not _META_CACHE_HIT:
            # Sentinel identity already excluded above; the declared union is
            # only there to keep the sentinel flow type-safe.
            return copy.deepcopy(cast("dict[str, Any] | None", hit))
        if stamp == _META_CACHE_MISSING:
            meta: dict[str, Any] | None = None
        else:
            try:
                meta = json.loads(p.read_text())
            except Exception:
                meta = None  # corrupt/racing meta: fail closed, never raise
        # Re-stat AFTER the read (cross-validation P1-3): a write that lands
        # between the deciding stat and read_text() would otherwise be
        # cached under the PRE-write stamp, and a later same-stamp rewrite
        # (same mtime_ns + size) could then serve the stale parse for the
        # process lifetime.  Storing only when the stamp still matches the
        # bytes we actually read closes that window in-process.
        try:
            post = p.stat()
            post_stamp = (post.st_mtime_ns, post.st_size)
        except OSError:
            post_stamp = _META_CACHE_MISSING
        if post_stamp != stamp:
            return copy.deepcopy(meta)  # changed mid-read: serve fresh, cache nothing
        with _meta_cache_lock:
            _META_CACHE[key] = (stamp[0], stamp[1], meta)
            if len(_META_CACHE) > _META_CACHE_MAX:  # bound across many datasets
                _META_CACHE.pop(next(iter(_META_CACHE)), None)
        return copy.deepcopy(meta)

    def read_created_by(self, name: str) -> "str | None":
        """The dataset's ``created_by`` stamp (D23 ownership), or None.

        None covers every "no provable creator" case: missing dataset,
        corrupt/unreadable meta, and pre-D23 metas that never carried the
        stamp — the callers (owner-or-admin delete, creator public toggle)
        all treat None as admin-only, so a corrupt read can never widen
        a non-admin's powers (fail-closed by construction).
        """
        meta = self._read_meta(name)
        if not meta:
            return None
        creator = str(meta.get("created_by") or "").strip()
        return creator or None

    def _write_meta(self, name: str, meta: dict[str, Any]) -> None:
        p = self._meta_path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: write to a temp file then os.replace() so a crash
        # mid-write never leaves a truncated meta.json.
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, indent=2, default=str))
        os.replace(tmp, p)
        # Publish the new bytes before dropping the cached parse, so a
        # concurrent _read_meta can only see the old entry (with the old
        # stamp, which now misses) or the new file — never a torn mix.
        _invalidate_meta_cache(p)

    def _sync_count_from_qdrant(self, name: str, meta: dict[str, Any]) -> None:
        """Update ``document_count`` in *meta* to match the actual Qdrant point count."""
        try:
            rag = self._get_rag(name)
            vs = rag.vector_store
            if vs is not None and not isinstance(vs, dict):
                client = vs._client  # type: ignore[attr-defined]
                coll = vs.collection_name  # type: ignore[attr-defined]
                info = client.get_collection(coll)
                qdrant_count = info.points_count
                if meta.get("document_count", 0) != qdrant_count:
                    # Re-read meta inside the cross-process lock so we
                    # don't clobber a concurrent update (e.g. description
                    # change) that happened between the caller's read and
                    # our write.
                    with self._get_meta_lock(name):
                        fresh = self._read_meta(name)
                        if fresh:
                            fresh["document_count"] = qdrant_count
                            self._write_meta(name, fresh)
        except Exception:
            pass  # Best-effort: don't fail if Qdrant is unreachable

    def contextualize_preview(self, dataset_name: str) -> dict[str, Any]:
        """Cost preview for enabling contextual retrieval (feature: contextual
        retrieval).

        The estimate is deliberately simple and LABELED as an estimate: the
        live Qdrant point count (multiplied by 2 when the collection is
        bm25/hybrid-capable, because real-text chunks get text-only twins that
        carry the same context line) × the token math of one context call
        (shared preamble + chunk input + ~OUTPUT_TOKENS_PER_CHUNK output) +
        the configured model name.  It is a *count-based* projection of what
        the NEXT ingest of this much content would cost — not a measured
        figure, and not a promise about content not yet uploaded.

        Raises ``FileNotFoundError`` when the dataset does not exist.
        """
        from multimodal_rag.utils import contextualizer

        meta = self._read_meta(dataset_name)
        if not meta:
            raise FileNotFoundError(f"Dataset '{dataset_name}' not found")

        # The live count IS the estimate basis (D12 note: when
        # RAG_DEFER_COUNT_SYNC defers syncing, meta["document_count"] may be
        # stale — read Qdrant directly so the preview never quotes a stale
        # number).
        points = 0
        hybrid_capable = False
        try:
            rag = self._get_rag(dataset_name, check_embedder=False)
            vs = rag.vector_store
            if vs is not None and not isinstance(vs, dict):
                client = vs._client  # type: ignore[attr-defined]
                info = client.get_collection(vs.collection_name)  # type: ignore[attr-defined]
                points = int(info.points_count or 0)
                hybrid_capable = bool(getattr(vs, "supports_hybrid", lambda: False)())
        except Exception:
            logger.debug("contextualize_preview: Qdrant count unavailable for %s", dataset_name, exc_info=True)
            points = int(meta.get("document_count", 0) or 0)

        chunk_count = points * 2 if hybrid_capable else points
        model_name = (getattr(self.vlm, "model_name", "") or "") if self.vlm else ""

        digest_n = contextualizer.digest_chars()
        preamble_tokens = contextualizer.sample_preamble_tokens(digest_n)
        # Average chunk: the embedder chunk budget (tokens), roughly — chars/4
        # back into tokens.  Raw-document drops can exceed it; the preview is
        # an estimate.
        chunk_tokens = max(1, int(getattr(self.embedder, "chunk_size", 2048) or 2048))
        input_per_chunk = preamble_tokens + chunk_tokens
        output_per_chunk = contextualizer.OUTPUT_TOKENS_PER_CHUNK
        total_tokens = chunk_count * (input_per_chunk + output_per_chunk)

        return {
            "dataset": dataset_name,
            "model": model_name,
            "estimated_chunk_count": chunk_count,
            "point_count": points,
            "hybrid_twin_multiplier": 2 if hybrid_capable else 1,
            "count_source": "qdrant" if points else "meta_document_count",
            "estimate_basis": (
                f"{chunk_count} chunk context call(s) — {preamble_tokens} cached-preamble tokens "
                f"(shared per document), ~{chunk_tokens} chunk-input tokens, ~{output_per_chunk} "
                "output tokens per chunk. ESTIMATE — actual cost depends on real chunk sizes "
                "and the serving engine's prefix-cache hit rate."
            ),
            "estimated_input_tokens_per_chunk": input_per_chunk,
            "estimated_output_tokens_per_chunk": output_per_chunk,
            "estimated_total_tokens": total_tokens,
            "contextual_now": bool(meta.get("contextual", False)),
        }

    def _get_meta_lock(self, name: str) -> contextlib.AbstractContextManager:
        """Return a cross-process lock for *meta.json* read→modify→write.

        Acquire this around any read-modify-write of a dataset's meta so
        that concurrent pods (sharing the RWX PVC) cannot clobber each
        other's updates.
        """
        return _cross_process_lock(self._dataset_dir(name) / ".meta.lock")

    def _increment_count(self, name: str, n: int, file_type: str | None = None) -> None:
        with self._get_meta_lock(name):
            meta = self._read_meta(name)
            if meta:
                meta["document_count"] = meta.get("document_count", 0) + n
                if file_type:
                    ft = meta.setdefault("file_type_counts", {})
                    ft[file_type] = ft.get(file_type, 0) + n
                self._write_meta(name, meta)

    def _decrement_count(self, name: str, n: int, file_type: str | None = None) -> None:
        """Decrement the document count (and optional file_type count) for a dataset."""
        with self._get_meta_lock(name):
            meta = self._read_meta(name)
            if meta:
                meta["document_count"] = max(0, meta.get("document_count", 0) - n)
                if file_type:
                    ft = meta.setdefault("file_type_counts", {})
                    ft[file_type] = max(0, ft.get(file_type, 0) - n)
                self._write_meta(name, meta)

    # ------------------------------------------------------------------
    # Ingest dedup: per-dataset set of content hashes that were
    # successfully embedded, so re-uploading byte-identical files (e.g. the
    # same archive twice, or the same file again) does not create duplicate
    # vectors.  Stored in a sidecar JSON next to the dataset files.
    # ------------------------------------------------------------------

    def _ingested_hashes_path(self, dataset_name: str) -> Path:
        return self._dataset_dir(dataset_name) / "files" / _INGESTED_HASHES_FILE

    def _is_ingested(self, dataset_name: str, file_hash: str) -> bool:
        """True if *file_hash* was already successfully ingested here."""
        return file_hash in _load_ingested_hashes(self._ingested_hashes_path(dataset_name))

    def _mark_ingested(self, dataset_name: str, file_hash: str) -> None:
        """Record *file_hash* as successfully ingested for *dataset_name*."""
        p = self._ingested_hashes_path(dataset_name)
        with _cross_process_lock(p.with_suffix(".lock")):
            hashes = _load_ingested_hashes(p)
            if file_hash in hashes and p.exists():
                return
            hashes.add(file_hash)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(sorted(hashes)), encoding="utf-8")
            os.replace(tmp, p)
            with _ingested_hashes_cache_lock:
                _ingested_hashes_cache[p] = (p.stat().st_mtime_ns, hashes)

    def _clear_ingested_hashes(self, dataset_name: str) -> None:
        """Forget every ingest-dedup hash for *dataset_name*.

        Used by :meth:`recreate_dataset` so the freshly-recreated (empty)
        collection actually re-embeds the on-disk files instead of skipping
        them all as "already ingested".
        """
        p = self._ingested_hashes_path(dataset_name)
        with _cross_process_lock(p.with_suffix(".lock")):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                logger.warning("Could not clear ingest-dedup index %s", p)
            with _ingested_hashes_cache_lock:
                _ingested_hashes_cache.pop(p, None)

    # ------------------------------------------------------------------
    # Watched S3 sources (feature: watched sources): the per-dataset
    # ETag/Size skip state in files/.watched_state.json — the sidecar the
    # reconciler's skip logic reads and the successful ingest writes.
    # Same discipline as the dedup hashes: fcntl-locked read→modify→write,
    # atomic promote, mtime-cached reads; the state is an optimisation, so
    # any failure degrades to "re-download everything", never to a wrong skip.
    # ------------------------------------------------------------------

    def _watched_state_path(self, dataset_name: str) -> Path:
        return self._dataset_dir(dataset_name) / "files" / _WATCHED_STATE_FILE

    def _watched_state_load(self, dataset_name: str) -> dict[str, dict[str, Any]]:
        """Read the watched-source skip state for *dataset_name* (may be empty)."""
        return _load_watched_state(self._watched_state_path(dataset_name))

    def _watched_state_update(
        self,
        dataset_name: str,
        fingerprints: dict[str, dict[str, Any]],
        only_urls: set[str] | None = None,
    ) -> None:
        """Record *fingerprints* (``s3://bucket/key → {etag, size}``) as the
        last-successfully-ingested versions, merged under the cross-process
        lock.

        Entries the batch did NOT see are left untouched (a prefix A sync must
        not invalidate prefix B's state).  ``only_urls`` optionally narrows the
        merge to those canonical URLs (the URLs actually attempted this run) —
        a listed-but-failed object must not get a state entry, or the next run
        would wrongly skip it.
        """
        if not fingerprints:
            return
        p = self._watched_state_path(dataset_name)
        with _cross_process_lock(p.with_suffix(".lock")):
            state = _load_watched_state(p)
            for url, fp in fingerprints.items():
                key = _canonical_s3_url(url)
                if only_urls is not None and key not in only_urls:
                    continue
                state[key] = {"etag": fp.get("etag"), "size": fp.get("size")}
            try:
                _save_watched_state(p, state)
            except OSError:
                # Best-effort: losing the state only costs re-downloads.
                logger.warning("Could not write watched-source state %s", p, exc_info=True)

    def _watched_state_clear(self, dataset_name: str) -> None:
        """Forget every watched-source fingerprint for *dataset_name*.

        Called by :meth:`recreate_dataset` (the collection was dropped — every
        source must re-ingest, exactly like the dedup-hash clear beside it).
        """
        p = self._watched_state_path(dataset_name)
        with _cross_process_lock(p.with_suffix(".lock")):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                logger.warning("Could not clear watched-source state %s", p)
            with _watched_state_cache_lock:
                _watched_state_cache.pop(p, None)

    def _watched_state_forget(self, dataset_name: str, urls: list[str]) -> None:
        """Drop the state entries for *urls* (canonicalised).

        Sync's prune half deletes a source's points when its object leaves the
        bucket — its fingerprint entry must go too, so a pruned-and-reappeared
        object is treated as new (and the force-re-ingest path embeds it).
        """
        if not urls:
            return
        p = self._watched_state_path(dataset_name)
        with _cross_process_lock(p.with_suffix(".lock")):
            state = _load_watched_state(p)
            changed = False
            for url in urls:
                key = _canonical_s3_url(url)
                if key in state:
                    del state[key]
                    changed = True
            if changed:
                try:
                    _save_watched_state(p, state)
                except OSError:
                    logger.warning("Could not update watched-source state %s", p, exc_info=True)

    def _finish_ingest(self, dataset_name: str, content_hash: str | None, result: dict[str, Any]) -> dict[str, Any]:
        """Mark *content_hash* ingested when the result stored vectors, then return it."""
        if content_hash and result.get("stored_ids"):
            self._mark_ingested(dataset_name, content_hash)
        return result

    # ------------------------------------------------------------------
    # Hybrid BM25 (roadmap feature 2): per-dataset document-frequency
    # counts for the lexical lane, in a sidecar next to .hashes.json.
    # The ingest path (rag_system → utils.bm25.record_documents) does the
    # writing, under the same cross-process lock discipline as the dedup
    # hashes; these are the dataset-manager-side hooks — the path, the
    # recreate reset, and the delete-path decrements that keep df from
    # inflating forever (memory datasets churn session history constantly).
    # ------------------------------------------------------------------

    def _bm25_stats_path(self, dataset_name: str) -> Path:
        return self._dataset_dir(dataset_name) / "files" / bm25_lane.BM25_STATS_FILENAME

    def reset_bm25_stats(self, dataset_name: str) -> None:
        """Forget every BM25 df count for *dataset_name*.

        :meth:`recreate_dataset` must reset the stats together with the
        collection: re-ingesting the same files on top of the old counts
        would double every document frequency and flatten idf.
        """
        bm25_lane.reset_stats(self._bm25_stats_path(dataset_name))

    def forget_bm25_documents(self, dataset_name: str, doc_ids: list[Any]) -> None:
        """Decrement BM25 df stats for points that are about to be deleted.

        The deleted points' texts are fetched (payloads still exist here —
        call BEFORE ``client.delete``) and their terms decremented, mirroring
        the ingest-side increment.  Strictly best-effort: any failure only
        leaves the idf weighting slightly stale, so it must never break a
        delete.  No-op for legacy collections (no BM25 lane) — probed once
        per process via the store's capability cache.
        """
        if not doc_ids:
            return
        try:
            vs = self._get_rag(dataset_name, check_embedder=False).vector_store
            if vs is None or isinstance(vs, dict) or not getattr(vs, "supports_hybrid", lambda: False)():
                return
            from qdrant_client.models import PayloadSelectorInclude

            client = vs._client  # type: ignore[attr-defined]
            texts: list[str] = []
            # Chunked like the delete batches themselves — a huge prune would
            # otherwise build one oversized retrieve request.
            ids = list(doc_ids)
            for i in range(0, len(ids), 500):
                records = client.retrieve(
                    collection_name=vs.collection_name,  # type: ignore[attr-defined]
                    ids=ids[i : i + 500],
                    # page_content is all the df decrement needs — never the
                    # multi-MB tier-3 base64 media payloads.
                    with_payload=PayloadSelectorInclude(include=["page_content"]),
                    with_vectors=False,
                )
                texts.extend((rec.payload or {}).get("page_content") or "" for rec in records)
            self._forget_bm25_texts(dataset_name, texts)
        except Exception:
            logger.debug("BM25 df decrement failed", exc_info=True)

    def _forget_bm25_texts(self, dataset_name: str, texts: list[str]) -> None:
        """Decrement the BM25 df stats by *texts* (locked, best-effort)."""
        try:
            term_maps = [bm25_lane.term_counts(t) for t in texts if t]
            if term_maps:
                bm25_lane.forget_documents(self._bm25_stats_path(dataset_name), term_maps)
        except Exception:
            logger.debug("BM25 df decrement failed", exc_info=True)

    def _store_file(
        self,
        dataset_name: str,
        source_path: str,
        original_name: str | None = None,
        defer_write: bool = False,
    ) -> Path:
        """Copy *source_path* into the dataset's files directory and return the new path.

        Files are deduplicated by SHA-256 hash: if a file with the same
        content was already stored, the existing path is returned without
        copying.

        With ``defer_write=True`` (batch ingests) the new hash entry is not
        written to ``.hashes.json`` immediately: it is recorded in a dirty
        set merged into the on-disk index by
        :func:`_flush_hash_index_writes` at the end of the batch.  Writing
        the whole index once per file made N-file ingests O(n²) in disk I/O.
        Callers that defer MUST flush (batch paths do so in ``finally``);
        crash before flush only loses dedup entries — files remain on disk
        and a retry re-copies them.
        """
        self._validate_name(dataset_name)
        files_dir = self._dataset_dir(dataset_name) / "files"
        files_dir.mkdir(parents=True, exist_ok=True)

        # Compute hash of incoming file
        h = hashlib.sha256()
        with open(source_path, "rb") as f:
            while chunk := f.read(8192):
                h.update(chunk)
        file_hash = h.hexdigest()

        # Copy FIRST — outside the lock.  The destination name is a fresh
        # UUID, so concurrent writers never collide on the file itself; the
        # cross-process lock only guards the .hashes.json read-modify-write.
        # (It used to be held across the whole copy, serializing concurrent
        # uploads to a dataset behind each full file copy on the NFS PVC.)
        if original_name:
            stem = Path(original_name).stem
            suffix = Path(original_name).suffix
        else:
            stem = Path(source_path).stem
            suffix = Path(source_path).suffix
        dest = files_dir / f"{uuid.uuid4().hex}_{stem}{suffix}"
        import shutil

        # Atomic copy (audit C4): write a UNIQUE ``.part`` sibling and rename
        # it onto the final path only once the bytes are all there.  The old
        # ``copy2(source, dest)`` wrote straight to the final path, so a
        # crash mid-copy (pod eviction, full disk) left a TRUNCATED file
        # under a name the hash index — and the recreate fallback — treat as
        # a complete document, re-embedding garbage.  Same pattern as the
        # preprocessed-media writers (unique temp sibling → validate →
        # ``Path.replace``): the ``.part`` name keeps this call's fresh UUID
        # prefix, so concurrent writers never share a temp path.
        #
        # The name is DOT-prefixed (``.<dest>.part``) — same convention as
        # ``_unique_tmp_sibling`` — because a SIGKILL between copy and
        # replace cannot run the cleanup below, and every file listing
        # (MCP ``get_dataset_files``, API export/storage stats, the backup
        # archive's ``rglob``) filters with ``name.startswith(".")``: a
        # non-dot ``.part`` would surface as a phantom dataset file.
        part = dest.with_name(f".{dest.name}.part")
        try:
            shutil.copy2(source_path, part)
            os.replace(part, dest)  # atomic on POSIX — no partial file ever visible
        except Exception:
            with contextlib.suppress(OSError):
                part.unlink(missing_ok=True)
            raise

        # Load or create hash index.  The read→modify→write of .hashes.json
        # is guarded by a cross-process file lock so concurrent uploads
        # (possibly from different pods sharing the RWX PVC) cannot lose
        # hash entries and corrupt dedup.
        hash_index_path = files_dir / ".hashes.json"
        with _cross_process_lock(files_dir / ".hashes.lock"):
            hash_index = _load_hash_index(hash_index_path)

            # Check for existing file with same hash
            if file_hash in hash_index:
                existing = Path(hash_index[file_hash])
                if existing.exists():
                    # Another writer stored this file while we were copying
                    # — drop our duplicate and reuse the indexed copy.
                    dest.unlink(missing_ok=True)
                    return existing
                # Stale entry — replace it with our fresh copy
                del hash_index[file_hash]

            hash_index[file_hash] = str(dest)
            if defer_write:
                with _hash_index_dirty_lock:
                    _hash_index_dirty.setdefault(hash_index_path, {})[file_hash] = str(dest)
            else:
                _write_hash_index(hash_index_path, hash_index)
            return dest
