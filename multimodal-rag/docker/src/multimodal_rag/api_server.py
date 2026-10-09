"""
FastAPI application for the multimodal RAG dataset manager.

Run with::

    uvicorn multimodal_rag.api_server:app --host 0.0.0.0 --port 8000

Or via Python::

    python -m multimodal_rag.api_server
"""

import argparse
import asyncio
import contextvars
import datetime
import html
import io
import json
import math
import mimetypes
import os
import random
import re
import resource
import tarfile
import tempfile
import threading
import time
import urllib.parse
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any, cast

import httpx2
from fastapi import (
    Body,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from multimodal_rag import __version__
from multimodal_rag.dataset_manager import (
    DatasetManager,
    EmbedderMismatchError,
    _check_media_url_policy,
    _cross_process_lock,
    _load_upload_history,
    _notify_ingest_hook,
    _record_upload_history,
    _save_upload_history,
    _upload_history_path,
)
from multimodal_rag.rag_system import (
    _arerank_with,
    _ingest_warnings,
    merge_federated_results,
    resolve_federated_targets,
)
from multimodal_rag.utils import access_store as _access_store
from multimodal_rag.utils import admin_registry as _admin_registry
from multimodal_rag.utils import clients_registry as _clients_registry
from multimodal_rag.utils.general_tools import sync_pool
from multimodal_rag.utils.logging_utils import logging, setup_logger
from multimodal_rag.utils.media_paths import MediaRefError
from multimodal_rag.vector_store import RrfParams

logger = logging.getLogger(__name__)


def _submit_with_context(executor, fn, *args, **kwargs):
    """Submit *fn(*args, **kwargs)* to *executor* keeping this thread's
    contextvars active in the worker thread.

    ``run_in_executor`` hands *fn* to a thread-pool worker whose contextvars
    are the empty default, so the ingest-warning collector (``_ingest_warnings``
    in :mod:`rag_system`) set in this request handler would be invisible there
    and every skip/caption warning would silently vanish before the UI can
    return it.  ``contextvars.copy_context()`` is evaluated HERE (the handler
    thread, with the collector set) and ``Context.run`` executes *fn* under it
    in the worker — and transitively under the background event loop and the
    consumer threads that ``sync_wrapper_safe``/``add_files_batch`` spawn, so
    warnings accumulate into the same list the handler returns.
    """
    ctx = contextvars.copy_context()
    return asyncio.get_running_loop().run_in_executor(executor, ctx.run, lambda: fn(*args, **kwargs))


# ---------------------------------------------------------------------------
# Upload job tracker (thread-safe, poll-based progress)
# ---------------------------------------------------------------------------


class _UploadJobTracker:
    """Tracks background upload jobs for poll-based progress reporting.

    Replaces the previous SSE stream approach which tied upload lifecycle
    to a persistent HTTP connection.  With polling, a browser tab can be
    closed and reopened without losing progress tracking.

    Job state lives in-process **and** is mirrored to Redis (when
    ``REDIS_URL`` is configured and reachable), so in multi-worker /
    multi-replica deployments any API process can answer a progress poll for
    a job that another process created — the in-memory-only tracker returned
    "Job not found" whenever the load balancer landed the poll on a different
    gunicorn worker or pod (e.g. recreate under the 4-replica × 4-worker
    scale chart).  Without Redis it degrades to in-memory-only (single
    process) behaviour.
    """

    _REDIS_PREFIX = "upload_job:"
    # Long enough for big batch/recreate ingests; cleanup is via Redis TTL.
    _REDIS_TTL_SECONDS = 6 * 3600

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _redis(self):
        """Best-effort shared Redis client (reuses dataset_manager's)."""
        try:
            return _get_redis()
        except Exception:
            return None

    def _redis_key(self, job_id: str) -> str:
        return f"{self._REDIS_PREFIX}{job_id}"

    def _redis_set(self, job: dict[str, Any]) -> None:
        r = self._redis()
        if r is None:
            return
        try:
            r.set(self._redis_key(job["job_id"]), json.dumps(job, default=str), ex=self._REDIS_TTL_SECONDS)
        except Exception:
            # Best-effort mirror; the in-process copy remains authoritative
            # for the owning worker.
            pass

    def _redis_get(self, job_id: str) -> dict[str, Any] | None:
        r = self._redis()
        if r is None:
            return None
        try:
            raw = r.get(self._redis_key(job_id))
        except Exception:
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    def create(self, dataset_name: str, total_files: int, source: str = "files") -> str:
        job_id = uuid.uuid4().hex[:12]
        job = {
            "job_id": job_id,
            "dataset": dataset_name,
            "source": source,
            "status": "uploading",
            "total_files": total_files,
            "processed_files": 0,
            "total_chunks": 0,
            "events": [],
            "result": None,
            "error": None,
            "created_at": time.time(),
            "completed_at": None,
        }
        with self._lock:
            self._jobs[job_id] = job
        self._redis_set(job)
        return job_id

    def add_event(self, job_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["events"].append(event)
            # Keep last 500 events to bound memory
            if len(job["events"]) > 500:
                job["events"] = job["events"][-500:]
            # Update aggregate counters
            status = event.get("status")
            if status == "complete" and event.get("chunks") is not None:
                job["processed_files"] += 1
                job["total_chunks"] += event.get("chunks", 0)
            elif status == "error":
                job["processed_files"] += 1
        self._redis_set(job)

    def complete(self, job_id: str, result: dict[str, Any] | None = None) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["status"] = "complete"
            job["result"] = result
            job["completed_at"] = time.time()
            source = str(job.get("source") or "files")
        self._redis_set(job)
        from multimodal_rag.utils.metrics import INGEST_JOBS

        INGEST_JOBS.labels(source=source, state="complete").inc()

    def fail(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["status"] = "error"
            job["error"] = error
            job["completed_at"] = time.time()
            source = str(job.get("source") or "files")
        self._redis_set(job)
        from multimodal_rag.utils.metrics import INGEST_JOBS

        INGEST_JOBS.labels(source=source, state="error").inc()

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is not None:
            return dict(job)
        return self._redis_get(job_id)

    def cleanup_old(self, max_age_seconds: int = 3600) -> None:
        """Remove completed jobs older than *max_age_seconds*.

        Only the in-process copy is pruned here; Redis keys expire via their
        own TTL so another worker can still answer a poll for them.
        """
        now = time.time()
        with self._lock:
            stale = [
                jid
                for jid, job in self._jobs.items()
                if job.get("completed_at") and now - job["completed_at"] > max_age_seconds
            ]
            for jid in stale:
                del self._jobs[jid]


_upload_jobs = _UploadJobTracker()

# ---------------------------------------------------------------------------
# Upload history (persisted per-file log shown on the Manage page)
# ---------------------------------------------------------------------------
# Each completed upload/ingestion job appends one entry per processed file so
# the Manage page can show a table of what was uploaded and when.  The log
# lives under DATA_PATH (the shared RWX PVC) so it survives restarts and is
# visible across pods; a cross-process fcntl lock serializes appends.
#
# Wave-5 (F2): the implementation moved to dataset_manager (single source of
# truth) so the MCP document-management tools record the SAME events as these
# REST twins. The names are imported at the top of this module —
# `api_server._record_upload_history` keeps working for callers and tests.


# ---------------------------------------------------------------------------
# Application config from environment
# ---------------------------------------------------------------------------

DATA_PATH = os.environ.get("DATA_PATH", "/data")
QDRANT_HOST = os.environ.get("QDRANT_HOST", "")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
# Path where the Qdrant PVC is mounted read-only (so we can report its disk
# usage). Empty when the mount is not configured (e.g. sharded Qdrant cluster).
QDRANT_STORAGE_PATH = os.environ.get("QDRANT_STORAGE_PATH", "")
# Per-replica Qdrant PVC size (e.g. "100Gi" in the scale charts). Lets the
# management page show capacity alongside per-replica shard placement even
# when the per-replica PVCs are not mounted on the API pod. Empty when unset.
QDRANT_PVC_SIZE = os.environ.get("QDRANT_PVC_SIZE", "")
# Optional ``:``-separated directories of mounted ConfigMap/Secret files
# (one file per env key).  When set, the watcher live-reloads the model
# configuration when these files change — no pod rollout required.
CONFIG_DIR = os.environ.get("CONFIG_DIR", "")
RAG_REMOTE = os.environ.get("RAG_REMOTE", "true").lower() in ("true", "1", "yes")
RAG_OCR_DEFAULT = os.environ.get("RAG_OCR_DEFAULT", "false").lower() in (
    "true",
    "1",
    "yes",
)
# Weighted-RRF create-time default (feature: weighted RRF, dataset-defaults
# slice; chart values rag.rrfDefault).  Disabled by default: when unset the
# server stamps NO per-dataset defaults and new datasets inherit the global
# 1.0/1.0 code default (no behaviour change for existing deployments or
# existing datasets — the env only affects NEW datasets at create time).
# Format: "dense,sparse[,k]" e.g. "1.0,3.0" or "1.0,0.5,2".
RAG_RRF_DEFAULT = os.environ.get("RAG_RRF_DEFAULT", "").strip()
# Contextual-retrieval create-time default (feature: contextual retrieval;
# chart values rag.contextual).  Disabled by default: when false, new datasets
# stamp contextual: false and ingest exactly as before (the per-request body
# "contextual" value always wins).  One small LLM call per real-text chunk at
# ingest — the preview endpoint exists so the operator sees the cost before
# flipping this deployment-wide.
RAG_CONTEXTUAL_DEFAULT = os.environ.get("RAG_CONTEXTUAL_DEFAULT", "false").lower() in (
    "true",
    "1",
    "yes",
)
RAG_CAPTION_WITH_ASR = os.environ.get("RAG_CAPTION_WITH_ASR", "false").lower() in (
    "true",
    "1",
    "yes",
)

# ---------------------------------------------------------------------------
# Lazy-initialised DatasetManager singleton
# ---------------------------------------------------------------------------

_dm: DatasetManager | None = None
_dm_lock = threading.Lock()


def get_manager() -> DatasetManager:
    global _dm
    if _dm is not None:
        return _dm

    with _dm_lock:
        # Double-check after acquiring the lock — another thread may
        # have completed initialisation while we were waiting.
        if _dm is not None:
            return _dm

        # Read config from env (set by CLI args or K8s deployment) rather than
        # the module-level variables which are captured at import time.
        data_path = os.environ.get("DATA_PATH", "/data")
        qdrant_host = os.environ.get("QDRANT_HOST", "")
        qdrant_port = int(os.environ.get("QDRANT_PORT", "6333"))
        rag_remote = os.environ.get("RAG_REMOTE", "true").lower() in (
            "true",
            "1",
            "yes",
        )
        rag_caption_with_asr = os.environ.get("RAG_CAPTION_WITH_ASR", "false").lower() in (
            "true",
            "1",
            "yes",
        )
        rag_caption_with_vlm = os.environ.get("RAG_CAPTION_WITH_VLM", "false").lower() in (
            "true",
            "1",
            "yes",
        )
        rag_dedup_threshold = float(os.environ.get("RAG_DEDUP_THRESHOLD", "0.995"))

        from multimodal_rag.model_config import build_all

        embedder, reranker, vlm, asr = build_all()

        _dm = DatasetManager(
            base_path=data_path,
            qdrant_host=qdrant_host,
            qdrant_port=qdrant_port,
            embedder=embedder,
            reranker=reranker,
            vlm=vlm,
            asr=asr,
            caption_with_asr=rag_caption_with_asr,
            caption_with_vlm=rag_caption_with_vlm,
            remote=rag_remote,
            dedup_threshold=rag_dedup_threshold,
            contextualize=RAG_CONTEXTUAL_DEFAULT,
        )
        logger.info(
            "DatasetManager initialised: data=%s qdrant=%s:%s remote=%s",
            data_path,
            qdrant_host or "(local)",
            qdrant_port,
            rag_remote,
        )
        return _dm


async def get_manager_async() -> DatasetManager:
    """Async-safe wrapper — offloads get_manager() to a thread pool.

    On the fast path (_dm already initialised) this is nearly free.
    On the slow path (startup init failed) it avoids blocking the event
    loop while model endpoints are probed under _dm_lock.
    """
    if _dm is not None:
        return _dm
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(sync_pool, get_manager)


# ---------------------------------------------------------------------------
# Unlock cache (password once, use-for-N-minutes)
# ---------------------------------------------------------------------------

# Identity is derived from the authenticated user (oauth2-proxy headers)
# rather than the client IP, which is unreliable behind Istio/oauth2-proxy
# (many users may share one proxy IP — one unlock would open the dataset
# for everyone).  Falls back to X-Forwarded-For / client host for
# non-authenticated deployments.
_UNLOCK_CACHE: dict[tuple[str, str], tuple[float, str]] = {}
_UNLOCK_CACHE_LOCK = threading.Lock()
_UNLOCK_TTL = int(os.environ.get("UNLOCK_TTL", "1800"))  # seconds, default 30 min

# Upper bound an explicit unlock TTL may take (the /unlock endpoint clamps
# into this; default 86400 = 24 h, the historical hard cap).  The special
# value 0 OPTS THE DEPLOYMENT INTO NO-EXPIRY UNLOCKS: a caller may then pass
# ttl=0 and the unlock persists until explicitly revoked (POST /lock) — the
# /access page's "No expiry (0)" option is meaningful only when this knob is
# 0.  Any positive value caps ALL unlocks at that many seconds; 0 is
# deliberately opt-in because a no-expiry unlock caches a dataset's password
# plaintext in the unlock store indefinitely (Redis when configured — treat
# that store with the same care as the dataset passwords themselves).
# Read per call so a config change needs no restart (the env-var convention).
_UNLOCK_TTL_MAX_ENV = "RAG_UNLOCK_MAX_TTL"


def _unlock_ttl_max() -> int:
    """Configured max unlock TTL (default 86400); 0 = no-expiry unlocks allowed.

    Read per call (config without restart).  Negative or malformed values
    fall back to the historical 24-hour cap.
    """
    raw = os.environ.get(_UNLOCK_TTL_MAX_ENV, "").strip()
    if not raw:
        return 86400
    try:
        val = int(raw)
    except ValueError:
        return 86400
    return val if val >= 0 else 86400


# Optional Redis backend for cross-pod unlock sharing.  Enabled when
# REDIS_URL is set (the scale chart sets it); otherwise the per-process
# dict above is used (a user may be prompted to re-unlock if routed to a
# different pod).
_REDIS_URL = os.environ.get("REDIS_URL", "")
_redis_client: Any = None
_redis_client_lock = threading.Lock()


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
            logger.info("Unlock cache: Redis backend connected at %s", _REDIS_URL)
        except Exception as exc:
            logger.warning("Unlock cache: Redis unavailable (%s); falling back to in-memory", exc)
            _redis_client = None
    return _redis_client


def _shared_unlock_enabled() -> bool:
    """True when RAG_MCP_SHARED_UNLOCK opts OUT of per-caller unlock scoping
    (decision D10 escape hatch for gateway-fronted single-user deployments):
    every caller shares one unlock cache and one throttle bucket — the
    pre-D10 shared behaviour.  Read per call so a config change needs no
    restart."""
    return os.environ.get("RAG_MCP_SHARED_UNLOCK", "").strip().lower() in ("1", "true", "yes")


def _request_identity(request: Request) -> str:
    """Resolve the per-caller identity (decision D10), most precise source first.

    Order of availability:

    1. **Provided identity** — the auth proxy's identity headers
       (``X-Auth-Request-Email`` / ``X-Auth-Request-User`` / ``X-Email`` /
       ``X-User``), honoured only when ``RAG_TRUST_PROXY_IDENTITY`` confirms
       an enforcing proxy sits in front of this server (client-spoofable
       otherwise) — per-USER.
    2. **Forwarded-for chain** — ``X-Forwarded-For``, under the same trust
       gate — per client IP behind a proxy that sets it.  The FULL
       normalized chain is the identity value, so a caller cannot
       reconstruct another user's chain value to read their cached unlock;
       an XFF-appending proxy does leave client-sent prefixes in the chain
       (fresh throttle buckets per fake prefix — residual documented in
       mcp_server's mirror of this helper; the provided identity wins when
       present).
    3. **Socket peer** — direct connections (single-user direct deployments:
       unchanged — the peer is stable across requests and sessions).

    The opencode session id is deliberately NOT an unlock identity (see the
    mirror comment in mcp_server: spoofable, and it would split a direct
    single user's unlock per conversation).
    """
    if _TRUST_PROXY_IDENTITY:
        for header in ("X-Auth-Request-Email", "X-Auth-Request-User", "X-Email", "X-User"):
            val = request.headers.get(header)
            if val and val.strip():
                return val.strip()
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            chain = ",".join(p.strip() for p in xff.split(",") if p.strip())
            if chain:
                return f"xff:{chain}"
    client = request.client
    return client.host if client else "unknown"


def _unlock_client_id(request: Request) -> str:
    """Return the per-caller identifier for the unlock cache and the
    password-failure throttle (decision D10: per-caller).

    Resolution order: a D15 registry-key identity (``key:<name>`` — stable
    per key, per-key throttle buckets ride this machinery) → the D10 identity
    (provided identity → forwarded-for chain → socket peer, trust-gated) →
    the shared ``"default"`` under ``RAG_MCP_SHARED_UNLOCK=1`` — the pre-D10
    shared behaviour, restored explicitly for gateway-fronted single-user
    deployments.
    """
    if _clients_registry.registry_configured():
        identity = _clients_registry.current_identity()
        if identity is not None and not identity.is_admin:
            return identity.client_id
    if _shared_unlock_enabled():
        return "default"
    return _request_identity(request)


def _unlock_cache_key(dataset: str, cid: str) -> str:
    return f"unlock:{dataset}:{cid}"


_UNLOCK_NO_EXPIRY = float("inf")  # in-memory sentinel for ttl=0 (no expiry)


def _unlock_cache_get(dataset: str, cid: str) -> str | None:
    """Return a cached password if present and unexpired, else None."""
    r = _get_redis()
    if r is not None:
        try:
            val = r.get(_unlock_cache_key(dataset, cid))
            # Redis returns bytes; decode to str for downstream use.
            if val is not None:
                return val.decode() if isinstance(val, bytes) else val
            return None
        except Exception:
            return None
    with _UNLOCK_CACHE_LOCK:
        entry = _UNLOCK_CACHE.get((dataset, cid))
    if entry is None:
        return None
    expiry, cached_pw = entry
    if time.monotonic() < expiry:  # _UNLOCK_NO_EXPIRY is always in the future
        return cached_pw
    with _UNLOCK_CACHE_LOCK:
        _UNLOCK_CACHE.pop((dataset, cid), None)
    return None


def _unlock_cache_set(dataset: str, cid: str, password: str, ttl: int | None = None) -> None:
    """Cache an unlock.  ``ttl=0`` means NO expiry (persists until revoked).

    In-memory: the expiry is ``_UNLOCK_NO_EXPIRY`` (always in the future).
    Redis: the key is set WITHOUT ``ex`` — the operator is responsible for
    the store's own retention (``RAG_UNLOCK_MAX_TTL=0`` is the explicit
    opt-in for this; the /access page surfaces it as "No expiry (0)").
    """
    no_expiry = ttl is not None and ttl <= 0
    r = _get_redis()
    if r is not None:
        try:
            if no_expiry:
                r.set(_unlock_cache_key(dataset, cid), password)
            else:
                r.set(_unlock_cache_key(dataset, cid), password, ex=(ttl if ttl is not None else _UNLOCK_TTL))
            return
        except Exception:
            pass
    expiry = _UNLOCK_NO_EXPIRY if no_expiry else time.monotonic() + (ttl if ttl is not None else _UNLOCK_TTL)
    with _UNLOCK_CACHE_LOCK:
        _UNLOCK_CACHE[(dataset, cid)] = (expiry, password)


def _unlock_cache_set_ttl(dataset: str, cid: str, password: str, ttl: int) -> None:
    """Alias so the unlock endpoint can pass an explicit TTL."""
    _unlock_cache_set(dataset, cid, password, ttl)


def _unlock_cache_del(dataset: str, cid: str) -> bool:
    """Revoke an unlock (Redis when configured, in-process otherwise)."""
    r = _get_redis()
    if r is not None:
        try:
            return bool(r.delete(_unlock_cache_key(dataset, cid)))
        except Exception:
            return False
    with _UNLOCK_CACHE_LOCK:
        return _UNLOCK_CACHE.pop((dataset, cid), None) is not None


# ---------------------------------------------------------------------------
# Password-failure throttling (brute-force mitigation)
# ---------------------------------------------------------------------------

_PW_FAIL_WINDOW = float(os.environ.get("PW_FAIL_WINDOW", "300.0"))  # seconds
_PW_MAX_FAILURES = max(1, int(os.environ.get("PW_MAX_FAILURES", "10")))
_pw_fail_buckets: dict[str, list[float]] = {}
_pw_fail_lock = threading.Lock()


def _pw_failure_count(cid: str) -> int:
    now = time.monotonic()
    with _pw_fail_lock:
        lst = _pw_fail_buckets.get(cid)
        if not lst:
            return 0
        lst[:] = [t for t in lst if now - t < _PW_FAIL_WINDOW]
        return len(lst)


def _pw_record_failure(cid: str) -> None:
    now = time.monotonic()
    with _pw_fail_lock:
        lst = _pw_fail_buckets.setdefault(cid, [])
        lst[:] = [t for t in lst if now - t < _PW_FAIL_WINDOW]
        lst.append(now)
        if len(_pw_fail_buckets) > 10_000:
            stale = [k for k, v in _pw_fail_buckets.items() if not v]
            for k in stale:
                _pw_fail_buckets.pop(k, None)


def _pw_reset_failures(cid: str) -> None:
    with _pw_fail_lock:
        _pw_fail_buckets.pop(cid, None)


def _check_pw_throttle(cid: str) -> None:
    if _pw_failure_count(cid) >= _PW_MAX_FAILURES:
        raise HTTPException(429, "Too many password attempts — try again later.")


# ---------------------------------------------------------------------------
# Short-lived media tokens (shared secret with the MCP server)
# ---------------------------------------------------------------------------

_MEDIA_TOKEN_SECRET = os.environ.get("MEDIA_TOKEN_SECRET", "")
_MEDIA_TOKEN_TTL = max(60, int(os.environ.get("MEDIA_TOKEN_TTL", "3600")))


def _sign_media_token(dataset_name: str, rel_path: str, expiry: int | None = None) -> str:
    """Mint an expiring HMAC token authorising ``{dataset}/{rel_path}``.

    A ``rel_path`` of ``"*"`` produces a *dataset-scoped* token valid for any
    file in the dataset (used by the web UI, which cannot mint a token per
    media URL on every render).

    The signature is truncated to 128 bits (32 hex chars) — shorter media
    URLs are copied more reliably by LLMs, and 128 bits is ample for a token
    that expires after ``MEDIA_TOKEN_TTL``.
    """
    import hashlib
    import hmac

    expiry = expiry or (int(time.time()) + _MEDIA_TOKEN_TTL)
    msg = f"{dataset_name}:{rel_path}:{expiry}".encode()
    sig = hmac.new(_MEDIA_TOKEN_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:32]
    return f"{expiry}.{sig}"


def _verify_media_token(dataset_name: str, rel_path: str, token: str) -> bool:
    """True if *token* is an unexpired HMAC authorising ``{dataset}/{rel_path}``.

    Also accepts a dataset-scoped token (minted against ``*``), which grants
    access to any file under the dataset until it expires.
    """
    import hashlib
    import hmac

    if not _MEDIA_TOKEN_SECRET:
        return False
    try:
        expiry_s, sig = token.split(".", 1)
        expiry = int(expiry_s)
    except (ValueError, TypeError):
        return False
    now = int(time.time())
    if expiry < now or expiry > now + _MEDIA_TOKEN_TTL + 300:
        return False
    for candidate in (rel_path, "*"):
        msg = f"{dataset_name}:{candidate}:{expiry}".encode()
        full = hmac.new(_MEDIA_TOKEN_SECRET.encode(), msg, hashlib.sha256).hexdigest()
        # Accept the current 32-char (128-bit) signature and the legacy
        # 64-char full signature so tokens minted before a rolling deploy
        # keep working until they expire.
        if hmac.compare_digest(full[:32], sig) or hmac.compare_digest(full, sig):
            return True
    return False


async def _require_dataset_password(
    dm: DatasetManager,
    name: str,
    password: str | None,
    request: Request | None = None,
) -> None:
    """Raise 401/403 if the dataset is password protected and the password
    is missing or incorrect.

    If *request* is provided, the unlock cache is also checked so that a
    previously-unlocked session can skip the password.

    The blocking parts (the NFS meta read in ``has_password`` and the
    PBKDF2-600k hashing in ``verify_password`` — ~0.2–0.5s of pure CPU) run
    in ``sync_pool``: hashing on the event loop stalled every concurrent
    request for the duration of each attempt.
    """
    loop = asyncio.get_running_loop()
    if not await loop.run_in_executor(sync_pool, dm.has_password, name):
        return
    cid = _unlock_client_id(request) if request is not None else "unknown"

    # 0. D16: a saved selection password (the identity verified it once at
    #    selection time — selecting a protected dataset IS the unlock).
    if request is not None:
        identity = _clients_registry.current_identity()
        saved = _access_store.selection_password(identity, name)
        if saved:
            return

    # 1. If a password was supplied, verify and cache it
    if password:
        _check_pw_throttle(cid)
        if await loop.run_in_executor(sync_pool, dm.verify_password, name, password):
            _pw_reset_failures(cid)
            if request is not None:
                _unlock_cache_set(name, cid, password)
            return
        _pw_record_failure(cid)
        raise HTTPException(403, f"Incorrect password for dataset '{name}'")

    # 2. Check the unlock cache (trust the cached password without
    #    re-hashing — it was verified when first cached, and the TTL
    #    is short.  Re-verifying with PBKDF2-600k would add ~0.5s to
    #    every cached request.)
    if request is not None:
        cid = _unlock_client_id(request)
        cached = _unlock_cache_get(name, cid)
        if cached is not None:
            return

    _check_pw_throttle(cid)
    raise HTTPException(401, f"Dataset '{name}' is password protected")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Run startup initialisation, then serve.

    Replaces the deprecated ``@app.on_event("startup")`` hook (removed in
    FastAPI 0.99+).  ``_eager_init`` is defined later in this module; the
    reference resolves lazily when the lifespan actually runs at startup.
    """
    if not _RAG_API_KEY:
        logger.warning(
            "RAG_API_KEY is not set: the REST API is unauthenticated. "
            "Admin endpoints (/api/admin/*), dataset deletion/update and any "
            "dataset without a password are reachable by anyone who can "
            "reach this server — set RAG_API_KEY (helm: security.apiKey) or "
            "restrict access at the ingress."
        )
    # D21: an OIDC resolver switched on without an issuer is inert (fail
    # closed — every JWT 401s); scream about it once at startup.
    from multimodal_rag.utils import oidc_identity

    oidc_identity.warn_if_misconfigured("the REST API server")
    await _eager_init()
    yield


app = FastAPI(
    title="Multimodal RAG Dataset Manager",
    version=__version__,
    lifespan=_lifespan,
)


@app.exception_handler(EmbedderMismatchError)
async def _embedder_mismatch_handler(request: Request, exc: EmbedderMismatchError):
    """Return 409 when a dataset's vectors were built with a different
    embedder than the one currently configured."""
    return JSONResponse(status_code=409, content={"detail": str(exc)})


# Optional API-key authentication for the REST API.  Enabled by setting
# RAG_API_KEY.  When set, every request must present either the
# ``X-RAG-Api-Key`` header or ``Authorization: Bearer <key>``.  Liveness /
# readiness probes, the HTML frontend pages, dataset media serving (which is
# password/token protected anyway) and staged-file serving are exempt.
# With the key set, interactive docs (/docs) are effectively disabled.
_RAG_API_KEY = os.environ.get("RAG_API_KEY", "")

# Multi-user API keys → dataset ACLs (fleet decision D15, Wave-5 — OPT-IN).
# When RAG_API_KEY_CLIENTS is set, REST requests may also authenticate with a
# registry key; the resolved per-key identity is ACL-enforced on every
# dataset-scoped path (list/read/search/unlock/manage) and denied the admin
# surface.  The deployment keys keep full (admin) access.  DEFAULT (registry
# unset): the single-key check below behaves exactly as before.

# Identity headers (X-Auth-Request-Email / X-Auth-Request-User / X-Email /
# X-User) are client-supplied unless an enforcing auth proxy overwrites them
# on every request.  They are only honoured for unlock-cache scoping and the
# password-failure throttle when RAG_TRUST_PROXY_IDENTITY is set (helm:
# security.trustProxyIdentity) — otherwise the socket peer is used, so a
# caller cannot rotate fake identity headers to hijack another user's unlock
# or bypass the brute-force throttle.
_TRUST_PROXY_IDENTITY = os.environ.get("RAG_TRUST_PROXY_IDENTITY", "").lower() in ("1", "true", "yes")


def _trust_proxy_identity() -> bool:
    """RAG_TRUST_PROXY_IDENTITY, read PER REQUEST (the house convention —
    a config change needs no restart; D24 reads this for the proxy-identity
    credential candidate, so the flag must not be frozen at import)."""
    return os.environ.get("RAG_TRUST_PROXY_IDENTITY", "").strip().lower() in ("1", "true", "yes")


# Periodic embedder liveness monitor.  The embedder is the only required
# model; it is probed once per minute in the background and the result is
# surfaced in /api/admin/health.  Health/readiness probes deliberately do
# NOT gate on it (a remote embedder outage cannot be fixed by restarting
# this pod).
_MODEL_HEALTH_INTERVAL = float(os.environ.get("MODEL_HEALTH_INTERVAL", "60"))
_MODEL_HEALTH_FAIL_THRESHOLD = int(os.environ.get("MODEL_HEALTH_FAIL_THRESHOLD", "3"))

_model_health: dict[str, Any] = {
    "embedder": {
        "status": "unknown",
        "last_check": None,
        "error": None,
        "consecutive_failures": 0,
    }
}

_PUBLIC_PATHS = frozenset(
    {
        "/healthz",
        "/readyz",
        "/favicon.png",
        "/",
        "/manage",
        "/access",
        "/oauth/login",
        "/oauth/oidc/callback",
        "/oauth/logout",
        "/oauth/logged-out",
    }
)

# Routes that must stay reachable without the API key, matched by the
# endpoint function name so future prefix-based routes are NOT silently
# exempted (the old string-prefix check made any `/api/datasets/*/files/*`
# or `/api/staging/*` route public, and `/api/staging/` with a trailing
# slash slipped past the exact-match exclusion).
_PUBLIC_ENDPOINT_NAMES = frozenset(
    {
        "healthz",
        "readyz",
        "api_metrics",  # Prometheus exposition (in-cluster scrapers)
        "favicon",
        "index",
        "manage",
        "access",  # per-user key page (self-authenticating; no key injected)
        "api_serve_file",  # dataset media (password/token protected)
        "api_staging_serve",  # staged media (short-lived ids)
        "api_oidc_session",  # D21 whoami: identity preview only, never a grant
        "api_stats",  # D23 read-only stats: identity-filtered, no secrets —
        # reachable pre-signin so the SPA stats tab works (the identity
        # filtering scopes everything it reports; anonymous callers get the
        # empty-world shape).
    }
)


def _is_public_path(path: str, endpoint: Any = None) -> bool:
    if path in _PUBLIC_PATHS:
        return True
    if endpoint is not None:
        name = getattr(endpoint, "__name__", None)
        if name in _PUBLIC_ENDPOINT_NAMES:
            return True
    return False


# ---------------------------------------------------------------------------
# D23 HTML auth gate — RAG_SSO_GATE
# ---------------------------------------------------------------------------

# A minimal inline sign-in page (a tiny CONSTANT string, deliberately NOT a
# template — templates/ is not touched by D23).  Rendered instead of the
# requested page when RAG_SSO_GATE is on and the request presents no
# credential at all.  No key material is ever embedded; when SSO is enabled
# it links the browser into /oauth/login.
_SSO_GATE_401_PAGE = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
    "<title>Sign in required</title>"
    "<style>body{font-family:system-ui,sans-serif;display:flex;align-items:center;"
    "justify-content:center;height:100vh;margin:0;background:#f6f7f9;color:#1f2937}"
    "main{text-align:center}a{color:#2563eb}</style></head><body><main>"
    "<h1>Sign in required</h1>"
    "<p>This deployment requires an authenticated session to view this page.</p>"
    "{sso_link}"
    '<p style="margin-top:1.5em"><a href="/">Retry</a></p>'
    "</main></body></html>"
)


def _sso_gate_required() -> bool:
    """True when RAG_SSO_GATE gates the HTML pages (read per request —
    flipping the env needs no restart).  Default off = the pages stay
    public, byte-for-byte today's behaviour."""
    return os.environ.get("RAG_SSO_GATE", "").strip().lower() in ("1", "true", "yes")


def _html_gate_response(request: "Request | None") -> "Response | None":
    """The 401 response for a gated HTML page, or None when the caller may
    through (D23, RAG_SSO_GATE).

    The gate fires only when the request presents NO credential of any kind
    (no API key header, no forwarded JWT envelope, no SSO cookie — the
    ``_presented_pairs`` collection is exactly what the auth middleware and
    the whoami read).  A request that carries a credential keeps today's
    page-serving behaviour: the embedded-key / SSO / whoami logic in the
    handlers still governs WHAT the page shows (the gate is a sign-in
    requirement, not a resolution — an invalid credential falls back to the
    normal page, byte-identically, because the handlers already render the
    signed-out view for it).  Health/probe/oauth routes and media serving
    are exempt by construction (they never call this helper).
    """
    if not _sso_gate_required():
        return None
    if request is None:
        # Direct callers (tests, the /manage fallback) have no request
        # context: no credential can be presented — the gate applies.
        return _sso_gate_401_response()
    if _presented_pairs(request):
        return None
    return _sso_gate_401_response()


def _sso_gate_401_response() -> Response:
    """The minimal inline "Sign in required" page (401, HTML).  The link to
    /oauth/login appears only when the SSO flow is configured; otherwise the
    page just tells the caller to authenticate (an API-key deployment's
    browser flow pastes the key into the page — which the gate itself now
    requires a session for, so the honest message is the point)."""
    try:
        from multimodal_rag.utils import oidc_sso

        sso_link = '<p><a href="/oauth/login">Sign in with SSO</a></p>' if oidc_sso.sso_enabled() else ""
    except Exception:
        sso_link = ""
    return HTMLResponse(_SSO_GATE_401_PAGE.replace("{sso_link}", sso_link), status_code=401)


def _rag_acl_path_denial(path: str, method: str, identity) -> "str | None":
    """D15/D16 REST enforcement for a registry-key identity: the denial
    reason for *path*, or None when allowed.

    Rules (fail-closed; matching the MCP tool checks):
      * ``/api/admin/*``      — admin surface, never reachable with a client key.
      * ``POST /api/datasets`` — dataset creation ("manage"): only with the
        ``*`` grant (a named-ACL key cannot mint datasets outside its grant).
      * ``POST /api/datasets/{name}/select`` — ALWAYS allowed for a client
        key (D16: selecting is how access is GAINED; the endpoint itself
        enforces the password proof / denylist — the middleware must not
        pre-empt it, or self-service could never add anything).
      * ``/api/datasets/{name}(/…)`` — dataset must be in the key's ACL ∪
        self-selections (D16: the access store widens, never narrows).
      * everything else (federated /api/search, staging, …) — allowed; the
        federated resolution filters ACL-denied datasets itself.
    """
    if path.startswith("/api/admin"):
        return "Dataset ACLs are configured (D15): this API key has no admin access."
    if path == "/api/datasets" or path == "/api/datasets/":
        if method == "POST" and "*" not in (identity.datasets or frozenset()):
            return "Dataset ACLs are configured (D15): this API key cannot create datasets."
        return None
    name = _clients_registry.dataset_name_from_path(path)
    if name is not None:
        if method == "POST" and path.endswith("/select"):
            return None  # D16: the select endpoint enforces its own proof
        if method == "POST" and path.endswith("/deselect"):
            # 2026-10: unchecking always works — the deselect only removes
            # from the caller's own store (selection delete / exclusion
            # record); it can never widen access, so it is fail-soft here
            # (re-deselecting an already-excluded dataset must not 403).
            return None
        if not _access_store.dataset_allowed(identity, name):
            return _clients_registry.DatasetAccessDenied(
                f"Dataset '{name}' is not permitted for this API key (dataset ACLs are configured — D15)."
            ).args[0]
        # D26: the ro gate — PATH-SHAPED, not method-shaped (cross-validation
        # 2026-10: a blanket POST gate 403'd the READ-shaped POST verbs —
        # /search, /unlock, /lock, /verify-password, /media-token — killing
        # every read for ro-only keys, the exact users D26 exists for).
        # Gated surfaces: document ingest (POST documents/files/batch-files/
        # batch-urls), document deletes (DELETE .../documents/{id}), and the
        # dataset manage surfaces (PATCH/PUT/DELETE on the dataset itself).
        # Deliberate exemptions: /select and /deselect (returned above — the
        # D16 self-service verbs only touch the caller's OWN store:
        # selecting a global records ro; deselecting stays the exclusion
        # escape hatch) and */public (the D23 creator-publish surface —
        # ownership-gated underneath; no DELETE route exists today).
        if method in ("POST", "PUT", "PATCH", "DELETE") and not path.endswith("/public"):
            tail = path.rstrip("/")
            suffix = tail.rsplit("/", 1)[-1]
            if method == "POST":
                is_write = suffix in ("documents", "files", "batch-files", "batch-urls")
            elif method == "DELETE":
                is_write = suffix == name or "/documents/" in tail
            else:  # PATCH/PUT — only the dataset root is a manage surface
                is_write = suffix == name
            if is_write and not _access_store.dataset_write_allowed(identity, name):
                return _clients_registry.DatasetAccessDenied(
                    f"Dataset '{name}' is read-only for this API key (a ro grant cannot be written — D26)."
                ).args[0]
    return None


def _presented_pairs(request: Request) -> "list[tuple[str, str]]":
    """The (key, header-source) candidates one REST request presents.

    Shared by the auth middleware, the ``/api/oidc-session`` whoami and the
    index renderer — one definition so a new envelope (D21 added the
    auth-proxy's forwarded access token; D22 added the SSO cookie; D24 adds
    the enforcing proxy's identity headers) engages everywhere at once.
    """
    presented: list[tuple[str, str]] = []
    for header, via in (
        ("X-RAG-Api-Key", "x-api-key"),
        ("X-API-Key", "x-api-key"),
        ("Authorization", "authorization"),
        ("X-Auth-Request-Access-Token", "forwarded"),
    ):
        val = request.headers.get(header) or ""
        if via == "authorization":
            if val.startswith("Bearer ") and val[len("Bearer ") :].strip():
                presented.append((val[len("Bearer ") :].strip(), via))
        elif via == "forwarded":
            val = val.strip()
            if val[:7].lower() == "bearer ":
                val = val[7:].strip()
            if val:
                presented.append((val, via))
        elif val.strip():
            presented.append((val.strip(), via))
    # D22: the SSO session cookie is the LAST envelope — an explicit key or
    # header always outranks a browser session, and an absent/invalid cookie
    # costs nothing.
    try:
        from multimodal_rag.utils import oidc_sso

        cookie_token = request.cookies.get(oidc_sso.cookie_name())
        if cookie_token:
            presented.append((cookie_token, "cookie"))
    except Exception:
        pass
    # D24 (the Clearwing/DSH pattern): when an ENFORCING auth proxy is
    # confirmed (RAG_TRUST_PROXY_IDENTITY), the proxy-injected identity
    # headers ARE the credential — the gateway authenticated the user at the
    # edge (oauth2-proxy AuthorizationPolicy) and overwrites these headers on
    # EVERY request; the app cannot be reached with attacker-set values.
    # Candidate source "proxy-identity": the identity NAME (not a token) —
    # resolve_presented maps it straight to the registry identity
    # (fail-closed, never admin, lowest precedence).  Absent headers (a
    # direct/non-proxied caller) contribute nothing — fail closed.
    if _trust_proxy_identity():
        proxied_user = (
            request.headers.get("X-Auth-Request-Preferred-Username") or request.headers.get("X-Auth-Request-User") or ""
        ).strip()
        if proxied_user:
            presented.append((proxied_user, "proxy-identity"))
    return presented


# ---------------------------------------------------------------------------
# Request-body cap (audit 2026-10-02 P1-11, remediation + cross-validation
# 2026-10-02).
#
# MAX_UPLOAD_BYTES caps multipart streaming, but every Body(...) endpoint
# (/search, /batch-urls, /api/datasets/{name}/documents, the rrf knobs, …)
# buffered the ENTIRE body in RAM before any check — one authenticated
# request with a multi-GB body could OOM the pod (all tenants down).
#
# Cap: RAG_MAX_BODY_BYTES, default 256 MiB (0 disables).  Sized to cover a
# 190 MB decoded base64 media query — far beyond any legitimate search
# payload (the embedder downscales to 720×720 anyway) — while bounding a
# 4-worker pod's worst-case concurrent buffering to ~1 GiB against 8 Gi
# limits.
#
# Scope: every non-multipart body on a body-accepting method.  Cross-
# validation P0-1 (2026-10-02): FastAPI buffers request.body() for ANY non-
# form content type BEFORE inspecting the header — a `text/plain` or
# content-type-less POST to a Body() endpoint sailed past a JSON-only gate
# uncounted.  Multipart/form-data stays exempt (Starlette disk-spools it and
# MAX_UPLOAD_BYTES caps the stream); every other declared-oversized body is
# refused.  A body with no Content-Length and a non-JSON type is a
# documented residual (declared-check only — the counting receive stays
# JSON-scoped to keep the swap off non-JSON request paths).
#
# ORDERING TRUTH (cross-validation P1-2 — the previous comment had it
# backwards): first-registered = INNERMOST, so the effective chain is
# metrics → auth → body-cap.  Auth therefore runs BEFORE the cap: an
# unauthenticated oversized request gets 401 (and pays the route-scan), and
# a registry-keyed caller pays identity resolution before the 413.  That is
# acceptable and deliberate — D20 identity contextvars must exist for
# endpoints, and auth-on-body-reads is the house pattern.  Do NOT "fix" the
# order based on this comment's predecessor.  Read per request (house
# convention: flip or resize without restart).
# ---------------------------------------------------------------------------


def _max_body_bytes() -> int:
    raw = os.environ.get("RAG_MAX_BODY_BYTES", "").strip()
    if not raw:
        return 256 * 1024 * 1024
    try:
        value = int(raw)
    except ValueError:
        return 256 * 1024 * 1024
    if value < 0:
        # A typo'd negative would otherwise silently DISABLE the cap
        # (max(0, -1) == 0 == "disabled") — treat it as unset instead.
        return 256 * 1024 * 1024
    return value


@app.middleware("http")
async def _json_body_cap(request: Request, call_next):
    limit = _max_body_bytes()
    if limit > 0 and request.method in ("POST", "PUT", "PATCH"):
        content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        # Multipart is disk-spooled by Starlette and capped by the existing
        # MAX_UPLOAD_BYTES streaming guard — never RAM-buffered wholesale.
        is_json = content_type == "application/json"
        if content_type == "multipart/form-data":
            return await call_next(request)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            # Return (not raise): BaseHTTPMiddleware would wrap an exception
            # raised in this inner-middleware frame as a 500 before the app's
            # exception handlers ever see it.  A JSONResponse flows out
            # cleanly through every outer wrapper.
            return JSONResponse(
                {
                    "detail": f"Request body exceeds RAG_MAX_BODY_BYTES ({limit} bytes) — send fewer/smaller documents per request."
                },
                status_code=413,
            )
        # Counting-receive enforcement: JSON only (cross-validation P0-1
        # follow-up — the receive swap stays scoped so non-JSON request
        # paths never carry it; non-JSON bodies WITHOUT a Content-Length
        # header are a documented residual, refused by the endpoint's own
        # 415/422 after buffering).
        if not is_json:
            return await call_next(request)
        # No/lying Content-Length: count what actually arrives and cut the
        # stream at the cap.  BaseHTTPMiddleware bridges the inner app's
        # body reads through request.receive → self._receive, so swapping
        # request._receive intercepts them.  The ORIGINAL receive must be
        # captured BEFORE the swap — calling request._receive() inside the
        # wrapper after the assignment would recurse into itself (which
        # surfaced as FastAPI's "error parsing the body").
        original_receive = request._receive
        received = 0

        async def _counting_receive():
            nonlocal received
            message = await original_receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # Same return-shape constraint: this raise happens inside
                    # call_next's body pump, so the best we can do is refuse
                    # to feed more bytes — the outer handler converts the
                    # aborted parse into a 4xx/500 (never a clean 200).  The
                    # declared-header fast path above is the enforced one for
                    # honest clients.
                    raise HTTPException(413, f"JSON body exceeds RAG_MAX_BODY_BYTES ({limit} bytes).")
            return message

        request._receive = _counting_receive
    return await call_next(request)


@app.middleware("http")
async def _api_key_auth(request: Request, call_next):
    registry_on = _clients_registry.registry_configured()
    # D20 fail-closed default handled below, AFTER the public-path
    # exemptions (media/staged serving authorize themselves; healthz,
    # pages and probes stay public).
    unconfigured = not _RAG_API_KEY and not registry_on
    # Resolve the matched endpoint EXPLICITLY.  An http middleware runs
    # BEFORE routing, so scope["route"] is not set here — relying on it made
    # every endpoint-name exemption silently fail the moment auth was
    # enabled (media serving / staged media / everything exempt 401'd).
    endpoint = None
    route = request.scope.get("route")
    if route is not None:
        endpoint = getattr(route, "endpoint", None)
    else:
        from starlette.routing import Match

        match_scope = {"type": "http", "path": request.url.path, "method": request.method}
        for r in app.routes:
            try:
                match, _ = r.matches(match_scope)
            except Exception:
                continue
            if match == Match.FULL:
                endpoint = getattr(r, "endpoint", None)
                break
    if _is_public_path(request.url.path, endpoint):
        return await call_next(request)
    if unconfigured:
        # Ratified default posture (2026-09-24, D20): an unconfigured
        # deployment is FAIL-CLOSED, not open.  Bind an anonymous
        # registry-client identity whose ONLY grant is the deployment's
        # memory dataset (MEMORY_DATASET env, when set) — backwards
        # compatibility for single-user memory setups — so listings are
        # empty, dataset paths 403, admin 403, and the memory tools keep
        # working against exactly that one dataset.  Public paths (above)
        # stay public: pages, probes, and self-authorizing media/staged
        # serving.
        memory_ds = os.environ.get("MEMORY_DATASET", "").strip()
        anon = _clients_registry.Identity(
            kind="client",
            name="__anonymous__",
            datasets=frozenset({memory_ds} if memory_ds else ()),
        )
        denial = _rag_acl_path_denial(request.url.path, request.method, anon)
        if denial is not None:
            return JSONResponse({"detail": denial}, status_code=403)
        reset_handle = _clients_registry.set_current_identity(anon)
        try:
            return await call_next(request)
        finally:
            _clients_registry.reset_current_identity(reset_handle)
    # D19 delegation precedence: X-RAG-Api-Key (REST's X-API-Key analogue)
    # and X-API-Key both outrank a co-forwarded Authorization Bearer token
    # (a gateway's own platform/admin auth).  Collect (key, header-source)
    # pairs so resolve_presented can honor the caller's DELEGATED identity
    # instead of escalating to admin via the forwarded token.
    # D21: X-Auth-Request-Access-Token (the auth-proxy's forwarded OIDC
    # access token) is collected as source "forwarded" — an envelope only;
    # the JWT inside is fully verified before it resolves anything, and it
    # can never outrank an explicit X-API-Key candidate.
    presented = _presented_pairs(request)
    key = presented[0][0] if presented else ""
    import secrets

    if _RAG_API_KEY and key and secrets.compare_digest(key, _RAG_API_KEY):
        # Deployment key: full access. Bind the (admin) identity so the D15
        # surfaces resolve it consistently.
        reset_handle = None
        if registry_on:
            reset_handle = _clients_registry.set_current_identity(
                _clients_registry.Identity(kind="admin", name=None, datasets=None)
            )
        try:
            return await call_next(request)
        finally:
            if reset_handle is not None:
                _clients_registry.reset_current_identity(reset_handle)
    if registry_on:
        # D21: JWT-shaped candidates resolve inside resolve_presented via the
        # oidc_identity pipeline (same registry identity as the user's key,
        # never admin; opaque keys cost only a shape pre-check).  The
        # deployment-key fast path above can never match a JWT, so OIDC
        # engages entirely through this call — no separate middleware step.
        identity = _clients_registry.resolve_presented([k for k, _ in presented], [via for _, via in presented])
        if identity is not None and identity.is_admin:
            # An MCP-keyset key (MCP_API_KEYS / RAG_API_KEYS) — admin semantics.
            reset_handle = _clients_registry.set_current_identity(identity)
            try:
                return await call_next(request)
            finally:
                _clients_registry.reset_current_identity(reset_handle)
        if identity is not None:
            denial = _rag_acl_path_denial(request.url.path, request.method, identity)
            if denial is not None:
                return JSONResponse({"detail": denial}, status_code=403)
            reset_handle = _clients_registry.set_current_identity(identity)
            try:
                return await call_next(request)
            finally:
                _clients_registry.reset_current_identity(reset_handle)
    return JSONResponse({"detail": "Missing or invalid API key"}, status_code=401)


@app.middleware("http")
async def _metrics_middleware(request: Request, call_next):
    """HTTP request count + latency by route template (roadmap feature 7).

    Registered after the API-key middleware so it is the OUTERMOST wrapper
    (Starlette applies middleware in reverse registration order) — the
    timings include auth overhead, which is the point.

    The route template (e.g. ``/api/datasets/{name}/search``) keeps label
    cardinality bounded; unmatched paths (404s, scanners) are labelled
    ``unmatched``.  Route resolution mirrors the API-key middleware: routing
    has not run when the middleware starts, so the template is resolved up
    front via an explicit match against ``app.routes``.
    """
    from starlette.routing import Match

    from multimodal_rag.utils.metrics import HTTP_LATENCY, HTTP_REQUESTS

    route_template = "unmatched"
    match_scope = {"type": "http", "path": request.url.path, "method": request.method}
    for r in app.routes:
        try:
            match, _ = r.matches(match_scope)
        except Exception:
            continue
        if match == Match.FULL:
            route_template = getattr(r, "path", None) or route_template
            break

    start = time.perf_counter()
    try:
        response = await call_next(request)
        status = str(response.status_code)
    except Exception:
        status = "500"
        HTTP_REQUESTS.labels(route=route_template, method=request.method, status=status).inc()
        HTTP_LATENCY.labels(route=route_template, method=request.method).observe(max(0.0, time.perf_counter() - start))
        raise
    HTTP_REQUESTS.labels(route=route_template, method=request.method, status=status).inc()
    HTTP_LATENCY.labels(route=route_template, method=request.method).observe(max(0.0, time.perf_counter() - start))
    return response


# --- /metrics auth gate (fleet wave-3 C2: the exposition is unauthenticated
# by default — in-cluster scrapers; bound the exposure with a
# NetworkPolicy / AuthorizationPolicy if the pod is otherwise broadly
# reachable).  RAG_METRICS_AUTH=1 requires a key on every scrape, accepted
# from the same headers as the REST + MCP auth (X-RAG-Api-Key / X-API-Key /
# Authorization: Bearer) and validated against RAG_API_KEY plus the MCP key
# set (MCP_API_KEYS / RAG_API_KEYS) — the ServiceMonitor then needs
# metrics.bearerTokenSecret (values: metrics.serviceMonitorBearerSecret).


def _metrics_auth_enabled() -> bool:
    """True when RAG_METRICS_AUTH gates the /metrics exposition (read per
    request — a config change needs no restart).  Default off = the
    exposition stays public, byte-for-byte today's behaviour."""
    return os.environ.get("RAG_METRICS_AUTH", "").strip().lower() in ("1", "true", "yes")


def _metrics_key_ok(request: Request) -> bool:
    """True when the request presents a key that satisfies the metrics gate."""
    import hmac

    presented: list[str] = []
    key = request.headers.get("X-RAG-Api-Key") or request.headers.get("X-API-Key") or ""
    if key.strip():
        presented.append(key.strip())
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and auth[len("Bearer ") :].strip():
        presented.append(auth[len("Bearer ") :].strip())
    if not presented:
        return False
    from multimodal_rag.utils.mcp_auth import configured_keys

    valid = [k for k in (_RAG_API_KEY, *configured_keys(("MCP_API_KEYS", "RAG_API_KEYS"))) if k]
    for candidate in presented:
        for v in valid:
            if hmac.compare_digest(candidate.encode("utf-8"), v.encode("utf-8")):
                return True
    return False


@app.get("/metrics")
async def api_metrics(request: Request):
    """Prometheus exposition (process-local counters/histograms).

    Exempt from ``RAG_API_KEY`` by default, like the health endpoints
    (scrapers are in-cluster; bound the exposure with a NetworkPolicy /
    AuthorizationPolicy if the pod is otherwise broadly reachable).  Set
    ``RAG_METRICS_AUTH=1`` (helm: security.metricsAuth) to require an API
    key on every scrape — see ``_metrics_key_ok`` for the accepted headers.
    """
    if _metrics_auth_enabled() and not _metrics_key_ok(request):
        raise HTTPException(401, "Metrics exposition requires an API key (RAG_METRICS_AUTH is on)")
    from multimodal_rag.utils.metrics import render_metrics

    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)


def _reload_models() -> None:
    """Rebuild the model objects from the (already re-applied) environment.

    Used by the config watcher when the mounted ConfigMap/Secret files
    change.  The new embedder endpoint is verified *before* swapping; if it
    is unreachable the old configuration is kept so a bad edit cannot take
    the deployment down.  On success the RAG cache and embedder fingerprints
    are invalidated so the next request rebuilds with the new models.
    """
    from multimodal_rag.model_config import build_all

    embedder, reranker, vlm, asr = build_all()
    if embedder is None:
        logger.warning("Config reload produced no embedder — keeping previous configuration")
        return
    dm = get_manager()
    try:
        dm._verify_endpoint(embedder, "embedder")
    except Exception as exc:
        logger.error("Config reload aborted — new embedder unreachable: %s", exc)
        return
    with dm._rag_cache_lock:
        dm.embedder, dm.reranker, dm.vlm, dm.asr = embedder, reranker, vlm, asr
        dm._rag_cache.clear()
    dm._embedder_verified.clear()
    dm._embedder_dim_cache.clear()
    # Force the periodic liveness monitor to re-evaluate against the new endpoint.
    _model_health["embedder"].update(
        {
            "status": "unknown",
            "last_check": None,
            "error": None,
            "consecutive_failures": 0,
        }
    )
    logger.info(
        "Swapped embedder=%s reranker=%s vlm=%s asr=%s",
        getattr(embedder, "model_name", "?"),
        getattr(reranker, "model_name", "?") if reranker else "-",
        getattr(vlm, "model_name", "?") if vlm else "-",
        getattr(asr, "model_name", "?") if asr else "-",
    )


async def _eager_init() -> None:
    """Pre-initialise the DatasetManager before accepting traffic.

    ``get_manager()`` performs several synchronous HTTP calls (model
    endpoint verification) that can block for seconds.  By running this
    in a thread pool *and awaiting it* we ensure initialisation completes
    before uvicorn starts accepting connections — so /healthz probes and
    real requests never hit a partially-initialised manager or block on
    ``_dm_lock``.

    If a model endpoint is unreachable, the exception is logged but does
    not prevent startup — the first real request will retry.
    """
    # Hot config: merge mounted ConfigMap/Secret files into env BEFORE the
    # manager builds the models, then start the change watcher.
    if CONFIG_DIR:
        from multimodal_rag.model_config import apply_config_dirs, start_config_watcher

        apply_config_dirs(CONFIG_DIR)
        start_config_watcher(CONFIG_DIR, _reload_models)

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(sync_pool, get_manager)
    except Exception as exc:
        logger.warning("DatasetManager startup init failed (will retry on first request): %s", exc)

    # Periodic prune of completed upload jobs so they don't linger forever
    # when no client polls upload-status (which is the only other prune path).
    async def _prune_upload_jobs() -> None:
        while True:
            await asyncio.sleep(300)  # every 5 minutes
            try:
                _upload_jobs.cleanup_old()
            except Exception:
                logger.debug("Suppressed exception", exc_info=True)

    asyncio.create_task(_prune_upload_jobs())

    # Periodic embedder liveness probe (background, once per minute).
    asyncio.create_task(_model_health_loop())


async def _model_health_loop() -> None:
    """Probe the required embedder endpoint every ``_MODEL_HEALTH_INTERVAL``.

    Updates ``_model_health`` so ``/api/admin/health`` and ``/healthz`` can
    reflect embedder reachability without re-checking synchronously.
    """
    while True:
        await asyncio.sleep(_MODEL_HEALTH_INTERVAL)
        try:
            dm = await get_manager_async()
        except Exception:
            # Not initialised yet (e.g. embedder was down at startup) —
            # /healthz already reports 503 until init succeeds.
            continue
        if _model_health["embedder"]["status"] == "unknown":
            # Manager init already verified the embedder once, so seed the
            # monitor as healthy until the first periodic probe disagrees.
            _model_health["embedder"].update(
                {
                    "status": "healthy",
                    "last_check": datetime.datetime.now(datetime.UTC).isoformat(),
                    "error": None,
                    "consecutive_failures": 0,
                }
            )
        try:
            loop = asyncio.get_running_loop()

            def _probe_embedder() -> str | None:
                try:
                    dm._verify_endpoint(dm.embedder, "embedder")
                    return None
                except Exception as exc:
                    return str(exc)

            error = await loop.run_in_executor(sync_pool, _probe_embedder)
        except Exception as exc:
            logger.debug("Embedder health probe failed unexpectedly: %s", exc)
            continue
        embedder = _model_health["embedder"]
        if error is None:
            embedder.update(
                {
                    "status": "healthy",
                    "last_check": datetime.datetime.now(datetime.UTC).isoformat(),
                    "error": None,
                    "consecutive_failures": 0,
                }
            )
        else:
            embedder["status"] = "unhealthy"
            embedder["last_check"] = datetime.datetime.now(datetime.UTC).isoformat()
            embedder["error"] = error
            embedder["consecutive_failures"] += 1
            logger.warning(
                "Embedder endpoint unreachable (%d/%d checks) — %s",
                embedder["consecutive_failures"],
                _MODEL_HEALTH_FAIL_THRESHOLD,
                error,
            )


# ---------------------------------------------------------------------------
# API routes — REST
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz():
    """Liveness probe — 503 only if the manager hasn't initialised yet.

    Deliberately does NOT gate on model endpoints: the embedder is always a
    remote vLLM/SGLang service, and restarting this pod cannot bring it back.
    Embedder reachability is surfaced in ``/api/admin/health`` instead.
    """
    if _dm is None:
        raise HTTPException(503, "DatasetManager not yet initialised")
    return {"status": "ok"}


@app.get("/api/admin/models")
async def api_model_availability() -> dict[str, Any]:
    """Return which optional models (VLM, ASR) are configured.

    Used by the frontend to enable/disable caption checkboxes and default
    them based on model availability.
    """
    dm = await get_manager_async()
    return {
        "vlm": dm.vlm is not None,
        "asr": dm.asr is not None,
        "reranker": dm.reranker is not None,
    }


@app.get("/api/admin/connections")
async def api_connections() -> dict[str, Any]:
    """Live-check all configured model endpoints via ``/v1/models``.

    Returns one entry per role with a three-state status: ``healthy``
    (reachable), ``not_provided`` (not configured), or ``unhealthy``
    (configured but unreachable).  Used by the management page
    "Test connections" button.
    """
    dm = await get_manager_async()
    loop = asyncio.get_running_loop()
    roles = await loop.run_in_executor(sync_pool, dm.check_model_connections)
    return {"roles": roles}


@app.get("/readyz")
async def readyz():
    """Readiness probe — manager up and Qdrant reachable.

    Deliberately does NOT gate on the embedder: it is always a remote
    vLLM/SGLang service, and restarting this pod cannot bring it back.
    Embedder reachability is surfaced in ``/api/admin/health`` instead.
    Uses a single lightweight GET to Qdrant's own ``/readyz`` endpoint
    instead of calling ``list_datasets()`` (which iterates every collection
    and can be slow under load).
    """
    if _dm is None:
        raise HTTPException(503, "DatasetManager not yet initialised")
    try:
        url = f"http://{_dm.qdrant_host}:{_dm.qdrant_port}/readyz"
        async with httpx2.AsyncClient(timeout=2.0) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            raise HTTPException(503, f"Qdrant not ready (HTTP {resp.status_code})")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, f"Qdrant unreachable: {e}")
    return {"status": "ready"}


# -- Datasets -----------------------------------------------------------------


@app.post("/api/datasets")
async def api_create_dataset(body: dict[str, Any] = Body(...)):
    """Create a new named dataset.

    Request body::

        {"name": "my-dataset", "description": "...", "caption_with_asr": false, "caption_with_vlm": false, "keep_originals": true, "ocr": false, "password": "secret"}

    ``caption_with_asr`` (defaults to the server config,
    ``RAG_CAPTION_WITH_ASR``) controls whether audio tracks from uploaded
    videos are transcribed during ingestion.
    ``caption_with_vlm`` (defaults to ``RAG_CAPTION_WITH_VLM``) controls
    whether images/videos are described by the VLM during ingestion
    (enriching text for the embedder and enabling VLM-skip at retrieval
    for generic queries).  When the corresponding model is unavailable,
    the flag is auto-disabled with a warning.
    ``keep_originals`` (default ``true``) controls whether original
    full-quality files are kept on disk after preprocessing.
    ``password`` is optional — if set, all read operations on the dataset
    will require it.
    ``rrf`` is optional per-dataset weighted-RRF defaults
    (``{"dense_weight": …, "sparse_weight": …, "k": …}``, any subset) —
    applied to single-dataset searches that carry no explicit override.
    Omitted keys / an omitted ``rrf`` fall back to the deployment default
    (``RAG_RRF_DEFAULT``, chart values ``rag.rrfDefault`` — disabled by
    default) and beyond that the global 1.0/1.0.  Existing datasets are
    never touched by the env; it only stamps NEW datasets at create time.
    ``contextual`` enables ingest-time contextual retrieval for this dataset
    (one small LLM call per real-text chunk at ingest — see
    ``POST /api/admin/datasets/{name}/contextual-preview`` for the cost
    estimate).  Omitted → the deployment default (``RAG_CONTEXTUAL_DEFAULT``,
    chart values ``rag.contextual`` — disabled by default); an explicit value
    in the body always wins.  Affects new ingests only; Recreate
    re-contextualizes existing files.

    Whitespace runs in ``name`` are auto-converted to ``_`` before creation
    (``"my dataset"`` → ``"my_dataset"``) — the common hand-typo, and the
    one invalid character a user can type without noticing.  Any other
    name the validator rejects comes back as HTTP 400, not 500.

    D23 dataset ownership: the dataset's meta.json is stamped
    ``created_by`` with the CURRENT request identity — a registry / JWT /
    SSO identity stamps its name, an admin identity stamps ``admin``
    (admins act as the deployment), and the D20 anonymous identity stamps
    ``anonymous``.  The stamp is what DELETE (owner-or-admin) and the
    creator's public toggle read back; datasets created before D23 carry
    no stamp and stay admin-managed (never backfilled).
    """
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(400, "Field 'name' is required")
    # Dataset names allow [A-Za-z0-9._-]; spaces are the invalid character
    # users hit by accident, so convert whitespace runs to '_' instead of
    # failing the request outright.
    name = re.sub(r"\s+", "_", name)
    description = body.get("description", "")
    dm = await get_manager_async()
    # Defaults follow the server-wide config (env RAG_CAPTION_WITH_ASR /
    # RAG_CAPTION_WITH_VLM, set from the Helm chart); per-request values
    # in the body override them.
    caption_with_asr = body.get("caption_with_asr", dm.caption_with_asr)
    caption_with_vlm = body.get("caption_with_vlm", dm.caption_with_vlm)
    keep_originals = body.get("keep_originals", True)
    password = body.get("password") or None
    # OCR fallback opt-in: omitted -> server-wide default (RAG_OCR_DEFAULT,
    # chart values rag.ocr); explicit false in the body always wins.
    body_ocr = body.get("ocr")
    ocr = bool(body_ocr) if body_ocr is not None else RAG_OCR_DEFAULT
    # Contextual retrieval opt-in: omitted -> server-wide default
    # (RAG_CONTEXTUAL_DEFAULT, chart values rag.contextual); an explicit
    # value in the body always wins.  Cost is real (one LLM call per
    # real-text chunk) — the UI confirm line next to the checkbox and the
    # contextual-preview endpoint exist to make that visible first.
    body_contextual = body.get("contextual")
    contextual = bool(body_contextual) if body_contextual is not None else RAG_CONTEXTUAL_DEFAULT
    # Weighted-RRF defaults (feature: weighted RRF, dataset-defaults slice):
    # an explicit body ``rrf`` object wins; omitted → the deployment default
    # (RAG_RRF_DEFAULT, chart values rag.rrfDefault) when that is enabled;
    # both absent → no per-dataset default (global 1.0/1.0).  Validation and
    # clamping live in _sanitize_rrf_meta — a bad value is a 400, not a
    # silent store.
    body_rrf = body.get("rrf")
    if body_rrf is not None and not isinstance(body_rrf, dict):
        raise HTTPException(400, "Field 'rrf' must be an object")
    if body_rrf is not None:
        try:
            rrf_meta = dm._sanitize_rrf_meta(body_rrf)
        except ValueError as e:
            raise HTTPException(400, str(e))
    else:
        rrf_meta = _rrf_default_meta_from_env()
    try:
        loop = asyncio.get_running_loop()
        meta = await loop.run_in_executor(
            sync_pool,
            partial(
                dm.create_dataset,
                name,
                description,
                bool(caption_with_asr),
                bool(caption_with_vlm),
                bool(keep_originals),
                password,
                ocr,
                rrf=rrf_meta,
                contextual=bool(contextual),
                created_by=_creator_name_for_request(),
            ),
        )
        return {"status": "ok", "dataset": meta}
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        # _validate_name rejections (invalid characters etc.) — a clear 400
        # carrying the validator message, not an unhandled ValueError → 500.
        raise HTTPException(400, str(e))


@app.post("/api/datasets/{name}/verify-password")
async def api_verify_dataset_password(name: str, request: Request, body: dict[str, Any] = Body(...)):
    """Verify a dataset password.

    Request body::

        {"password": "secret"}

    Returns 200 on success, 401/403 on failure.
    """
    dm = await get_manager_async()
    try:
        dm.get_dataset(name, sync_count=False)  # ensure dataset exists
        # Pass *request* so the failure throttle is scoped to the client,
        # not collapsed into the shared "unknown" bucket.
        await _require_dataset_password(dm, name, body.get("password", ""), request)
        return {"status": "ok", "verified": True}
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")
    except HTTPException:
        raise


@app.post("/api/datasets/{name}/unlock")
async def api_unlock_dataset(name: str, request: Request, body: dict[str, Any] = Body(...)):
    """Unlock a password-protected dataset (default 30 minutes).

    Request body::

        {"password": "secret", "ttl": 1800}   # ttl in seconds (optional)

    Once unlocked, subsequent API calls to this dataset from the same
    client identity (D10/D15) can omit the ``X-Dataset-Password`` header
    for the TTL duration.  TTL bounds: 60..RAG_UNLOCK_MAX_TTL seconds
    (default max 86400 = 24 h).  ``ttl=0`` = NO expiry — the unlock lasts
    until ``POST /lock`` — and requires the deployment opt-in
    ``RAG_UNLOCK_MAX_TTL=0`` (the /access page exposes it when enabled).
    """
    dm = await get_manager_async()
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(sync_pool, dm.get_dataset, name, False)
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")

    if not await loop.run_in_executor(sync_pool, dm.has_password, name):
        return {"status": "ok", "message": f"Dataset '{name}' is not password protected — nothing to unlock."}

    password = body.get("password", "")
    if not password:
        raise HTTPException(400, "Field 'password' is required")

    cid = _unlock_client_id(request)
    _check_pw_throttle(cid)
    # PBKDF2-600k: ~0.2–0.5s of pure CPU — must not run on the event loop.
    if not await loop.run_in_executor(sync_pool, dm.verify_password, name, password):
        _pw_record_failure(cid)
        raise HTTPException(403, f"Incorrect password for dataset '{name}'")
    _pw_reset_failures(cid)

    ttl = body.get("ttl", _UNLOCK_TTL)
    if not isinstance(ttl, int) or isinstance(ttl, bool):
        raise HTTPException(400, "TTL must be an integer number of seconds")
    ttl_max = _unlock_ttl_max()
    if ttl == 0:
        # "No expiry" is an explicit deployment opt-in (RAG_UNLOCK_MAX_TTL=0):
        # the unlock persists until POST /lock.  Unconfigured deployments
        # keep the bounded-by-default posture.
        if ttl_max != 0:
            raise HTTPException(
                400,
                "ttl=0 (no expiry) is disabled on this deployment (set RAG_UNLOCK_MAX_TTL=0 to enable)",
            )
    elif ttl < 60 or (ttl_max and ttl > ttl_max):
        raise HTTPException(400, f"TTL must be between 60 and {ttl_max} seconds (or 0 for no expiry when enabled)")

    # Use the shared cache writer so unlocked state is visible across API
    # replicas (Redis when configured, in-process otherwise).  ttl=0 (the
    # RAG_UNLOCK_MAX_TTL=0 opt-in) persists until POST /lock.
    _unlock_cache_set_ttl(name, cid, password, ttl)

    if ttl == 0:
        return {
            "status": "ok",
            "message": f"Dataset '{name}' unlocked with no expiry (until locked).",
            "ttl_seconds": 0,
        }
    return {
        "status": "ok",
        "message": f"Dataset '{name}' unlocked for {ttl // 60} minutes.",
        "ttl_seconds": ttl,
    }


@app.post("/api/datasets/{name}/lock")
async def api_lock_dataset(name: str, request: Request):
    """Immediately revoke the unlock for a dataset (if any)."""
    dm = await get_manager_async()
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(sync_pool, dm.get_dataset, name, False)
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")

    cid = _unlock_client_id(request)
    was = _unlock_cache_del(name, cid)

    if was:
        return {"status": "ok", "message": f"Dataset '{name}' locked."}
    return {"status": "ok", "message": f"Dataset '{name}' was not unlocked."}


# ---------------------------------------------------------------------------
# D16 self-service dataset selection (/access page + MCP select_dataset)
# ---------------------------------------------------------------------------


def _require_select_identity() -> Any:
    """The registry-key identity for selection endpoints, or a 4xx.

    Admin keys and store-off deployments have nothing to select INTO; an
    unauthenticated store-off deployment never reaches here via client keys
    (the middleware resolved the identity already — we re-read the context).
    """
    if not _access_store.store_enabled():
        raise HTTPException(409, "Dataset selection is not enabled on this deployment (RAG_ACCESS_STORE).")
    identity = _clients_registry.current_identity()
    if identity is None or identity.is_admin:
        raise HTTPException(409, "Dataset selection applies to per-user API keys (registry keys).")
    return identity


@app.get("/api/access/selections")
async def api_access_selections(request: Request):
    """The caller's selection state (D16) — per-identity, password-free.

    Returns ``{"enabled", "selections": {name: {selected_at, source,
    has_password}}, "memory_dataset"}`` for the presented key.  Selection
    passwords are NEVER returned — only whether one is saved.
    """
    identity = _clients_registry.current_identity()
    enabled = _access_store.store_enabled()
    sels: dict[str, dict[str, Any]] = {}
    memory = None
    if enabled and identity is not None and not identity.is_admin:
        st = _access_store.stats(identity)
        memory = st.get("memory_dataset")
        for name in sorted(_access_store.selections_for(identity)):
            entry = _access_store.selection_entry(identity, name) or {}
            sels[name] = {
                "selected_at": entry.get("selected_at"),
                "source": entry.get("source"),
                "has_password": bool(entry.get("password")),
            }
    return {"enabled": enabled, "selections": sels, "memory_dataset": memory}


@app.post("/api/datasets/{name}/select")
async def api_select_dataset(name: str, request: Request, body: dict[str, Any] = Body(default=None)):
    """Select a dataset for YOUR key (D16 self-service; the checkbox model).

    Request body (optional)::

        {"password": "secret"}   # required when the dataset is protected

    A public dataset needs no body at all.  A protected dataset is selected
    only with its correct password — which is then SAVED per identity, so
    every REST/MCP call for this key works without sending it again.
    Fail-closed elsewhere: denylisted datasets refuse regardless of proof;
    operator-ACL'd datasets are accepted as a no-op (the ACL already grants).
    """
    dm = await get_manager_async()
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(sync_pool, dm.get_dataset, name, False)
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")
    identity = _require_select_identity()
    cid = _unlock_client_id(request)
    password = (body or {}).get("password") if isinstance(body, dict) else None
    _check_pw_throttle(cid)
    try:
        entry = await loop.run_in_executor(
            sync_pool,
            lambda: _access_store.verify_and_select(identity, name, password, dm.has_password, dm.verify_password),
        )
    except _access_store.SelectionDenied as exc:
        # Audit 2026-10-02 P0-1 follow-up (cross-validation #1): the proof
        # gate refuses with SelectionDenied (a PermissionError, NOT a
        # ValueError) — unhandled it surfaced as a 500.  Map it to 403 with
        # the caller-safe message; no throttle charge (no password proof was
        # attempted).
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        _pw_record_failure(cid)  # wrong/missing password → throttle bucket
        raise HTTPException(403, str(exc))
    _pw_reset_failures(cid)
    if entry.get("password"):
        # Selection IS the unlock for this identity — warm the cache too.
        _unlock_cache_set(name, cid, entry["password"])
    if entry.get("source") == "acl":
        return {
            "status": "ok",
            "message": f"Dataset '{name}' is already granted to your key by the operator ACL — nothing to add.",
            "source": "acl",
        }
    if entry.get("source") == "reincluded":
        return {
            "status": "ok",
            "message": f"Dataset '{name}' re-included in your datasets.",
            "source": "reincluded",
        }
    if entry.get("source") == "acl+pw":
        # ACL-granted, but the caller proved the password anyway: saved as a
        # password-only sidecar (the memory-binding case — the ★ binding can
        # now resolve the password for the memory tools).
        return {
            "status": "ok",
            "message": f"Password saved for '{name}' (already ACL-granted) — memory tools and reads need no password now.",
            "source": "acl+pw",
        }
    protected = " (password saved — no password header needed)" if entry.get("password") else ""
    return {"status": "ok", "message": f"Dataset '{name}' selected for your key{protected}.", "source": "self"}


@app.post("/api/datasets/{name}/deselect")
async def api_deselect_dataset(name: str, request: Request):
    """Remove a dataset from YOUR key's self-selected set (D16).

    Only the caller's OWN selections are removed (an operator-ACL grant is
    untouched); the saved password is dropped with the selection.
    """
    identity = _require_select_identity()
    removed = _access_store.deselect_dataset(identity, name)
    if removed:
        return {
            "status": "ok",
            "message": f"Dataset '{name}' removed from your datasets (saved password dropped; re-select any time).",
        }
    return {"status": "ok", "message": f"No self-selection for '{name}' on your key — nothing removed."}


@app.post("/api/access/memory-dataset")
async def api_set_memory_dataset(request: Request, body: dict[str, Any] = Body(default=None)):
    """Bind YOUR memory dataset (the /access page's ★, server-side).

    Request body (optional)::

        {"dataset": "andrew-memory"}   # omit the field to clear the binding

    The bound dataset must be accessible to the key (operator ACL or a prior
    ``/select``).  Once bound, the MCP memory tools resolve dataset and
    password from this binding — opencode needs no memory env vars.
    """
    identity = _require_select_identity()
    name = (body or {}).get("dataset") if isinstance(body, dict) else None
    try:
        bound = _access_store.set_memory_dataset(identity, name)
    except _access_store.SelectionDenied as exc:
        raise HTTPException(409, str(exc))
    if bound:
        return {"status": "ok", "message": f"Memory dataset bound: '{bound}'.", "memory_dataset": bound}
    return {"status": "ok", "message": "Memory dataset binding cleared.", "memory_dataset": None}


# ---------------------------------------------------------------------------
# D17 admin key registry (mint / grant / revoke from the /access page)
# ---------------------------------------------------------------------------


def _require_admin_identity() -> Any:
    """The ADMIN identity for the registry endpoints, or 403.

    Registry (per-user) keys are NEVER admitted — key minting is exactly the
    power a registry key must not have.  With the middleware's admin surface
    check already denying ``/api/admin/*`` to clients, these endpoints are
    admin-key-only by construction; the explicit check here is defense in
    depth (and gives the page a clean 403 to branch on).
    """
    identity = _clients_registry.current_identity()
    if identity is None or not identity.is_admin:
        raise HTTPException(403, "Client key management requires an ADMIN API key.")
    return identity


def _require_admin_file() -> None:
    """409 when the D17 overlay is disabled (nothing to write into)."""
    if not _admin_registry.admin_file_enabled():
        raise HTTPException(
            409,
            "The admin key registry is not enabled (set RAG_ACCESS_STORE=1, the D16 knob).",
        )


# ---------------------------------------------------------------------------
# D25 SSO self-mint — the identity mints its OWN long-lived API key
# ---------------------------------------------------------------------------


def _sso_proof_for_identity(request: Request, identity: Any) -> "str | None":
    """``"jwt"`` | ``"proxy-identity"`` when THIS request carries SSO proof
    for the BOUND identity, else ``None`` (D25).

    The proof is per-identity, not per-channel: the request must present a
    credential whose verification — outside the registry — proves the realm
    (or the enforcing proxy) vouches for exactly the identity that got
    bound.  That keeps every D17 invariant while surviving credential
    precedence:

    * a JWT candidate (any envelope: ``Authorization: Bearer``, the
      auth-proxy's forwarded token, the D22 SSO cookie) that RS256-verifies
      and resolves to the bound identity's NAME — the realm signature is
      the proof;
    * a ``proxy-identity`` candidate equal to the bound name (only ever
      collected when ``RAG_TRUST_PROXY_IDENTITY`` confirmed an enforcing
      proxy) — the edge authentication is the proof.

    An OPAQUE API key is never proof: a key-authenticated request with no
    co-present SSO credential gets ``None`` → the endpoint 403s (minting
    stays exactly the power a key must never have).  When an SSO session
    AND a pasted key co-exist (the SPA's paste mode), a JWT resolving to
    the SAME identity satisfies the proof — two independent attestations
    of one human; the mint still acts only on that identity's own entry.
    """
    pairs = _presented_pairs(request)
    if _trust_proxy_identity():
        for candidate, via in pairs:
            if via == "proxy-identity" and candidate == str(identity.name):
                return "proxy-identity"
    try:
        from multimodal_rag.utils import oidc_identity

        if oidc_identity.oidc_enabled():
            for candidate, _via in pairs:
                if not oidc_identity.is_jwt_format(candidate):
                    continue
                jwt_ident = oidc_identity.resolve_jwt(candidate)
                if jwt_ident is not None and str(getattr(jwt_ident, "name", "")) == str(identity.name):
                    return "jwt"
    except Exception:
        return None  # a broken verification sidecar can never mint (fail-closed)
    return None


def _require_self_mint(request: Request) -> Any:
    """The full D25 gate chain — ``(identity, source)`` or a 4xx.

    Order: the knob (409 — the deployment does not offer it) → an
    authenticated non-admin per-user identity (403 — anonymous fallbacks
    and admins are not self-mint subjects; admins hold the D17 panel) →
    SSO proof for that identity (403 — key-only callers may never mint).
    """
    if not _admin_registry.self_mint_enabled():
        raise HTTPException(
            409,
            "SSO self-mint is not enabled on this deployment (set RAG_ACCESS_SELF_MINT=1).",
        )
    identity = _clients_registry.current_identity()
    if identity is None or identity.is_admin or not identity.name or identity.name == "__anonymous__":
        raise HTTPException(403, "SSO self-mint applies to SSO-authenticated per-user identities only.")
    source = _sso_proof_for_identity(request, identity)
    if source is None:
        raise HTTPException(
            403,
            "Key minting is not a power an API key carries (D15/D17) — sign in with SSO "
            "(a verified JWT, the SSO session, or trusted proxy identity) to mint your own key.",
        )
    return identity, source


@app.get("/api/access/key")
async def api_access_own_key(request: Request):
    """The caller's OWN key view (D25) — masked, owner-safe.

    SSO-sourced identities only (verified JWT / SSO cookie / trusted proxy
    identity).  Key material NEVER appears here — even the owner sees the
    masked form (the full key exists exactly once, in the mint response).
    409 when self-mint is off (the SPA hides the card), 403 for
    key-authenticated callers.
    """
    identity, source = _require_self_mint(request)
    loop = asyncio.get_running_loop()
    try:
        view = await loop.run_in_executor(sync_pool, _admin_registry.own_key_view, identity.name)
    except _access_store.SelectionDenied as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    view["source"] = source
    view["enabled"] = True
    view["identity"] = view["name"]  # the SPA-contract alias
    return view


@app.post("/api/access/mint-key")
async def api_access_self_mint(request: Request, body: dict[str, Any] = Body(default=None)):
    """Mint (or deliberately rotate) YOUR OWN long-lived API key (D25).

    Request body (optional)::

        {"rotate": true}   # required to replace an existing key

    The generated key is returned ONCE in ``key`` — this response is the
    only time the caller sees the material.  Rotation kills the previous
    key immediately (everything re-reads per request).  The mint NEVER
    touches grants: the new key carries exactly the identity's current
    access (operator ACLs ∪ self-selections resolve on the next request).
    """
    identity, source = _require_self_mint(request)
    rotate = bool((body or {}).get("rotate")) if isinstance(body, dict) else False
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            sync_pool, lambda: _admin_registry.self_mint_key(identity.name, rotate=rotate)
        )
    except _access_store.SelectionDenied as exc:
        raise HTTPException(409, str(exc))
    except FileExistsError as exc:
        raise HTTPException(409, str(exc))
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except KeyError as exc:
        raise HTTPException(409, str(exc.args[0] if exc.args else exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    logger.info(
        "SSO self-mint: identity '%s' minted/rotated its own API key (source=%s, rotated=%s)",
        result["name"],
        source,
        result["rotated"],
    )
    return {
        "status": "ok",
        "name": result["name"],
        "key": result["key"],  # the ONLY full-key response in the D25 surface
        "rotated": result["rotated"],
        "created": result["created"],
        "message": (
            f"Key rotated for '{result['name']}' — the previous key no longer authenticates."
            if result["rotated"]
            else f"API key minted for '{result['name']}' — shown only once, copy it now."
        ),
    }


# ---------------------------------------------------------------------------
# D27 — MCP Inspector: the SPA's Inspector tab (direct MCP tool calls)
# ---------------------------------------------------------------------------
# The homepage SPA's fifth tab lets a signed-in user browse the MCP server's
# tool catalog and INVOKE a tool directly, authenticated as the session's own
# credential.  The browser never talks to the MCP endpoint itself — the MCP
# sidecar (same pod, port 9090) is not on the ingress path and the D23 hard
# rule stands (page scripts never see an SSO-derived key) — so the calls are
# PROXIED through this API server:
#
#   GET  /api/inspector/tools   → MCP ``tools/list``   (catalog for the picker)
#   POST /api/inspector/call    → MCP ``tools/call``   (one tool invocation)
#
# The proxy forwards EXACTLY the credential envelope the request itself
# authenticated with (the same `_presented_pairs` the auth middleware
# resolved), re-expressed in the MCP middleware's accepted headers — nothing
# more, never a wider credential:
#
#   * an X-RAG-Api-Key / X-API-Key session key → ``X-API-Key`` (the same
#     registry identity resolves on the MCP surface — D19 source "x-api-key");
#   * an Authorization Bearer session key → ``Authorization: Bearer``;
#   * the auth-proxy's forwarded OIDC token → ``X-Auth-Request-Access-Token``;
#   * the D22 SSO session cookie (the realm's access-token JWT) →
#     ``Authorization: Bearer`` — the MCP middleware's D21 fall-through
#     resolves JWTs, and a JWT can only ever yield a client identity, so the
#     re-enveloped cookie can never escalate;
#   * a D24 trusted-proxy identity session has NO credential to forward (the
#     identity headers are names, not secrets) — the MCP sidecar then answers
#     on its own terms (open when unkeyed, 401 when keyed) and the tab
#     surfaces that verbatim.  Fail-closed, never invented around.
#
# The caller's identity contextvar is deliberately NOT re-bound on the proxy
# call: the MCP sidecar resolves identity from the forwarded credential
# itself, so ACLs / selections / ★ memory binding are the sidecar's own
# decisions against the SAME registry store — the proxy exercises the real
# MCP path (auth middleware, per-caller unlock scoping, audit hooks), not a
# shortcut around it.  Target: ``RAG_INSPECTOR_MCP_URL`` (default
# ``http://127.0.0.1:9090/mcp`` — the chart's sidecar topology), timeout
# ``RAG_INSPECTOR_TIMEOUT_S`` (default 300; tool calls embed and search).

_INSPECTOR_DEFAULT_MCP_URL = "http://127.0.0.1:9090/mcp"

# Session-credential source → the short label the Inspector chip renders.
_INSPECTOR_VIA_LABELS = {
    "x-api-key": "api-key",
    "authorization": "bearer-token",
    "forwarded": "forwarded-jwt",
    "cookie": "sso-jwt",
}

_INSPECTOR_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


def _inspector_mcp_url() -> str:
    """The MCP endpoint the Inspector proxies to (read per request — a
    config change needs no restart).  Default: the in-pod MCP sidecar."""
    raw = os.environ.get("RAG_INSPECTOR_MCP_URL", "").strip()
    if raw:
        return raw
    return _INSPECTOR_DEFAULT_MCP_URL


def _inspector_timeout_s() -> float:
    """Per-call timeout for the proxied MCP request (default 300 s)."""
    raw = os.environ.get("RAG_INSPECTOR_TIMEOUT_S", "").strip()
    try:
        value = float(raw) if raw else 300.0
    except ValueError:
        return 300.0
    return value if value > 0 else 300.0


def _inspector_forward_headers(request: Request) -> "tuple[list[tuple[str, str]], str]":
    """The MCP-accepted credential headers for THIS request's session, plus
    the session-credential label for the UI chip.

    Re-derives the presented (key, source) pairs from the request headers
    (the identical `_presented_pairs` the auth middleware used) and maps
    each to the MCP middleware's accepted envelope, preserving the pair
    order so the sidecar's D19 delegation precedence sees the same
    candidates in the same order.  Duplicate envelopes (the same value
    twice via X-RAG-Api-Key and X-API-Key, say) are collapsed.  Returns the
    label of the FIRST forwardable source ("none" when the session has no
    forwardable credential at all — the D24 proxy-identity and D20
    anonymous cases).
    """
    headers: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    session_via = "none"
    for key, via in _presented_pairs(request):
        if via == "proxy-identity":
            # A trusted-proxy identity is a NAME, not a secret: nothing to
            # forward.  The sidecar decides (open / 401) — never invent a
            # credential around it.
            continue
        if session_via == "none":
            session_via = _INSPECTOR_VIA_LABELS.get(via, "none")
        if via == "forwarded":
            name, value = "X-Auth-Request-Access-Token", key
        elif via in ("authorization", "cookie"):
            # The cookie token IS the realm access-token JWT (D22) — the
            # MCP surface accepts JWTs from a Bearer envelope (D21), and a
            # JWT can only ever resolve to a client identity (never admin).
            name, value = "Authorization", f"Bearer {key}"
        else:
            # "x-api-key" — covers both X-RAG-Api-Key and X-API-Key sources;
            # the MCP middleware's X-API-Key carries the identical D19
            # delegation semantics.
            name, value = "X-API-Key", key
        dedupe = (name.lower(), value)
        if dedupe in seen:
            continue
        seen.add(dedupe)
        headers.append((name, value))
    # Per-caller unlock-cache scoping rides the client IP (D10) — honored by
    # the sidecar only under its own RAG_TRUST_PROXY_IDENTITY, so forwarding
    # the honest peer widens nothing.
    if request.client and request.client.host:
        headers.append(("X-Forwarded-For", request.client.host))
    return headers, session_via


async def _inspector_rpc(request: Request, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """One JSON-RPC request to the MCP endpoint, forwarding the session
    credential.  Raises HTTPException(502/504) with a caller-safe detail on
    every transport-level failure — the MCP JSON-RPC error objects flow back
    to the route handlers (they are TOOL facts, not proxy failures)."""
    url = _inspector_mcp_url()
    fwd, _ = _inspector_forward_headers(request)
    headers: list[tuple[str, str]] = [
        ("Content-Type", "application/json"),
        # Both types: a json_response=True sidecar needs only JSON, but an
        # SSE-mode target refuses an Accept without text/event-stream.
        ("Accept", "application/json, text/event-stream"),
        *fwd,
    ]
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    timeout = _inspector_timeout_s()
    try:
        async with httpx2.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, content=json.dumps(payload), headers=headers)
    except httpx2.TimeoutException:
        raise HTTPException(504, f"MCP endpoint timed out after {timeout:g}s ({url}).")
    except Exception as exc:
        raise HTTPException(502, f"MCP endpoint unreachable at {url}: {type(exc).__name__}")
    if resp.status_code == 401:
        raise HTTPException(
            502,
            "The MCP endpoint rejected this session's credential (401 unauthorized) — "
            "this session has no credential the MCP surface accepts; paste an API key "
            "in the Access tab.",
        )
    if resp.status_code != 200:
        raise HTTPException(502, f"MCP endpoint returned HTTP {resp.status_code} ({url}).")
    try:
        body = resp.json()
    except ValueError:
        raise HTTPException(
            502, f"MCP endpoint returned a non-JSON response ({url}) — is it the streamable-http endpoint?"
        )
    if not isinstance(body, dict):
        raise HTTPException(502, f"MCP endpoint returned an unexpected response shape ({url}).")
    return body


@app.get("/api/inspector/tools")
async def api_inspector_tools(request: Request) -> dict[str, Any]:
    """The Inspector tab's tool catalog: MCP ``tools/list`` through the
    session credential (D27).

    Shape: ``{"endpoint", "session_credential", "tools": [{name, description,
    inputSchema}, ...]}`` — the same public-schema metadata any MCP client
    sees.  ``session_credential`` names the envelope the proxy will forward
    (``"api-key"`` | ``"bearer-token"`` | ``"forwarded-jwt"`` | ``"sso-jwt"`` |
    ``"none"``) so the tab can show what a call will authenticate as — never
    the credential itself.
    """
    body = await _inspector_rpc(request, "tools/list", {})
    error = body.get("error")
    if error:
        raise HTTPException(502, f"MCP tools/list failed: {json.dumps(error)[:300]}")
    result = body.get("result")
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        tools = []
    _, session_via = _inspector_forward_headers(request)
    return {
        "endpoint": _inspector_mcp_url(),
        "session_credential": session_via,
        "tools": [t for t in tools if isinstance(t, dict)],
    }


@app.post("/api/inspector/call")
async def api_inspector_call(request: Request, body: dict[str, Any] = Body(...)):
    """Invoke ONE MCP tool as THIS session (D27).

    Request body::

        {"name": "search_dataset", "arguments": {"dataset_name": "notes", "query": "…"}}

    Returns ``{"ok", "tool", "duration_ms", "result", "error"}``:
    ``result`` is the MCP CallToolResult (``content`` blocks,
    ``structuredContent``, ``isError``) and ``error`` a JSON-RPC error
    object.  A tool-level failure (``isError: true`` or a JSON-RPC error)
    is a 200 with ``ok: false`` — the round trip itself succeeded, and the
    tab renders the tool's own error text.  Transport-level failures (the
    sidecar unreachable, a rejected credential) are 502/504 HTTPException
    from :func:`_inspector_rpc`.
    """
    name = body.get("name")
    if not isinstance(name, str) or not _INSPECTOR_TOOL_NAME_RE.match(name):
        raise HTTPException(400, "Field 'name' is required (a valid MCP tool name)")
    arguments = body.get("arguments")
    if arguments is None or arguments == {}:
        arguments = {}
    elif not isinstance(arguments, dict):
        raise HTTPException(400, "Field 'arguments' must be an object")
    started = time.perf_counter()
    rpc = await _inspector_rpc(request, "tools/call", {"name": name, "arguments": arguments})
    duration_ms = int((time.perf_counter() - started) * 1000)
    rpc_error = rpc.get("error")
    if rpc_error:
        return {"ok": False, "tool": name, "duration_ms": duration_ms, "result": None, "error": rpc_error}
    result = rpc.get("result")
    is_error = bool(isinstance(result, dict) and result.get("isError"))
    return {"ok": not is_error, "tool": name, "duration_ms": duration_ms, "result": result, "error": None}


# ---------------------------------------------------------------------------
# D23 dataset ownership — creator stamp + owner-or-admin enforcement
# ---------------------------------------------------------------------------


def _creator_name_for_request() -> str:
    """The identity name to stamp as ``created_by`` at dataset CREATE (D23).

    The CURRENT request identity (the middleware bound it): a registry /
    JWT / SSO identity stamps its name, an ADMIN identity stamps ``admin``
    (admins act as the deployment — every dataset made with a deployment
    key is the deployment's), and a bound-but-nameless/anonymous identity
    (the D20 unconfigured fallback) stamps ``anonymous``.  No identity at
    all also reads ``anonymous`` — the stamp records what happened, the
    enforcement matrix (owner-or-admin) does the gating.
    """
    identity = _clients_registry.current_identity()
    if identity is not None:
        if identity.is_admin:
            return "admin"
        name = str(getattr(identity, "name", "") or "").strip()
        # The D20 fallback name is a SENTINEL, not a user: a dataset created
        # unauthenticated is nobody's — "anonymous" (a name the registry's
        # own regex can never mint, so it can never collide with a real key
        # identity).
        if name and name != "__anonymous__":
            return name
    return "anonymous"


def _identity_may_manage_dataset(dm: DatasetManager, name: str) -> None:
    """D23 ownership gate for destructive dataset surfaces (delete, the
    creator's public toggle): 403 unless the caller is the creator or an
    admin.

    Rules (fleet decision D23):
      * an ADMIN identity always passes (deployment keys, MCP keyset keys);
      * otherwise the caller's identity NAME must equal the dataset's
        ``created_by`` stamp (meta.json, written at create time);
      * a dataset with NO stamp — pre-D23, or one whose meta is missing /
        unreadable — is ADMIN-ONLY: no backfill, no proof of creation, and
        a corrupt read must never widen a non-admin's powers (fail-closed).
    The password gate on those surfaces stays layered ON TOP of this (the
    check runs first so the 403 does not leak whether a password exists).
    """
    identity = _clients_registry.current_identity()
    if identity is not None and identity.is_admin:
        return
    caller = str(getattr(identity, "name", "") or "").strip()
    creator = ""
    try:
        creator = str(dm.read_created_by(name) or "").strip()
    except Exception:
        creator = ""  # unreadable meta → no provable creator → admin-only
    if creator and caller and caller == creator:
        return
    if not creator:
        raise HTTPException(
            403,
            f"Dataset '{name}' predates dataset ownership (no creator recorded) — only an admin can manage it.",
        )
    raise HTTPException(
        403,
        f"Dataset '{name}' is owned by '{creator}' — only its creator or an admin can delete it.",
    )


@app.get("/api/admin/clients")
async def api_admin_list_clients():
    """List the per-user registry (env ∪ overlay) — key material masked.

    Admin-key only.  Overlay entries are editable from the page; env entries
    are listed for visibility but remain authoritative in the env (the page
    shows them as ``source: env`` and refuses to overwrite their keys).
    """
    _require_admin_file()
    _require_admin_identity()
    loop = asyncio.get_running_loop()
    return {"clients": await loop.run_in_executor(sync_pool, _admin_registry.list_clients)}


@app.post("/api/admin/clients")
async def api_admin_mint_client(request: Request, body: dict[str, Any] = Body(...)):
    """Mint (or key-rotate) a per-user client — ADMIN key required.

    Request body::

        {"name": "alice", "datasets": ["reports", "notes"], "key": "…optional…"}

    The generated key is returned ONCE in ``key`` (masked everywhere else) —
    this response is the only time the operator sees it.  An existing name
    rotates its key (the old key stops authenticating immediately).
    """
    _require_admin_file()
    _require_admin_identity()
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(400, "Field 'name' is required")
    datasets = body.get("datasets", [])
    if not isinstance(datasets, list):
        raise HTTPException(400, "Field 'datasets' must be a list of dataset names")
    custom = body.get("key")
    if custom is not None and (not isinstance(custom, str) or not custom.strip()):
        raise HTTPException(400, "Field 'key' must be a non-empty string when provided")
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            sync_pool,
            lambda: _admin_registry.mint_client(name, datasets=datasets, key=custom),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    logger.info("Admin minted/rotated client '%s' (%d dataset(s)) via /access", result["name"], len(result["datasets"]))
    return {
        "status": "ok",
        "name": result["name"],
        "key": result["key"],  # the ONLY full-key response — copy it now
        "datasets": result["datasets"],
        "rotated": result["rotated"],
        "message": (
            f"Key rotated for '{result['name']}' — the previous key no longer authenticates."
            if result["rotated"]
            else f"Client '{result['name']}' minted."
        ),
    }


@app.patch("/api/admin/clients/{name}")
async def api_admin_grant_client(name: str, request: Request, body: dict[str, Any] = Body(...)):
    """Replace a client's dataset grant (the checkbox set) — ADMIN key only.

    Request body::

        {"datasets": ["reports", "notes"]}   # ["*"] = everything, [] = none

    The key itself is untouched.  Applies on the next request (no restart).

    D21 (optional extra fields on the same PATCH — no new route):
    ``"oidc": "<alias>"`` binds the client to a JWT identity (empty string
    clears the binding) and ``"blocked": true/false`` blocks/unblocks the
    identity (key AND JWT — nothing resolves for it).  Both are ignored when
    absent, so the grant-only body keeps its exact behaviour.
    """
    _require_admin_file()
    _require_admin_identity()
    datasets = body.get("datasets")
    oidc_field = body.get("oidc")
    blocked_field = body.get("blocked")
    # D21 flag-only PATCHes (the SPA's Block and OIDC-bind buttons) carry NO
    # datasets field — grant validation applies only when datasets is
    # actually present.  A body with none of the three fields is still a
    # 400 (nothing to do).
    if datasets is None and oidc_field is None and blocked_field is None:
        raise HTTPException(400, "Provide 'datasets' (a list), 'oidc' (a string), or 'blocked' (a bool)")
    if datasets is not None and not isinstance(datasets, list):
        raise HTTPException(400, "Field 'datasets' must be a list of dataset names")
    if oidc_field is not None and (not isinstance(oidc_field, str)):
        raise HTTPException(400, "Field 'oidc' must be a string (an alias, or '' to clear)")
    if blocked_field is not None and not isinstance(blocked_field, bool):
        raise HTTPException(400, "Field 'blocked' must be a boolean")
    loop = asyncio.get_running_loop()
    try:
        result = None
        if datasets is not None:
            result = await loop.run_in_executor(sync_pool, lambda: _admin_registry.grant_datasets(name, datasets))
        if oidc_field is not None or blocked_field is not None:
            # set_client_flags AUTO-MINTS an empty-grant entry for unknown
            # names (the observed-JWT-user flow) — so a flag-only PATCH on an
            # observed row works without a prior mint (Block and OIDC-bind).
            flags = await loop.run_in_executor(
                sync_pool,
                lambda: _admin_registry.set_client_flags(
                    name,
                    oidc=None if oidc_field is None else oidc_field,
                    blocked=None if blocked_field is None else blocked_field,
                ),
            )
        else:
            flags = None
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except TypeError as exc:  # D26: a non-str/dict grants payload element
        raise HTTPException(400, str(exc))
    except KeyError:
        raise HTTPException(404, f"Client '{name}' not found in the admin registry")
    result_datasets = result["datasets"] if result is not None else None
    if result is not None:
        logger.info("Admin updated grants for '%s' → %s", name, result["datasets"])

    # D26: grants are a mixed list (legacy "name" strings = rw; {"name",
    # "mode"} objects) — format each for the human message instead of
    # joining, which would TypeError on the object form.
    def _grant_label(g: Any) -> str:
        if isinstance(g, dict):
            return f"{g.get('name')} ({g.get('mode')})"
        return str(g)

    message = (
        f"Grants for '{name}' updated: {', '.join(_grant_label(g) for g in (result_datasets or [])) or '(none)'}."
        if result is not None
        else f"Flags for '{name}' updated."
    )
    if flags is not None:
        detail = []
        if "oidc" in flags:
            detail.append(f"oidc={flags['oidc'] or '(cleared)'}")
        detail.append(f"blocked={flags['blocked']}")
        message += f" D21 flags: {', '.join(detail)}."
    return {
        "status": "ok",
        "name": name,
        "datasets": result_datasets if result_datasets is not None else (flags or {}).get("datasets", []),
        "message": message,
    }


@app.delete("/api/admin/clients/{name}")
async def api_admin_revoke_client(name: str):
    """Revoke an overlay client (its key stops working immediately) — ADMIN
    key only.  An env-side client with the same name is NOT removable here
    (the env registry keeps authority); the response says so explicitly.
    """
    _require_admin_file()
    _require_admin_identity()
    loop = asyncio.get_running_loop()
    try:
        removed = await loop.run_in_executor(sync_pool, _admin_registry.revoke_client, name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if removed:
        logger.info("Admin revoked client '%s' via /access", name)
        return {"status": "ok", "message": f"Client '{name}' revoked — its key no longer authenticates."}
    # Distinguish "never existed" from "exists only in env" for the UI.
    from multimodal_rag.utils.clients_registry import registry_clients

    if name in set(registry_clients().values()):
        return {
            "status": "ok",
            "message": f"'{name}' is defined in the ENV registry (RAG_API_KEY_CLIENTS) — remove it there; the page cannot edit env config.",
        }
    raise HTTPException(404, f"Client '{name}' not found in the admin registry")


@app.post("/api/datasets/{name}/media-token")
async def api_media_token(name: str, request: Request):
    """Mint a short-lived HMAC token for fetching this dataset's media files.

    The caller must be authorised (``X-Dataset-Password`` header, the
    ``password`` form field, or a prior ``/unlock``).  The returned token is
    dataset-scoped: it can be appended to ``?token=`` on any
    ``/api/datasets/{name}/files/...`` URL for ``MEDIA_TOKEN_TTL`` seconds,
    letting ``<img>/<video>/<audio>`` tags fetch protected media without the
    dataset password ever appearing in a URL.
    """
    dm = await get_manager_async()
    password = request.query_params.get("password", "")
    x_password = request.headers.get("X-Dataset-Password", "")
    try:
        await _require_dataset_password(dm, name, (x_password or password) or None, request)
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")
    return {"token": _sign_media_token(name, "*"), "ttl_seconds": _MEDIA_TOKEN_TTL}


def _rag_acl_filter_datasets(datasets: list, catalog: bool = False) -> tuple:
    """D15/D16: drop datasets outside the caller's dataset world.

    Returns ``(visible, hidden_count)``.  The visible set is the caller's
    EFFECTIVE access: (operator ACL ∪ self-selections) − exclusions —
    access isolation is the design (the ratified 2026-09-24 ruling REVERSED
    the earlier discovery-mode flip: a listing never shows names the key
    cannot use).  Admin identities see everything; the anonymous identity
    (unconfigured deployment, D20) sees only its memory-dataset grant, so
    the filter runs whenever an identity is bound — not only when the
    registry is configured.

    *catalog* (the /access page's view, 2026-10) widens the visible set to
    the selection CATALOG: world ∪ public-available ∪ excluded — public
    datasets are advertised so a user can opt in, and excluded datasets
    stay visible (stamped ``excluded: true``) so a user can re-include
    them.  API listings (MCP/REST without the flag) keep the world-only
    filter: an unselected public dataset never force-enters a key's world.
    """
    identity = _clients_registry.current_identity()
    if identity is None or identity.is_admin:
        return datasets, 0
    if not catalog:
        visible = [d for d in datasets if _access_store.dataset_allowed(identity, str(d.get("name", "")))]
        return visible, len(datasets) - len(visible)
    excluded = _access_store.exclusions_for(identity)
    visible = []
    for d in datasets:
        name = str(d.get("name", ""))
        if (
            _access_store.dataset_allowed(identity, name)
            or _clients_registry.is_public_dataset(name)
            or name in excluded
        ):
            if name in excluded:
                d = dict(d)
                d["excluded"] = True
            visible.append(d)
    return visible, len(datasets) - len(visible)


def _annotate_public(datasets: list) -> None:
    """Stamp ``public: true`` on datasets carrying the admin-set public flag
    (meta.json ``public`` — feature: public-to-all-keys), plus the D23
    ownership stamps (``created_by`` and ``owned_by_me``) on every row.

    The /access page renders a 🌐 badge from the first; the UI renders owner
    info from ``created_by`` (and the "mine" affordance from
    ``owned_by_me`` — true ONLY when the current identity's name equals the
    stamp; admins/anonymous/pre-D23 rows never claim ownership).  Both meta
    reads are mtime-cached (``is_public_dataset`` / ``created_by_dataset``),
    so this costs no per-request NFS round trip beyond the first.  Stamped
    on every listing surface (catalog and plain) — an admin marking a
    dataset global is deployment-wide information, and the creator stamp is
    how owners recognize their own rows.
    """
    identity = _clients_registry.current_identity()
    caller_name = str(getattr(identity, "name", "") or "").strip() if identity is not None else ""
    for ds in datasets:
        try:
            if _clients_registry.is_public_dataset(str(ds.get("name", ""))):
                ds["public"] = True
        except Exception:  # never let a meta read break a listing
            pass
        # D23 ownership stamps (best-effort per row — same never-break rule).
        try:
            creator = _clients_registry.created_by_dataset(str(ds.get("name", "")))
            if creator:
                ds["created_by"] = creator
                if caller_name and caller_name == creator:
                    ds["owned_by_me"] = True
        except Exception:
            continue


def _annotate_acl_granted(datasets: list) -> None:
    """Stamp ``acl_granted: true`` on datasets the caller's identity can
    already reach via the OPERATOR ACL (env registry or the D17 admin
    overlay) — i.e. WITHOUT a self-selection.

    The /access page uses it to PRE-CHECK those rows (your grants are yours
    already — a user should never have to "select" what was granted) and to
    distinguish the three checkbox states the D16 store keeps:

      * ``acl_granted`` + no selection entry        → granted, no password saved
      * selection entry with ``has_password``       → password saved (memory-ready)
      * selection entry without ``has_password``    → self-selected public dataset

    Admin identities and the store-off case: nothing is stamped (the page
    treats an absent field as false) — byte-identical responses elsewhere.
    """
    if not _clients_registry.registry_configured():
        return
    identity = _clients_registry.current_identity()
    if identity is None or identity.is_admin:
        return
    for ds in datasets:
        name = str(ds.get("name", ""))
        if ds.get("excluded") is True:
            # Catalog view: the dataset is excluded by the user.  Report
            # whether the operator grant still stands underneath
            # (re-inclusion is then password-free) - but never pre-check
            # the row.
            if _clients_registry.dataset_allowed(identity, name):
                ds["acl_granted"] = True
            continue
        if _access_store.dataset_allowed(identity, name):
            ds["acl_granted"] = True


@app.get("/api/datasets")
async def api_list_datasets(
    request: Request,
    cursor: str = Query("", description="Opaque continuation token from a previous page's next_cursor"),
    limit: int = Query(0, ge=0, le=10000, description="Page size; 0 = no pagination (full list, default)"),
    catalog: bool = Query(
        False, description="/access page view: world + public-available + excluded (stamped excluded:true)"
    ),
):
    """List all datasets with metadata.

    Additive cursor pagination (Wave-4): with no ``cursor``/``limit`` the
    response is exactly the historical full listing.  Passing either
    parameter returns ``{"datasets": [...], "next_cursor": ...}`` — feed
    ``next_cursor`` back as ``cursor`` to walk the listing (``null`` = done).
    """
    dm = await get_manager_async()
    loop = asyncio.get_running_loop()
    if not cursor and not limit:
        datasets = await loop.run_in_executor(sync_pool, dm.list_datasets)
        cid = _unlock_client_id(request)
        with _UNLOCK_CACHE_LOCK:
            for ds in datasets:
                ds["unlocked"] = (ds["name"], cid) in _UNLOCK_CACHE
        datasets, _acl_hidden = _rag_acl_filter_datasets(datasets, catalog=catalog)
        _annotate_public(datasets)
        _annotate_acl_granted(datasets)
        return {"datasets": datasets, **({"acl_hidden": _acl_hidden} if _acl_hidden else {})}
    try:
        page = await loop.run_in_executor(
            sync_pool, lambda: dm.list_datasets(cursor=cursor or None, limit=limit or None)
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    datasets = page["datasets"]
    cid = _unlock_client_id(request)
    with _UNLOCK_CACHE_LOCK:
        for ds in datasets:
            ds["unlocked"] = (ds["name"], cid) in _UNLOCK_CACHE
    datasets, _acl_hidden = _rag_acl_filter_datasets(datasets, catalog=catalog)
    _annotate_public(datasets)
    _annotate_acl_granted(datasets)
    return {
        "datasets": datasets,
        "next_cursor": page["next_cursor"],
        **({"acl_hidden": _acl_hidden} if _acl_hidden else {}),
    }


# ---------------------------------------------------------------------------
# D23 read-only stats — GET /api/stats
# ---------------------------------------------------------------------------

# Last-known model health for /api/stats: the ONLY data source is state that
# already exists (the periodic embedder liveness monitor's ``_model_health``
# snapshot + which optional models are CONFIGURED — no synchronous HTTP
# probe per stats request, ever).  A tiny TTL cache smooths the one
# DatasetManager read so a poll-happy tab still cannot fan out manager
# construction.
_STATS_MODEL_TTL = 10.0
_stats_models_cache: dict[str, Any] = {"ts": 0.0, "models": []}


def _stats_model_rows() -> "list[dict[str, Any]]":
    """The ``models`` rows for /api/stats — last-known state only.

    One row per role with ``{name, url, healthy}``: ``healthy`` is True only
    when the role's liveness state is known-good (the embedder's periodic
    monitor; optional roles count as healthy merely when configured — their
    liveness is only ever checked on-demand by /api/admin/connections, and
    /api/stats deliberately never makes those calls).  Never raises.
    """
    now = time.monotonic()
    if now - _stats_models_cache["ts"] < _STATS_MODEL_TTL and _stats_models_cache["models"]:
        return _stats_models_cache["models"]

    def _collect() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        embedder = _model_health.get("embedder") or {}
        try:
            dm = get_manager()
        except Exception:
            dm = None
        for role, model in (
            ("embedder", getattr(dm, "embedder", None)),
            ("reranker", getattr(dm, "reranker", None)),
            ("vlm", getattr(dm, "vlm", None)),
            ("asr", getattr(dm, "asr", None)),
        ):
            if model is None:
                continue
            healthy = bool(role != "embedder" or (embedder.get("status") == "healthy"))
            rows.append(
                {
                    "name": str(getattr(model, "model_name", "") or ""),
                    "url": str(getattr(model, "url_remote", "") or "").rstrip("/"),
                    "healthy": healthy,
                }
            )
        return rows

    try:
        rows = _collect()
    except Exception:
        rows = []
    _stats_models_cache["ts"] = now
    _stats_models_cache["models"] = rows
    return rows


@app.get("/api/stats")
async def api_stats(request: Request = None) -> dict[str, Any]:  # type: ignore[assignment]  # direct callers/tests pass no request — FastAPI forbids Optional[Request] route params
    """Read-only deployment stats for the SPA's stats tab (D23) —
    identity-aware and deliberately cheap.

    Shape (public-schema JSON): ``{"datasets": {"total", "visible_to_you",
    "documents", "storage_bytes"}, "models": [{name, url, healthy}],
    "jobs": {"active_uploads", "recent_failures"}, "memory": {"dataset"},
    "generated_at"}``.

    Identity filtering mirrors the listing: the dataset counts come from the
    SAME ``_rag_acl_filter_datasets`` logic, so ``visible_to_you`` is exactly
    the caller's world (the D16 union — operator ACL ∪ self-selections −
    exclusions); an ADMIN identity sees deployment-wide numbers
    (``total == visible_to_you``); an unauthenticated caller (identity
    ``None``) gets the same shape with ``visible_to_you`` 0 and no model
    health — never an error.  ``storage_bytes`` sums the visible datasets'
    ``files/`` directories (one stat per file — the same walk
    /api/admin/storage does).

    ZERO write operations, and no per-request model probes: the meta counts
    ride ``list_datasets`` (mtime/counter-cached), model health is the
    periodic monitor's last-known snapshot behind a short TTL — the endpoint
    is safe to poll.
    """
    # api_stats is PUBLIC (the SPA stats tab works pre-signin), so the auth
    # middleware never binds the request identity for it — resolve it HERE,
    # exactly the way the middleware would have: the presented (key,
    # source) pairs through resolve_presented (admin keys, registry keys,
    # JWT envelopes, the D22 cookie).  No credential → identity None → the
    # empty-world anonymous shape below.
    identity = _clients_registry.current_identity()
    if identity is None and request is not None:
        presented = _presented_pairs(request)
        if presented:
            try:
                identity = _clients_registry.resolve_presented([k for k, _ in presented], [via for _, via in presented])
            except Exception:
                identity = None  # fail closed to the anonymous shape
    token = None
    if identity is not None:
        # Bind so the executor-side filter (and any helper) reads the same
        # identity the middleware would have bound.
        token = _clients_registry.set_current_identity(identity)
    try:
        return await _stats_body(identity)
    finally:
        if token is not None:
            _clients_registry.reset_current_identity(token)


async def _stats_body(identity) -> dict[str, Any]:
    """The /api/stats payload for *identity* (already resolved or None)."""
    try:
        dm = await get_manager_async()
    except Exception:
        dm = None  # a not-yet-initialised manager reads as an empty world

    def _collect(dm: "DatasetManager | None") -> tuple[int, int, int, int]:
        """(total, visible, documents, storage_bytes) over the caller's world."""
        if dm is None:
            return 0, 0, 0, 0
        datasets = dm.list_datasets()
        total = len(datasets)
        visible, _hidden = _rag_acl_filter_datasets(datasets)
        documents = 0
        storage_bytes = 0
        for ds in visible:
            documents += int(ds.get("document_count", 0) or 0)
            files_dir = dm._dataset_dir(str(ds.get("name", ""))) / "files"
            if files_dir.is_dir():
                for f in files_dir.rglob("*"):
                    try:
                        if f.is_file() and not f.name.startswith("."):
                            storage_bytes += f.stat().st_size
                    except OSError:
                        continue
        return total, len(visible), documents, storage_bytes

    total = visible = documents = storage_bytes = 0
    if identity is None:
        # Unauthenticated: the shape stays identical, the caller's world is
        # empty by definition (never probe anything on their behalf).
        visible = 0
    else:
        # _submit_with_context, NOT bare run_in_executor: _collect reads the
        # caller's identity (via _rag_acl_filter_datasets) and a pool worker
        # starts with EMPTY contextvars — the same trap the ingest-warning
        # collector hit (see _submit_with_context).
        total, visible, documents, storage_bytes = await _submit_with_context(sync_pool, _collect, dm)

    active_uploads = 0
    recent_failures = 0
    try:
        with _upload_jobs._lock:
            for job in _upload_jobs._jobs.values():
                if job.get("status") == "uploading":
                    active_uploads += 1
                elif (
                    job.get("status") == "error"
                    and job.get("completed_at")
                    and time.time() - float(job["completed_at"]) < 3600
                ):
                    recent_failures += 1
    except Exception:
        logger.debug("Suppressed exception", exc_info=True)

    memory_binding: str | None = None
    try:
        memory_binding = _access_store.memory_dataset_for(identity, None)
    except Exception:
        memory_binding = None

    if identity is None:
        # No identity: model health is deliberately withheld (all-unknown)
        # rather than probed for an anonymous caller.
        models: list[dict[str, Any]] = []
    else:
        models = await _submit_with_context(sync_pool, _stats_model_rows)

    # PVC capacity (the old /manage page's headline info — restored into the
    # identity-agnostic storage section of /api/stats): one shutil.statvfs
    # on the shared data PVC, plus the Qdrant PVC summary when the manager
    # exposes one.  Read-only; both are the same numbers /api/admin/health
    # and /api/admin/storage report — non-admins simply never had a page
    # that showed them.
    storage_pvc: dict[str, Any] | None = None
    qdrant_pvc: dict[str, Any] | None = None
    if dm is not None:
        try:
            import shutil as _shutil

            usage = _shutil.disk_usage(str(dm.base_path))
            storage_pvc = {
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "used_percent": round(usage.used / usage.total * 100, 1),
            }
        except Exception:
            storage_pvc = None
        try:
            # The Qdrant PVC is a SEPARATE RWO volume per replica — none
            # mounted on the API pod (mountReadOnly is off on multi-replica
            # charts: a replica's RWO PVC cannot bind to API pods on other
            # nodes).  Used bytes come from QDRANT'S OWN TELEMETRY via the
            # same helpers /api/admin/health uses (cluster API for peers,
            # per-peer telemetry for segment sizes, TTL-cached); capacity is
            # the configured per-replica PVC size.
            if QDRANT_STORAGE_PATH and Path(QDRANT_STORAGE_PATH).exists():
                # single-node deployment with a read-only mount: exact usage
                q_usage = _shutil.disk_usage(QDRANT_STORAGE_PATH)
                qdrant_pvc = {
                    "total_bytes": q_usage.total,
                    "used_bytes": q_usage.used,
                    "free_bytes": q_usage.free,
                    "used_percent": round(q_usage.used / q_usage.total * 100, 1),
                }
            else:
                qd_total = _parse_size_bytes(os.environ.get("QDRANT_PVC_SIZE", ""))
                replicas = _qdrant_cluster_replicas()
                replica_usage = _qdrant_replica_usage(
                    replicas, os.environ.get("QDRANT_HOST", ""), os.environ.get("QDRANT_PORT", "6333")
                )
                qd_used = sum(replica_usage.values())
                if qd_total and qd_used:
                    qdrant_pvc = {
                        "total_bytes": qd_total,
                        "used_bytes": qd_used,
                        "free_bytes": max(qd_total - qd_used, 0),
                        "used_percent": round(qd_used / qd_total * 100, 1),
                        "replicas": dict(replica_usage),
                    }
                elif replica_usage:
                    qdrant_pvc = {"used_bytes": qd_used, "replicas": dict(replica_usage)}
        except Exception:
            qdrant_pvc = None

    return {
        "datasets": {
            "total": total,
            "visible_to_you": visible,
            "documents": documents,
            "storage_bytes": storage_bytes,
        },
        "storage_pvc": storage_pvc,
        "qdrant_pvc": qdrant_pvc,
        "models": models,
        "jobs": {"active_uploads": active_uploads, "recent_failures": recent_failures},
        "memory": {"dataset": memory_binding},
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }


@app.get("/api/datasets/{name}")
async def api_get_dataset(
    name: str,
    request: Request,
    x_dataset_password: str | None = Header(None, alias="X-Dataset-Password"),
):
    """Get metadata for a single dataset."""
    dm = await get_manager_async()
    try:
        await _require_dataset_password(dm, name, x_dataset_password, request)
        loop = asyncio.get_running_loop()
        meta = await loop.run_in_executor(sync_pool, dm.get_dataset, name)
        return {"dataset": meta}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.patch("/api/datasets/{name}")
async def api_update_dataset(
    name: str,
    request: Request,
    body: dict[str, Any] = Body(...),
    x_dataset_password: str | None = Header(None, alias="X-Dataset-Password"),
):
    """Update dataset metadata (e.g. description).

    Password-protected datasets require the ``X-Dataset-Password`` header
    (or a cached unlock) — metadata changes must not be possible for
    callers who cannot read the dataset.
    """
    dm = await get_manager_async()
    try:
        await _require_dataset_password(dm, name, x_dataset_password, request)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(sync_pool, dm.update_dataset, name, body)
        return {"status": "ok", "updated": name}
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")
    except ValueError as exc:
        # Includes the admin-only 'public' key rejection (fail loud, not 500).
        raise HTTPException(400, str(exc))


@app.delete("/api/datasets/{name}")
async def api_delete_dataset(
    name: str,
    request: Request,
    x_dataset_password: str | None = Header(None, alias="X-Dataset-Password"),
):
    """Delete a dataset and its Qdrant collection.

    D23 dataset ownership — owner-or-admin: only the identity whose name
    equals the dataset's ``created_by`` stamp, or an ADMIN identity, may
    delete.  Everyone else gets 403
    (``Dataset '<name>' is owned by '<created_by>' — only its creator or an
    admin can delete it.``).  A dataset whose meta carries NO ``created_by``
    (pre-D23) is deletable by ADMINS ONLY — never backfilled, no provable
    creator.  The ownership check runs BEFORE the password gate so a
    non-owner learns nothing about the dataset's protection state.

    Password-protected datasets still require the ``X-Dataset-Password``
    header (or a cached unlock) — deleting must not be easier than reading;
    ownership is an ADDITIONAL check layered in front of it.
    """
    try:
        dm = await get_manager_async()
        _identity_may_manage_dataset(dm, name)
        await _require_dataset_password(dm, name, x_dataset_password, request)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(sync_pool, dm.delete_dataset, name)
        return {"status": "ok", "deleted": name}
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")


# -- Documents ----------------------------------------------------------------


@app.post("/api/datasets/{name}/documents")
async def api_add_documents(
    name: str,
    request: Request,
    body: Any = Body(None),
    x_dataset_password: str | None = Header(None, alias="X-Dataset-Password"),
):
    """Add documents (text strings or multimodal dicts) to a dataset.

    Accepts a JSON body that is either an array of documents or a single
    document dict/string.  Each document can be a plain string or a dict
    with optional ``text``, ``image``, ``video``, ``audio`` keys.
    """
    if body is None:
        raise HTTPException(400, "Request body is required (JSON array or object)")

    payload: list[Any]
    if isinstance(body, list):
        payload = body
    elif isinstance(body, (dict, str)):
        payload = [body]
    else:
        # A non-JSON body (e.g. form-encoded) surfaces here as an opaque
        # object that would otherwise flow into the pipeline and crash it
        # with a 500 several layers deep.
        raise HTTPException(422, "Send JSON (Content-Type: application/json): a document or an array of documents")
    if not all(isinstance(d, (str, dict)) for d in payload):
        raise HTTPException(422, "Each document must be a string or an object")

    try:
        dm = await get_manager_async()
        await _require_dataset_password(dm, name, x_dataset_password, request)
        warnings: list[str] = []
        token = _ingest_warnings.set(warnings)
        try:
            ids = await _submit_with_context(sync_pool, dm.add_documents, name, payload)
        finally:
            _ingest_warnings.reset(token)
        resp = {"status": "ok", "stored_ids": ids, "count": len(ids)}
        if warnings:
            resp["warnings"] = warnings
        return resp
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")
    except MediaRefError as exc:
        # A document carried a media ref the server refuses to read/fetch
        # (local path outside the allowlist, blocked http host, s3://).
        raise HTTPException(400, str(exc))


# -- Files -------------------------------------------------------------------


@app.post("/api/datasets/{name}/files")
async def api_upload_file(
    name: str,
    request: Request,
    file: UploadFile = File(...),
    password: str = Form(""),
):
    """Upload a file to a dataset.

    Supported types: PDF, image (jpg/png/gif/bmp/webp), video (mp4/mkv/avi/mov),
    audio (mp3/wav/flac/ogg), and text files.  Files are processed and stored
    as vector entries in the dataset's Qdrant collection.
    """
    dm = await get_manager_async()
    loop = asyncio.get_running_loop()
    try:
        await _require_dataset_password(dm, name, password or None, request)
        await loop.run_in_executor(sync_pool, dm.get_dataset, name, False)
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")

    # Save uploaded file to a temporary location
    suffix = Path(file.filename or "upload").suffix if file.filename else ".bin"
    tmp_path = ""
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        try:
            await _stream_upload(file, tmp)
            tmp_path = tmp.name
        finally:
            tmp.close()

        warnings: list[str] = []
        token = _ingest_warnings.set(warnings)
        try:
            result = await _submit_with_context(sync_pool, dm.add_file, name, tmp_path, file.filename)
        finally:
            _ingest_warnings.reset(token)
        _record_upload_history(
            name,
            [{"file": file.filename, "chunks": result.get("chunks", 0)}],
            "files",
        )
        resp = {"status": "ok", "file": file.filename, **result}
        if warnings:
            resp["warnings"] = warnings
        return resp
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                logger.debug("Suppressed exception", exc_info=True)


@app.post("/api/datasets/{name}/batch-files")
async def api_upload_files_batch(
    name: str,
    request: Request,
    files: list[UploadFile] = File(...),
    password: str = Form(""),
):
    """Upload multiple files at once with poll-based progress tracking.

    Returns a ``job_id`` immediately.  Poll ``GET /api/datasets/{name}/upload-status/{job_id}``
    every 2-3 seconds for progress updates.
    """
    dm = await get_manager_async()
    loop = asyncio.get_running_loop()
    try:
        await _require_dataset_password(dm, name, password or None, request)
        await loop.run_in_executor(sync_pool, dm.get_dataset, name, False)
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")

    # Write files to temp before starting the background job
    file_entries: list[tuple[str, str]] = []
    try:
        for f in files:
            suffix = Path(f.filename or "upload").suffix if f.filename else ".bin"
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            try:
                await _stream_upload(f, tmp)
            finally:
                tmp.close()
            file_entries.append((tmp.name, f.filename or "upload"))
    except BaseException:
        for p, _ in file_entries:
            try:
                os.unlink(p)
            except Exception:
                logger.debug("Suppressed exception", exc_info=True)
        raise

    job_id = _upload_jobs.create(name, len(file_entries), source="files")
    warnings: list[str] = []
    warnings_token = _ingest_warnings.set(warnings)

    def _process() -> None:
        try:

            def cb(e):
                _upload_jobs.add_event(job_id, e)

            r = dm.add_files_batch(name, file_entries, progress_callback=cb)
            if warnings:
                r["warnings"] = warnings
            _upload_jobs.complete(job_id, r)
            _record_upload_history(name, r.get("files") or [], "files")
        except Exception as exc:
            _upload_jobs.fail(job_id, str(exc))
            _notify_ingest_hook(name, 0, "error")
            _record_upload_history(
                name,
                [{"file": orig, "chunks": 0, "error": str(exc)} for _, orig in file_entries],
                "files",
            )
        finally:
            # Clean up temp files
            for p, _ in file_entries:
                try:
                    os.unlink(p)
                except Exception:
                    logger.debug("Suppressed exception", exc_info=True)

    _submit_with_context(None, _process)
    _ingest_warnings.reset(warnings_token)

    return {"job_id": job_id, "status": "uploading", "total_files": len(file_entries)}


@app.post("/api/datasets/{name}/batch-urls")
async def api_upload_urls_batch(
    name: str,
    request: Request,
    body: dict[str, Any] = Body(...),
    x_dataset_password: str | None = Header(None, alias="X-Dataset-Password"),
):
    """Ingest files from URLs (S3, HTTP) into a dataset with poll-based progress.

    Returns a ``job_id`` immediately.  Poll ``GET /api/datasets/{name}/upload-status/{job_id}``
    every 2-3 seconds for progress updates.

    Body flags (S3 prefix URLs only):

      * ``sync: true`` — reconcile the dataset with the bucket prefix: after
        the ingest, stored documents whose source is under a synced prefix
        but absent from the bucket listing are pruned (objects deleted
        upstream disappear here too).
      * ``sync_dry_run: true`` — report the planned ingest/prune sets
        (``would_ingest`` / ``would_prune``) without touching anything.
    """
    urls = body.get("urls", [])
    if not urls or not isinstance(urls, list):
        raise HTTPException(400, "Field 'urls' must be a non-empty array of URL strings")
    sync = bool(body.get("sync", False))
    sync_dry_run = bool(body.get("sync_dry_run", False))
    if sync_dry_run:
        sync = True

    dm = await get_manager_async()
    loop = asyncio.get_running_loop()
    try:
        await _require_dataset_password(dm, name, x_dataset_password, request)
        await loop.run_in_executor(sync_pool, dm.get_dataset, name, False)
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")

    if sync_dry_run:
        # No ingest — answer synchronously.
        try:
            plan = await loop.run_in_executor(
                sync_pool,
                partial(dm.add_urls_batch, name, urls, sync=True, sync_dry_run=True),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"dataset": name, **plan}

    job_id = _upload_jobs.create(name, len(urls), source="urls")
    warnings: list[str] = []
    warnings_token = _ingest_warnings.set(warnings)

    def _process() -> None:
        try:

            def cb(e):
                _upload_jobs.add_event(job_id, e)

            r = dm.add_urls_batch(name, urls, progress_callback=cb, sync=sync)
            if warnings:
                r["warnings"] = warnings
            _upload_jobs.complete(job_id, r)
            _record_upload_history(name, r.get("files") or [], "urls")
        except Exception as exc:
            _upload_jobs.fail(job_id, str(exc))
            _notify_ingest_hook(name, 0, "error")
            failed_files = [
                {"file": Path(url.split("?")[0].rstrip("/")).name or "file", "chunks": 0, "error": str(exc)}
                for url in urls
            ]
            _record_upload_history(name, failed_files, "urls")

    _submit_with_context(None, _process)
    _ingest_warnings.reset(warnings_token)

    return {"job_id": job_id, "status": "uploading", "total_files": len(urls)}


@app.get("/api/datasets/{name}/upload-status/{job_id}")
async def api_upload_status(
    name: str,
    job_id: str,
    request: Request,
    x_dataset_password: str | None = Header(None, alias="X-Dataset-Password"),
):
    """Poll the status of an upload/ingestion job.

    Returns the job's current status, accumulated events, and aggregate
    counters.  When ``status`` is ``complete`` or ``error``, the job is
    finished and polling can stop.
    """
    dm = await get_manager_async()
    try:
        await _require_dataset_password(dm, name, x_dataset_password, request)
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")

    job = _upload_jobs.get(job_id)
    if job is None or job.get("dataset") != name:
        raise HTTPException(404, f"Job '{job_id}' not found")

    # Opportunistic cleanup of old completed jobs
    _upload_jobs.cleanup_old()

    return job


# -- Search ------------------------------------------------------------------


# Weighted-RRF bounds (feature: weighted RRF) — same discipline as the MCP
# surface: weights are rank-space tilts (0.0–10.0, 3 decimals), k is the
# ranking constant (default 2 when unpinned).  Out-of-range values clamp
# rather than reject, and the response's ``rrf`` block reports exactly what
# was applied.
_RRF_WEIGHT_MAX = 10.0


def _rrf_default_meta_from_env() -> "dict[str, Any] | None":
    """Parse ``RAG_RRF_DEFAULT`` (``"dense,sparse[,k]"``) to a meta payload.

    Returns ``None`` when unset/empty/disabled — no per-dataset default is
    stamped and new datasets inherit the global 1.0/1.0 code default (the
    deployment-level knob is opt-in; disabled-by-default is part of the
    ruling).  A payload that clamps to pure defaults also returns ``None``
    (nothing worth stamping).  Malformed values degrade to the disabled
    default with a loud log — a bad chart value must never block dataset
    creation.
    """
    raw = RAG_RRF_DEFAULT
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return None
    try:
        dense = float(parts[0])
        sparse = float(parts[1]) if len(parts) > 1 else 1.0
        k = int(parts[2]) if len(parts) > 2 else None
    except (TypeError, ValueError):
        logger.warning(
            "RAG_RRF_DEFAULT=%r is not 'dense,sparse[,k]' — per-dataset RRF defaults stay disabled",
            raw,
        )
        return None
    from multimodal_rag.dataset_manager import DatasetManager

    try:
        payload: dict[str, Any] = {"dense_weight": dense, "sparse_weight": sparse}
        if k is not None:
            payload["k"] = k
        return DatasetManager._sanitize_rrf_meta(payload)
    except ValueError as exc:
        logger.warning("RAG_RRF_DEFAULT=%r rejected (%s) — per-dataset RRF defaults stay disabled", raw, exc)
        return None


def _clamp_rrf_weight(value: Any, name: str) -> float:
    """Clamp a search weight to ``[0.0, 10.0]``, rounded to 3 decimals."""
    try:
        w = float(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"Field '{name}' must be a number")
    if math.isnan(w) or math.isinf(w):
        raise HTTPException(400, f"Field '{name}' must be a finite number")
    return round(min(max(w, 0.0), _RRF_WEIGHT_MAX), 3)


def _rrf_params_from_request(dense_weight: Any = None, sparse_weight: Any = None, k: Any = None) -> "RrfParams | None":
    """Build the weighted-RRF override from raw request params, or ``None``.

    ``None`` (no override present) keeps the default fusion request
    byte-identical.  Present-but-unset values (``1.0``/``1.0`` with no k)
    are clamped and returned anyway — the response's ``rrf`` block then
    echoes the effective parameters while the executed request stays
    rank-arithmetic-identical to the default.
    """
    from multimodal_rag.vector_store import RrfParams

    if dense_weight is None and sparse_weight is None and k is None:
        return None
    k_val: int | None = None
    if k is not None:
        try:
            k_val = int(k)
        except (TypeError, ValueError):
            raise HTTPException(400, "Field 'k' must be an integer")
        k_val = max(1, min(k_val, 1000))
    return RrfParams(
        dense_weight=_clamp_rrf_weight(dense_weight, "dense_weight") if dense_weight is not None else 1.0,
        sparse_weight=_clamp_rrf_weight(sparse_weight, "sparse_weight") if sparse_weight is not None else 1.0,
        k=k_val,
    )


def _rrf_response_block(rrf: "RrfParams", results: list[dict[str, Any]]) -> dict[str, Any]:
    """The response-level ``rrf`` block: agree with the first-entry block.

    The first entry's ``rrf`` block (folded by ``DatasetManager.search``
    from the per-doc stamps before they are popped) is the source of truth
    for what was actually applied — the REST layer must not re-derive it
    from a private state that ``dm.search`` has already consumed.  When no
    entry carries the folded block (e.g. an empty result list), ``applied``
    is ``false`` — never label a request as applied without evidence.
    """
    applied = bool(results and isinstance(results[0].get("rrf"), dict) and results[0]["rrf"].get("applied") is True)
    return {"dense": rrf.dense_weight, "sparse": rrf.sparse_weight, "k": rrf.k, "applied": applied}


def _search_filters_from_params(
    file_types: str = "",
    severities: str = "",
    source_prefix: str = "",
    date_from: str = "",
    date_to: str = "",
) -> dict[str, Any] | None:
    """Build the metadata-filter dict for search endpoints from raw params.

    ``file_types``/``severities`` are comma-separated strings; anything
    unparsed is dropped.  Raises ``HTTPException(400)`` on an invalid date so
    callers get a clear error instead of a silent no-op filter.
    """
    filters: dict[str, Any] = {}
    ft = [t.strip() for t in (file_types or "").split(",") if t.strip()]
    if ft:
        filters["file_types"] = ft
    sv = [t.strip() for t in (severities or "").split(",") if t.strip()]
    if sv:
        filters["severities"] = sv
    if (source_prefix or "").strip():
        filters["source_prefix"] = source_prefix.strip()
    if (date_from or "").strip():
        filters["date_from"] = date_from.strip()
    if (date_to or "").strip():
        filters["date_to"] = date_to.strip()
    if not filters:
        return None
    from multimodal_rag.vector_store import build_payload_filter

    try:
        build_payload_filter(filters)  # validation pass (dates)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return filters


@app.get("/api/datasets/{name}/search")
async def api_search(
    name: str,
    request: Request,
    q: str = Query(..., description="Query text"),
    top_k: int = Query(10, ge=1, le=100),
    use_reranker: bool = Query(False),
    reranker_top_k: int = Query(3, ge=1, le=50),
    dense_weight: float | None = Query(None, description="Weighted-RRF dense-lane weight (0.0–10.0, rank-space tilt)"),
    sparse_weight: float | None = Query(None, description="Weighted-RRF sparse/BM25-lane weight (0.0–10.0)"),
    k: int | None = Query(None, description="Weighted-RRF ranking constant (default 2; 1–1000)"),
    file_types: str = Query(
        "",
        description="Comma-separated file-type filter (pdf,image,video,audio,text,json,table,code,office,html,xml,yaml,notebook,ebook,log,unknown)",
    ),
    severities: str = Query(
        "", description="Comma-separated log-severity filter (TRACE,DEBUG,INFO,WARN,ERROR,FATAL,CRITICAL)"
    ),
    source_prefix: str = Query("", description="Only results whose stored source path starts with this prefix"),
    date_from: str = Query("", description="Only results with timestamp_start >= this ISO-8601 datetime"),
    date_to: str = Query("", description="Only results with timestamp_start <= this ISO-8601 datetime"),
    x_dataset_password: str | None = Header(None, alias="X-Dataset-Password"),
):
    """Search within a dataset using a text query.

    Returns ranked results with content and similarity scores.

    Optional metadata filters (AND-combined, applied in Qdrant before
    ranking): ``file_types``, ``severities``, ``source_prefix``,
    ``date_from``/``date_to``.

    Optional weighted-RRF override (feature: weighted RRF): ``dense_weight``
    / ``sparse_weight`` (0.0–10.0, rank-space tilts — NOT score multipliers)
    and ``k`` (ranking constant, default 2).  Omitted params keep the
    default fusion request; any override applies only to the hybrid lane
    and is reported back in the response's ``rrf`` block
    (``{dense, sparse, k, applied}``) — ``applied=false`` on dense-degraded
    searches rather than labelling dense results with weights.
    """
    dm = await get_manager_async()
    try:
        await _require_dataset_password(dm, name, x_dataset_password, request)
        filters = _search_filters_from_params(file_types, severities, source_prefix, date_from, date_to)
        rrf = _rrf_params_from_request(dense_weight, sparse_weight, k)
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(
            sync_pool,
            partial(
                dm.search,
                name,
                q,
                top_k=top_k,
                use_reranker=use_reranker,
                reranker_top_k=reranker_top_k,
                filters=filters,
                rrf=rrf,
            ),
        )
        payload: dict[str, Any] = {"query": q, "filters": filters, "results": results}
        if rrf is not None:
            payload["rrf"] = _rrf_response_block(rrf, results)
        return payload
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")


@app.post("/api/datasets/{name}/search")
async def api_search_multimodal(
    name: str,
    request: Request,
    body: dict[str, Any] = Body(...),
):
    """Search within a dataset using a multimodal query (text + image + video + audio).

    Request body::

        {
            "query": {
                "text": "description of what to find",
                "image": "data:image/png;base64,..."
            },
            "password": "my-dataset-password",
            "top_k": 10,
            "use_reranker": false,
            "reranker_top_k": 3
        }

    The ``query`` dict can contain any of the following keys:
      - ``text`` — plain text description
      - ``image`` — a data URL, remote URL, or list of URLs
      - ``video`` — a data URL, remote URL, or list of URLs
      - ``audio`` — a data URL, remote URL, or list of URLs

    Media values (image/video/audio) can be:
      - A single data URL string (``data:image/png;base64,...``)
      - A list of data URL strings
      - A remote HTTP(S) URL (``https://example.com/photo.jpg``)

    An optional ``filters`` object narrows the search server-side
    (AND-combined, applied in Qdrant before ranking)::

        {
            "filters": {
                "file_types": ["pdf", "log"],
                "severities": ["ERROR"],
                "source_prefix": "reports/2025/",
                "date_from": "2026-08-01T00:00:00",
                "date_to": "2026-08-31T23:59:59"
            }
        }

    An optional weighted-RRF override (feature: weighted RRF) re-tilts the
    hybrid fusion: ``dense_weight`` / ``sparse_weight`` (0.0–10.0, rank-space
    tilts — NOT score multipliers) and ``k`` (ranking constant, default 2).
    Omitted fields keep the default fusion request.  Any override applies
    only to the hybrid lane and is reported back in the response's ``rrf``
    block (``{dense, sparse, k, applied}``) — ``applied=false`` on
    dense-degraded searches rather than labelling dense results with weights.

    Returns ranked results with content and similarity scores.
    """
    query = body.get("query")
    if query is None:
        raise HTTPException(400, "Field 'query' is required (string or dict)")
    password = body.get("password") or None
    top_k = body.get("top_k", 10)
    use_reranker = body.get("use_reranker", False)
    reranker_top_k = body.get("reranker_top_k", 3)
    rrf = _rrf_params_from_request(body.get("dense_weight"), body.get("sparse_weight"), body.get("k"))

    # Query-time SSRF guard: remote media URLs in the query are fetched
    # server-side by the embedder, so apply the same host policy as ingest
    # (loopback is allowed — clients legitimately pass the server's own
    # media URLs back).
    if isinstance(query, dict):
        try:
            for media_key in ("image", "video", "audio"):
                value = query.get(media_key)
                urls = value if isinstance(value, list) else [value]
                for u in urls:
                    if isinstance(u, str):
                        _check_media_url_policy(u)
        except ValueError as exc:
            raise HTTPException(400, f"Query media rejected: {exc}")

    dm = await get_manager_async()
    try:
        await _require_dataset_password(dm, name, password, request)
        raw_filters = body.get("filters")
        if raw_filters is not None and not isinstance(raw_filters, dict):
            raise HTTPException(400, "Field 'filters' must be an object")
        from multimodal_rag.vector_store import build_payload_filter

        try:
            build_payload_filter(raw_filters)  # validation pass (dates)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(
            sync_pool,
            partial(
                dm.search,
                name,
                query,
                top_k=top_k,
                use_reranker=use_reranker,
                reranker_top_k=reranker_top_k,
                filters=raw_filters,
                rrf=rrf,
            ),
        )
        payload: dict[str, Any] = {"query": query, "filters": raw_filters, "results": results}
        if rrf is not None:
            payload["rrf"] = _rrf_response_block(rrf, results)
        return payload
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")


# -- Federated multi-dataset search (roadmap feature 8) ----------------------


def _federated_concurrency() -> int:
    """Max datasets searched CONCURRENTLY per federated fan-out (default 8).

    Read per call so the bound is tunable without a restart; unset/garbage
    values fall back to the default.  Before this bound, the fan-out launched
    one task per dataset (bounded only by ``sync_pool``, which then queued
    FIFO — a caller naming 50 datasets enqueued 50 searches at once).
    """
    try:
        return max(1, int(os.environ.get("RAG_FEDERATED_CONCURRENCY", "8")))
    except (TypeError, ValueError):
        return 8


def _federated_timeout_seconds() -> float:
    """Per-dataset search timeout (seconds) for one federated fan-out.

    Read per call; ``0`` DISABLES the timeout (the search is awaited
    unwrapped).  Default 60s: one hung dataset must not pin the whole
    federated call (Qdrant has no client timeout in the base chart, so
    nothing else bounds a stuck ``dm.search``).
    """
    try:
        return max(0.0, float(os.environ.get("RAG_FEDERATED_TIMEOUT_SECONDS", "60")))
    except (TypeError, ValueError):
        return 60.0


async def _afederated_bounded_call(sem: asyncio.Semaphore, coro_fn: Any, *args: Any) -> Any:
    """Run ONE fan-out coroutine under the concurrency bound + per-dataset deadline.

    Mirror of ``mcp_server._afederated_bounded_call`` (the D-mirror
    convention: both federated twins bound their fan-out identically).  The
    semaphore is held for the whole per-dataset search and released on every
    exit path (result, exception, cancellation); both env knobs are read
    inside so they stay per-call.

    The inner coroutine is wrapped in a TASK before the deadline is applied:
    with a bare coroutine, a ``TimeoutError`` raised INSIDE the search is
    indistinguishable from the deadline and would be mis-reported as a
    fan-out timeout.  With a task, a deadline that fires wins synchronously,
    so the settled state tells the two apart: ``done and not cancelled``
    means the inner error was already raised.

    An ``async def`` wrapper in both twins — callers simply ``await`` it.
    """

    async def _run() -> Any:
        async with sem:
            timeout = _federated_timeout_seconds()
            if timeout <= 0:
                return await coro_fn(*args)  # 0 = disabled: no wrap, no cancel
            task = asyncio.ensure_future(coro_fn(*args))
            try:
                return await asyncio.wait_for(task, timeout)
            except TimeoutError as exc:
                inner = task.exception() if (task.done() and not task.cancelled()) else None
                if inner is not None:
                    raise inner  # the inner timeout won the race; keep it as-is
                raise TimeoutError(f"timed out after {timeout:g}s") from exc
            finally:
                # Slot-release ordering (cross-validation P1-3; mirror of the
                # MCP twin): cancel fire-and-forget so the semaphore slot
                # frees as soon as the deadline fires, even if the abandoned
                # task suppresses its cancellation.  The task keeps running
                # to completion in the background (executor work cannot be
                # un-run); it holds no semaphore resources.
                if not task.done():
                    task.cancel()

    return await _run()


async def _federated_rerank_rag(dm: DatasetManager, targets: list[str]) -> Any:
    """Return the first target dataset's RAG that has a reranker configured.

    The reranker is server-wide (per-dataset model overrides are explicitly
    not planned), so any dataset's instance works for the merged pass.
    """
    loop = asyncio.get_running_loop()
    for name in targets:
        rag = await loop.run_in_executor(sync_pool, dm._get_rag, name)
        if getattr(rag, "reranker", None) is not None:
            return rag
    return None


async def _federated_rest_search(
    dm: DatasetManager,
    datasets: "str | list[Any] | tuple[Any, ...]",
    q: str,
    top_k: int = 5,
    use_reranker: bool = False,
    reranker_top_k: int = 3,
    filters: dict[str, Any] | None = None,
    is_unlocked: "Any" = None,
) -> dict[str, Any]:
    """Fan one query out over several datasets and merge the pools.

    Testable core behind ``POST /api/search`` (the endpoint only parses the
    request body and wires the per-client unlock predicate).  Per-dataset
    searches are the same sync ``dm.search`` calls the single-dataset
    endpoints use, offloaded to ``sync_pool`` and gathered concurrently
    (bounded by ``RAG_FEDERATED_CONCURRENCY``, timed per dataset by
    ``RAG_FEDERATED_TIMEOUT_SECONDS``); a failing or timed-out dataset
    becomes a per-dataset error note and never fails the
    call.  Merging labels every hit with its dataset, dedups on the
    dataset-qualified twin-identity key and sorts by score; *use_reranker*
    runs ONE rerank pass over the merged pool (the reranker is content-based,
    so cross-dataset pairs are fine).

    Password-protected datasets without a cached unlock for the caller are
    skipped with a note — no password is accepted here, mirroring the MCP
    federated tool (v3.0.0: passwords stay out of request/tool signatures;
    use ``POST /api/datasets/{name}/unlock`` first).
    """
    loop = asyncio.get_running_loop()
    # D15/D16: a registry-key identity's ACL ∪ self-selections restricts the
    # fan-out (skipped with a note, exactly like a password lock).
    # None = no enforcement.
    identity = _clients_registry.current_identity() if _clients_registry.registry_configured() else None
    allowed = None
    if identity is not None and not identity.is_admin:
        allowed = lambda name: _access_store.dataset_allowed(identity, name)
    try:
        targets, skipped, errors = await loop.run_in_executor(
            sync_pool, partial(resolve_federated_targets, dm, datasets, is_unlocked, allowed)
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc))

    if not targets:
        return {"query": q, "filters": filters, "results": [], "skipped": skipped, "errors": errors}

    async def _search_one(name: str) -> list[dict[str, Any]]:
        return await loop.run_in_executor(
            sync_pool,
            partial(
                dm.search,
                name,
                q,
                top_k=top_k,
                use_reranker=False,  # one rerank pass over the merged pool instead
                reranker_top_k=top_k,
                filters=filters,
            ),
        )

    # Bounded (RAG_FEDERATED_CONCURRENCY, default 8) and timed
    # (RAG_FEDERATED_TIMEOUT_SECONDS, default 60, 0=disabled) per dataset,
    # exactly like the MCP twin: without both, N targets meant N concurrent
    # searches and one hung dataset pinned the whole gather.  A deadline
    # surfaces as ``TimeoutError("timed out after Ns")`` below and takes the
    # same per-dataset error-note path as any other failure.
    sem = asyncio.Semaphore(_federated_concurrency())
    outcomes = await asyncio.gather(
        *(_afederated_bounded_call(sem, _search_one, name) for name in targets), return_exceptions=True
    )

    entries: list[tuple[str, Any, float]] = []
    extra_fields: dict[int, dict[str, Any]] = {}
    for name, outcome in zip(targets, outcomes):
        if isinstance(outcome, BaseException):
            errors.append({"dataset": name, "error": f"{type(outcome).__name__}: {outcome}"})
            continue
        for entry in outcome:
            doc = entry.get("content")
            extra = {k: v for k, v in entry.items() if k not in ("content", "score")}
            if extra:
                extra_fields[id(doc)] = extra
            entries.append((name, doc, float(entry.get("score", 0.0))))

    merged = merge_federated_results(entries)

    if use_reranker and merged:
        rerank_rag = await _federated_rerank_rag(dm, targets)
        if rerank_rag is None:
            logger.warning("Federated rerank requested but no target dataset has a reranker — keeping embedding order.")
        else:
            labelled: list[tuple[Any, float]] = []
            for name, doc, score in merged:
                d = dict(doc) if isinstance(doc, dict) else {"text": str(doc)}
                # The label rides on the doc through the sort/truncate; the
                # reranker input conversion reads only text + media keys, so
                # the private key never reaches the model.
                d["_federated_dataset"] = name
                labelled.append((d, score))
            ranked = await _arerank_with(rerank_rag.rank, rerank_rag._extract_doc, q, labelled, reranker_top_k)
            merged = [
                ((d.pop("_federated_dataset", "") if isinstance(d, dict) else ""), d, score) for d, score in ranked
            ]

    results: list[dict[str, Any]] = []
    for name, doc, score in merged:
        entry_out: dict[str, Any] = {"dataset": name}
        # "extra" above already names a rerank-path dict — distinct name here.
        extra_meta = extra_fields.get(id(doc))
        if extra_meta:
            entry_out.update(extra_meta)
        entry_out["content"] = doc
        entry_out["score"] = round(float(score), 4)
        if isinstance(doc, dict):
            if "_embedding_score" in doc and "embedding_score" not in entry_out:
                entry_out["embedding_score"] = doc["_embedding_score"]
            if "_reranker_score" in doc and "reranker_score" not in entry_out:
                entry_out["reranker_score"] = doc["_reranker_score"]
        results.append(entry_out)

    return {"query": q, "filters": filters, "results": results, "skipped": skipped, "errors": errors}


@app.post("/api/search")
async def api_search_federated(
    request: Request,
    body: dict[str, Any] = Body(...),
):
    """Search SEVERAL datasets with one query and merge the results.

    Request body::

        {
            "datasets": ["notes", "reports"] | "all",
            "q": "deployment runbook",
            "top_k": 5,
            "use_reranker": false,
            "reranker_top_k": 3,
            "filters": {"file_types": ["pdf"], "severities": ["ERROR"]}
        }

    ``datasets`` is a list of dataset names, a single name, or ``"all"`` —
    which expands to every dataset readable WITHOUT a password (no password
    set, or unlocked for this caller via ``POST /api/datasets/{name}/unlock``).

    ``filters`` is the same metadata-filter dict the per-dataset search
    endpoints accept (``file_types``, ``severities``, ``source_prefix``,
    ``date_from``/``date_to`` — AND-combined, applied in Qdrant before
    ranking) and is applied to EVERY dataset.

    ``top_k`` is PER DATASET (the merged pool holds at most
    ``len(datasets) * top_k`` hits); ``reranker_top_k`` truncates the merged
    pool after the single rerank pass.  Every returned hit carries the
    ``dataset`` it came from and keeps its per-dataset score breakdown.

    Password-protected datasets that are not unlocked for this caller are
    skipped and reported under ``skipped`` (federated search never accepts a
    password — unlock the dataset first).  A dataset that fails to search is
    reported under ``errors``; the other datasets still contribute results.
    """
    q = body.get("q")
    if not isinstance(q, str):
        raise HTTPException(400, "Field 'q' is required (string)")
    datasets = body.get("datasets")
    if datasets is None:
        raise HTTPException(400, "Field 'datasets' is required (list of names or \"all\")")
    if isinstance(datasets, str):
        pass  # a single name or "all" — resolved by _federated_rest_search
    elif not isinstance(datasets, (list, tuple)):
        raise HTTPException(400, "Field 'datasets' must be a list of dataset names or \"all\"")

    top_k = body.get("top_k", 5)
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 100:
        raise HTTPException(400, "Field 'top_k' must be an integer between 1 and 100")
    reranker_top_k = body.get("reranker_top_k", 3)
    if not isinstance(reranker_top_k, int) or isinstance(reranker_top_k, bool) or not 1 <= reranker_top_k <= 50:
        raise HTTPException(400, "Field 'reranker_top_k' must be an integer between 1 and 50")
    use_reranker = bool(body.get("use_reranker", False))

    raw_filters = body.get("filters")
    if raw_filters is not None and not isinstance(raw_filters, dict):
        raise HTTPException(400, "Field 'filters' must be an object")
    from multimodal_rag.vector_store import build_payload_filter

    try:
        build_payload_filter(raw_filters)  # validation pass (dates)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    dm = await get_manager_async()
    cid = _unlock_client_id(request)

    return await _federated_rest_search(
        dm,
        datasets,
        q,
        top_k=top_k,
        use_reranker=use_reranker,
        reranker_top_k=reranker_top_k,
        filters=raw_filters,
        is_unlocked=lambda name: _unlock_cache_get(name, cid) is not None,
    )


# -- Documents list ----------------------------------------------------------


@app.get("/api/datasets/{name}/documents")
async def api_list_documents(
    name: str,
    request: Request,
    limit: int = Query(50, ge=1, le=1000),
    cursor: str | None = Query(
        None, description="Opaque continuation token from a previous page's next_cursor ('' = start of listing)"
    ),
    x_dataset_password: str | None = Header(None, alias="X-Dataset-Password"),
):
    """List stored document payloads in a dataset.

    Additive cursor pagination (Wave-4): with no ``cursor`` parameter the
    response is exactly the historical first-page listing.  Passing
    ``cursor`` (empty string to start, or a previous ``next_cursor``)
    returns ``{"documents": [...], "next_cursor": ...}`` and resumes at the
    token instead of re-reading the collection from the top.
    """
    dm = await get_manager_async()
    try:
        await _require_dataset_password(dm, name, x_dataset_password, request)
        loop = asyncio.get_running_loop()
        if cursor is None:
            docs = await loop.run_in_executor(sync_pool, lambda: dm.list_documents(name, limit, check_embedder=False))
            _meta = await loop.run_in_executor(sync_pool, dm.get_dataset, name, False)
            total = _meta.get("document_count", 0)
            entries = [{"id": doc_id, "payload": payload} for doc_id, payload in docs]
            return {"documents": entries, "count": total}
        try:
            # Pass cursor through verbatim: "" is the documented "start of
            # listing" token — coercing it to None (cursor or None) flips
            # list_documents to its list return shape and 500s below.
            page = await loop.run_in_executor(
                sync_pool, lambda: dm.list_documents(name, limit, cursor, check_embedder=False)
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        # list_documents' shared signature returns list|dict; with a cursor the
        # page dict is the live shape (the None-cursor list path ran above).
        page = cast("dict[str, Any]", page)
        entries = [{"id": doc_id, "payload": payload} for doc_id, payload in page["documents"]]
        return {"documents": entries, "next_cursor": page["next_cursor"]}
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")


@app.delete("/api/datasets/{name}/documents/{doc_id}")
async def api_delete_document(
    name: str,
    doc_id: str,
    request: Request,
    x_dataset_password: str | None = Header(None, alias="X-Dataset-Password"),
):
    """Delete a single document from a dataset by its point ID."""
    dm = await get_manager_async()
    loop = asyncio.get_running_loop()
    try:
        await _require_dataset_password(dm, name, x_dataset_password, request)
        await loop.run_in_executor(sync_pool, dm.get_dataset, name, False)
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")
    try:
        dm = await get_manager_async()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(sync_pool, dm.delete_document, name, doc_id)
        return {"status": "ok", "deleted": doc_id}
    except Exception as e:
        raise HTTPException(500, str(e))


class _TarStreamSink(io.RawIOBase):
    """Non-seekable sink that accumulates tar stream chunks for HTTP streaming."""

    def __init__(self) -> None:
        self._buf = io.BytesIO()
        self._chunks: list[bytes] = []

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        self._buf.write(data)
        if self._buf.tell() >= (1 << 20):  # flush ~1 MiB at a time
            self._chunks.append(self._buf.getvalue())
            self._buf = io.BytesIO()
        return len(data)

    def drain(self) -> list[bytes]:
        if self._buf.tell():
            self._chunks.append(self._buf.getvalue())
            self._buf = io.BytesIO()
        out, self._chunks = self._chunks, []
        return out


def _dataset_export_stream(dm: DatasetManager, name: str):
    """Yield gzipped tar chunks for a full dataset backup.

    Includes ``meta.json`` (password hash stripped), ``documents.jsonl``
    (every Qdrant point as ``{"id", "payload"}`` NDJSON, streamed via
    paginated scroll) and ``files/`` (all on-disk files referenced by the
    dataset).  Streams to the response without buffering the whole archive
    in memory.
    """
    sink = _TarStreamSink()
    with tarfile.open(fileobj=sink, mode="w|gz") as tar:

        def _add_bytes(arcname: str, data: bytes) -> Iterator[bytes]:
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            yield from sink.drain()

        meta = dm._read_meta(name) or {}
        meta.pop("password_hash", None)
        yield from _add_bytes("meta.json", json.dumps(meta, default=str).encode("utf-8"))

        # Documents: write to a temp file to avoid holding huge collections
        # in memory, then add the file to the archive.
        fd, tmp = tempfile.mkstemp(suffix=".jsonl")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:

                def _emit(doc: dict[str, Any]) -> None:
                    fh.write(json.dumps(doc, default=str))
                    fh.write("\n")

                dm.stream_all_documents(name, _emit, check_embedder=False)
            with open(tmp, "rb") as rf:
                info = tarfile.TarInfo("documents.jsonl")
                info.size = os.fstat(rf.fileno()).st_size
                tar.addfile(info, rf)
            yield from sink.drain()
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

        files_dir = dm._dataset_dir(name) / "files"
        if files_dir.is_dir():
            for f in sorted(files_dir.rglob("*")):
                if not f.is_file() or f.name.startswith(".") or f.name == ".hashes.json":
                    continue
                arcname = "files/" + f.relative_to(files_dir).as_posix()
                tar.add(str(f), arcname=arcname)
                yield from sink.drain()

    yield from sink.drain()


@app.get("/api/datasets/{name}/export")
async def api_export_dataset(
    name: str,
    request: Request,
    password: str = Query(""),
    x_dataset_password: str | None = Header(None, alias="X-Dataset-Password"),
):
    """Download a complete dataset backup as a ``.tar.gz``.

    Contains ``meta.json`` (password hash stripped), ``documents.jsonl``
    (every Qdrant document as JSON Lines: ``{"id", "payload"}``) and
    ``files/`` (all on-disk files).  Password-protected datasets require the
    ``X-Dataset-Password`` header or ``?password=`` query param.  Restore by
    re-adding the documents (``POST /documents``) and files
    (``POST /batch-files``), or a fresh ingest from ``files/``.
    """
    dm = await get_manager_async()
    loop = asyncio.get_running_loop()
    try:
        await _require_dataset_password(dm, name, x_dataset_password or None or password or None, request)
        await loop.run_in_executor(sync_pool, dm.get_dataset, name, False)
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")

    # Resolve the collection BEFORE the response starts: once the stream
    # begins the headers are already sent, and a failure inside the generator
    # would surface to the client as an empty 200 (Starlette's "response
    # already started").  Payload reads bypass the embedder guard — export
    # embeds nothing; re-embedding happens on the import side.
    def _probe_collection() -> None:
        rag = dm._get_rag(name, check_embedder=False)
        vs = rag.vector_store
        if vs is None or isinstance(vs, dict):
            return
        vs._client.scroll(vs.collection_name, limit=1)  # type: ignore[attr-defined]

    await loop.run_in_executor(sync_pool, _probe_collection)

    # Generate the (blocking) tar stream in a worker thread so the event
    # loop stays free, yielding chunks to the response as they are produced.
    loop = asyncio.get_running_loop()
    iterator = iter(_dataset_export_stream(dm, name))

    async def _stream():
        while True:
            try:
                chunk = await loop.run_in_executor(sync_pool, partial(next, iterator))
            except StopIteration:
                break
            yield chunk

    return StreamingResponse(
        _stream(),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{name}.tar.gz"',
        },
    )


def _documents_download_stream(dm: DatasetManager, name: str, fmt: str):
    """Yield a documents-only download for *name* — no binary files.

    ``fmt="jsonl"``: one JSON object per line — ``{"id", "text", "metadata"}``
    — full fidelity for scripts/re-import.  ``fmt="md"``: human-readable
    Markdown (source/page heading + text), convenient for reading a dataset
    such as long-term memories.  Heavy tier-3 base64 media keys are stripped
    (same policy as search results); tier-2 ``preprocessed_*`` file refs are
    kept so media stays locatable.  Paginated Qdrant scroll — no memory
    blow-up on large collections.
    """
    rag = dm._get_rag(name, check_embedder=False)
    vs = rag.vector_store
    if vs is None or isinstance(vs, dict):
        return
    client = vs._client  # type: ignore[attr-defined]
    coll = vs.collection_name  # type: ignore[attr-defined]

    def _clean_meta(meta: dict[str, Any]) -> dict[str, Any]:
        out = dict(meta or {})
        for modality in ("image", "video"):
            val = out.get(modality)
            heavy = isinstance(val, str) and val.startswith("data:")
            if not heavy and isinstance(val, list):
                heavy = any(isinstance(v, str) and v.startswith("data:") for v in val)
            if heavy:
                out.pop(modality, None)
        return out

    def _render(doc: dict[str, Any]) -> str:
        payload = doc.get("payload") or {}
        meta = _clean_meta(payload.get("metadata") or {})
        text = payload.get("page_content", "") or ""
        if fmt == "jsonl":
            return json.dumps(
                {"id": doc.get("id"), "text": text, "metadata": meta},
                default=str,
                ensure_ascii=False,
            )
        # Markdown: heading from source/title, then the text, then media refs
        heading = meta.get("source") or meta.get("title") or doc.get("id", "")
        extras = []
        if meta.get("page") not in (None, ""):
            extras.append(f"page {meta['page']}")
        if meta.get("kind"):
            extras.append(str(meta["kind"]))
        if extras:
            heading = f"{heading} ({', '.join(extras)})"
        lines = [f"## {heading}", "", text if text else "_(no text)_"]
        for modality in ("image", "video", "audio"):
            ref = meta.get(modality) or meta.get(f"preprocessed_{modality}")
            if isinstance(ref, str) and ref:
                lines.extend(["", f"- {modality}: `{ref}`"])
        return "\n".join(lines)

    offset = None
    while True:
        pts, offset = client.scroll(
            coll,
            limit=500,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for pt in pts:
            yield (_render({"id": str(pt.id), "payload": pt.payload}) + "\n").encode("utf-8")
        if offset is None:
            break


@app.get("/api/datasets/{name}/documents/download")
async def api_download_documents(
    name: str,
    request: Request,
    format: str = Query("md", pattern="^(md|jsonl)$"),
    password: str = Query(""),
    x_dataset_password: str | None = Header(None, alias="X-Dataset-Password"),
):
    """Download every document in a dataset as a single file (no binaries).

    ``format=md`` (default): human-readable Markdown — one ``##`` section per
    document with source/page and the text; ideal for reading a memories
    dataset.  ``format=jsonl``: ``{"id", "text", "metadata"}`` per line for
    scripts/re-import.  Password-protected datasets require the
    ``X-Dataset-Password`` header or ``?password=``.
    """
    dm = await get_manager_async()
    loop = asyncio.get_running_loop()
    try:
        await _require_dataset_password(dm, name, x_dataset_password or password or None, request)
        await loop.run_in_executor(sync_pool, dm.get_dataset, name, False)
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")

    fmt = "jsonl" if format == "jsonl" else "md"
    iterator = iter(_documents_download_stream(dm, name, fmt))

    async def _stream():
        while True:
            try:
                chunk = await loop.run_in_executor(sync_pool, partial(next, iterator))
            except StopIteration:
                break
            yield chunk

    media_type = "application/x-ndjson" if fmt == "jsonl" else "text/markdown; charset=utf-8"
    return StreamingResponse(
        _stream(),
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{name}-documents.{fmt}"',
        },
    )


# -- File serving -----------------------------------------------------------


@app.get("/api/datasets/{name}/files/{filepath:path}")
async def api_serve_file(
    name: str,
    filepath: str,
    request: Request,
    password: str = Query(""),
    token: str = Query(""),
    t: str = Query("", description="Alias for ``token`` (accepts ``?t=``)"),
    x_dataset_password: str | None = Header(None, alias="X-Dataset-Password"),
):
    """Serve a stored file from a dataset's files directory.

    Accepts the password via the ``X-Dataset-Password`` header, the
    ``password`` query parameter (for ``<img>/<video>/<audio>`` tags which
    cannot set custom headers), or a short-lived HMAC ``token`` minted by
    the MCP server when ``MEDIA_TOKEN_SECRET`` is configured.  The media
    token is also accepted via ``?t=`` — LLM-generated markdown occasionally
    truncates ``token`` to ``t``, and verifying the short alias keeps those
    links working.
    """
    if not token and t:
        token = t
    dm = await get_manager_async()
    if token:
        # A media token fully authorises this specific file — no password check.
        if not _verify_media_token(name, filepath, token):
            raise HTTPException(403, "Invalid or expired media token")
    else:
        try:
            await _require_dataset_password(dm, name, password or x_dataset_password or None, request)
        except FileNotFoundError:
            raise HTTPException(404, f"Dataset '{name}' not found")

    from urllib.parse import quote

    from fastapi.responses import FileResponse

    files_dir = (dm._dataset_dir(name) / "files").resolve()
    file_path = (files_dir / filepath).resolve()
    # Resolve to prevent directory traversal (e.g. "../../etc/passwd")
    try:
        file_path.relative_to(files_dir)
    except ValueError:
        raise HTTPException(403, "Invalid file path")
    if not file_path.is_file():
        # Short human-readable filename (uuid prefix stripped by the MCP
        # server): find the stored ``{uuid}_{filepath}`` file.  A dataset
        # file normally has exactly one uuid-prefixed copy; if several
        # exist (re-uploaded copies), serve the most recently modified one.
        import glob as _glob

        candidates = _glob.glob(str(files_dir / f"*_{filepath}"))
        candidates = [c for c in candidates if os.path.isfile(c) and os.path.basename(c) != filepath]
        if not candidates:
            raise HTTPException(404, "File not found")
        file_path = Path(max(candidates, key=os.path.getmtime))
        try:
            file_path.relative_to(files_dir)
        except ValueError:
            raise HTTPException(403, "Invalid file path")

    # Only evergreen media MIME types may render inline.  SVG (image/svg+xml),
    # HTML, JSON, etc. are forced to `attachment` so a crafted uploaded file
    # cannot execute script in the origin when a frontend embeds it.
    media_type, _ = mimetypes.guess_type(str(file_path))
    inline = (
        media_type is not None
        and media_type.split("/")[0] in ("image", "video", "audio")
        and media_type != "image/svg+xml"
    )
    headers: dict[str, str] = {}
    if not inline:
        safe_name = quote(file_path.name, safe="")
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{safe_name}"
    # Derived media (`*_preprocessed`, `*_segment_*`, `*_truncated`) can be
    # regenerated at any time by a repair pass or another replica's
    # preprocessing — the bytes under a given URL are NOT immutable.  Without
    # an explicit policy, browsers apply heuristic caching and will keep
    # serving stale (historically: corrupt, pre-repair) bytes for days
    # without ever revalidating the etag.  `no-cache` forces conditional
    # revalidation (etag/last-modified are already sent by FileResponse), so
    # every client sees regenerated bytes as soon as they exist — at the cost
    # of one 304-round-trip per reuse.  Originals are immutable (content-hash
    # names), so normal caching stays.
    stem_matches = file_path.stem.endswith("_preprocessed") or file_path.stem.endswith("_truncated")
    if stem_matches or "_segment_" in file_path.name:
        headers["Cache-Control"] = "no-cache"
    return FileResponse(str(file_path), headers=headers)


# -- Staging (transient media handoff for MCP tools) -------------------------


# Cap on multipart upload bytes (dataset file uploads + staging).  The stream
# loop aborts past this so a key-holder cannot fill the disk.  0 disables.
_MAX_UPLOAD_BYTES = max(0, int(os.environ.get("MAX_UPLOAD_BYTES", str(1024 * 1024 * 1024))))


async def _stream_upload(file: StarletteUploadFile, dest: Any, max_bytes: int = _MAX_UPLOAD_BYTES) -> int:
    """Stream an UploadFile to *dest*, aborting once *max_bytes* is exceeded.

    Returns the number of bytes written.
    """
    written = 0
    while chunk := await file.read(1 << 20):  # 1 MiB
        written += len(chunk)
        if max_bytes > 0 and written > max_bytes:
            raise HTTPException(413, f"Upload exceeds MAX_UPLOAD_BYTES ({max_bytes} bytes)")
        dest.write(chunk)
    return written


_STAGING_TTL = int(os.environ.get("STAGING_TTL", "3600"))  # seconds, default 1h
# Fraction of staging requests that trigger a background sweep (0..1).
# Default 0.1 ≈ 1 in 10 uploads. 0 disables sweeping entirely.
_STAGING_SWEEP_RATE = float(os.environ.get("STAGING_SWEEP_RATE", "0.1"))
# Guards against overlapping sweeps; only one sweep runs at a time.
_sweep_lock = threading.Lock()


def _staging_root() -> Path:
    """Return (creating if needed) the staging directory under DATA_PATH."""
    # Read DATA_PATH from the environment on every call — the CLI may set it
    # after this module is imported (see main()), and a module-level constant
    # would silently point at the wrong mount.
    d = Path(os.environ.get("DATA_PATH", "/data")) / "staging"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sweep_staging() -> None:
    """Remove staging subdirectories older than ``_STAGING_TTL`` seconds.

    Designed to run off the request path (see ``_maybe_sweep_staging``):
    the directory is flat (one subdir per staged file) so iteration is
    O(uploads), and every filesystem error is suppressed so a sweep never
    raises into the caller.
    """
    base = Path(os.environ.get("DATA_PATH", "/data")) / "staging"
    if not base.exists():
        return
    cutoff = time.time() - _STAGING_TTL
    for sub in base.iterdir():
        try:
            if not sub.is_dir():
                continue
            if sub.stat().st_mtime < cutoff:
                for f in sub.iterdir():
                    try:
                        f.unlink()
                    except Exception:
                        logger.debug("staging sweep: unlink failed", exc_info=True)
                try:
                    sub.rmdir()
                except Exception:
                    logger.debug("staging sweep: rmdir failed", exc_info=True)
        except Exception:
            logger.debug("staging sweep: failed on %s", sub, exc_info=True)


def _try_sweep_staging() -> None:
    """Run ``_sweep_staging`` only if no other sweep is in progress.

    Acquires ``_sweep_lock`` non-blocking: if a sweep is already running,
    returns immediately.  Any exception is logged and swallowed so the
    executor thread never propagates a failure to a fire-and-forget caller.
    """
    if not _sweep_lock.acquire(blocking=False):
        return
    try:
        _sweep_staging()
    except Exception:
        logger.debug("staging sweep: unexpected error", exc_info=True)
    finally:
        _sweep_lock.release()


async def _maybe_sweep_staging() -> None:
    """Probabilistically trigger a background sweep without blocking.

    With probability ``_STAGING_SWEEP_RATE`` (default 0.1), schedules
    ``_try_sweep_staging`` on ``sync_pool`` and returns immediately —
    fire-and-forget — so the staging request is not delayed by cleanup.
    Concurrent sweeps are skipped via ``_sweep_lock``.
    """
    if _STAGING_SWEEP_RATE <= 0:
        return
    if random.random() >= _STAGING_SWEEP_RATE:
        return
    loop = asyncio.get_running_loop()
    loop.run_in_executor(sync_pool, _try_sweep_staging)


def _is_valid_staging_id(staging_id: str) -> bool:
    """A staging id is a 32-char lowercase hex uuid4."""
    return len(staging_id) == 32 and all(c in "0123456789abcdef" for c in staging_id)


def _preprocess_staged_file(dest: Path) -> Path:
    """Downscale staged media to the embedder's native processing caps.

    Reads ``mm_processor_kwargs`` (``max_pixels``, ``fps``) from the
    embedder config so the staged query file is sized to what the embedder
    will actually process — no wasted bytes, no payload-size rejections.
    A 37 MB phone photo becomes a ≤720×720 JPEG (a few hundred KB); a
    high-bitrate video is capped at the embedder's ``fps`` and per-frame
    pixel budget before it ever reaches the embedding endpoint.

    Returns the path to the (possibly new) preprocessed file.  When a new
    ``*_preprocessed`` sibling is produced, the original is removed and
    the preprocessed file is moved into its place so the staged URL and
    filename stay clean.  Audio and non-media files are returned
    unchanged.
    """
    from multimodal_rag.dataset_manager import (
        _classify_file,
        _preprocess_image_file,
        _preprocess_video_file,
        _truncate_audio_file,
    )

    mpk = _get_embedder_mm_kwargs()
    max_pixels = mpk.get("max_pixels", 720 * 720)
    fps = mpk.get("fps", 1.0)

    file_type = _classify_file(dest.name)
    try:
        if file_type == "image":
            result = _preprocess_image_file(dest, max_pixels=max_pixels)
        elif file_type == "video":
            result = _preprocess_video_file(dest, max_pixels=max_pixels, max_fps=fps)
        elif file_type == "audio":
            result = _truncate_audio_file(dest, max_seconds=60.0)
        else:
            return dest
    except Exception:
        logger.warning("staging preprocess failed for %s", dest.name, exc_info=True)
        return dest

    if result == dest:
        return dest  # no resize needed

    # Replace the original with the preprocessed file so the staged URL
    # and filename are unchanged.
    try:
        dest.unlink(missing_ok=True)
        result.replace(dest)
        return dest
    except Exception:
        logger.warning("staging preprocess replace failed; using sibling", exc_info=True)
        return result


# Cached embedder mm_processor_kwargs — resolved once from env vars, no
# endpoint probing.  Used by _preprocess_staged_file to size staged media
# to the embedder's native processing caps.
_embedder_mm_kwargs: dict[str, Any] | None = None


def _get_embedder_mm_kwargs() -> dict[str, Any]:
    """Return the embedder's ``mm_processor_kwargs`` (cached).

    Builds the embedder config from environment variables via
    :func:`build_embedder` — this reads ``MODEL_EMBEDDER_EXTRA`` /
    ``MODEL_EMBEDDER_NAME`` etc. and never invokes the embedding API,
    so it is safe to call at staging time before the DatasetManager is
    initialised.  If ``MODEL_EMBEDDER_NAME`` is omitted the model id is
    auto-discovered via a best-effort ``GET /v1/models`` probe (harmless
    to fail).  Falls back to the default Qwen3-VL kwargs when the
    embedder env vars are not set.
    """
    global _embedder_mm_kwargs
    if _embedder_mm_kwargs is not None:
        return _embedder_mm_kwargs

    from multimodal_rag.model_config import _DEFAULT_MM_KWARGS, build_embedder

    emb = build_embedder()
    if emb is not None and emb.mm_processor_kwargs:
        _embedder_mm_kwargs = dict(emb.mm_processor_kwargs)
    else:
        _embedder_mm_kwargs = dict(_DEFAULT_MM_KWARGS)
    logger.info("staging preprocess caps: %s", _embedder_mm_kwargs)
    return _embedder_mm_kwargs


@app.post("/api/staging")
async def api_staging_upload(
    request: Request,
    file: UploadFile = File(...),
):
    """Stage a transient media file so an MCP tool can consume it.

    Used by the Open WebUI filter to hand uploaded images / video / audio
    to the RAG MCP ``search_dataset`` tool **without** injecting base64
    data into the LLM context.  The filter uploads the raw bytes here and
    injects only the returned short URL as a hint; the LLM then calls
    ``search_dataset(image=...)`` (etc.) with that URL, and the MCP
    server — which shares this pod's PVC — reads the file directly from
    disk via its ``file://`` path.

    Files are written under ``DATA_PATH/staging/{uuid}/{filename}`` and
    swept after ``STAGING_TTL`` seconds (default 1h).  Sweeping runs
    probabilistically in the background (see ``_maybe_sweep_staging``)
    and never blocks an upload.
    """
    await _maybe_sweep_staging()

    # Sanitise the filename: keep only the basename (no path traversal).
    raw_name = file.filename or "upload"
    safe_name = Path(raw_name).name or "upload"
    if safe_name in (".", ".."):
        safe_name = "upload"

    staging_id = uuid.uuid4().hex
    sub = _staging_root() / staging_id
    try:
        sub.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        # uuid4 collision — astronomically unlikely; retry once.
        staging_id = uuid.uuid4().hex
        sub = _staging_root() / staging_id
        sub.mkdir(parents=True, exist_ok=False)

    dest = sub / safe_name
    try:
        with open(dest, "wb") as out:
            await _stream_upload(file, out)
    except Exception as exc:
        try:
            dest.unlink(missing_ok=True)
            sub.rmdir()
        except Exception:
            logger.debug("staging upload: cleanup failed", exc_info=True)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(500, f"Failed to stage uploaded file: {exc}")

    # Downscale oversized staged media so the MCP search_dataset tool
    # doesn't hand a multi-megabyte file to the embedder (which rejects
    # payloads above its size cap — e.g. the 37 MB phone photo that
    # triggered "Maximum file size exceeded").  Reuses the same PVC
    # preprocessing helpers as ingest so the staged query is bounded by
    # the same pixel / fps caps the system was designed to embed.
    # Runs in the sync thread pool because PIL / ffmpeg are blocking.
    raw_size = dest.stat().st_size
    try:
        dest = await asyncio.get_running_loop().run_in_executor(sync_pool, _preprocess_staged_file, dest)
    except Exception:
        logger.warning("staging preprocess failed for %s", safe_name, exc_info=True)
    preprocessed_size = dest.stat().st_size
    if preprocessed_size != raw_size:
        logger.info(
            "staging preprocess: %s %.1f MB → %.1f MB",
            safe_name,
            raw_size / (1 << 20),
            preprocessed_size / (1 << 20),
        )
    elif raw_size > 5 * (1 << 20):  # > 5 MB and not shrunk — suspicious
        logger.warning(
            "staging preprocess NO-OP: %s is %.1f MB but was not shrunk "
            "(PIL/ffmpeg missing, file type unsupported, or already within caps)",
            safe_name,
            raw_size / (1 << 20),
        )

    # The MCP server is a sidecar in the same pod and shares this PVC,
    # so a file:// URL is directly resolvable by it (no HTTP hop, no auth).
    file_url = f"file://{dest.resolve()}"

    # Also provide an HTTP URL for clients that cannot read the PVC
    # (e.g. an MCP server deployed as a separate pod).  Uses localhost
    # because, in the standard helm chart, the API and MCP containers
    # share a network namespace.
    port = request.url.port or 8000
    http_url = f"http://localhost:{port}/api/staging/{staging_id}"

    return {
        "id": staging_id,
        "filename": safe_name,
        "file_url": file_url,
        "http_url": http_url,
        "size_bytes": dest.stat().st_size,
        "ttl_seconds": _STAGING_TTL,
    }


@app.get("/api/staging/{staging_id}")
async def api_staging_serve(staging_id: str):
    """Serve a previously staged file (one file per staging id).

    Primarily intended for MCP deployments where the server cannot read
    the shared PVC and must fetch over HTTP.  Staged files are swept in
    the background by uploads (see ``_maybe_sweep_staging``) and after
    ``STAGING_TTL`` seconds; this endpoint does not block on sweeping.
    """
    if not _is_valid_staging_id(staging_id):
        raise HTTPException(400, "Invalid staging id")
    sub = Path(os.environ.get("DATA_PATH", "/data")) / "staging" / staging_id
    if not sub.is_dir():
        raise HTTPException(404, "Staged file not found or expired")
    # Ignore leftover "_preprocessed" siblings (only produced when the atomic
    # replace failed) and pick a deterministic file.
    files = sorted(
        f for f in sub.iterdir() if f.is_file() and not f.name.endswith("_preprocessed") and not f.name.startswith(".")
    )
    if not files:
        raise HTTPException(404, "Staged file not found or expired")
    target = files[0]
    media_type, _ = mimetypes.guess_type(target.name)
    from urllib.parse import quote

    from fastapi.responses import FileResponse

    # Non-media types (HTML, SVG, JSON, …) are forced to `attachment` so a
    # crafted staged file cannot render/execute in the origin — mirrors the
    # dataset file-serving endpoint.
    inline = (
        media_type is not None
        and media_type.split("/")[0] in ("image", "video", "audio")
        and media_type != "image/svg+xml"
    )
    headers: dict[str, str] = {}
    if not inline:
        safe_name = quote(target.name, safe="")
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{safe_name}"
    return FileResponse(str(target), media_type=media_type or "application/octet-stream", headers=headers)


# -- Admin / Management ------------------------------------------------------


# Module-level state for CPU% calculation (delta between calls)
_last_cpu_time: float = 0.0
_last_wall_time: float = 0.0


def _read_cgroup_memory_limit() -> int:
    """Read the cgroup v2/v1 memory limit (in bytes). Returns -1 if unknown."""
    for path in (
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    ):
        try:
            val = int(Path(path).read_text().strip())
            if val > 0 and val < (1 << 62):  # "max" or huge value = no limit
                return val
        except Exception:
            logger.debug("Suppressed exception", exc_info=True)
    return -1


def _read_cpu_limit() -> float:
    """Read the cgroup CPU limit in cores. Returns os.cpu_count() as fallback."""
    # cgroup v2: "400000 100000" → 4 cores
    try:
        parts = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if parts[0] == "max":
            return float(os.cpu_count() or 1)
        return int(parts[0]) / int(parts[1])
    except Exception:
        logger.debug("Suppressed exception", exc_info=True)
    # cgroup v1
    try:
        quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text().strip())
        period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text().strip())
        if quota > 0 and period > 0:
            return quota / period
    except Exception:
        logger.debug("Suppressed exception", exc_info=True)
    return float(os.cpu_count() or 1)


@app.get("/api/admin/health")
async def api_health_stats() -> dict[str, Any]:
    """Lightweight health stats — polled every 10s by the management page.

    Returns CPU%, memory (RSS vs cgroup limit), file PVC disk usage,
    and Qdrant connection status + total points.
    """
    # The blocking work (disk_usage, Qdrant HTTP calls, /proc reads) runs in
    # the sync pool so this frequently-polled endpoint never stalls the
    # event loop (which would also delay health/readiness probes).
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(sync_pool, _collect_health_stats)


_SIZE_UNIT_MAP = {
    "k": 1024,
    "kb": 1024,
    "ki": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mi": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gi": 1024**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "ti": 1024**4,
    "tib": 1024**4,
}


def _parse_size_bytes(raw: str) -> int | None:
    """Parse a Kubernetes-style size string ("100Gi", "25Mi") into bytes.

    Returns None when the value is empty or unparseable.
    """
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$", raw.strip().lower())
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2)
    mult = _SIZE_UNIT_MAP.get(unit) if unit else 1
    if mult is None:
        return None
    return int(val * mult)


def _peer_host_label(uri: str) -> str:
    """Best-effort human label for a Qdrant peer URI (e.g. ``qdrant-0``)."""
    try:
        host = urllib.parse.urlparse(uri).hostname or ""
    except Exception:
        host = ""
    if not host:
        return uri
    label = host.split(".", 1)[0]
    return label or uri


# Per-replica disk usage from Qdrant telemetry.  Fetched once per
# ``_QDRANT_TELEMETRY_TTL`` seconds because /telemetry with full detail is a
# relatively heavy call (per-segment stats for every collection) and the
# health endpoint is polled every 10s.
_QDRANT_TELEMETRY_CACHE: dict[str, Any] = {"ts": 0.0, "usage": {}}
_QDRANT_TELEMETRY_TTL = 60.0


def _qdrant_cluster_replicas() -> "list[dict[str, Any]]":
    """Per-replica shard placement from Qdrant's /cluster API (the same
    discovery /api/admin/health performs inline), ordered by pod ordinal.
    Best-effort: [] on any failure (cluster disabled, unreachable)."""
    import httpx2

    qhost = os.environ.get("QDRANT_HOST", "")
    qport = os.environ.get("QDRANT_PORT", "6333")
    if not qhost:
        return []
    try:
        with httpx2.Client(timeout=5.0) as client:
            resp = client.get(f"http://{qhost}:{qport}/cluster")
            if resp.status_code != 200:
                return []
            result = resp.json().get("result") or {}
            if result.get("status") != "enabled":
                return []
            peers = result.get("peers") or {}
            peer_shards: dict[str, int] = {}
            for cinfo in (
                (result.get("collections") or {}).values() if isinstance(result.get("collections"), dict) else []
            ):
                if not isinstance(cinfo, dict):
                    continue
                for sh in cinfo.get("shards") or []:
                    if not isinstance(sh, dict):
                        continue
                    pid = sh.get("peer_id")
                    if pid is not None:
                        key = str(pid)
                        peer_shards[key] = peer_shards.get(key, 0) + 1
            replicas: list[dict[str, Any]] = []
            for pid in sorted(peers.keys(), key=lambda p: str(p)):
                ps = peer_shards.get(str(pid), 0)
                peer = peers[pid] or {}
                uri = peer.get("uri", "") if isinstance(peer, dict) else ""
                label = _peer_host_label(uri)
                if not label or label == uri:
                    label = f"peer-{pid}"
                replicas.append({"host": label, "shards": ps, "uri": uri})
            replicas.sort(key=lambda r: int(m.group(1)) if (m := re.search(r"(\d+)$", r["host"])) else -1)
            return replicas
    except Exception:
        logger.debug("Suppressed exception", exc_info=True)
        return []


def _qdrant_replica_usage(
    replicas: list[dict[str, Any]],
    qhost: str,
    qport: str,
) -> dict[str, int]:
    """Best-effort used bytes per replica host from Qdrant telemetry.

    Each node's telemetry lists only its own ``local`` shard segments; sum
    ``indexed_vectors_size`` + ``payload_data_size`` per segment.  Returns
    ``{host: used_bytes}`` for the replicas that answered; failures and nodes
    without detail telemetry are simply omitted.
    """
    now = time.time()
    if now - _QDRANT_TELEMETRY_CACHE["ts"] <= _QDRANT_TELEMETRY_TTL:
        return dict(_QDRANT_TELEMETRY_CACHE["usage"])
    try:
        import httpx2

        usage: dict[str, int] = {}
        for r in replicas:
            host = r.get("host", "")
            if not host:
                continue
            try:
                with httpx2.Client(timeout=10.0) as client:
                    # details_level=6 (full) exposes per-shard local storage.
                    resp = client.get(f"http://{host}.{qhost}:{qport}/telemetry?details_level=6")
                    resp.raise_for_status()
                    res = resp.json().get("result") or {}
                    cols = res.get("collections") or {}
                    inner = cols.get("collections") if isinstance(cols, dict) else None
                    items = inner.values() if isinstance(inner, dict) else (inner if isinstance(inner, list) else [])
                    total = 0
                    for cinfo in items:
                        if not isinstance(cinfo, dict):
                            continue
                        for sh in cinfo.get("shards") or []:
                            if not isinstance(sh, dict):
                                continue
                            local = sh.get("local")
                            if isinstance(local, dict):
                                total += int(local.get("vectors_size_bytes") or 0)
                                total += int(local.get("payloads_size_bytes") or 0)
                    usage[host] = total
            except Exception:
                logger.debug("Suppressed exception", exc_info=True)
        _QDRANT_TELEMETRY_CACHE.update({"ts": now, "usage": usage})
        return dict(usage)
    except Exception:
        logger.debug("Suppressed exception", exc_info=True)
        return dict(_QDRANT_TELEMETRY_CACHE.get("usage") or {})


def _collect_health_stats() -> dict[str, Any]:
    """Synchronous helper that does the actual work for /api/admin/health."""
    global _last_cpu_time, _last_wall_time

    import shutil

    # -- CPU (delta from last call) -------------------------------------------
    rusage = resource.getrusage(resource.RUSAGE_SELF)
    cpu_seconds = rusage.ru_utime + rusage.ru_stime
    now = time.monotonic()
    cpu_limit = _read_cpu_limit()
    if _last_wall_time > 0:
        delta_cpu = cpu_seconds - _last_cpu_time
        delta_wall = now - _last_wall_time
        cpu_percent = round(min(delta_cpu / delta_wall / cpu_limit * 100, 100.0), 1) if delta_wall > 0 else 0.0
    else:
        cpu_percent = 0.0
    _last_cpu_time = cpu_seconds
    _last_wall_time = now

    # -- Memory ----------------------------------------------------------------
    rss_kb = rusage.ru_maxrss  # max RSS in KB (Linux)
    # Also read current VmRSS from /proc for live value
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                rss_kb = int(line.split()[1])
                break
    except Exception:
        logger.debug("Suppressed exception", exc_info=True)
    rss_bytes = rss_kb * 1024
    mem_limit = _read_cgroup_memory_limit()
    mem_percent = round(rss_bytes / mem_limit * 100, 1) if mem_limit > 0 else 0.0

    # -- File PVC disk ----------------------------------------------------------
    dm = get_manager()
    usage = shutil.disk_usage(str(dm.base_path))

    # -- Qdrant status + total points ------------------------------------------
    qdrant_status = "unknown"
    qdrant_collections = 0
    qdrant_total_points = 0
    try:
        import httpx2

        with httpx2.Client(timeout=5.0) as client:
            qhost = os.environ.get("QDRANT_HOST", "")
            qport = os.environ.get("QDRANT_PORT", "6333")
            if qhost:
                base = f"http://{qhost}:{qport}"
                resp = client.get(f"{base}/collections")
                if resp.status_code == 200:
                    qdrant_status = "ok"
                    cols = resp.json()["result"]["collections"]
                    qdrant_collections = len(cols)
                    for col in cols:
                        info = client.get(f"{base}/collections/{col['name']}").json()["result"]
                        qdrant_total_points += info.get("points_count", 0)
    except Exception:
        qdrant_status = "unreachable"

    # -- Qdrant PVC disk usage (only when mounted read-only) -------------------
    # The Qdrant PVC is a separate volume from the file PVC; Qdrant's HTTP API
    # does not expose on-disk usage, so we read it from the read-only mount
    # configured in the Helm chart (env QDRANT_STORAGE_PATH).
    qdrant_pvc: dict[str, Any] | None = None
    if QDRANT_STORAGE_PATH and Path(QDRANT_STORAGE_PATH).exists():
        try:
            q_usage = shutil.disk_usage(QDRANT_STORAGE_PATH)
            qdrant_pvc = {
                "total_bytes": q_usage.total,
                "used_bytes": q_usage.used,
                "free_bytes": q_usage.free,
                "used_percent": round(q_usage.used / q_usage.total * 100, 1),
            }
        except Exception:
            logger.debug("Suppressed exception", exc_info=True)

    # -- Qdrant cluster (per-replica shard placement + configured capacity) ----
    # Qdrant does not expose on-disk usage per node, so for a sharded cluster
    # (one RWO PVC per replica, none mounted on the API pod) we surface the
    # per-replica shard placement from the cluster API plus the configured
    # per-replica PVC size (QDRANT_PVC_SIZE) so operators can gauge storage
    # spread without exec/kubectl.
    cluster_info: dict[str, Any] | None = None
    try:
        import httpx2

        with httpx2.Client(timeout=5.0) as client:
            qhost = os.environ.get("QDRANT_HOST", "")
            qport = os.environ.get("QDRANT_PORT", "6333")
            if qhost:
                resp = client.get(f"http://{qhost}:{qport}/cluster")
                if resp.status_code == 200:
                    result = resp.json().get("result") or {}
                    if result.get("status") != "enabled":
                        cluster_info = {
                            "enabled": False,
                            "replicas": [],
                            "total_shards": 0,
                            "per_replica_capacity_bytes": _parse_size_bytes(QDRANT_PVC_SIZE),
                        }
                    else:
                        peers = result.get("peers") or {}
                        # Shard placement comes from the per-collection
                        # /collections/{name}/cluster endpoint — the /cluster
                        # response's "collections" map is empty in practice.
                        peer_shards: dict[str, int] = {}
                        base = f"http://{qhost}:{qport}"
                        try:
                            coll_resp = client.get(f"{base}/collections")
                            col_names = (coll_resp.json().get("result") or {}).get("collections") or []
                            for col in col_names:
                                name = col.get("name") if isinstance(col, dict) else None
                                if not name:
                                    continue
                                cc = client.get(f"{base}/collections/{name}/cluster")
                                c_res = cc.json().get("result") or {}
                                for sh in c_res.get("local_shards") or []:
                                    key = str(c_res.get("peer_id", ""))
                                    peer_shards[key] = peer_shards.get(key, 0) + 1
                                for sh in c_res.get("remote_shards") or []:
                                    pid = sh.get("peer_id") if isinstance(sh, dict) else None
                                    if pid is not None:
                                        key = str(pid)
                                        peer_shards[key] = peer_shards.get(key, 0) + 1
                        except Exception:
                            logger.debug("Suppressed exception", exc_info=True)
                        replicas: list[dict[str, Any]] = []
                        for pid in sorted(peers.keys(), key=lambda p: str(p)):
                            ps = peer_shards.get(str(pid), 0)
                            peer = peers[pid] or {}
                            uri = peer.get("uri", "") if isinstance(peer, dict) else ""
                            label = _peer_host_label(uri)
                            if not label or label == uri:
                                label = f"peer-{pid}"
                            replicas.append({"host": label, "shards": ps})
                        # Order by pod ordinal (qdrant-0, qdrant-1, qdrant-2) —
                        # raft peer ids are assigned in join order and are
                        # unrelated to the StatefulSet ordinals.
                        replicas.sort(key=lambda r: int(m.group(1)) if (m := re.search(r"(\d+)$", r["host"])) else -1)
                        for _i, _r in enumerate(replicas):
                            _r["index"] = _i
                        # Per-replica disk usage from Qdrant telemetry (needs
                        # QDRANT__TELEMETRY_DETAIL_LEVEL=full).  Each node only
                        # reports its OWN local shard segments, so fetch each
                        # peer's telemetry directly.  Sum of segment vector +
                        # payload sizes ≈ used bytes (excludes WAL/meta).
                        if replicas:
                            _usage = _qdrant_replica_usage(replicas, qhost, qport)
                            for _r in replicas:
                                _u = _usage.get(_r["host"])
                                if _u is not None:
                                    _r["used_bytes"] = _u
                        cluster_info = {
                            "enabled": True,
                            "replicas": replicas,
                            "total_shards": sum(peer_shards.values()),
                            "per_replica_capacity_bytes": _parse_size_bytes(QDRANT_PVC_SIZE),
                        }
    except Exception:
        logger.debug("Suppressed exception", exc_info=True)

    return {
        "cpu": {
            "percent": cpu_percent,
            "cores": cpu_limit,
            "limit_millicores": int(cpu_limit * 1000),
        },
        "memory": {
            "rss_bytes": rss_bytes,
            "limit_bytes": mem_limit,
            "percent": mem_percent,
        },
        "file_pvc": {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_percent": round(usage.used / usage.total * 100, 1),
        },
        "qdrant": {
            "status": qdrant_status,
            "collections": qdrant_collections,
            "total_points": qdrant_total_points,
            "pvc": qdrant_pvc,
            "cluster": cluster_info,
        },
        "models": {
            "embedder": {
                "status": _model_health["embedder"]["status"],
                "last_check": _model_health["embedder"]["last_check"],
                "error": _model_health["embedder"]["error"],
            }
        },
    }


@app.get("/api/admin/storage")
async def api_storage_stats() -> dict[str, Any]:
    """Return PVC disk usage and dataset statistics."""
    dm = await get_manager_async()

    # Run the potentially slow stats collection in a thread pool so the
    # event loop (and health/readiness probes) are not blocked.
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(sync_pool, _build_storage_stats, dm)


def _build_storage_stats(dm: DatasetManager) -> dict[str, Any]:
    """Synchronous helper that does the actual work for /api/admin/storage."""
    import shutil

    # PVC disk usage
    usage = shutil.disk_usage(str(dm.base_path))
    pvc = {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_percent": round(usage.used / usage.total * 100, 1),
    }

    # File-type extension → label mapping
    _FT_MAP: dict[str, str] = {
        ".jpg": "image",
        ".jpeg": "image",
        ".png": "image",
        ".gif": "image",
        ".bmp": "image",
        ".webp": "image",
        ".tiff": "image",
        ".mp4": "video",
        ".mkv": "video",
        ".avi": "video",
        ".mov": "video",
        ".webm": "video",
        ".m4v": "video",
        ".mp3": "audio",
        ".wav": "audio",
        ".flac": "audio",
        ".ogg": "audio",
        ".m4a": "audio",
        ".wma": "audio",
        ".pdf": "pdf",
        ".txt": "text",
        ".md": "text",
        ".py": "code",
        ".js": "code",
        ".ts": "code",
        ".java": "code",
        ".cpp": "code",
        ".c": "code",
        ".go": "code",
        ".rs": "code",
        ".csv": "table",
        ".tsv": "table",
        ".xlsx": "table",
        ".xls": "table",
        ".docx": "office",
        ".pptx": "office",
        ".odt": "office",
        ".odp": "office",
        ".html": "html",
        ".htm": "html",
        ".xml": "xml",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".ipynb": "notebook",
        ".epub": "ebook",
        ".zip": "archive",
        ".tar": "archive",
        ".gz": "archive",
    }

    # Dataset-level stats
    datasets = dm.list_datasets()
    ds_stats = []
    total_docs = 0
    for ds in datasets:
        name = ds["name"]
        doc_count = ds.get("document_count", 0)
        total_docs += doc_count

        # File storage size + type breakdown
        files_dir = dm._dataset_dir(name) / "files"
        file_bytes = 0
        file_types: dict[str, int] = {}
        if files_dir.exists():
            for f in files_dir.iterdir():
                if f.is_file() and not f.name.startswith("."):
                    sz = f.stat().st_size
                    file_bytes += sz
                    ext = f.suffix.lower()
                    label = _FT_MAP.get(ext, "other")
                    file_types[label] = file_types.get(label, 0) + sz

        # Document counts per file type — backfill from Qdrant when missing.
        # Uses paginated scroll with a small batch size so large collections
        # (8k+ points) don't spike memory or block.
        ft_counts: dict[str, int] = ds.get("file_type_counts", {}) or {}
        if not ft_counts and doc_count > 0 and dm._rag_cache.get(name):
            try:
                rag_cached = dm._rag_cache[name]
                vs = rag_cached.vector_store
                if vs is not None and not isinstance(vs, dict):
                    client = vs._client  # type: ignore[attr-defined]
                    coll = vs.collection_name  # type: ignore[attr-defined]
                    from qdrant_client.models import PayloadSelectorInclude

                    _SCROLL_BATCH = 256
                    _MAX_SCAN = 50000
                    offset = None
                    scanned = 0
                    while scanned < _MAX_SCAN:
                        scroll = client.scroll(
                            coll,
                            limit=_SCROLL_BATCH,
                            offset=offset,
                            with_payload=PayloadSelectorInclude(include=["metadata.source"]),
                            with_vectors=False,
                        )
                        points = scroll[0]
                        if not points:
                            break
                        for pt in points:
                            md = (pt.payload or {}).get("metadata") or {}
                            src = str(md.get("source") or "") if isinstance(md, dict) else ""
                            ext = Path(src).suffix.lower()
                            label = _FT_MAP.get(ext, "other")
                            ft_counts[label] = ft_counts.get(label, 0) + 1
                        scanned += len(points)
                        offset = scroll[1]
                        if offset is None:
                            break
                    if ft_counts:
                        ds["file_type_counts"] = ft_counts
            except Exception:
                logger.debug("Suppressed exception", exc_info=True)

        ds_stats.append(
            {
                "name": name,
                "documents": doc_count,
                "file_bytes": file_bytes,
                "file_types": file_types,
                "file_type_counts": ft_counts,
                "has_password": ds.get("has_password", False),
            }
        )

    return {
        "pvc": pvc,
        "datasets": ds_stats,
        "total_datasets": len(datasets),
        "total_documents": total_docs,
    }


@app.get("/api/admin/upload-history")
async def api_upload_history(
    dataset: str | None = Query(None),
    limit: int = Query(50, ge=1, le=2000),
) -> dict[str, Any]:
    """List persisted upload history, newest first.

    Optional ``dataset`` filters to a single dataset.  ``limit`` caps the
    number of entries returned (default 50, max 2000).
    """
    entries = _load_upload_history()
    if dataset:
        entries = [e for e in entries if e.get("dataset") == dataset]
    entries = sorted(entries, key=lambda e: str(e.get("timestamp", "")), reverse=True)
    return {"history": entries[:limit], "count": len(entries[:limit])}


@app.delete("/api/admin/upload-history")
async def api_clear_upload_history(dataset: str | None = Query(None)) -> dict[str, Any]:
    """Clear persisted upload history, optionally for a single dataset."""
    lock_path = _upload_history_path().with_suffix(".lock")
    with _cross_process_lock(lock_path):
        if dataset is None:
            removed = len(_load_upload_history())
            p = _upload_history_path()
            if p.exists():
                os.unlink(p)
            return {"status": "ok", "removed": removed}
        entries = _load_upload_history()
        remaining = [e for e in entries if e.get("dataset") != dataset]
        removed = len(entries) - len(remaining)
        if removed:
            _save_upload_history(remaining)
        return {"status": "ok", "removed": removed}


@app.post("/api/admin/datasets/{name}/public")
async def api_admin_set_dataset_public(
    name: str,
    request: Request,
    body: dict[str, Any] = Body(...),
):
    """Set a dataset's "public" flag (feature: public-to-all-keys).

    ADMIN-ONLY: the middleware 403s registry-client keys on /api/admin/*
    and the D20 anonymous identity alike - only deployment keys reach this
    handler.  Body: {"public": true|false}.  A password-protected dataset
    is refused with 409 (remove the password first if publication is
    really intended).  Takes effect on the next request from any replica
    (query-time only - no re-ingest, no restart).

    D23: a NON-admin identity can publish/unpublish ONLY a dataset it
    created (``created_by`` == its identity name) — via the parallel
    creator route ``POST /api/datasets/{name}/public`` (this /api/admin
    route is unreachable for client keys by construction).  A pre-D23
    dataset (no stamp) can never be made public by a non-admin.
    """
    enabled = bool(body.get("public"))
    dm = await get_manager_async()
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(sync_pool, dm.set_public, name, enabled)
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    logger.info("Dataset '%s' public flag set to %s (admin)", name, result["public"])
    return {"status": "ok", **result}


@app.post("/api/datasets/{name}/public")
async def api_set_dataset_public_creator(
    name: str,
    request: Request,
    body: dict[str, Any] = Body(...),
):
    """Set a dataset's "public" flag — the D23 CREATOR path.

    A NON-admin identity may publish/unpublish ONLY a dataset it created
    (meta.json ``created_by`` == the caller's identity name; admins are not
    expected here — their surface is /api/admin/datasets/{name}/public, and
    an admin reaching this route is allowed with the same effect).
    Non-creators (including the D20 anonymous identity and pre-D23 datasets
    with no stamp) are 403ed by the ownership gate — no provable creator,
    no self-service publication.  Body: {"public": true|false}.  The
    password-protected-never-public rule is enforced underneath
    (``set_public`` → 409).  Takes effect on the next request from any
    replica (query-time only).
    """
    dm = await get_manager_async()
    _identity_may_manage_dataset(dm, name)
    enabled = bool(body.get("public"))
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(sync_pool, dm.set_public, name, enabled)
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    logger.info("Dataset '%s' public flag set to %s (creator)", name, result["public"])
    return {"status": "ok", **result}


@app.post("/api/admin/datasets/{name}/migrate-tier-schema")
async def api_migrate_tier_schema(
    name: str,
    request: Request,
    x_dataset_password: str | None = Header(None, alias="X-Dataset-Password"),
) -> dict[str, Any]:
    """Migrate a dataset's Qdrant points to the three-tier media schema.

    Renames ``original_video`` → ``preprocessed_video``, derives tier-1
    ``original_*`` paths, and converts ``image``/``video`` ``file://`` refs
    to tier-3 base64 data URLs.  Idempotent.

    Password-protected datasets require the ``X-Dataset-Password`` header
    (or a cached unlock) — this rewrites the collection's payloads.
    """
    dm = await get_manager_async()
    await _require_dataset_password(dm, name, x_dataset_password, request)

    def _do_migrate() -> dict[str, Any]:
        return dm.migrate_tier_schema(name)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(sync_pool, _do_migrate)


@app.post("/api/admin/datasets/{name}/backfill-search-metadata")
async def api_backfill_search_metadata(
    name: str,
    request: Request,
    x_dataset_password: str | None = Header(None, alias="X-Dataset-Password"),
) -> dict[str, Any]:
    """Backfill ``metadata.file_type`` + search payload indexes on a dataset.

    Metadata-filtered search keys off ``metadata.file_type``, which only docs
    ingested after the feature carry.  This idempotent migration derives the
    file type from each stored point's ``metadata.source`` and creates the
    payload indexes (``file_type``/``severities``/``source`` keyword,
    ``timestamp_start`` datetime).  Points that already carry ``file_type``
    are untouched.  Safe to re-run (e.g. after ingesting into a dataset
    concurrently).

    Password-protected datasets require the ``X-Dataset-Password`` header
    (or a cached unlock) — this rewrites the collection's payloads.
    """
    dm = await get_manager_async()
    await _require_dataset_password(dm, name, x_dataset_password, request)

    def _do_backfill() -> dict[str, Any]:
        return dm.backfill_search_metadata(name)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(sync_pool, _do_backfill)


@app.post("/api/admin/datasets/{name}/recreate")
async def api_recreate_dataset(
    name: str,
    request: Request,
    x_dataset_password: str | None = Header(None, alias="X-Dataset-Password"),
) -> dict[str, Any]:
    """Rebuild a dataset's Qdrant collection from its on-disk files.

    Drops the existing collection (whose vectors were built with an older
    embedder) and re-ingests the dataset's original files with the
    currently configured embedder.  Returns a ``job_id`` immediately; poll
    ``GET /api/datasets/{name}/upload-status/{job_id}`` for progress.

    Password-protected datasets require the ``X-Dataset-Password`` header
    (or a cached unlock) — this drops and rebuilds the collection.
    """
    dm = await get_manager_async()
    loop = asyncio.get_running_loop()
    await _require_dataset_password(dm, name, x_dataset_password, request)
    try:
        await loop.run_in_executor(sync_pool, dm.get_dataset, name, False)
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")

    loop = asyncio.get_running_loop()
    file_entries = await loop.run_in_executor(sync_pool, dm._list_recreate_files, name)
    if not file_entries:
        raise HTTPException(409, f"Dataset '{name}' has no source files on disk to recreate from")

    job_id = _upload_jobs.create(name, len(file_entries), source="recreate")

    def _process() -> None:
        try:

            def cb(e):
                _upload_jobs.add_event(job_id, e)

            r = dm.recreate_dataset(name, file_entries, progress_callback=cb)
            _upload_jobs.complete(job_id, r)
        except Exception as exc:
            _upload_jobs.fail(job_id, str(exc))

    loop.run_in_executor(None, _process)

    return {"job_id": job_id, "status": "recreating", "total_files": len(file_entries)}


@app.post("/api/admin/datasets/{name}/repair-preprocessed")
async def api_repair_preprocessed(
    name: str,
    request: Request,
    dry_run: bool = False,
    x_dataset_password: str | None = Header(None, alias="X-Dataset-Password"),
) -> dict[str, Any]:
    """Regenerate broken ``*_preprocessed`` media files from their originals.

    Concurrent preprocessing of one file (multiple server replicas share the
    dataset PVC) can leave an interleaved, unplayable ``*_preprocessed`` file
    behind even though the original is intact — the classic symptom is a
    browser reporting "the video can't be played because the file is corrupt"
    while the dataset's search index is fine.  This scans the dataset's
    ``files/`` directory, validates every ``*_preprocessed`` file (ffprobe +
    MP4 atom walk for video/audio, full PIL decode for images) and re-runs
    preprocessing on the tier-1 original for each broken copy.

    Pass ``?dry_run=true`` to only report which files would be repaired.
    Synchronous: only broken files are re-transcoded, nothing is re-embedded,
    so the call returns the full repair report directly.
    """
    dm = await get_manager_async()
    await _require_dataset_password(dm, name, x_dataset_password, request)
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(sync_pool, dm.get_dataset, name, False)
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")
    return await loop.run_in_executor(sync_pool, dm.repair_preprocessed_media, name, dry_run)


@app.post("/api/admin/datasets/{name}/contextual-preview")
async def api_contextual_preview(
    name: str,
    request: Request,
    x_dataset_password: str | None = Header(None, alias="X-Dataset-Password"),
) -> dict[str, Any]:
    """Cost preview for enabling contextual retrieval on *name*.

    Returns a LABELED ESTIMATE: the live Qdrant point count (doubled for
    hybrid-capable collections — real-text chunks get text-only twins that
    carry the same context line) × one context call's token math (shared
    cached preamble + chunk input + ~80 output tokens) + the configured VLM
    model name.  Read-only, no side effects; surfaces as the confirm line
    next to the contextual checkbox in the dashboard before the flag flips.

    Password-protected datasets require the ``X-Dataset-Password`` header
    (it reveals per-dataset content scale).
    """
    dm = await get_manager_async()
    await _require_dataset_password(dm, name, x_dataset_password, request)
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(sync_pool, dm.contextualize_preview, name)
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")


@app.post("/api/admin/datasets/import")
async def api_import_dataset(request: Request) -> dict[str, Any]:
    """Restore a dataset from a ``GET /api/datasets/{name}/export`` backup.

    Accepts either a multipart upload (``file`` = the ``.tar.gz`` export;
    optional form fields ``new_name``, ``overwrite``, ``password``) or a JSON
    body ``{"s3_uri": "s3://bucket/backup.tar.gz", "overwrite": false,
    "new_name": "...", "password": "..."}`` — the backup CronJob writes
    exports to S3, so restores can pull straight from there.

    The export carries no vectors (``documents.jsonl`` is payloads only), so
    a restore **re-embeds**: datasets with ``files/`` go through the same
    recreate flow as an embedder swap (twins, captions and media tiers are
    rebuilt like a fresh ingest); raw-documents backups (no files) replay
    their ``documents.jsonl`` text through the embedder instead.

    Returns a ``job_id`` immediately — poll
    ``GET /api/datasets/{name}/upload-status/{job_id}`` for progress.  The
    restored dataset is unprotected unless ``password`` is provided (the
    exported ``meta.json`` strips the password hash by design).
    """
    dm = await get_manager_async()
    content_type = request.headers.get("content-type", "")
    tmp_path: Path | None = None
    completed = False
    try:
        loop = asyncio.get_running_loop()
        if "multipart/form-data" in content_type:
            form = await request.form()
            upload = form.get("file")
            if upload is None or isinstance(upload, str):
                raise HTTPException(400, "Multipart field 'file' with the export .tar.gz is required")
            raw_new_name = form.get("new_name")
            new_name = raw_new_name if isinstance(raw_new_name, str) and raw_new_name.strip() else None
            raw_overwrite = form.get("overwrite")
            overwrite = str(raw_overwrite or "").lower() in ("1", "true", "yes", "on")
            raw_password = form.get("password")
            password = raw_password if isinstance(raw_password, str) and raw_password else None
            fd, tmp_name = tempfile.mkstemp(suffix=".tar.gz")
            os.close(fd)
            tmp_path = Path(tmp_name)
            with open(tmp_path, "wb") as out:
                await _stream_upload(upload, out)
        elif "application/json" in content_type:
            body = await request.json()
            s3_uri = str(body.get("s3_uri") or "").strip()
            if not s3_uri:
                raise HTTPException(400, "JSON body must provide 's3_uri' (or upload multipart 'file')")
            if not s3_uri.startswith("s3://"):
                raise HTTPException(400, "s3_uri must be an s3:// URI (s3://bucket/key.tar.gz)")
            new_name = body.get("new_name") or None
            overwrite = bool(body.get("overwrite", False))
            password = body.get("password") or None
            from multimodal_rag.dataset_manager import _download_s3

            tmp_path = Path(await loop.run_in_executor(sync_pool, _download_s3, s3_uri))
        else:
            raise HTTPException(415, "Send multipart/form-data with a 'file' field, or JSON with 's3_uri'")

        # Password-gate overwrites of EXISTING datasets: prepare_import
        # DELETES the target when overwrite=true, and destructive routes are
        # password-gated (delete/recreate/migrate/backfill all are) — the
        # import path must not be a bypass.  The target name comes from
        # new_name or the archive's own meta.json, whichever prepare_import
        # will use; peek it before anything is touched.
        if overwrite:
            try:
                backup_meta = await loop.run_in_executor(sync_pool, dm.peek_backup_meta, tmp_path)
            except KeyError as exc:
                # Not an export archive — map to 400 (KeyError isn't a
                # ValueError, so the generic handler below would 500 it).
                raise HTTPException(400, f"Not a dataset export (missing {exc}): cannot determine the overwrite target")
            except TypeError as exc:
                raise HTTPException(400, str(exc))
            candidate = str(new_name or backup_meta.get("name") or "").strip()
            if candidate:
                dm._validate_name(candidate)
                # SECURITY: the fresh variant, not the TTL-cached check — a
                # pod that cached a stale negative (NFS visibility delay or
                # the 30s TTL from before the dataset was protected) would
                # skip this gate and replace a protected dataset without its
                # password (demonstrated on the 4-replica rollout).
                if await loop.run_in_executor(sync_pool, dm.has_password_fresh, candidate):
                    await _require_dataset_password(dm, candidate, password, request)

        plan = await loop.run_in_executor(
            sync_pool,
            partial(dm.prepare_import, tmp_path, new_name=new_name, overwrite=overwrite),
        )
        target = plan["dataset"]
        if password:
            await loop.run_in_executor(sync_pool, dm.set_password, target, password)

        expected = plan["file_count"] or plan["document_rows"] or 1
        job_id = _upload_jobs.create(target, expected, source="import")
        tar_for_job = str(tmp_path)

        def _process() -> None:
            try:
                if plan["mode"] == "re-embed-files":
                    result = dm.recreate_dataset(target)
                else:
                    result = dm.replay_imported_documents(target, tar_for_job)
                _upload_jobs.complete(job_id, result)
            except Exception as exc:
                _upload_jobs.fail(job_id, str(exc))
            finally:
                try:
                    os.unlink(tar_for_job)
                except OSError:
                    pass

        loop.run_in_executor(None, _process)
        completed = True
        return {
            "job_id": job_id,
            "status": "importing",
            "dataset": target,
            "mode": plan["mode"],
            "backup_was_protected": plan["had_password"],
            "password_restored": bool(password),
            "note": (
                f"Poll GET /api/datasets/{target}/upload-status/{job_id} — the export "
                "carries no vectors, so the restore re-embeds everything."
            ),
        }
    except HTTPException:
        raise
    except FileExistsError as exc:
        raise HTTPException(409, str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        if tmp_path is not None and not completed:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# HTML frontend
# ---------------------------------------------------------------------------


def _whoami_flags() -> dict:
    """The D23 ``flags`` object for an AUTHENTICATED whoami (snake_case).

    * ``can_create_datasets`` — matches the middleware's POST /api/datasets
      rule (``_rag_acl_path_denial``): the registry is NOT configured, or
      the identity carries the ``*`` grant, or it is an admin.
    * ``sso_enabled`` — the D22 browser SSO flow is fully configured.
    * ``memory_dataset`` — the caller's effective ★ binding or null (the
      same fail-soft resolution the MCP memory tools use).
    * ``self_mint`` (D25) — the deployment offers SSO self-mint (knob +
      overlay on).  The SPA shows the "Your API key" card from this flag;
      the endpoints re-check everything server-side per request.
    """
    identity = _clients_registry.current_identity()
    can_create = False
    if identity is None:
        # No identity bound: creation is governed by the D15 config — the
        # middleware's rule denies only when the registry is configured
        # without the ``*`` grant; with nothing configured there is no
        # registry gate (the D20 fail-closed branch binds an identity, so
        # None here means a genuinely legacy surface — creation allowed).
        can_create = not _clients_registry.registry_configured()
    elif identity.is_admin:
        can_create = True
    else:
        can_create = "*" in (identity.datasets or frozenset())
    memory = None
    try:
        memory = _access_store.memory_dataset_for(identity, None)
    except Exception:
        memory = None
    try:
        self_mint = bool(_admin_registry.self_mint_enabled())
    except Exception:
        self_mint = False
    try:
        from multimodal_rag.utils import oidc_sso

        sso_on = bool(oidc_sso.sso_enabled())
    except Exception:
        sso_on = False
    return {
        "can_create_datasets": bool(can_create),
        "sso_enabled": sso_on,
        "memory_dataset": memory,
        "self_mint": self_mint,
    }


def _oidc_session_payload(presented: "list[tuple[str, str]]") -> dict:
    """The D21/D22/D23 whoami body for *presented* (key, source) pairs.

    Never a grant: it previews the identity the presented credential WOULD
    resolve to (the auth middleware re-resolves on every protected call).
    Resolution order mirrors the middleware: an explicit opaque key wins
    (source: key — and an admin key NEVER leaks its status here, the payload
    stays anonymous-shaped), then JWT envelopes in collection order
    (Authorization Bearer, then the auth-proxy's forwarded access token,
    then the D22 SSO session cookie).  When OIDC is disabled the JWT path is
    inert, so the payload is the anonymous shape regardless.

    D23 additions to the AUTHENTICATED shape only: ``is_admin`` (true only
    for cookie/JWT identities that resolve as admin — which the resolver
    never yields for JWTs, so in practice always false here; an OPAQUE admin
    key keeps the anonymity rule and never reaches this branch) and
    ``flags`` (``can_create_datasets`` / ``sso_enabled`` /
    ``memory_dataset``).  The anonymous shape ``{"authenticated": false}``
    stays EXACTLY that — the SPA treats missing keys as false.
    """
    from multimodal_rag.utils import oidc_identity

    if not oidc_identity.oidc_enabled():
        return {"authenticated": False}
    # An explicit opaque key means the page's own credential governs — the
    # whoami stays anonymous-shaped (an admin key must never leak that it
    # is one, and the page already has its own key by definition).  The D22
    # SSO cookie is a JWT envelope, not an opaque key — it does NOT trigger
    # this bail-out.
    if any(
        via not in ("forwarded", "cookie", "proxy-identity") and not oidc_identity.is_jwt_format(candidate)
        for candidate, via in presented
    ):
        return {"authenticated": False}
    for candidate, via in presented:
        if not oidc_identity.is_jwt_format(candidate):
            continue
        ident = oidc_identity.resolve_jwt(candidate)
        if ident is None:
            continue  # invalid/expired/blocked — try the next envelope
        # D23: the whoami never binds the request's contextvar (it is a
        # preview, not a resolution), so the flags are computed against the
        # resolved identity directly.
        token = _clients_registry.set_current_identity(ident)
        try:
            flags = _whoami_flags()
        finally:
            _clients_registry.reset_current_identity(token)
        return {
            "authenticated": True,
            "identity": ident.name,
            "source": "sso-cookie" if via == "cookie" else "oidc",
            "datasets": sorted(ident.datasets or ()),
            "is_admin": bool(ident.is_admin),
            "flags": flags,
            "oidc": oidc_identity.oidc_status(),
        }
    # D25 v2 (the G2 edge case): behind an ENFORCING proxy
    # (RAG_TRUST_PROXY_IDENTITY) a request may carry ONLY the injected
    # identity headers — no JWT envelope, no key — yet the middleware still
    # resolves the D24 proxy-identity candidate to a real per-user identity.
    # The whoami must preview THAT too, or the SPA renders an SSO visitor as
    # an anonymous "API-key user" (the exact self-mint card hidden report,
    # 2026-10-02).  Mirror the middleware's D24 precedence position: it
    # engages only when no explicit credential resolved above.
    if _trust_proxy_identity():
        for candidate, via in presented:
            if via != "proxy-identity":
                continue
            name = str(candidate or "").strip()
            if not name or not _admin_registry.valid_name(name):
                continue
            grants = _clients_registry.dataset_acls().get(name, {})
            ident = _clients_registry.Identity(
                kind="client",
                name=name,
                datasets=frozenset(grants),
                grants=dict(grants),
            )
            token = _clients_registry.set_current_identity(ident)
            try:
                flags = _whoami_flags()
            finally:
                _clients_registry.reset_current_identity(token)
            return {
                "authenticated": True,
                "identity": name,
                "source": "proxy-identity",
                "datasets": sorted(grants),
                "grants": [{"name": n, "mode": m} for n, m in sorted(grants.items())],
                "is_admin": False,  # a proxy identity is never admin (D24)
                "flags": flags,
                "oidc": oidc_identity.oidc_status(),
            }
    return {"authenticated": False}


@app.get("/api/oidc-session")
async def api_oidc_session(request: Request):
    """D21 browser whoami — WHICH identity would this request resolve to?

    Public by design (it leaks nothing: the response is an identity-name
    preview plus the caller's own ACL datasets — exactly what the caller
    can already see via any authenticated listing; failure and absence both
    return the same anonymous shape, so it is not an oracle either).  The
    browser homepage calls this WITHOUT its embedded key first: when the
    auth proxy forwards the OIDC access token (or the session presents a
    Bearer JWT), the page renders the user's identity instead of
    embedding/using the admin key.
    """
    return _oidc_session_payload(_presented_pairs(request))


# ---------------------------------------------------------------------------
# Browser SSO — the OIDC authorization-code flow (D22, the OWUI pattern)
# ---------------------------------------------------------------------------


def _sso_cookie_headers(token: str, max_age: int, secure: bool) -> list[tuple[str, str]]:
    """Set-Cookie for the SSO session: HttpOnly (page JS never sees the
    token), SameSite=Lax (CSRF-hard default; the OIDC round trip is
    top-level navigation so Lax works), Secure on https deployments."""
    from multimodal_rag.utils import oidc_sso

    parts = [
        f"{oidc_sso.cookie_name()}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={max_age}",
    ]
    if secure:
        parts.append("Secure")
    return [("set-cookie", "; ".join(parts))]


def _sso_state_cookie_headers(state: str, secure: bool) -> list[tuple[str, str]]:
    """Set-Cookie for the login-attempt CSRF state (short-lived, HttpOnly,
    SameSite=Lax — read back at the callback and cleared immediately)."""
    from multimodal_rag.utils import oidc_sso

    parts = [
        f"{oidc_sso.state_cookie_name()}={state}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={600}",
    ]
    if secure:
        parts.append("Secure")
    return [("set-cookie", "; ".join(parts))]


def _sso_clear_cookie_headers() -> list[tuple[str, str]]:
    from multimodal_rag.utils import oidc_sso

    return [
        ("set-cookie", f"{oidc_sso.cookie_name()}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"),
        ("set-cookie", f"{oidc_sso.state_cookie_name()}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"),
    ]


@app.get("/oauth/login")
async def oauth_login(next: str = "/"):
    """Start the SSO round trip: redirect the browser to the realm's
    authorization endpoint with a CSRF ``state`` planted in a short-lived
    cookie.  404 when SSO is inert (the route does not exist then)."""
    from starlette.responses import RedirectResponse

    from multimodal_rag.utils import oidc_sso

    if not oidc_sso.sso_enabled():
        raise HTTPException(404, "SSO is not enabled on this deployment")
    url = oidc_sso.build_authorization_url(oidc_sso.new_state())
    if not url:
        raise HTTPException(503, "OIDC provider discovery unavailable — try again shortly")
    # The state cookie value is "<state>|<safe-next>" — the callback splits
    # it back (never trusting the query string for the redirect target).
    state = url.split("state=")[-1]
    target = oidc_sso.safe_next_path(next)
    resp = RedirectResponse(url, status_code=302)
    secure = oidc_sso.cookie_secure()
    parts = [
        f"{oidc_sso.state_cookie_name()}={state}|{target}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        "Max-Age=600",
    ]
    if secure:
        parts.append("Secure")
    resp.headers.append("set-cookie", "; ".join(parts))
    return resp


@app.get("/oauth/oidc/callback")
async def oauth_callback(request: Request, code: str = "", state: str = ""):
    """Finish the SSO round trip: exchange the code, verify the token with
    the D21 machinery, plant the session cookie, redirect to the ``next``
    path that rode INSIDE the state cookie (never the query string — an
    open redirect would launder the auth code).

    Every failure redirects to the homepage WITHOUT a cookie (fail closed,
    no error detail leaked)."""
    from starlette.responses import RedirectResponse

    from multimodal_rag.utils import oidc_sso

    if not oidc_sso.sso_enabled():
        raise HTTPException(404, "SSO is not enabled on this deployment")
    if not code or not state:
        return RedirectResponse("/?sso=error", status_code=302)
    # Split the state cookie: <nonce>|<safe-next>.
    raw_state = request.cookies.get(oidc_sso.state_cookie_name(), "")
    cookie_state, _, cookie_target = raw_state.partition("|")
    if not oidc_sso.state_matches(cookie_state, state):
        logger.warning("OIDC SSO callback: state mismatch (CSRF check failed)")
        return RedirectResponse("/?sso=error", status_code=302)
    token_response = oidc_sso.exchange_code(code)
    if not token_response:
        return RedirectResponse("/?sso=error", status_code=302)
    token = str(token_response.get("access_token") or "")
    if not token:
        return RedirectResponse("/?sso=error", status_code=302)
    # Verify with the D21 machinery — the same pipeline every credential
    # goes through (RS256/JWKS, iss/aud/exp).  An unverifiable token never
    # becomes a session.
    ident = _clients_registry.resolve_presented([token])
    if ident is None:
        logger.warning("OIDC SSO callback: token did not resolve to an identity (verification failed)")
        return RedirectResponse("/?sso=error", status_code=302)
    target = oidc_sso.safe_next_path(cookie_target or "/")
    resp = RedirectResponse(target, status_code=302)
    for header in _sso_cookie_headers(
        token,
        oidc_sso.cookie_max_age_for(token),
        oidc_sso.cookie_secure(),
    ):
        resp.headers.append(*header)
    # Clear the state cookie (its one job is done).
    resp.headers.append(
        "set-cookie",
        f"{oidc_sso.state_cookie_name()}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0",
    )
    logger.info("SSO sign-in: identity '%s' session established", ident.name)
    return resp


@app.get("/oauth/logged-out")
async def oauth_logged_out():
    """The post-sign-out landing page (D24): confirms BOTH sessions were
    ended (the gateway's oauth2-proxy session via /oauth2/sign_out, and the
    app-side D22 cookie via /oauth/logout) and offers the way back in.
    Public by construction — it renders for whoever lands here, including
    an edge-re-authenticated visitor (in which case the banner explains
    the SSO re-auth behavior honestly)."""
    return HTMLResponse(
        "<!DOCTYPE html><html><head><meta charset='UTF-8'>"
        "<title>Signed out — Multimodal RAG</title>"
        "<style>body{font-family:system-ui,sans-serif;background:#0b0f14;color:#e8e8e8;"
        "display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}"
        ".card{background:#11161d;border:1px solid #232a33;border-radius:8px;padding:32px 40px;"
        "max-width:420px;text-align:center}a{color:#2eb872}</style></head><body>"
        "<div class='card'><h2>Signed out</h2>"
        "<p style='font-size:.9rem;line-height:1.5'>Your app session and the gateway"
        " session were ended. Note: with platform single sign-on, clicking"
        " <b>Sign in with SSO</b> may sign you straight back in without a password —"
        " that is your identity provider's session, which ends when you sign out"
        " of the platform (or close all SSO apps).</p>"
        "<p style='margin-top:20px'><a href='/'>← Back to Multimodal RAG</a></p>"
        "</div></body></html>",
        status_code=200,
    )


@app.get("/oauth/logout")
async def oauth_logout():
    """Clear the SSO session cookies and land on the homepage."""
    from starlette.responses import RedirectResponse

    resp = RedirectResponse("/", status_code=302)
    for header in _sso_clear_cookie_headers():
        resp.headers.append(*header)
    return resp


def _load_html(template: str) -> str | None:
    """Load one template file from ``templates/`` (None when missing)."""
    path = Path(__file__).parent / "templates" / template
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


_HTML_INDEX: str | None = None


@app.get("/", response_class=HTMLResponse)
async def index(request: Request = None):  # type: ignore[assignment]  # direct callers (the /manage fallback, tests) pass no request — FastAPI forbids Optional[Request] route params
    global _HTML_INDEX
    # D23 auth gate (RAG_SSO_GATE): an unauthenticated request with no
    # credential of any kind gets the minimal sign-in page instead — health,
    # probes, oauth routes and media serving are exempt (they never enter
    # this handler).  Env off (the default) = byte-identical public pages.
    gate = _html_gate_response(request)
    if gate is not None:
        return gate
    if _HTML_INDEX is None:
        _HTML_INDEX = _load_html("index.html") or "<html><body><h1>Frontend not found</h1></body></html>"
    # Server-injected page configuration via meta tags: the OCR-fallback
    # default (RAG_OCR_DEFAULT, chart values rag.ocr) pre-checks the create
    # form's "OCR fallback" box; the weighted-RRF create default
    # (RAG_RRF_DEFAULT, chart values rag.rrfDefault) pre-fills the create
    # form's weight placeholders (absent → the page's own 1.0/1.0 defaults
    # show); the API key (when auth is on) lets the browser keep working —
    # the page is public by design and IS the auth boundary for browser
    # users; direct/scripted callers still send X-RAG-Api-Key themselves.
    page = _HTML_INDEX
    if RAG_OCR_DEFAULT:
        page = page.replace("</head>", '<meta name="rag-ocr-default" content="true"></head>', 1)
    if RAG_CONTEXTUAL_DEFAULT:
        # Contextual-retrieval create default (RAG_CONTEXTUAL_DEFAULT, chart
        # values rag.contextual): pre-checks the create form's "Contextual
        # retrieval" box.  Injected ONLY when enabled, so a disabled
        # deployment renders the byte-identical pre-feature page.
        page = page.replace("</head>", '<meta name="rag-contextual-default" content="true"></head>', 1)
    rrf_default_meta = _rrf_default_meta_from_env()
    if rrf_default_meta is not None:
        # "dense,sparse[,k]" — exactly the RAG_RRF_DEFAULT syntax the page's
        # placeholders mirror; attribute-escaped so a hand-edited env can
        # never break out of the meta tag.
        content = ",".join(
            str(rrf_default_meta.get(key))
            for key in ("dense_weight", "sparse_weight", "k")
            if rrf_default_meta.get(key) is not None
        )
        page = page.replace(
            "</head>",
            f'<meta name="rag-rrf-default" content="{html.escape(content, quote=True)}"></head>',
            1,
        )
    # The unlock endpoint's configured bounds, so the SPA's TTL selector can
    # match the server (and carry the deployment's no-expiry knob).  The
    # legacy /access page has always injected this meta; the D23 SPA (this
    # route) never did — so a RAG_UNLOCK_MAX_TTL=0 deployment showed the
    # "No expiry (0)" option DISABLED on the homepage's Unlock forms (found
    # live on G2, 2026-10-02).  Injected UNCONDITIONALLY (the SPA treats an
    # absent/NaN meta as bounded — injecting the actual value, whatever it
    # is, only ever ALIGNS the selector with the server).
    page = page.replace(
        "</head>",
        f'<meta name="rag-unlock-ttl-max" content="{_unlock_ttl_max()}"></head>',
        1,
    )
    # D21: when the request itself presents a resolvable JWT (browser SSO —
    # the auth proxy forwards the access token), DO NOT embed the admin key:
    # the page's auth header would outrank the forwarded JWT and escalate
    # every SSO visitor to admin.  The page probes /api/oidc-session first;
    # in SSO mode it sends no key and the forwarded JWT resolves per request.
    # A direct call (request=None — the /manage fallback, tests) has no SSO
    # context: presented is empty, the meta is embedded, history preserved.
    # D23 ratified behavior (audit 2026-10-02, P1-1): the embedded key is
    # the key-unconfigured LEGACY mode ONLY — the page is public, so an
    # embedded master key hands full admin to anything that can reach the
    # ClusterIP.  When multi-user key enforcement is active (D15/D17), the
    # SPA uses its key-entry / SSO flows instead; the page renders the
    # signed-out view and the SPA prompts for the key client-side.
    presented = _presented_pairs(request) if request is not None else []
    if (
        _RAG_API_KEY
        and not _clients_registry.registry_configured()
        and not _oidc_session_payload(presented).get("authenticated", False)
    ):
        page = page.replace(
            "</head>",
            f'<meta name="rag-api-key" content="{html.escape(_RAG_API_KEY, quote=True)}"></head>',
            1,
        )
    return page


@app.get("/favicon.png")
async def favicon():
    from fastapi.responses import FileResponse

    favicon_path = Path(__file__).parent / "templates" / "favicon.png"
    if favicon_path.exists():
        return FileResponse(favicon_path, media_type="image/png")
    return Response(status_code=404)


@app.get("/manage", response_class=HTMLResponse)
async def manage(request: Request = None):  # type: ignore[assignment]  # /manage forwards its own request; FastAPI forbids Optional[Request] route params
    """The management page — the same render as ``/`` (including the D23
    RAG_SSO_GATE gate: the request is forwarded so an unauthenticated
    gated deployment gets the sign-in page here too)."""
    return await index(request)


@app.get("/access", response_class=HTMLResponse)
async def access(request: Request = None):  # type: ignore[assignment]  # FastAPI forbids Optional[Request] route params
    """Per-user access page (the key-holder's counterpart to the operator UI).

    Served public like ``/`` and ``/manage`` — the page IS its own auth
    boundary: the user pastes their OWN API key into the page, which is then
    sent as ``X-RAG-Api-Key`` on every API call the page makes.  The server
    deliberately injects NO key meta tag here (unlike ``/``, which embeds the
    deployment key for its operator context): under D15 the page is used with
    per-user registry keys, and injecting any deployment key would leak
    admin credentials onto a public page.  See ``documentation/API.md``
    § "The /access page".

    D23 auth gate (RAG_SSO_GATE): an unauthenticated request with no
    credential gets the minimal sign-in page instead of the access UI.
    """
    gate = _html_gate_response(request)
    if gate is not None:
        return gate
    page = _load_html("access.html")
    if not page:
        return await index()
    # The unlock endpoint's configured bounds, so the page's TTL selector
    # can match the server (and carry the deployment's no-expiry knob).
    page = page.replace(
        "</head>",
        f'<meta name="rag-unlock-ttl-max" content="{_unlock_ttl_max()}"></head>',
        1,
    )
    return page


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Multimodal RAG API Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-path", default="/data")
    parser.add_argument("--qdrant-host", default="")
    parser.add_argument("--qdrant-port", type=int, default=6333)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    os.environ["DATA_PATH"] = args.data_path
    os.environ["QDRANT_HOST"] = args.qdrant_host
    os.environ["QDRANT_PORT"] = str(args.qdrant_port)
    os.environ["RAG_REMOTE"] = os.environ.get("RAG_REMOTE", "true")

    setup_logger(level=args.log_level)

    # Live autopsy hook: `kill -USR1 <pid>` dumps every thread's stack to the
    # container log — the definitive way to see where a hung request is
    # actually parked (pool exhaustion, blocked model call, deadlocked lock).
    import faulthandler
    import signal

    faulthandler.register(signal.SIGUSR1)
    logger.info("Stack-dump hook installed: kill -USR1 <pid> dumps all thread stacks")

    # Must share MEDIA_TOKEN_SECRET with the MCP server so protected media is
    # served via short-lived HMAC tokens (never a clear ?password= URL).
    if not os.environ.get("MEDIA_TOKEN_SECRET", ""):
        logger.error(
            "MEDIA_TOKEN_SECRET is required: media URLs are verified with a short-lived "
            "HMAC token (the legacy ?password= suffix was removed for security). "
            "Set the shared MEDIA_TOKEN_SECRET env var (helm: security.mediaTokenSecret)."
        )
        raise SystemExit(1)

    import uvicorn

    uvicorn.run(
        "multimodal_rag.api_server:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level.lower(),
    )


if __name__ == "__main__":
    main()
