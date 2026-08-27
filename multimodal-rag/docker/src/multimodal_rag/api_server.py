"""
FastAPI application for the multimodal RAG dataset manager.

Run with::

    uvicorn multimodal_rag.api_server:app --host 0.0.0.0 --port 8000

Or via Python::

    python -m multimodal_rag.api_server
"""

import argparse
import asyncio
import datetime
import io
import json
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
from functools import partial
from pathlib import Path
from typing import Any

import httpx
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

from multimodal_rag.dataset_manager import DatasetManager, EmbedderMismatchError, _cross_process_lock
from multimodal_rag.rag_system import _ingest_warnings
from multimodal_rag.utils.general_tools import sync_pool
from multimodal_rag.utils.logging_utils import logging, setup_logger

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Upload job tracker (thread-safe, poll-based progress)
# ---------------------------------------------------------------------------


class _UploadJobTracker:
    """Tracks background upload jobs for poll-based progress reporting.

    Replaces the previous SSE stream approach which tied upload lifecycle
    to a persistent HTTP connection.  With polling, a browser tab can be
    closed and reopened without losing progress tracking.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create(self, dataset_name: str, total_files: int, source: str = "files") -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._jobs[job_id] = {
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

    def complete(self, job_id: str, result: dict[str, Any] | None = None) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["status"] = "complete"
            job["result"] = result
            job["completed_at"] = time.time()

    def fail(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["status"] = "error"
            job["error"] = error
            job["completed_at"] = time.time()

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return dict(job)

    def cleanup_old(self, max_age_seconds: int = 3600) -> None:
        """Remove completed jobs older than *max_age_seconds*."""
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
    now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
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


def _unlock_client_id(request: Request) -> str:
    """Return a per-user identifier for the unlock cache.

    Prefers the authenticated user identity injected by oauth2-proxy
    (``X-Auth-Request-Email`` / ``X-Auth-Request-User``), then falls back
    to the socket peer — for deployments without an auth proxy.

    ``X-Forwarded-For`` is deliberately NOT used: it is client-supplied and
    spoofable, so trusting it would let a caller impersonate another user's
    unlock cache entry (and its cached plaintext password).
    """
    for header in ("X-Auth-Request-Email", "X-Auth-Request-User", "X-Email", "X-User"):
        val = request.headers.get(header)
        if val:
            return val.strip()
    client = request.client
    return client.host if client else "unknown"


def _unlock_cache_key(dataset: str, cid: str) -> str:
    return f"unlock:{dataset}:{cid}"


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
    if time.monotonic() < expiry:
        return cached_pw
    with _UNLOCK_CACHE_LOCK:
        _UNLOCK_CACHE.pop((dataset, cid), None)
    return None


def _unlock_cache_set(dataset: str, cid: str, password: str, ttl: int | None = None) -> None:
    r = _get_redis()
    if r is not None:
        try:
            r.set(_unlock_cache_key(dataset, cid), password, ex=(ttl if ttl is not None else _UNLOCK_TTL))
            return
        except Exception:
            pass
    with _UNLOCK_CACHE_LOCK:
        _UNLOCK_CACHE[(dataset, cid)] = (time.monotonic() + (ttl if ttl is not None else _UNLOCK_TTL), password)


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


def _require_dataset_password(
    dm: DatasetManager,
    name: str,
    password: str | None,
    request: Request | None = None,
) -> None:
    """Raise 401/403 if the dataset is password protected and the password
    is missing or incorrect.

    If *request* is provided, the unlock cache is also checked so that a
    previously-unlocked session can skip the password.
    """
    if not dm.has_password(name):
        return
    cid = _unlock_client_id(request) if request is not None else "unknown"

    # 1. If a password was supplied, verify and cache it
    if password:
        _check_pw_throttle(cid)
        if dm.verify_password(name, password):
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

app = FastAPI(
    title="Multimodal RAG Dataset Manager",
    version="1.0.0",
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

_PUBLIC_PATHS = frozenset({"/healthz", "/readyz", "/favicon.png", "/", "/manage"})

# Routes that must stay reachable without the API key, matched by the
# endpoint function name so future prefix-based routes are NOT silently
# exempted (the old string-prefix check made any `/api/datasets/*/files/*`
# or `/api/staging/*` route public, and `/api/staging/` with a trailing
# slash slipped past the exact-match exclusion).
_PUBLIC_ENDPOINT_NAMES = frozenset(
    {
        "healthz",
        "readyz",
        "favicon",
        "index",
        "manage",
        "api_serve_file",  # dataset media (password/token protected)
        "api_staging_serve",  # staged media (short-lived ids)
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


@app.middleware("http")
async def _api_key_auth(request: Request, call_next):
    if not _RAG_API_KEY:
        return await call_next(request)
    route = request.scope.get("route")
    endpoint = getattr(route, "endpoint", None) if route is not None else None
    if _is_public_path(request.url.path, endpoint):
        return await call_next(request)
    key = request.headers.get("X-RAG-Api-Key") or ""
    if not key:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            key = auth[len("Bearer ") :]
    import secrets

    if secrets.compare_digest(key, _RAG_API_KEY):
        return await call_next(request)
    return JSONResponse({"detail": "Missing or invalid API key"}, status_code=401)


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


@app.on_event("startup")
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
        async with httpx.AsyncClient(timeout=2.0) as client:
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

        {"name": "my-dataset", "description": "...", "caption_with_asr": false, "caption_with_vlm": false, "keep_originals": true, "password": "secret"}

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
    """
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(400, "Field 'name' is required")
    description = body.get("description", "")
    dm = await get_manager_async()
    # Defaults follow the server-wide config (env RAG_CAPTION_WITH_ASR /
    # RAG_CAPTION_WITH_VLM, set from the Helm chart); per-request values
    # in the body override them.
    caption_with_asr = body.get("caption_with_asr", dm.caption_with_asr)
    caption_with_vlm = body.get("caption_with_vlm", dm.caption_with_vlm)
    keep_originals = body.get("keep_originals", True)
    password = body.get("password") or None
    try:
        loop = asyncio.get_running_loop()
        meta = await loop.run_in_executor(
            sync_pool,
            dm.create_dataset,
            name,
            description,
            bool(caption_with_asr),
            bool(caption_with_vlm),
            bool(keep_originals),
            password,
        )
        return {"status": "ok", "dataset": meta}
    except FileExistsError as e:
        raise HTTPException(409, str(e))


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
        _require_dataset_password(dm, name, body.get("password", ""), request)
        return {"status": "ok", "verified": True}
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")
    except HTTPException:
        raise


@app.post("/api/datasets/{name}/unlock")
async def api_unlock_dataset(name: str, request: Request, body: dict[str, Any] = Body(...)):
    """Unlock a password-protected dataset for 30 minutes.

    Request body::

        {"password": "secret", "ttl": 1800}   # ttl in seconds (optional)

    Once unlocked, subsequent API calls to this dataset from the same
    client IP can omit the ``X-Dataset-Password`` header for the TTL
    duration.
    """
    dm = await get_manager_async()
    try:
        dm.get_dataset(name, sync_count=False)
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")

    if not dm.has_password(name):
        return {"status": "ok", "message": f"Dataset '{name}' is not password protected — nothing to unlock."}

    password = body.get("password", "")
    if not password:
        raise HTTPException(400, "Field 'password' is required")

    cid = _unlock_client_id(request)
    _check_pw_throttle(cid)
    if not dm.verify_password(name, password):
        _pw_record_failure(cid)
        raise HTTPException(403, f"Incorrect password for dataset '{name}'")
    _pw_reset_failures(cid)

    ttl = body.get("ttl", _UNLOCK_TTL)
    if not isinstance(ttl, int) or ttl < 60 or ttl > 86400:
        raise HTTPException(400, "TTL must be between 60 and 86400 seconds")

    # Use the shared cache writer so unlocked state is visible across API
    # replicas (Redis when configured, in-process otherwise).
    _unlock_cache_set_ttl(name, cid, password, ttl)

    return {
        "status": "ok",
        "message": f"Dataset '{name}' unlocked for {ttl // 60} minutes.",
        "ttl_seconds": ttl,
    }


@app.post("/api/datasets/{name}/lock")
async def api_lock_dataset(name: str, request: Request):
    """Immediately revoke the unlock for a dataset (if any)."""
    dm = await get_manager_async()
    try:
        dm.get_dataset(name, sync_count=False)
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")

    cid = _unlock_client_id(request)
    was = _unlock_cache_del(name, cid)

    if was:
        return {"status": "ok", "message": f"Dataset '{name}' locked."}
    return {"status": "ok", "message": f"Dataset '{name}' was not unlocked."}


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
        _require_dataset_password(dm, name, (x_password or password) or None, request)
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")
    return {"token": _sign_media_token(name, "*"), "ttl_seconds": _MEDIA_TOKEN_TTL}


@app.get("/api/datasets")
async def api_list_datasets(request: Request):
    """List all datasets with metadata."""
    dm = await get_manager_async()
    loop = asyncio.get_running_loop()
    datasets = await loop.run_in_executor(sync_pool, dm.list_datasets)
    cid = _unlock_client_id(request)
    with _UNLOCK_CACHE_LOCK:
        for ds in datasets:
            ds["unlocked"] = (ds["name"], cid) in _UNLOCK_CACHE
    return {"datasets": datasets}


@app.get("/api/datasets/{name}")
async def api_get_dataset(
    name: str,
    request: Request,
    x_dataset_password: str | None = Header(None, alias="X-Dataset-Password"),
):
    """Get metadata for a single dataset."""
    dm = await get_manager_async()
    try:
        _require_dataset_password(dm, name, x_dataset_password, request)
        loop = asyncio.get_running_loop()
        meta = await loop.run_in_executor(sync_pool, dm.get_dataset, name)
        return {"dataset": meta}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.patch("/api/datasets/{name}")
async def api_update_dataset(name: str, body: dict[str, Any] = Body(...)):
    """Update dataset metadata (e.g. description)."""
    dm = await get_manager_async()
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(sync_pool, dm.update_dataset, name, body)
        return {"status": "ok", "updated": name}
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")


@app.delete("/api/datasets/{name}")
async def api_delete_dataset(name: str):
    """Delete a dataset and its Qdrant collection."""
    try:
        dm = await get_manager_async()
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
    else:
        payload = [body]

    try:
        dm = await get_manager_async()
        _require_dataset_password(dm, name, x_dataset_password, request)
        loop = asyncio.get_running_loop()
        warnings: list[str] = []
        token = _ingest_warnings.set(warnings)
        try:
            ids = await loop.run_in_executor(sync_pool, dm.add_documents, name, payload)
        finally:
            _ingest_warnings.reset(token)
        resp = {"status": "ok", "stored_ids": ids, "count": len(ids)}
        if warnings:
            resp["warnings"] = warnings
        return resp
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")


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
    try:
        _require_dataset_password(dm, name, password or None, request)
        dm.get_dataset(name, sync_count=False)
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

        loop = asyncio.get_running_loop()
        warnings: list[str] = []
        token = _ingest_warnings.set(warnings)
        try:
            result = await loop.run_in_executor(sync_pool, dm.add_file, name, tmp_path, file.filename)
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
    try:
        _require_dataset_password(dm, name, password or None, request)
        dm.get_dataset(name, sync_count=False)
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

    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _process)
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
    """
    urls = body.get("urls", [])
    if not urls or not isinstance(urls, list):
        raise HTTPException(400, "Field 'urls' must be a non-empty array of URL strings")

    dm = await get_manager_async()
    try:
        _require_dataset_password(dm, name, x_dataset_password, request)
        dm.get_dataset(name, sync_count=False)
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")

    job_id = _upload_jobs.create(name, len(urls), source="urls")
    warnings: list[str] = []
    warnings_token = _ingest_warnings.set(warnings)

    def _process() -> None:
        try:

            def cb(e):
                _upload_jobs.add_event(job_id, e)

            r = dm.add_urls_batch(name, urls, progress_callback=cb)
            if warnings:
                r["warnings"] = warnings
            _upload_jobs.complete(job_id, r)
            _record_upload_history(name, r.get("files") or [], "urls")
        except Exception as exc:
            _upload_jobs.fail(job_id, str(exc))
            failed_files = [
                {"file": Path(url.split("?")[0].rstrip("/")).name or "file", "chunks": 0, "error": str(exc)}
                for url in urls
            ]
            _record_upload_history(name, failed_files, "urls")

    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _process)
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
        _require_dataset_password(dm, name, x_dataset_password, request)
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")

    job = _upload_jobs.get(job_id)
    if job is None or job.get("dataset") != name:
        raise HTTPException(404, f"Job '{job_id}' not found")

    # Opportunistic cleanup of old completed jobs
    _upload_jobs.cleanup_old()

    return job


# -- Search ------------------------------------------------------------------


@app.get("/api/datasets/{name}/search")
async def api_search(
    name: str,
    request: Request,
    q: str = Query(..., description="Query text"),
    top_k: int = Query(10, ge=1, le=100),
    use_reranker: bool = Query(False),
    reranker_top_k: int = Query(3, ge=1, le=50),
    x_dataset_password: str | None = Header(None, alias="X-Dataset-Password"),
):
    """Search within a dataset using a text query.

    Returns ranked results with content and similarity scores.
    """
    dm = await get_manager_async()
    try:
        _require_dataset_password(dm, name, x_dataset_password, request)
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(
            sync_pool,
            dm.search,
            name,
            q,
            top_k,
            use_reranker,
            reranker_top_k,
        )
        return {"query": q, "results": results}
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

    Returns ranked results with content and similarity scores.
    """
    query = body.get("query")
    if query is None:
        raise HTTPException(400, "Field 'query' is required (string or dict)")
    password = body.get("password") or None
    top_k = body.get("top_k", 10)
    use_reranker = body.get("use_reranker", False)
    reranker_top_k = body.get("reranker_top_k", 3)

    dm = await get_manager_async()
    try:
        _require_dataset_password(dm, name, password, request)
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(
            sync_pool,
            dm.search,
            name,
            query,
            top_k,
            use_reranker,
            reranker_top_k,
        )
        return {"query": query, "results": results}
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")


# -- Documents list ----------------------------------------------------------


@app.get("/api/datasets/{name}/documents")
async def api_list_documents(
    name: str,
    request: Request,
    limit: int = Query(50, ge=1, le=1000),
    x_dataset_password: str | None = Header(None, alias="X-Dataset-Password"),
):
    """List stored document payloads in a dataset."""
    dm = await get_manager_async()
    try:
        _require_dataset_password(dm, name, x_dataset_password, request)
        loop = asyncio.get_running_loop()
        docs = await loop.run_in_executor(sync_pool, dm.list_documents, name, limit)
        total = dm.get_dataset(name, sync_count=False).get("document_count", 0)
        entries = [{"id": doc_id, "payload": payload} for doc_id, payload in docs]
        return {"documents": entries, "count": total}
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
    try:
        _require_dataset_password(dm, name, x_dataset_password, request)
        dm.get_dataset(name, sync_count=False)
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

                dm.stream_all_documents(name, _emit)
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
    try:
        _require_dataset_password(dm, name, x_dataset_password or None or password or None, request)
        dm.get_dataset(name, sync_count=False)
    except FileNotFoundError:
        raise HTTPException(404, f"Dataset '{name}' not found")

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
            _require_dataset_password(dm, name, password or x_dataset_password or None, request)
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
    return FileResponse(str(file_path), headers=headers)


# -- Staging (transient media handoff for MCP tools) -------------------------


# Cap on multipart upload bytes (dataset file uploads + staging).  The stream
# loop aborts past this so a key-holder cannot fill the disk.  0 disables.
_MAX_UPLOAD_BYTES = max(0, int(os.environ.get("MAX_UPLOAD_BYTES", str(1024 * 1024 * 1024))))


async def _stream_upload(file: UploadFile, dest: Any, max_bytes: int = _MAX_UPLOAD_BYTES) -> int:
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
    files = sorted(f for f in sub.iterdir() if f.is_file() and not f.name.endswith("_preprocessed"))
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
        import httpx

        usage: dict[str, int] = {}
        for r in replicas:
            host = r.get("host", "")
            if not host:
                continue
            try:
                with httpx.Client(timeout=10.0) as client:
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
        import httpx

        with httpx.Client(timeout=5.0) as client:
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
        import httpx

        with httpx.Client(timeout=5.0) as client:
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


@app.post("/api/admin/datasets/{name}/migrate-tier-schema")
async def api_migrate_tier_schema(name: str) -> dict[str, Any]:
    """Migrate a dataset's Qdrant points to the three-tier media schema.

    Renames ``original_video`` → ``preprocessed_video``, derives tier-1
    ``original_*`` paths, and converts ``image``/``video`` ``file://`` refs
    to tier-3 base64 data URLs.  Idempotent.
    """
    dm = await get_manager_async()

    def _do_migrate() -> dict[str, Any]:
        return dm.migrate_tier_schema(name)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(sync_pool, _do_migrate)


@app.post("/api/admin/datasets/{name}/recreate")
async def api_recreate_dataset(name: str) -> dict[str, Any]:
    """Rebuild a dataset's Qdrant collection from its on-disk files.

    Drops the existing collection (whose vectors were built with an older
    embedder) and re-ingests the dataset's original files with the
    currently configured embedder.  Returns a ``job_id`` immediately; poll
    ``GET /api/datasets/{name}/upload-status/{job_id}`` for progress.
    """
    dm = await get_manager_async()
    try:
        dm.get_dataset(name, sync_count=False)
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


# ---------------------------------------------------------------------------
# HTML frontend
# ---------------------------------------------------------------------------

_HTML_INDEX: str | None = None


@app.get("/", response_class=HTMLResponse)
async def index():
    global _HTML_INDEX
    if _HTML_INDEX is None:
        html_path = Path(__file__).parent / "templates" / "index.html"
        if html_path.exists():
            _HTML_INDEX = html_path.read_text(encoding="utf-8")
        else:
            _HTML_INDEX = "<html><body><h1>Frontend not found</h1></body></html>"
    return _HTML_INDEX


@app.get("/favicon.png")
async def favicon():
    from fastapi.responses import FileResponse

    favicon_path = Path(__file__).parent / "templates" / "favicon.png"
    if favicon_path.exists():
        return FileResponse(favicon_path, media_type="image/png")
    return Response(status_code=404)


@app.get("/manage", response_class=HTMLResponse)
async def manage():
    return await index()


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
