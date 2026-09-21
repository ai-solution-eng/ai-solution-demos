"""
MCP server exposing multimodal RAG retrieval as tools for LLM consumption.

Run in stdio mode (for MCP client integration)::

    python -m multimodal_rag.mcp_server

Or with a specific transport::

    python -m multimodal_rag.mcp_server --transport sse --port 8001
"""

import argparse
import asyncio
import concurrent.futures
import contextvars
import functools
import hashlib
import html
import json
import os
import re
import threading
import time
from array import array
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from starlette.types import ASGIApp, Receive, Scope, Send

from multimodal_rag.dataset_manager import DatasetManager
from multimodal_rag.rag_system import (
    MultimodalRAG,
    _arerank_with,
    _media_payloads_needed,
    merge_federated_results,
    resolve_federated_targets,
)
from multimodal_rag.utils.logging_utils import logging, setup_logger

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (same env vars as the API server)
# ---------------------------------------------------------------------------

DATA_PATH = os.environ.get("DATA_PATH", "/data")
QDRANT_HOST = os.environ.get("QDRANT_HOST", "")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
RAG_REMOTE = os.environ.get("RAG_REMOTE", "true").lower() in ("true", "1", "yes")
MEDIA_BASE_URL = os.environ.get("MEDIA_BASE_URL", "")

# ---------------------------------------------------------------------------
# Media token signing (shared secret with the API server).
# Media URLs surfaced to the LLM always carry a short-lived HMAC
# ``?token=...`` scoped to ``{dataset}:{relpath}``.  The legacy
# ``?password=`` suffix (which leaked the dataset password into URLs, tool
# output, and logs) is gone.  MEDIA_TOKEN_SECRET is REQUIRED — both servers
# refuse to start without it (see ``main()``).
# ---------------------------------------------------------------------------

_MEDIA_TOKEN_SECRET = os.environ.get("MEDIA_TOKEN_SECRET", "")
_MEDIA_TOKEN_TTL = max(60, int(os.environ.get("MEDIA_TOKEN_TTL", "3600")))

# Periodic embedder liveness monitor (same env vars as the API server).
# The embedder is the only required model; it is probed in the background
# and the result is surfaced via /api/admin/health.  Health/readiness
# probes deliberately do NOT gate on it (a remote embedder outage cannot
# be fixed by restarting this pod).
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

# Optional ``:``-separated directories of mounted ConfigMap/Secret files
# (one file per env key).  When set, the watcher live-reloads the model
# configuration when these files change — no pod rollout required.
CONFIG_DIR = os.environ.get("CONFIG_DIR", "")

# Storage adds a 32-hex uuid prefix to every file to avoid collisions.  It is
# stripped from public media URLs so models only have to reproduce the
# human-readable filename (see ``_short_rel``).  ``glob``-compatible.
_UUID_PREFIX_RE = re.compile(r"^[0-9a-f]{32}_")


def _sign_media_token(dataset_name: str, rel_path: str, expiry: int | None = None) -> str:
    """Mint an expiring HMAC token authorising ``{dataset_name}/{rel_path}``.

    The signature is truncated to 128 bits (32 hex chars) — shorter media
    URLs are copied more reliably by LLMs when they reproduce suggested
    markdown, and 128 bits is ample for a token that expires after
    ``MEDIA_TOKEN_TTL``.
    """
    import hashlib
    import hmac

    expiry = expiry or (int(time.time()) + _MEDIA_TOKEN_TTL)
    msg = f"{dataset_name}:{rel_path}:{expiry}".encode()
    sig = hmac.new(_MEDIA_TOKEN_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:32]
    return f"{expiry}.{sig}"


def _media_url_suffix(dataset_name: str, rel_path: str, legacy_password: str | None = None) -> str:
    """Return the URL query suffix for a converted media URL.

    Always an expiring HMAC token (``?token=...``); the legacy clear
    ``?password=`` suffix is no longer emitted so the dataset password never
    appears in URLs / tool output / logs.  ``MEDIA_TOKEN_SECRET`` is required
    (enforced at startup), so a token is always mintable here.
    """
    return "?" + urlencode({"token": _sign_media_token(dataset_name, rel_path)})


# Allowlist of prefixes for ``file://`` / local-path media read by the MCP
# tools (describe_media, transcribe_audio, audio queries).  Paths outside the
# allowed prefixes are refused (fail-closed).  Prefixes are colon-separated
# (os.pathsep), e.g.
#   MEDIA_ALLOW_PATH_PREFIXES=/data/datasets:/data/staging
# When unset, the default is ``DATA_PATH/datasets`` + ``DATA_PATH/staging``
# (matching the chart's default layout).  An explicitly empty value allows
# nothing.
_DEFAULT_DATA_PATH = os.environ.get("DATA_PATH", "/data")
_MEDIA_ALLOW_DEFAULT = os.pathsep.join(
    (
        os.path.join(_DEFAULT_DATA_PATH, "datasets"),
        os.path.join(_DEFAULT_DATA_PATH, "staging"),
    )
)
_MEDIA_ALLOW_PATH_PREFIXES: tuple[str, ...] = tuple(
    os.path.normpath(p).rstrip(os.sep)
    for p in os.environ.get("MEDIA_ALLOW_PATH_PREFIXES", _MEDIA_ALLOW_DEFAULT).split(os.pathsep)
    if p.strip()
)


def _media_path_allowed(raw: str) -> bool:
    """True if *raw* (a file:// or local path) is inside an allowed prefix.

    With no configured prefixes nothing is allowed (fail-closed).  The env
    default is ``DATA_PATH``/datasets + ``DATA_PATH``/staging.
    """
    if not _MEDIA_ALLOW_PATH_PREFIXES:
        return False
    p = raw.removeprefix("file://")
    try:
        resolved = os.path.realpath(p)
    except Exception:
        return False
    for prefix in _MEDIA_ALLOW_PATH_PREFIXES:
        if resolved == prefix or resolved.startswith(prefix + os.sep):
            return True
    return False


def _classify_by_url_extension(url: str) -> str | None:
    """Classify an http(s) URL by the extension in its *path* (ignoring the
    query string / fragment).  ``https://x/y.jpg?hmac=...`` → ``image``.

    Returns ``None`` when the path has no recognisable media extension.
    """
    from urllib.parse import urlsplit

    from multimodal_rag.dataset_manager import _classify_file

    path = urlsplit(url).path
    if not path or path.endswith("/"):
        return None
    ft = _classify_file(path)
    return ft if ft in ("image", "video") else None


def _classify_media_bytes(header: bytes, content_type: str = "") -> str | None:
    """Classify a media blob as ``'image'``/``'video'``/``None`` from its
    ``Content-Type`` and magic bytes."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct.startswith("image/"):
        return "image"
    if ct.startswith("video/"):
        return "video"
    if header.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"BM")):
        return "image"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image"
    if len(header) >= 8 and header[4:8] == b"ftyp":
        return "video"
    if header.startswith(b"\x1a\x45\xdf\xa3"):  # Matroska / WebM
        return "video"
    if header.startswith((b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3")):  # MPEG vid
        return "video"
    return None


def _probe_remote_media_type(url: str, timeout: float = 25.0) -> str | None:
    """Best-effort classify a remote URL over the network.

    Only the response headers plus the first chunk are consumed (the stream
    is closed immediately), so this never downloads the full resource.  Used
    as a fallback when the URL has no recognisable extension, e.g. a CDN or
    unversioned endpoint.
    """
    import httpx

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client, client.stream("GET", url) as resp:
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            first = next(resp.iter_bytes(), b"")
            return _classify_media_bytes(first, content_type)
    except Exception:
        logger.debug("remote media probe failed for %s", url[:120], exc_info=True)
        return None


def _classify_media_type(ref: str) -> str | None:
    """Best-effort classify *ref* (data:/http(s)/file/local) as image|video|None.

    Order of operations (cheapest first):
      1. ``data:`` MIME prefix
      2. URL *path* extension (query/fragment ignored) — definitive for URLs
         like ``…/photo.jpg?token=…``
      3. remote probe (Content-Type + magic bytes of the first chunk) for
         extension-less URLs
      4. local file extension, then magic-byte sniffing for bare/staged paths
    """
    from multimodal_rag.dataset_manager import _classify_file

    if ref.startswith("data:"):
        mime = ref.split(",", 1)[0].split(";", 1)[0].removeprefix("data:").strip().lower()
        if mime.startswith("image/"):
            return "image"
        if mime.startswith("video/"):
            return "video"
        return None

    if ref.startswith(("http://", "https://")):
        ft = _classify_by_url_extension(ref)
        if ft is not None:
            return ft
        return _probe_remote_media_type(ref)

    # File: extension first, then magic bytes for extension-less files.
    local = ref.removeprefix("file://")
    ft = _classify_file(local)
    if ft in ("image", "video"):
        return ft
    if os.path.exists(local):
        try:
            from multimodal_rag.utils.model_adapters import _detect_media_type

            mime = _detect_media_type(local)
            if mime.startswith("image/"):
                return "image"
            if mime.startswith("video/"):
                return "video"
        except Exception:
            logger.debug("magic-byte detection failed for %s", local[-60:], exc_info=True)
    return None


# Word-boundary hints used to infer the expected modality from the caller's
# *query* text (e.g. "can you describe the image?" → image).  Only used as a
# low-confidence fallback when URL/extension detection cannot decide.
_IMAGE_HINT_RE = re.compile(r"\b(?:image|images|photo|photos|picture|pictures|screenshot|screenshots|imagery)\b")
_VIDEO_HINT_RE = re.compile(r"\b(?:video|videos|clip|footage|movie|film|recording)\b")


def _infer_media_type_from_query(query: str | None) -> str | None:
    """Best-effort 'image'/'video'/'None' from the language of *query*.

    Returns ``None`` when the query mentions neither/both equally, so the
    caller can fall back to stronger signals.
    """
    if not query:
        return None
    q = query.lower()
    n_image = len(_IMAGE_HINT_RE.findall(q))
    n_video = len(_VIDEO_HINT_RE.findall(q))
    if n_image and not n_video:
        return "image"
    if n_video and not n_image:
        return "video"
    if n_image and n_video:
        return "image" if n_image > n_video else ("video" if n_video > n_image else None)
    return None


# ---------------------------------------------------------------------------
# Thread pool for offloading blocking tool bodies off the MCP event loop.
# MCP tool functions are async and delegate their (sync, blocking) work here
# so a single slow search can no longer stall every concurrent MCP client.
# Sized via MCP_POOL_SIZE (default 64).
# ---------------------------------------------------------------------------


def _mcp_pool_size() -> int:
    try:
        return max(1, int(os.environ.get("MCP_POOL_SIZE", "64")))
    except (TypeError, ValueError):
        return 64


_mcp_pool = concurrent.futures.ThreadPoolExecutor(max_workers=_mcp_pool_size(), thread_name_prefix="mcp-tool")


async def _offload(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a sync callable in the MCP thread pool.

    Propagates the current ``contextvars`` context into the worker thread
    so per-request ContextVars (memory dataset/password/session-id set by
    ``_MemoryHeaderMiddleware``) remain visible inside offloaded tool
    bodies.
    """
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    call = functools.partial(ctx.run, functools.partial(fn, *args, **kwargs))
    return await loop.run_in_executor(_mcp_pool, call)


# ---------------------------------------------------------------------------
# Unlock cache: (dataset_name, client) -> (expiry_timestamp, password)
# Allows providing the password once and skipping it for subsequent
# operations within the TTL window (default 30 minutes).
#
# The cache is scoped by *client identity* (see ``_MemoryHeaderMiddleware``:
# oauth2-proxy identity headers, else X-Forwarded-For, else a shared
# "default") so that one MCP caller unlocking a dataset does not implicitly
# unlock it for every other caller on the pod.  Under a non-authenticated
# deployment all requests collapse to the same identity, preserving the
# legacy behaviour.
# ---------------------------------------------------------------------------

_unlocked: dict[tuple[str, str], tuple[float, str]] = {}
_unlocked_lock = threading.Lock()

_UNLOCK_TTL = int(os.environ.get("UNLOCK_TTL", "1800"))  # seconds, default 30 min

# Hard upper bound on unlock-cache entries to protect against unbounded growth
# from a large number of distinct client identities.
_MAX_UNLOCK_ENTRIES = max(1, int(os.environ.get("UNLOCK_CACHE_MAX", "4096")))


def _bounded_cache_put(cache: dict, key: Any, value: Any, max_entries: int) -> None:
    """Insert into *cache*, evicting the oldest entry when over *max_entries*.

    Plain dicts are insertion-ordered, and we re-insert on every put, so the
    evicted key is the least-recently-used.  Callers must hold the cache's
    lock.
    """
    cache.pop(key, None)
    cache[key] = value
    if len(cache) > max_entries:
        cache.pop(next(iter(cache)))


# ---------------------------------------------------------------------------
# Password-failure throttling for MCP ``unlock_dataset`` (mirrors the API
# server's guard; the MCP path had no brute-force protection, and each wrong
# attempt pins an MCP pool thread during PBKDF2 verification).
# ---------------------------------------------------------------------------

_MCP_PW_FAIL_WINDOW = float(os.environ.get("PW_FAIL_WINDOW", "300.0"))
_MCP_PW_MAX_FAILURES = max(1, int(os.environ.get("PW_MAX_FAILURES", "10")))
_mcp_pw_fail_buckets: dict[str, list[float]] = {}
_mcp_pw_fail_lock = threading.Lock()


def _mcp_pw_failure_count(cid: str) -> int:
    now = time.monotonic()
    with _mcp_pw_fail_lock:
        lst = _mcp_pw_fail_buckets.get(cid)
        if not lst:
            return 0
        lst[:] = [t for t in lst if now - t < _MCP_PW_FAIL_WINDOW]
        return len(lst)


def _mcp_pw_check_throttle(cid: str) -> None:
    if _mcp_pw_failure_count(cid) >= _MCP_PW_MAX_FAILURES:
        raise ToolError("Too many password attempts — try again later.")


def _mcp_pw_record_failure(cid: str) -> None:
    now = time.monotonic()
    with _mcp_pw_fail_lock:
        lst = _mcp_pw_fail_buckets.setdefault(cid, [])
        lst[:] = [t for t in lst if now - t < _MCP_PW_FAIL_WINDOW]
        lst.append(now)
        if len(_mcp_pw_fail_buckets) > 10_000:
            for k in [k for k, v in _mcp_pw_fail_buckets.items() if not v]:
                _mcp_pw_fail_buckets.pop(k, None)


def _mcp_pw_reset_failures(cid: str) -> None:
    with _mcp_pw_fail_lock:
        _mcp_pw_fail_buckets.pop(cid, None)


def _is_unlocked(dataset_name: str) -> str | None:
    """Return the cached password if *dataset_name* is still unlocked (for this client), else None."""
    key = (dataset_name, _unlock_client_id())
    with _unlocked_lock:
        entry = _unlocked.get(key)
        if entry is None:
            return None
        expiry, pw = entry
        if time.monotonic() >= expiry:
            del _unlocked[key]
            return None
        return pw


def _cache_unlock(dataset_name: str, password: str, ttl: int | None = None) -> None:
    """Cache the password for *dataset_name* (for this client) for *ttl* seconds."""
    key = (dataset_name, _unlock_client_id())
    with _unlocked_lock:
        _bounded_cache_put(_unlocked, key, (time.monotonic() + (ttl or _UNLOCK_TTL), password), _MAX_UNLOCK_ENTRIES)


def _check_unlocked_or_password(
    dm: "DatasetManager",
    dataset_name: str,
    password: str | None,
) -> str | None:
    """Return the verified password or raise ToolError.

    Priority:
      1. If *password* is provided and correct, cache it and return it.
      2. If the dataset is in the unlock cache, return the cached password.
      3. Raise ToolError.
    """
    if password:
        if dm.verify_password(dataset_name, password):
            _cache_unlock(dataset_name, password)
            return password
        raise ToolError(f"Incorrect password for dataset '{dataset_name}'.")
    cached = _is_unlocked(dataset_name)
    if cached is not None:
        return cached
    if dm.has_password(dataset_name):
        raise ToolError(
            f"Dataset '{dataset_name}' is password protected. "
            "Provide the correct 'password' parameter or use the "
            "'unlock_dataset' tool to unlock it for your session."
        )
    return None


# ---------------------------------------------------------------------------
# Memory-dataset identity (per-request, transport-level)
# ---------------------------------------------------------------------------
#
# The ``add_memory`` / ``search_memory`` tools resolve *which* dataset is
# the caller's personal memory store WITHOUT the LLM having to pass a
# dataset name or password in tool arguments.  An MCP client (e.g. opencode)
# sends ``X-Memory-Dataset`` and ``X-Dataset-Password`` HTTP headers on every
# request; this pure-ASGI middleware captures them into ContextVars that the
# memory tools read.
#
# SECURITY: these ContextVars are consulted ONLY by ``_resolve_memory_*`` /
# ``add_memory`` / ``search_memory``.  The general dataset tools
# (``search_dataset``, ``get_dataset_files``, ...) never read them, so a
# memory password arriving on the connection CANNOT silently unlock some
# other dataset — it is scoped to the memory tools alone.
#
# Tool functions are ``async def`` and delegate their blocking bodies to the
# MCP thread pool via ``_offload`` (see ``_mcp_pool``).  ``_offload`` copies
# the current ``contextvars`` context into the worker thread, so a
# ContextVar set here by the middleware is reliably visible inside the
# offloaded tool bodies.

_memory_dataset_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("rag_memory_dataset", default=None)
_memory_password_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("rag_memory_password", default=None)
_opencode_session_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "opencode_session_id", default=None
)
# Authenticated client identity used to scope the unlock cache so one MCP
# caller's unlock does not open the dataset for every caller on the pod.
_client_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("rag_client_id", default=None)

# Request headers that carry an authenticated identity injected by the
# auth proxy (e.g. oauth2-proxy).  Order matters — first header wins.
_AUTH_IDENTITY_HEADERS = (
    b"x-auth-request-email",
    b"x-auth-request-user",
    b"x-email",
    b"x-user",
)

# These headers are *client-supplied* unless an auth proxy is guaranteed to
# overwrite them on every request.  Trusting them unconditionally would let
# a caller impersonate another user's unlock-cache entry (and its cached
# plaintext password) or rotate identities to bypass the password-failure
# throttle.  They are therefore only honoured when RAG_TRUST_PROXY_IDENTITY
# is set (helm: security.trustProxyIdentity) — i.e. when the operator
# confirms an enforcing proxy sits in front of this server.
_TRUST_PROXY_IDENTITY = os.environ.get("RAG_TRUST_PROXY_IDENTITY", "").lower() in ("1", "true", "yes")


def _unlock_client_id() -> str:
    """Return the per-request client identity used to scope the unlock cache.

    Prefers an auth-proxy identity header captured by ``_MemoryHeaderMiddleware``
    (only when ``RAG_TRUST_PROXY_IDENTITY`` is set — the headers are
    client-spoofable otherwise), then falls back to the socket peer, and
    finally to a shared ``"default"`` identity for non-authenticated
    deployments.  ``X-Forwarded-For`` is deliberately not used: it is
    client-supplied and spoofable.
    """
    cid = _client_id_ctx.get()
    if cid:
        return cid
    return "default"


class _MemoryHeaderMiddleware:
    """ASGI middleware that funnels memory-identity headers into ContextVars.

    Captures:
      * ``X-Memory-Dataset``     → memory dataset name
      * ``X-Dataset-Password``   → memory dataset password
      * ``X-Opencode-Session-ID``→ opencode session ID (auto-tagged on memories)
      * Auth-proxy headers       → client identity (unlock-cache scoping)
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        ds: str | None = None
        pw: str | None = None
        sid: str | None = None
        cid: str | None = None
        for name, value in scope.get("headers") or []:
            if name == b"x-memory-dataset":
                ds = value.decode("latin-1").strip() or None
            elif name == b"x-dataset-password":
                pw = value.decode("latin-1").strip() or None
            elif name == b"x-opencode-session-id":
                sid = value.decode("latin-1").strip() or None
        if _TRUST_PROXY_IDENTITY:
            # Only honour identity headers when the operator confirmed an
            # enforcing auth proxy overwrites them (RAG_TRUST_PROXY_IDENTITY);
            # otherwise they are client-supplied and spoofable.
            for name, value in scope.get("headers") or []:
                if name in _AUTH_IDENTITY_HEADERS:
                    cid = value.decode("latin-1").strip() or None
                    if cid:
                        break
        # No auth-proxy header — fall back to the socket peer.  X-Forwarded-For
        # is deliberately ignored: it is client-supplied and spoofable, so
        # trusting it would let a caller read another identity's cached
        # unlock password.
        if not cid:
            peer = (scope.get("client") or (None, None))[0]
            if peer:
                cid = peer
        ds_tok = _memory_dataset_ctx.set(ds)
        pw_tok = _memory_password_ctx.set(pw)
        sid_tok = _opencode_session_id_ctx.set(sid)
        cid_tok = _client_id_ctx.set(cid)
        try:
            await self.app(scope, receive, send)
        finally:
            _memory_dataset_ctx.reset(ds_tok)
            _memory_password_ctx.reset(pw_tok)
            _opencode_session_id_ctx.reset(sid_tok)
            _client_id_ctx.reset(cid_tok)


def _resolve_memory_dataset(dataset_name: str | None) -> str:
    """Resolve the memory dataset name: explicit arg → request header → env."""
    ds = dataset_name or _memory_dataset_ctx.get() or os.environ.get("MEMORY_DATASET")
    if not ds:
        raise ToolError(
            "No memory dataset specified. Provide 'dataset_name', send the "
            "'X-Memory-Dataset' header from your MCP client, or set the "
            "MEMORY_DATASET environment variable on the server."
        )
    return ds


def _resolve_memory_password(password: str | None) -> str | None:
    """Resolve the memory dataset password: explicit arg → request header."""
    return password if password else _memory_password_ctx.get()


def _resolve_session_id(existing: str | None = None) -> str | None:
    """Resolve the opencode session ID: explicit arg → request header → env.

    Resolution order:
      1. *existing* — a session ID already present in the caller's metadata
         (the LLM may have passed it explicitly via the ``session-id`` tool).
      2. ``X-Opencode-Session-ID`` request header (captured by the middleware
         into ``_opencode_session_id_ctx``).
      3. ``OPENCODE_SESSION_ID`` environment variable (e.g. set by an opencode
         plugin via the ``shell.env`` hook).
    """
    return existing or _opencode_session_id_ctx.get() or os.environ.get("OPENCODE_SESSION_ID")


# ---------------------------------------------------------------------------
# Memory size budget (cap the token size of each stored memory)
# ---------------------------------------------------------------------------

_MEMORY_MAX_TOKENS_DEFAULT = 8192


def _memory_max_tokens() -> int:
    """Max token budget per memory text, from ``MEMORY_MAX_TOKENS`` (default 8192)."""
    try:
        return max(1, int(os.environ.get("MEMORY_MAX_TOKENS", _MEMORY_MAX_TOKENS_DEFAULT)))
    except (TypeError, ValueError):
        return _MEMORY_MAX_TOKENS_DEFAULT


_memory_splitters: dict[int, Any] = {}


def _memory_splitter(chunk_size: int):
    """Return a token-count-aware splitter for the given budget (or ``None``).

    The bundled ``tokenizer.json`` is used when available so the count is a
    real token count; callers fall back to a character-based approximation.
    """
    if chunk_size in _memory_splitters:
        return _memory_splitters[chunk_size]
    try:
        from multimodal_rag.utils.token_text_splitter import TokenTextSplitter

        splitter = TokenTextSplitter.from_bundled(
            chunk_size=chunk_size,
            chunk_overlap=0,
        )
    except Exception:
        splitter = None
    if splitter is not None:
        _memory_splitters[chunk_size] = splitter
    return splitter


def _split_memory_header(text: str) -> tuple[str, str]:
    """Return ``(header, body)`` for a memory document.

    The header is everything up to (but excluding) the first markdown section
    heading (``## ...``).  For session histories this is the provenance block
    (title, session id, git info, file list); for free-form notes there is no
    header and the whole text is the body.
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("## "):
            header = "\n".join(lines[:i]).strip()
            body = "\n".join(lines[i:])
            return (header + "\n") if header else "", body
    return "", text


def _split_memory_text(text: str, max_tokens: int) -> tuple[list[str], bool]:
    """Split *text* into memory chunks of at most *max_tokens* tokens each.

    The document header (e.g. the session-history provenance block) is
    prepended to **every** chunk so each split stays identifiable — the
    ``session_id`` etc. travels with each chunk.  Uses the same tokenizer
    logic as dataset-side text splitting; falls back to ~4 chars/token.

    Returns ``(chunks, was_split)`` where each chunk is header + body-slice.
    """
    if not text or max_tokens <= 0:
        return ([text] if text else []), False

    header, body = _split_memory_header(text)
    if not body:
        return [text], False

    splitter = _memory_splitter(max_tokens)
    try:
        header_tokens = splitter.count_tokens(header) if header and splitter is not None else 0
    except Exception:
        header_tokens = 0
    if not header_tokens:
        header_tokens = max(0, len(header) // 4)

    content_budget = max(1, max_tokens - header_tokens)
    body_splitter = _memory_splitter(content_budget)
    if body_splitter is not None:
        try:
            chunks = [c for c in body_splitter.split_text(body) if c]
            if header:
                chunks = [header + c for c in chunks]
            return chunks, len(chunks) > 1
        except Exception:
            pass  # fall through to the character-based estimate

    # Character-based fallback: ~4 chars per token.
    budget_chars = content_budget * 4
    chunks = [body[i : i + budget_chars] for i in range(0, len(body), budget_chars)]
    chunks = [c for c in chunks if c]
    if header:
        chunks = [header + c for c in chunks]
    return chunks, len(chunks) > 1


def _resolve_and_unlock(
    dm: "DatasetManager",
    dataset_name: str,
    password: str | None,
) -> str | None:
    """Verify access to *dataset_name* and return the verified password.

    Wraps ``_check_unlocked_or_password`` (which raises ``ToolError`` on
    failure and caches a successful unlock) then returns the password to
    use for ``?password=`` media-URL suffixes.
    """
    _check_unlocked_or_password(dm, dataset_name, password)
    return _is_unlocked(dataset_name) or password


# ---------------------------------------------------------------------------
# Shared DatasetManager (lazy initialised)
# ---------------------------------------------------------------------------

_dm: DatasetManager | None = None
_dm_lock = threading.Lock()


def get_manager() -> DatasetManager:
    global _dm
    if _dm is not None:
        return _dm

    with _dm_lock:
        if _dm is not None:
            return _dm

        data_path = os.environ.get("DATA_PATH", "/data")
        qdrant_host = os.environ.get("QDRANT_HOST", "")
        qdrant_port = int(os.environ.get("QDRANT_PORT", "6333"))
        rag_remote = os.environ.get("RAG_REMOTE", "true").lower() in (
            "true",
            "1",
            "yes",
        )
        rag_dedup_threshold = float(os.environ.get("RAG_DEDUP_THRESHOLD", "0.995"))
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
    """Async-safe wrapper — offloads get_manager() to a thread.

    Uses the shared ``sync_pool`` (rather than spawning a new
    ``ThreadPoolExecutor`` per call, which leaked threads on every
    slow-path invocation after a startup failure).
    """
    if _dm is not None:
        return _dm
    import asyncio

    from multimodal_rag.utils.general_tools import sync_pool

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(sync_pool, get_manager)


def _model_health_loop() -> None:
    """Probe the required embedder endpoint every ``_MODEL_HEALTH_INTERVAL``.

    Runs in a daemon thread (``_start_model_health_thread``) because the MCP
    server is launched via ``uvicorn.run``, which owns the event loop.
    Updates ``_model_health`` so ``/api/admin/health`` can surface embedder
    reachability without re-checking synchronously.  Only needed in the
    HTTP transports (the /healthz and /readyz routes only exist there).
    """
    while True:
        time.sleep(_MODEL_HEALTH_INTERVAL)
        try:
            dm = get_manager()
        except Exception:
            # Not initialised yet — leave /readyz green; the first real
            # tool call will retry init.
            continue
        if _model_health["embedder"]["status"] == "unknown":
            # Manager init already verified the embedder once, so seed the
            # monitor as healthy until the first periodic probe disagrees.
            _model_health["embedder"].update(
                {
                    "status": "healthy",
                    "last_check": datetime.now(UTC).isoformat(),
                    "error": None,
                    "consecutive_failures": 0,
                }
            )
        try:
            dm._verify_endpoint(dm.embedder, "embedder")
            error = None
        except Exception as exc:
            error = str(exc)
        embedder = _model_health["embedder"]
        if error is None:
            embedder.update(
                {
                    "status": "healthy",
                    "last_check": datetime.now(UTC).isoformat(),
                    "error": None,
                    "consecutive_failures": 0,
                }
            )
        else:
            embedder["status"] = "unhealthy"
            embedder["last_check"] = datetime.now(UTC).isoformat()
            embedder["error"] = error
            embedder["consecutive_failures"] += 1
            logger.warning(
                "Embedder endpoint unreachable (%d/%d checks) — %s",
                embedder["consecutive_failures"],
                _MODEL_HEALTH_FAIL_THRESHOLD,
                error,
            )


def _start_model_health_thread() -> None:
    """Launch the periodic embedder probe in a daemon thread."""
    threading.Thread(target=_model_health_loop, daemon=True, name="model-health").start()


def _reload_models() -> None:
    """Rebuild the model objects from freshly-applied config (see api_server)."""
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
    _model_health["embedder"].update(
        {
            "status": "unknown",
            "last_check": None,
            "error": None,
            "consecutive_failures": 0,
        }
    )
    logger.info("MCP model configuration reloaded from %s", CONFIG_DIR)


def _start_config_watcher() -> None:
    """Apply mounted config files and start the live-reload watcher."""
    if not CONFIG_DIR:
        return
    from multimodal_rag.model_config import apply_config_dirs, start_config_watcher

    apply_config_dirs(CONFIG_DIR)
    start_config_watcher(CONFIG_DIR, _reload_models)


def _prefer_preprocessed_media(doc: Any) -> Any:
    """Return a copy of *doc* where tier-3 media keys are replaced by their
    tier-2 ``preprocessed_*`` counterparts when available.

    At ingest time ``image`` / ``video`` / ``audio`` hold the tier-3
    model-ready data URL used for embedding (e.g. a 1 fps, ≤720×720 video
    segment).  The ``preprocessed_image`` / ``preprocessed_video`` /
    ``preprocessed_audio`` keys (when present) point at the tier-2
    preprocessed file on the PVC (e.g. the full video at ≤720p @ 24 fps).
    Surfacing the tier-2 ref in the primary ``image`` / ``video`` / ``audio``
    keys ensures the LLM cites and links a user-viewable version rather than
    the embedding-grade data URL stored in Qdrant.
    """
    if not isinstance(doc, dict):
        return doc
    d = dict(doc)
    for modality in ("image", "video", "audio"):
        preproc = d.get(f"preprocessed_{modality}")
        if preproc:
            d[modality] = preproc
    return d


def _escape_markdown_attr(value: str) -> str:
    """Neutralize characters that could break out of a markdown/HTML context.

    ``alt`` / link labels and URLs can contain user-controlled filenames
    (``]()\"``, newlines).  Escaping keeps a crafted dataset filename from
    injecting markup/XSS when a frontend renders the tool's suggested
    markdown.  Like ``html.escape``, ``&<>"`` are encoded; ``]`` ``[`` ``(``
    are stripped so they cannot close a markdown link early.
    """
    value = html.escape(value, quote=True)
    for ch in ("[", "]", "(", ")"):
        value = value.replace(ch, "")
    return value


def _media_alt_label(url: str, fallback: str) -> str:
    """Derive a clean, escaped ``alt`` label from a media URL.

    The *path basename only* is used — the query string (``?token=…``) is
    deliberately dropped so an LLM copying the suggested markdown never sees
    a truncated ``…JPG?t`` fragment to echo into a broken URL.
    """
    from urllib.parse import urlsplit

    basename = urlsplit(url).path.rsplit("/", 1)[-1] if url else ""
    return _escape_markdown_attr((basename or fallback)[:60])


# ---------------------------------------------------------------------------
# Query-embedding cache + dataset-vector lookup
# ---------------------------------------------------------------------------
#
# Goal: never re-embed the same query media twice.  Two strategies:
#
#   1. If the query URL points to a file already in the target dataset,
#      pull that point's stored vector from Qdrant — zero model calls.
#      This covers the "find more like result #3" pattern where the LLM
#      passes a result URL back as image=/video=/audio=.
#
#   2. Otherwise (staged upload, fresh file), hash the file content and
#      remember the embedding in an in-process LRU keyed by
#      hash + embedder model + query text.  Same file in a later turn —
#      cache hit, no embedder call.
#
# Both paths return ``None`` on miss; the caller falls back to embedding
# (and caches the result for next time).
#
# Vectors are stored as ``array('f')``, not ``list[float]``: a 4096-dim
# embedding as a Python list costs ~130 KB (boxed float objects + pointers)
# vs ~16 KB packed — at the default cap of 4096 entries that is the
# difference between ~530 MB and ~65 MB of resident heap.  Reads convert
# back to ``list`` (microseconds for 4096 floats).
_query_emb_cache: dict[str, array] = {}
_query_emb_cache_lock = threading.Lock()

# Size caps for the in-process caches.  These are plain dicts (insertion
# ordered) trimmed via ``_bounded_cache_put`` so a long-running server never
# accumulates unbounded memory from unique query files / ASR transcripts.
_MAX_QUERY_EMB_CACHE = max(1, int(os.environ.get("QUERY_EMB_CACHE_MAX", "4096")))
_MAX_FILE_HASH_CACHE = max(1, int(os.environ.get("FILE_HASH_CACHE_MAX", "4096")))
_MAX_ASR_TRANSCRIPT_CACHE = max(1, int(os.environ.get("ASR_TRANSCRIPT_CACHE_MAX", "512")))

_file_hash_cache: dict[str, str] = {}
_file_hash_cache_lock = threading.Lock()


def _hash_file(path: str) -> str:
    """SHA-256 of *path* (cached per path so repeat calls are free)."""
    with _file_hash_cache_lock:
        cached = _file_hash_cache.get(path)
    if cached is not None:
        return cached
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    digest = h.hexdigest()
    with _file_hash_cache_lock:
        _bounded_cache_put(_file_hash_cache, path, digest, _MAX_FILE_HASH_CACHE)
    return digest


def _collect_cacheable_paths(query_dict: Any) -> list[tuple[str, str]]:
    """Return ``[(media_key, file_path), ...]`` for cacheable media in *query_dict*."""
    if not isinstance(query_dict, dict):
        return []
    paths: list[tuple[str, str]] = []
    for k in ("image", "video", "audio"):
        v = query_dict.get(k)
        if not v:
            continue
        urls = v if isinstance(v, list) else [v]
        for url in urls:
            path: str | None = None
            if isinstance(url, str):
                if url.startswith("file://"):
                    path = url[7:]
                elif url.startswith("/") and os.path.exists(url):
                    path = url
            if path and os.path.exists(path) and _media_path_allowed(path):
                paths.append((k, path))
    return paths


def _compute_query_cache_key(query_dict: Any, model_name: str, paths: list[tuple[str, str]]) -> str:
    """Build a deterministic cache key from query text + media hashes + model name."""
    text_part = query_dict.get("text", "") if isinstance(query_dict, dict) else ""
    parts = [f"text:{hashlib.sha256(text_part.encode()).hexdigest()}"]
    for k, path in paths:
        parts.append(f"{k}:{_hash_file(path)}")
    parts.append(f"model:{model_name}")
    return "|".join(parts)


def _lookup_dataset_vector(
    rag: MultimodalRAG,
    dataset_name: str,
    file_path: str,
) -> list[float] | None:
    """If *file_path* belongs to *dataset_name*, return its stored Qdrant vector.

    Returns ``None`` when the path is not under the dataset's files dir,
    Qdrant is unreachable, or no point references this file.
    """
    vs = rag.vector_store
    if vs is None or isinstance(vs, dict):
        return None

    data_path = os.environ.get("DATA_PATH", "/data")
    files_prefix = f"{data_path}/datasets/{dataset_name}/files/"
    if not file_path.startswith(files_prefix):
        return None

    client = vs._client  # type: ignore[attr-defined]
    coll = vs.collection_name  # type: ignore[attr-defined]
    vector_name = getattr(vs, "vector_name", None)

    file_url = f"file://{file_path}"
    possible_values = [file_path, file_url]

    # Check every metadata field where the path or file:// URL might be stored.
    fields_to_check = (
        "metadata.source",
        "metadata.image",
        "metadata.video",
        "metadata.audio",
        "metadata.preprocessed_image",
        "metadata.preprocessed_video",
        "metadata.preprocessed_audio",
        "metadata.original_image",
        "metadata.original_video",
        "metadata.original_audio",
    )

    try:
        from qdrant_client.models import FieldCondition, Filter, MatchAny

        # A single scroll with an OR (`should`) over all candidate fields —
        # faster than one Qdrant round-trip per field.
        results, _ = client.scroll(
            coll,
            limit=1,
            with_payload=False,
            with_vectors=True,
            scroll_filter=Filter(
                should=[FieldCondition(key=field, match=MatchAny(any=possible_values)) for field in fields_to_check]
            ),
        )
        if not results:
            return None
        vec = results[0].vector
        if vec is None:
            return None
        # Named vectors come back as a dict; unnamed as a list.
        if isinstance(vec, dict):
            if vector_name and vector_name in vec:
                return vec[vector_name]
            for v in vec.values():
                if isinstance(v, list):
                    return v
        elif isinstance(vec, list):
            return vec
    except Exception:
        logger.debug("Qdrant vector lookup failed", exc_info=True)
        return None

    return None


def _resolve_query_vector(
    rag: MultimodalRAG,
    dataset_name: str,
    query_dict: Any,
) -> list[float] | None:
    """Try to reuse a cached/stored embedding for *query_dict*.

    Returns the vector on hit, ``None`` on miss (caller falls back to embedding).
    """
    paths = _collect_cacheable_paths(query_dict)
    if not paths:
        return None

    # Single dataset-file query → use the stored Qdrant vector directly.
    if len(paths) == 1:
        _, path = paths[0]
        vec = _lookup_dataset_vector(rag, dataset_name, path)
        if vec is not None:
            logger.info("query vector: reused existing Qdrant vector for %s", path[-60:])
            from multimodal_rag.utils.metrics import CACHE_EVENTS

            CACHE_EVENTS.labels(cache="query_emb", event="hit").inc()
            return vec

    # Otherwise: hash-based in-process cache.
    model_name = rag.embedder.model_name
    cache_key = _compute_query_cache_key(query_dict, model_name, paths)
    with _query_emb_cache_lock:
        cached = _query_emb_cache.get(cache_key)
    if cached is not None:
        logger.info("query vector: cache HIT (%d media file(s))", len(paths))
        from multimodal_rag.utils.metrics import CACHE_EVENTS

        CACHE_EVENTS.labels(cache="query_emb", event="hit").inc()
        return list(cached)

    from multimodal_rag.utils.metrics import CACHE_EVENTS

    CACHE_EVENTS.labels(cache="query_emb", event="miss").inc()
    return None


def _cache_query_vector(
    rag: MultimodalRAG,
    query_dict: Any,
    vector: list[float],
) -> None:
    """Store a freshly-computed query vector so subsequent calls can skip embedding."""
    paths = _collect_cacheable_paths(query_dict)
    if not paths:
        return
    model_name = rag.embedder.model_name
    cache_key = _compute_query_cache_key(query_dict, model_name, paths)
    with _query_emb_cache_lock:
        # Packed array('f') — ~10x smaller at rest than a list[float] (see
        # the cache declaration above); reads convert back to list.
        _bounded_cache_put(_query_emb_cache, cache_key, array("f", vector), _MAX_QUERY_EMB_CACHE)
    logger.info("query vector: cached for future reuse (%d media file(s))", len(paths))


# ---------------------------------------------------------------------------
# Audio query: ASR → text → embed  (embedder doesn't support audio natively)
# ---------------------------------------------------------------------------

_asr_transcript_cache: dict[str, str] = {}
_asr_transcript_cache_lock = threading.Lock()


def _resolve_audio_query_vector(
    rag: MultimodalRAG,
    dataset_name: str,
    query_dict: dict[str, Any],
) -> list[float] | None:
    """Handle audio queries by transcribing via ASR, then embedding the text.

    The embedder (Qwen3-VL-Embedding) doesn't support audio, so a bare
    ``audio=`` query would embed a useless ``[Audio media]`` placeholder.
    Instead:

    1. If the audio file is already in the dataset → return its stored
       Qdrant vector (zero model calls — it was transcribed at ingest).
    2. Check the hash-based embedding cache (same file → same vector).
    3. On miss: ASR the (already-truncated-at-staging) audio file →
       build a text query with the transcript → embed the text → cache.

    Returns the query vector on success, ``None`` on failure (caller
    falls back to regular embedding).
    """
    from multimodal_rag.utils.general_tools import sync_wrapper_safe

    audio_val = query_dict.get("audio")
    if not audio_val:
        return None
    audio_urls = audio_val if isinstance(audio_val, list) else [audio_val]

    # Collect local file paths
    audio_paths: list[str] = []
    for url in audio_urls:
        if not isinstance(url, str):
            continue
        if url.startswith("file://"):
            path = url[7:]
        elif url.startswith("/") and os.path.exists(url):
            path = url
        else:
            continue
        if os.path.exists(path) and _media_path_allowed(path):
            audio_paths.append(path)

    if not audio_paths:
        return None

    # 1. Dataset file → reuse stored vector (no ASR, no embed)
    for path in audio_paths:
        vec = _lookup_dataset_vector(rag, dataset_name, path)
        if vec is not None:
            logger.info("audio query: reused stored Qdrant vector for %s", path[-60:])
            return vec

    # 2. Hash-based embedding cache
    model_name = rag.embedder.model_name
    cache_key = _compute_query_cache_key(query_dict, model_name, [("audio", p) for p in audio_paths])
    with _query_emb_cache_lock:
        cached = _query_emb_cache.get(cache_key)
    if cached is not None:
        logger.info("audio query: embedding cache HIT (%d file(s))", len(audio_paths))
        return list(cached)

    # 3. ASR → text → embed
    if rag.asr is None:
        logger.warning("audio query: ASR model unavailable — cannot transcribe")
        return None

    transcripts: list[str] = []
    for path in audio_paths:
        file_hash = _hash_file(path)
        # Check transcript cache first
        with _asr_transcript_cache_lock:
            cached_transcript = _asr_transcript_cache.get(file_hash)
        if cached_transcript is not None:
            logger.info("audio query: ASR cache HIT for %s", path[-60:])
            transcripts.append(cached_transcript)
            continue

        # ASR the file (already truncated to ≤60s at staging time)
        try:
            from multimodal_rag.rag_system import _transcribe_media

            transcript = sync_wrapper_safe(_transcribe_media, {"url": f"file://{path}", "asr": rag.asr})
            if transcript:
                with _asr_transcript_cache_lock:
                    _bounded_cache_put(_asr_transcript_cache, file_hash, transcript, _MAX_ASR_TRANSCRIPT_CACHE)
                transcripts.append(transcript)
                logger.info("audio query: ASR transcribed %s (%d chars)", path[-60:], len(transcript))
        except Exception:
            logger.warning("audio query: ASR failed for %s", path[-60:], exc_info=True)

    if not transcripts:
        return None

    # Build text-only query dict
    text_query: dict[str, Any] = {}
    original_text = query_dict.get("text", "")
    combined = original_text
    if combined:
        combined += "\n"
    combined += "[Audio transcription]: " + " ".join(transcripts)
    text_query["text"] = combined

    # Embed the text-only query
    try:
        vec = rag.embed_query(text_query)
        _cache_query_vector(rag, query_dict, vec)
        logger.info("audio query: embedded transcript text, cached vector")
        return vec
    except Exception:
        logger.warning("audio query: text embedding failed after ASR", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Shared retrieval + post-processing + formatting
# ---------------------------------------------------------------------------
#
# Factored out of ``search_dataset`` so that ``search_memory`` (and any
# future recall tool) reuses the exact same retrieval / post-processing /
# media-URL-conversion / markdown-append logic.  Keeping the two paths
# identical avoids drift between "search a dataset" and "search my memory".


def _format_memory_provenance(doc: dict[str, Any]) -> str:
    """Build a compact one-line provenance string for a memory document.

    Memories created via the opencode ``memory-provenance`` plugin carry git
    and session metadata (``git_before``, ``git_after``, ``git_branch``,
    ``session_id``, ...).  When present, surface a short ``[Provenance: ...]``
    line so the recalling LLM (and the user) can see *when* and *where* the
    memory was captured — e.g. whether it predates a refactor.

    Returns an empty string when the document carries no provenance fields,
    so the caller can treat it as optional.
    """
    parts: list[str] = []

    def _commit(label: str, key: str) -> None:
        val = doc.get(key)
        if isinstance(val, dict):
            short = val.get("short") or val.get("sha")
            subject = val.get("subject")
            if short:
                parts.append(f"{label}={short}" + (f" ({subject})" if subject else ""))
        elif isinstance(val, str) and val:
            parts.append(f"{label}={val[:12]}")

    _commit("before", "git_before")
    _commit("after", "git_after")

    branch = doc.get("git_branch")
    if isinstance(branch, str) and branch:
        parts.append(f"branch={branch}")

    dirty = doc.get("git_dirty")
    if dirty:
        parts.append("dirty")

    sid = doc.get("session_id")
    if isinstance(sid, str) and sid:
        parts.append(f"session={sid}")

    if not parts:
        return ""
    return "[Provenance: " + ", ".join(parts) + "]"


def _llm_modality_set(base_llm_modalities: list[str] | None) -> set[str]:
    """Normalise the ``base_llm_modalities`` tool argument to a set."""
    return set(base_llm_modalities if base_llm_modalities is not None else ["text"])


async def _acore_retrieval(
    rag: "MultimodalRAG",
    dataset_name: str,
    query_dict: "str | dict[str, Any]",
    query: str,
    top_k: int,
    use_reranker: bool,
    reranker_top_k: int,
    base_llm_modalities: list[str] | None,
    filters: dict[str, Any] | None = None,
) -> "tuple[list[Any], list[float]] | None":
    """Retrieval half of :func:`_arun_retrieval`.

    Resolves the query vector (reusing cached/stored media embeddings),
    runs the two-phase media-payload-aware ``aretrieve`` and returns
    ``(retrieved_docs, scores)`` — or ``None`` when the dataset returned no
    hits, so callers can reproduce ``_arun_retrieval``'s exact
    "No results found." short-circuit.

    Split out of ``_arun_retrieval`` so federated search (roadmap feature 8)
    can run this exact retrieval path per dataset, merge the pools, and only
    then post-process/format — without changing what the single-dataset
    tools return.
    """
    llm_modalities = _llm_modality_set(base_llm_modalities)

    # Try to reuse a cached/stored query vector so we don't re-embed
    # the same media twice (covers both "LLM passes back a result URL"
    # and "user uploads the same file again").  On miss we embed
    # up-front and cache the result for next time.
    # The sync helpers (_resolve_query_vector / _resolve_audio_query_vector)
    # do blocking I/O — Qdrant scroll, file hashing, and full ASR — so they
    # are offloaded to the MCP thread pool instead of stalling the event
    # loop (one slow audio query must not block every concurrent client).
    query_vector: list[float] | None = None
    if isinstance(query_dict, dict):
        query_vector = await _offload(_resolve_query_vector, rag, dataset_name, query_dict)

        # Audio: embedder doesn't support audio natively.
        if query_vector is None and query_dict.get("audio"):
            query_vector = await _offload(_resolve_audio_query_vector, rag, dataset_name, query_dict)

        if query_vector is None and any(query_dict.get(k) for k in ("image", "video", "audio")):
            try:
                query_vector = await rag.aembed_query(query_dict)
                _cache_query_vector(rag, query_dict, query_vector)
            except Exception:
                logger.warning("query embedding failed; falling back to retrieve()", exc_info=True)
                query_vector = None

    # Determine whether the base64 media payloads stored in Qdrant are
    # needed by any downstream consumer.  They are needed when:
    #   1. The reranker is enabled (and a reranker model exists), OR
    #   2. A VLM is configured (Postprocessor uses it for image/video
    #      description), OR
    #   3. The base LLM supports image/video natively (media is passed
    #      through for the LLM to consume directly).
    # When none apply, heavy base64 image/video keys are excluded from the
    # Qdrant response to avoid transferring megabytes of data that would
    # be immediately discarded and replaced with preprocessed_* file refs.
    # Two-phase media fetch: phase 1 searches WITHOUT the heavy tier-3
    # base64 payloads whenever the reranker doesn't need them upfront.
    # _media_payloads_needed() then predicts — from the light docs (captions
    # + tier-2 preprocessed refs, which the result JSON below surfaces as
    # the media links anyway) and the query — whether a consumer will
    # actually use the payloads; only then does phase 2 re-search with them
    # included (query_vector is reused, so no re-embed).  With a VLM
    # configured but a generic query over pre-captioned docs this skips a
    # multi-MB Qdrant transfer entirely.
    reranker_needs_media = use_reranker and rag.reranker is not None
    results = await rag.aretrieve(
        query_dict,
        top_k=top_k,
        use_reranker=use_reranker,
        reranker_top_k=reranker_top_k,
        query_vector=query_vector,
        need_media=reranker_needs_media,
        filters=filters,
    )
    if not results:
        return None

    retrieved_docs = [doc for doc, _ in results]

    if not reranker_needs_media and _media_payloads_needed(
        retrieved_docs,
        use_vlm=True,
        vlm=rag.vlm,
        llm_modalities=llm_modalities,
        query=query,
    ):
        results = await rag.aretrieve(
            query_dict,
            top_k=top_k,
            use_reranker=False,
            reranker_top_k=reranker_top_k,
            query_vector=query_vector,
            need_media=True,
            filters=filters,
        )
        retrieved_docs = [doc for doc, _ in results]

    return retrieved_docs, [score for _, score in results]


async def _apostprocess_docs(
    rag: "MultimodalRAG",
    retrieved_docs: list[Any],
    query: str,
    base_llm_modalities: list[str] | None,
) -> list[Any]:
    """Post-process retrieval hits: convert modalities the calling LLM
    cannot consume natively (image/video → VLM description → text,
    audio → ASR transcription → text).

    Content-based and dataset-independent (the VLM/ASR models are
    server-wide), so the federated merge reuses a single pass over the
    merged pool.
    """
    llm_modalities = _llm_modality_set(base_llm_modalities)
    needs_conversion = rag._postprocessor is not None and any(
        isinstance(d, dict) and any(k in d for k in ("image", "video", "audio")) for d in retrieved_docs
    )

    if needs_conversion:
        return await rag._postprocessor.acall(
            retrieved_docs,
            llm_modalities=llm_modalities,
            query=query,
        )
    return retrieved_docs


def _format_retrieval_result(
    dataset_name: str,
    retrieved_docs: list[Any],
    postprocessed: list[Any],
    scores: list[float],
    media_base_url: str | None,
) -> "str | dict[str, Any]":
    """Format one dataset's retrieval slice exactly as ``search_dataset``
    has always returned it.

    Returns the structured ``{"context": …, "results": …}`` payload (NOT
    json-encoded) so federated search can combine several datasets'
    payloads under per-dataset headers; ``_arun_retrieval`` json-encodes it
    for the single-dataset tools, keeping their output byte-identical.

    Returns a plain string instead in the degenerate case where no result
    carried any textual content (the historical behaviour).
    """
    # -- Format context for the LLM --
    context_parts: list[str] = []
    for i, doc in enumerate(postprocessed):
        if isinstance(doc, str):
            text = doc
        elif isinstance(doc, dict):
            text = doc.get("text", "")
            src = doc.get("source", "")
            page = doc.get("page", "")
            if src or page:
                ref = f"[Source: {src}" + (f", Page: {page}" if page else "") + "]"
                text = f"{ref}\n{text}" if text else ref
            # Memories carry git/session provenance (injected by the
            # opencode memory-provenance plugin). Surface it as a short
            # line above the memory text so the recalling LLM knows the
            # repository state at capture time.
            prov = _format_memory_provenance(doc)
            if prov:
                text = f"{prov}\n{text}" if text else prov
        else:
            text = str(doc)

        if text:
            context_parts.append(f"[Result {i + 1}] (score: {scores[i]:.4f})\n{text}")

    if not context_parts:
        return "No textual content found in results."

    context = "\n\n".join(context_parts)

    # -- Also include raw results as JSON for clients that want structured data --
    raw_results: list[dict[str, Any]] = []
    for i, doc in enumerate(retrieved_docs):
        entry: dict[str, Any] = {"score": scores[i]}
        if isinstance(doc, str):
            entry["text"] = doc
        elif isinstance(doc, dict):
            entry["embedding_score"] = doc.pop("_embedding_score", entry["score"])
            entry["reranker_score"] = doc.pop("_reranker_score", None)
            # Surface tier-2 preprocessed_* media (PVC files) as the
            # primary image/video/audio keys so the LLM cites a
            # user-viewable version, not the tier-3 data URL stored
            # in Qdrant.
            entry.update(_prefer_preprocessed_media(doc))
            # Never leak heavy tier-3 base64 data URLs into the result
            # JSON that is handed back to the LLM.  The postprocessor
            # (VLM/ASR) already consumed them internally to build the
            # text in `context`; a data URL with no tier-2 ref to swap
            # in is still megabytes of token garbage for a text-only LLM
            # (tens of thousands of tokens).  When a tier-2 ref exists it
            # was substituted above (and later converted to a signed HTTP
            # URL); when it doesn't, drop the key instead of emitting
            # base64.
            for modality in ("image", "video", "audio"):
                val = entry.get(modality)
                if isinstance(val, str):
                    heavy = val.startswith("data:")
                elif isinstance(val, list):
                    heavy = any(isinstance(v, str) and v.startswith("data:") for v in val)
                else:
                    heavy = False
                if heavy:
                    del entry[modality]
        raw_results.append(entry)

    # -- Optionally convert PVC paths to HTTP URLs --
    media_base_url = media_base_url or MEDIA_BASE_URL
    if media_base_url:
        data_path = os.environ.get("DATA_PATH", "/data")
        pvc_prefix = f"{data_path}/datasets/{dataset_name}/files/"
        file_prefix = f"file://{pvc_prefix}"
        api_prefix = f"{media_base_url}/api/datasets/{dataset_name}/files/"

        def _suffix_for(rel_path: str) -> str:
            # Media URLs always carry a short-lived HMAC token so
            # `<img>/<video>/<audio>` (which cannot set headers) can fetch
            # protected media without ever exposing the dataset password.
            return _media_url_suffix(dataset_name, rel_path)

        def _short_rel(rel: str) -> str:
            # Strip the per-file uuid prefix (``{32-hex}_``) that storage
            # adds to avoid collisions, so the URL an LLM must reproduce is
            # just the human-readable filename (e.g. ``DSC01373.JPG``) —
            # random hex in URLs is exactly what models garble.
            return _UUID_PREFIX_RE.sub("", rel)

        def _convert(val: str) -> str:
            if val.startswith(file_prefix):
                rel = val[len(file_prefix) :]
                short = _short_rel(rel)
                return api_prefix + short + _suffix_for(short)
            if val.startswith(pvc_prefix):
                rel = val[len(pvc_prefix) :]
                short = _short_rel(rel)
                return api_prefix + short + _suffix_for(short)
            return val

        def _convert_dict(d: dict[str, Any]) -> dict[str, Any]:
            return {k: _convert(v) if isinstance(v, str) else v for k, v in d.items()}

        raw_results = [_convert_dict(r) for r in raw_results]
        context = _convert(context)

    # -- Append ready-to-paste markdown image links --
    image_md_lines: list[str] = []
    for i, r in enumerate(raw_results):
        img = r.get("image")
        if not img:
            continue
        urls = img if isinstance(img, list) else [img]
        for url in urls:
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                src = r.get("source") or r.get("original_source") or ""
                alt = _media_alt_label(url, f"matched image {i + 1}")
                image_md_lines.append(f"![{alt}]({url})")
    if image_md_lines:
        context += (
            "\n\nMatched images — include these markdown image links "
            "verbatim in your response so the user sees them inline:\n" + "\n".join(image_md_lines)
        )
        if media_base_url:
            context += (
                "\nEach URL above already carries the correct host and a signed "
                "token. Copy them exactly as printed — do not shorten, re-type, "
                "or substitute the hostname (e.g. do not invent an "
                "'example.com' variant); doing so breaks the signed link."
            )

    # -- Append HTML5 audio players for matched audio --
    audio_md_lines: list[str] = []
    for i, r in enumerate(raw_results):
        aud = r.get("audio")
        if not aud:
            continue
        urls = aud if isinstance(aud, list) else [aud]
        for url in urls:
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                src = r.get("source") or r.get("original_source") or ""
                alt = _media_alt_label(url, f"matched audio {i + 1}")
                audio_md_lines.append(
                    f'<audio controls preload="none" src="{html.escape(url, quote=True)}" title="{alt}"></audio>\n'
                    f"([🎧 {alt}]({url}))"
                )
    if audio_md_lines:
        context += (
            "\n\nMatched audio — include these HTML5 audio players verbatim "
            "in your response so the user can listen inline:\n" + "\n".join(audio_md_lines)
        )

    # -- Append markdown links for documents (PDFs, text, code, etc.) --
    doc_link_lines: list[str] = []
    for i, r in enumerate(raw_results):
        # Skip results that already have image/audio markdown blocks
        if r.get("image") or r.get("audio"):
            continue
        src = r.get("source") or r.get("original_source") or ""
        if not isinstance(src, str) or not src.startswith(("http://", "https://")):
            continue
        # Derive a readable label from the filename + page
        label = src.split("/")[-1] if src else f"document {i + 1}"
        # Strip the uuid prefix for readability
        import re

        label = re.sub(r"^[0-9a-f]{32}_", "", label)[:60]
        page = r.get("page")
        if page:
            label += f" (p. {page})"
        doc_link_lines.append(f"- [📄 {_escape_markdown_attr(label)}]({src})")
    if doc_link_lines:
        context += (
            "\n\nMatched documents — include these links in your response "
            "so the user can open the source files:\n" + "\n".join(doc_link_lines)
        )

    return {
        "context": context,
        "results": raw_results,
    }


async def _arun_retrieval(
    rag: "MultimodalRAG",
    dataset_name: str,
    query_dict: "str | dict[str, Any]",
    query: str,
    top_k: int,
    use_reranker: bool,
    reranker_top_k: int,
    base_llm_modalities: list[str] | None,
    verified_password: str | None,
    media_base_url: str | None,
    filters: dict[str, Any] | None = None,
) -> str:
    """Run retrieval, post-process modalities, and format the JSON result.

    Composition of the three factored stages — :func:`_acore_retrieval`
    (retrieval), :func:`_apostprocess_docs` (modality conversion) and
    :func:`_format_retrieval_result` (formatting) — so the federated
    ``search_datasets`` tool reuses the exact same pipeline per dataset
    while this function keeps the single-dataset tools' output unchanged.

    Async version of the former ``_run_retrieval``.  Calls ``aretrieve``
    and ``aembed_query`` directly so I/O runs on the caller's event loop
    instead of being serialised through the single background event loop
    in ``sync_wrapper_safe``.  This allows concurrent searches to proceed
    in parallel (embed HTTP + Qdrant gRPC overlap across requests).

    The sync helpers ``_resolve_query_vector`` / ``_resolve_audio_query_vector``
    (multimodal-only) do blocking I/O (Qdrant scroll, file hashing, ASR) and
    are offloaded to the MCP thread pool via ``_offload`` so they never stall
    the event loop.  ``Postprocessor.acall`` runs async so it doesn't block
    the event loop.

    ``verified_password`` is accepted for signature compatibility with the
    tools (the retrieval path itself needs no password — access was already
    checked before retrieval).
    """
    core = await _acore_retrieval(
        rag,
        dataset_name,
        query_dict,
        query,
        top_k,
        use_reranker,
        reranker_top_k,
        base_llm_modalities,
        filters,
    )
    if core is None:
        return "No results found."
    retrieved_docs, scores = core

    postprocessed = await _apostprocess_docs(rag, retrieved_docs, query, base_llm_modalities)

    payload = _format_retrieval_result(dataset_name, retrieved_docs, postprocessed, scores, media_base_url)
    if isinstance(payload, str):
        return payload
    return json.dumps(
        payload,
        indent=2,
        default=str,
    )


# ---------------------------------------------------------------------------
# Federated multi-dataset search (roadmap feature 8)
# ---------------------------------------------------------------------------
#
# One query fanned out over several datasets concurrently, merged into a
# single labelled pool.  The fan-out / merge / dedup logic lives in these
# module-level functions (not inside the tool closure) so offline tests can
# exercise it directly, without the MCP runtime.


def _resolve_federated_targets(
    dm: "DatasetManager",
    datasets: "list[str] | str",
    is_unlocked: Any = None,
) -> "tuple[list[str], list[dict[str, str]], list[dict[str, str]]]":
    """MCP adapter over :func:`multimodal_rag.rag_system.resolve_federated_targets`.

    Same semantics (targets / skipped notes / error notes; ``"all"`` expands
    to every dataset readable WITHOUT a password), with the module's
    per-client unlock cache as the unlock predicate and a malformed
    *datasets* argument surfaced as a ``ToolError``.
    """
    try:
        return resolve_federated_targets(dm, datasets, is_unlocked if is_unlocked is not None else _is_unlocked)
    except (TypeError, ValueError) as exc:
        raise ToolError(str(exc))


_FEDERATED_DATASET_KEY = "_federated_dataset"


async def _arerank_federated(
    rag: "MultimodalRAG",
    query: str,
    entries: "list[tuple[str, Any, float]]",
    reranker_top_k: int,
) -> "list[tuple[str, Any, float]]":
    """Single rerank pass over the MERGED multi-dataset pool.

    Reuses the shared rerank helper (:func:`multimodal_rag.rag_system._arerank_with`,
    the exact ``rank.arerank`` call path ``search_dataset`` uses).  The
    reranker is content-based, so cross-dataset pairs are fine; the dataset
    label is carried ON each doc through the sort/truncate and read back
    afterwards.  The reranker's input conversion only reads ``text`` and the
    media keys, so the private label key never reaches the model.
    """
    labelled: list[tuple[Any, float]] = []
    for ds, doc, score in entries:
        if isinstance(doc, dict):
            d = dict(doc)
        else:
            # Metadata-less payloads surface as bare strings; wrap them so
            # the label (and the reranker-score annotation) can ride along.
            d = {"text": str(doc)}
        d[_FEDERATED_DATASET_KEY] = ds
        labelled.append((d, score))

    ranked = await _arerank_with(rag.rank, rag._extract_doc, query, labelled, reranker_top_k)

    out: list[tuple[str, Any, float]] = []
    for doc, rerank_score in ranked:
        ds = ""
        if isinstance(doc, dict):
            ds = doc.pop(_FEDERATED_DATASET_KEY, "") or ""
        out.append((ds, doc, rerank_score))
    return out


async def _afederated_one_dataset(
    dm: "DatasetManager",
    name: str,
    query_dict: "str | dict[str, Any]",
    query: str,
    top_k: int,
    base_llm_modalities: list[str] | None,
    filters: dict[str, Any] | None,
) -> "tuple[Any, list[tuple[str, Any, float]]]":
    """Retrieve one dataset's candidate pool for the federated merge.

    Runs the SAME retrieval core as ``search_dataset`` (two-phase media
    fetch, twin dedup) but with ``use_reranker=False`` — reranking happens
    ONCE over the merged pool instead of per dataset.  Returns
    ``(rag, [(dataset, doc, score), …])``; raises on failure (the caller's
    ``gather(return_exceptions=True)`` turns that into a per-dataset error
    note, never a failed call).
    """

    def _setup() -> Any:
        return dm._get_rag(name)

    rag = await _offload(_setup)
    core = await _acore_retrieval(
        rag,
        name,
        query_dict,
        query,
        top_k,
        use_reranker=False,  # single merged rerank pass happens after the merge
        reranker_top_k=top_k,
        base_llm_modalities=base_llm_modalities,
        filters=filters,
    )
    if core is None:
        return rag, []
    retrieved_docs, scores = core
    return rag, [(name, doc, score) for doc, score in zip(retrieved_docs, scores)]


def _merge_federated_sections(
    formatted: "list[tuple[str, str | dict[str, Any]]]",
    targets: list[str],
    skipped: list[dict[str, str]],
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    """Combine per-dataset formatted payloads into one federated response.

    The ``context`` text is grouped under a per-dataset header (datasets in
    the order they were searched, so output is stable); the ``results`` array
    keeps the MERGED ranking order with a ``dataset`` label on every entry.
    Skips/failures are surfaced both as notes in the context text (for
    text-only consumers) and as structured ``skipped``/``errors`` lists.
    """
    notes: list[str] = []
    if targets:
        notes.append(f"Federated search across {len(targets)} dataset(s): {', '.join(targets)}")
    for note in skipped:
        notes.append(f"Skipped dataset '{note['dataset']}': {note['reason']}")
    for note in errors:
        notes.append(f"Dataset '{note['dataset']}' failed: {note['error']}")

    sections: list[str] = []
    merged_results: list[dict[str, Any]] = []
    for name, payload in formatted:
        if isinstance(payload, str):
            # Degenerate single-dataset slice: no result carried any text.
            sections.append(f"### Dataset: {name}\n{payload}")
            continue
        count = len(payload["results"])
        sections.append(f"### Dataset: {name} — {count} result(s)\n\n{payload['context']}")
        for entry in payload["results"]:
            merged_results.append({"dataset": name, **entry})

    # The context is grouped per dataset (stable, readable); the structured
    # results array is the ranking — score-ordered across datasets (score is
    # the reranker score when the merged rerank ran, else the embedding
    # score).  Stable sort: ties keep the dataset order.
    merged_results.sort(key=lambda r: float(r.get("score", 0.0)), reverse=True)

    if not formatted:
        sections.append("No results found in any dataset.")

    context = "\n\n".join([*notes, *sections]) if (notes or sections) else "No results found in any dataset."
    return {
        "context": context,
        "results": merged_results,
        "datasets_searched": targets,
        "skipped": skipped,
        "errors": errors,
    }


async def _afederated_search(
    dm: "DatasetManager",
    datasets: "list[str] | str",
    query: str,
    image: str | None = None,
    video: str | None = None,
    audio: str | None = None,
    top_k: int = 5,
    use_reranker: bool = False,
    reranker_top_k: int = 3,
    base_llm_modalities: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    media_base_url: str | None = None,
    is_unlocked: Any = None,
) -> dict[str, Any]:
    """Fan one query out over several datasets and merge the results.

    The testable core behind the MCP ``search_datasets`` tool (the tool is
    thin glue: clamping, filter validation and JSON encoding happen there).
    Steps:

    1. Resolve *datasets* (a list of names, one name, or ``"all"``) via
       :func:`_resolve_federated_targets` — locked datasets are skipped with
       a note, never a hard failure.
    2. Fan the retrieval out CONCURRENTLY (``asyncio.gather`` with
       ``return_exceptions=True``); a failing dataset becomes an error note.
    3. Merge the per-dataset pools into one dataset-labelled pool
       (:func:`multimodal_rag.rag_system.merge_federated_results` —
       dataset-qualified twin/text dedup + score sort).
    4. Optionally run ONE rerank pass over the merged pool
       (:func:`_arerank_federated`) and truncate to *reranker_top_k*.
    5. Post-process each dataset's slice (concurrently) and format it with
       the single-dataset formatter, under per-dataset headers.
    """
    targets, skipped, errors = await _offload(_resolve_federated_targets, dm, datasets, is_unlocked)

    if not targets:
        return _merge_federated_sections([], [], skipped, errors)

    # -- Build the multimodal query dict once (shared by every dataset) --
    query_dict: str | dict[str, Any] = query
    if image or video or audio:
        query_dict = {}
        if query:
            query_dict["text"] = query
        if image:
            query_dict["image"] = image
        if video:
            query_dict["video"] = video
        if audio:
            query_dict["audio"] = audio

    # -- Concurrent fan-out; per-dataset failures become error notes --
    outcomes = await asyncio.gather(
        *[
            _afederated_one_dataset(dm, name, query_dict, query, top_k, base_llm_modalities, filters)
            for name in targets
        ],
        return_exceptions=True,
    )

    rags: dict[str, Any] = {}
    entries: list[tuple[str, Any, float]] = []
    for name, outcome in zip(targets, outcomes):
        if isinstance(outcome, BaseException):
            errors.append({"dataset": name, "error": f"{type(outcome).__name__}: {outcome}"})
            continue
        rag, dataset_entries = outcome
        rags[name] = rag
        entries.extend(dataset_entries)

    # -- Merge into one dataset-labelled pool (dedup + score sort) --
    merged = merge_federated_results(entries)

    # -- Optional single rerank pass over the MERGED pool --
    if use_reranker and merged:
        rerank_rag = next((r for r in rags.values() if getattr(r, "reranker", None) is not None), None)
        if rerank_rag is None:
            logger.warning("Federated rerank requested but no dataset has a reranker — keeping embedding order.")
        else:
            merged = await _arerank_federated(rerank_rag, query, merged, reranker_top_k)

    # -- Group the merged pool back per dataset for formatting --
    groups: dict[str, dict[str, list[Any]]] = {}
    for ds, doc, score in merged:
        group = groups.setdefault(ds, {"docs": [], "scores": []})
        group["docs"].append(doc)
        group["scores"].append(score)

    # -- Post-process each dataset's slice (concurrently) --
    # Per dataset rather than one merged call: the Postprocessor may DROP a
    # doc entirely (unconvertible media), which would shift a merged call's
    # index alignment across dataset boundaries.  VLM/ASR calls are async
    # I/O, so the per-dataset passes overlap.
    formatted: list[tuple[str, str | dict[str, Any]]] = []
    names = list(groups)

    async def _post_one(name: str) -> list[Any]:
        rag = rags.get(name)
        if rag is None:
            return groups[name]["docs"]
        return await _apostprocess_docs(rag, groups[name]["docs"], query, base_llm_modalities)

    pp_outcomes = await asyncio.gather(
        *[_post_one(name) for name in names],
        return_exceptions=True,
    )
    for name, outcome in zip(names, pp_outcomes):
        group = groups[name]
        if isinstance(outcome, BaseException):
            errors.append(
                {
                    "dataset": name,
                    "error": f"post-processing failed: {type(outcome).__name__}: {outcome}",
                }
            )
            outcome = group["docs"]  # best effort: surface the raw docs
        payload = _format_retrieval_result(
            name,
            group["docs"],
            outcome,
            group["scores"],
            media_base_url,
        )
        formatted.append((name, payload))

    return _merge_federated_sections(formatted, targets, skipped, errors)


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


def _clamp_tool_limit(value: Any, name: str, maximum: int, default: int = 1) -> int:
    """Coerce an MCP tool limit to an int clamped within ``[default, maximum]``.

    Tool callers (LLM agents) can pass arbitrary values, so every paging /
    ranking limit is normalised here to prevent absurd values (e.g. a
    ``top_k`` of 10**9) from ballooning a single request into a Qdrant /
    VLM / memory disaster.  Non-integer input raises ``ToolError``; values
    clammed to ``maximum``.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"'{name}' must be an integer.")
    if n < default:
        raise ToolError(f"'{name}' must be at least {default}.")
    return min(n, maximum)


try:
    from mcp.server import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.server.transport_security import TransportSecuritySettings

    mcp = MCPServer("multimodal-rag")
    _mcp_transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

    @mcp.tool()
    async def list_datasets() -> str:
        """List all available datasets with their metadata."""

        def _impl() -> str:
            datasets = get_manager().list_datasets()
            if not datasets:
                return "No datasets found."
            lines = ["Available datasets:"]
            for ds in datasets:
                desc = ds.get("description", "")
                caption = ""
                if ds.get("caption_with_asr"):
                    caption += " [asr]"
                if ds.get("caption_with_vlm"):
                    caption += " [vlm]"
                lock = " [password]" if ds.get("has_password") else ""
                unlocked = " [unlocked]" if _is_unlocked(ds["name"]) else ""
                lines.append(
                    f"  • {ds['name']}{caption}{lock}{unlocked} — {ds.get('document_count', 0)} documents"
                    f"{' — ' + desc if desc else ''}"
                )
            return "\n".join(lines)

        return await _offload(_impl)

    @mcp.tool()
    async def unlock_dataset(
        dataset_name: str,
        password: str,
        ttl: int = 1800,
    ) -> str:
        """Unlock a password-protected dataset for the current session.

        Once unlocked, other tools (``search_dataset``, ``get_dataset_files``,
        ``get_dataset_info``) will accept requests without the ``password``
        parameter for the duration of the TTL (default 30 minutes).

        Parameters
        ----------
        dataset_name:
            Name of the dataset to unlock.
        password:
            The dataset password.
        ttl:
            Unlock duration in seconds (default 1800 = 30 min).
        """

        def _impl() -> str:
            if ttl < 60 or ttl > 86400:
                raise ToolError("TTL must be between 60 seconds and 86400 seconds (24 hours).")
            cid = _unlock_client_id()
            _mcp_pw_check_throttle(cid)
            dm = get_manager()
            try:
                dm.get_dataset(dataset_name)
            except FileNotFoundError:
                raise ToolError(f"Dataset '{dataset_name}' not found.")
            if not dm.has_password(dataset_name):
                return f"Dataset '{dataset_name}' is not password protected — nothing to unlock."
            if not dm.verify_password(dataset_name, password):
                _mcp_pw_record_failure(cid)
                raise ToolError(f"Incorrect password for dataset '{dataset_name}'.")
            _mcp_pw_reset_failures(cid)
            _cache_unlock(dataset_name, password, ttl=ttl)
            return (
                f"Dataset '{dataset_name}' unlocked for {ttl // 60} minutes "
                f"(until approximately "
                f"{datetime.fromtimestamp(time.time() + ttl).strftime('%H:%M:%S')})."
            )

        return await _offload(_impl)

    @mcp.tool()
    async def search_dataset(
        dataset_name: str,
        query: str = "",
        image: str | None = None,
        video: str | None = None,
        audio: str | None = None,
        top_k: int = 10,
        use_reranker: bool = False,
        reranker_top_k: int = 3,
        base_llm_modalities: list[str] | None = None,
        password: str | None = None,
        media_base_url: str | None = None,
        file_types: list[str] | None = None,
        severities: list[str] | None = None,
        source_prefix: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> str:
        """Search a multimodal RAG dataset and return formatted context.

        Parameters
        ----------
        dataset_name:
            Name of the dataset to search.
        query:
            Search query text.
        image:
            Image data URL (base64) or remote URL to search with.
        video:
            Video data URL (base64) or remote URL to search with.
        audio:
            Audio data URL (base64) or remote URL to search with.
        top_k:
            Number of results to retrieve (max 100).
        use_reranker:
            Whether to re-rank results with the cross-encoder reranker.
        reranker_top_k:
            Number of results to keep after re-ranking.
        base_llm_modalities:
            Modalities the calling LLM supports natively, e.g.
            ``["text"]`` or ``["text", "image"]``.
            Unsupported modalities are automatically converted:
            image/video → VLM description → text,
            audio → ASR transcription → text.
        password:
            Optional if the dataset was previously unlocked with
            ``unlock_dataset``; required otherwise.  When provided this
            also acts as an implicit unlock for future calls.
        media_base_url:
            External base URL of the API server (the value of
            ``MEDIA_BASE_URL``). When set, ``file://`` PVC paths in results
            are converted to ``{media_base_url}/api/datasets/{name}/files/
            {path}`` HTTP URLs so the frontend can fetch media directly
            without large inline base64 payloads.  Use the exact host seen in
            the returned ``source`` URLs — never invent or substitute a
            hostname (there is no valid ``rag-mcp-server.example.com``).
        file_types:
            Optional metadata filter: only results whose file type is one of
            these (``pdf``, ``image``, ``video``, ``audio``, ``text``,
            ``json``, ``table``, ``code``, ``office``, ``html``, ``xml``,
            ``yaml``, ``notebook``, ``ebook``, ``log``).
        severities:
            Optional metadata filter for log corpora: only results whose
            observed severities include one of these (``ERROR``, ``WARN``,
            ``INFO``, ...).
        source_prefix:
            Optional metadata filter: only results whose stored source path
            starts with this prefix (e.g. ``reports/2025/``).
        date_from / date_to:
            Optional metadata filters (ISO-8601 datetimes) on a document's
            ``timestamp_start`` — log entries and timestamped documents.
        """

        # Sync setup — cheap (cached singleton, meta.json read, cached RAG).
        # Offloaded so it never blocks the MCP event loop.
        def _setup() -> tuple:
            dm = get_manager()
            try:
                dm.get_dataset(dataset_name)
            except FileNotFoundError:
                raise ToolError(f"Dataset '{dataset_name}' not found.")
            verified_password = _resolve_and_unlock(dm, dataset_name, password)
            rag = dm._get_rag(dataset_name)
            return rag, verified_password

        rag, verified_password = await _offload(_setup)

        # Clamp paging/ranking params to safe bounds (see _clamp_tool_limit).
        top_k = _clamp_tool_limit(top_k, "top_k", maximum=100)
        reranker_top_k = _clamp_tool_limit(reranker_top_k, "reranker_top_k", maximum=min(50, top_k))

        # Metadata filters (feature: filtered search) — validated here so a
        # bad date surfaces as a tool error instead of a silent no-op filter.
        filters: dict[str, Any] | None = None
        _raw_filters: dict[str, Any] = {}
        if file_types:
            _raw_filters["file_types"] = list(file_types)
        if severities:
            _raw_filters["severities"] = list(severities)
        if source_prefix and source_prefix.strip():
            _raw_filters["source_prefix"] = source_prefix.strip()
        if date_from and date_from.strip():
            _raw_filters["date_from"] = date_from.strip()
        if date_to and date_to.strip():
            _raw_filters["date_to"] = date_to.strip()
        if _raw_filters:
            from multimodal_rag.vector_store import build_payload_filter

            try:
                build_payload_filter(_raw_filters)
            except ValueError as exc:
                raise ToolError(f"Search filter rejected: {exc}")
            filters = _raw_filters

        # -- Build multimodal query dict --
        query_dict: str | dict[str, Any] = query
        if image or video or audio:
            # Query-time SSRF guard: remote media URLs are fetched
            # server-side by the embedder (loopback allowed — clients may
            # hand back the server's own media URLs).
            try:
                from multimodal_rag.dataset_manager import _check_media_url_policy

                for media_value in (image, video, audio):
                    if media_value:
                        _check_media_url_policy(media_value)
            except ValueError as exc:
                raise ToolError(f"Query media rejected: {exc}")
            query_dict = {}
            if query:
                query_dict["text"] = query
            if image:
                query_dict["image"] = image
            if video:
                query_dict["video"] = video
            if audio:
                query_dict["audio"] = audio

        # Async retrieval directly on the MCP event loop — concurrent
        # embed + Qdrant I/O across requests instead of being serialised
        # through the single background event loop in sync_wrapper_safe.
        return await _arun_retrieval(
            rag,
            dataset_name,
            query_dict,
            query,
            top_k,
            use_reranker,
            reranker_top_k,
            base_llm_modalities,
            verified_password,
            media_base_url,
            filters,
        )

    @mcp.tool()
    async def search_datasets(
        datasets: "list[str] | str",
        query: str = "",
        image: str | None = None,
        video: str | None = None,
        audio: str | None = None,
        top_k: int = 5,
        use_reranker: bool = False,
        reranker_top_k: int = 3,
        base_llm_modalities: list[str] | None = None,
        file_types: list[str] | None = None,
        severities: list[str] | None = None,
        source_prefix: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> str:
        """Search SEVERAL datasets at once with one query and merge the results.

        Use this instead of ``search_dataset`` when you don't know which
        dataset holds the answer, or when the answer may span datasets.
        Every hit is labelled with its dataset; duplicate chunks that appear
        in several datasets are kept per dataset (labelled), duplicates
        within a dataset are collapsed.

        Parameters
        ----------
        datasets:
            List of dataset names — or the string ``"all"`` to search every
            dataset that is readable WITHOUT a password (no password set, or
            unlocked in this session via ``unlock_dataset``).
        query:
            Search query text.
        image / video / audio:
            Optional media (data URL or remote URL) to search with.
        top_k:
            Number of results to retrieve PER DATASET (max 100).
        use_reranker:
            Whether to re-rank the MERGED result pool with the cross-encoder
            reranker (one pass over all datasets' candidates).
        reranker_top_k:
            Number of results to keep after re-ranking (max 50).
        base_llm_modalities:
            Modalities the calling LLM supports natively, e.g.
            ``["text"]`` or ``["text", "image"]``.  Unsupported modalities
            are automatically converted (image/video → VLM description →
            text, audio → ASR transcription → text).
        file_types / severities / source_prefix / date_from / date_to:
            Optional metadata filters applied to EVERY dataset (same
            meanings as in ``search_dataset``).

        Password-protected datasets that are not unlocked for this session
        are SKIPPED and reported under ``skipped`` — this tool deliberately
        accepts no ``password`` argument (unlock with ``unlock_dataset``
        first, then call again).  A dataset that fails to search is reported
        under ``errors`` and never fails the whole call; each remaining
        dataset still contributes its results.
        """

        # Clamp paging/ranking params to safe bounds (see _clamp_tool_limit).
        # top_k is PER DATASET; the merged pool is bounded by
        # len(datasets) * top_k, and reranker_top_k is applied to that pool.
        top_k = _clamp_tool_limit(top_k, "top_k", maximum=100)
        reranker_top_k = _clamp_tool_limit(reranker_top_k, "reranker_top_k", maximum=50)

        # Metadata filters (feature: filtered search) — validated here so a
        # bad date surfaces as a tool error instead of a silent no-op filter.
        filters: dict[str, Any] | None = None
        _raw_filters: dict[str, Any] = {}
        if file_types:
            _raw_filters["file_types"] = list(file_types)
        if severities:
            _raw_filters["severities"] = list(severities)
        if source_prefix and source_prefix.strip():
            _raw_filters["source_prefix"] = source_prefix.strip()
        if date_from and date_from.strip():
            _raw_filters["date_from"] = date_from.strip()
        if date_to and date_to.strip():
            _raw_filters["date_to"] = date_to.strip()
        if _raw_filters:
            from multimodal_rag.vector_store import build_payload_filter

            try:
                build_payload_filter(_raw_filters)
            except ValueError as exc:
                raise ToolError(f"Search filter rejected: {exc}")
            filters = _raw_filters

        # Query-time SSRF guard: remote media URLs are fetched server-side
        # by the embedder (loopback allowed — clients may hand back the
        # server's own media URLs).
        if image or video or audio:
            try:
                from multimodal_rag.dataset_manager import _check_media_url_policy

                for media_value in (image, video, audio):
                    if media_value:
                        _check_media_url_policy(media_value)
            except ValueError as exc:
                raise ToolError(f"Query media rejected: {exc}")

        payload = await _afederated_search(
            get_manager(),
            datasets,
            query,
            image=image,
            video=video,
            audio=audio,
            top_k=top_k,
            use_reranker=use_reranker,
            reranker_top_k=reranker_top_k,
            base_llm_modalities=base_llm_modalities,
            filters=filters,
            media_base_url=None,  # fall back to the global MEDIA_BASE_URL
        )
        return json.dumps(payload, indent=2, default=str)

    @mcp.tool()
    async def add_memory(
        text: str,
        image: str | None = None,
        video: str | None = None,
        audio: str | None = None,
        metadata: dict[str, Any] | None = None,
        dataset_name: str | None = None,
        password: str | None = None,
    ) -> str:
        """Store a memory (a durable fact / decision / preference) for later recall.

        Intended for LLM-curated long-term memory: the agent distils a
        concise, self-contained record — NOT a raw transcript — and stores
        it so future sessions can recall it via ``search_memory``.  The
        memory dataset and password are normally supplied by the MCP client
        via request headers, so the model does NOT need to pass
        ``dataset_name`` / ``password``.

        The stored text is split into documents of at most ``MEMORY_MAX_TOKENS``
        tokens each (default 8192), mirroring dataset-side text splitting.
        The header (session/provenance block) is prepended to **every** chunk,
        and the payload records ``chunk_index`` / ``chunk_total`` /
        ``memory_chunks`` / ``memory_truncated`` so split memories are
        identifiable and each chunk carries the session id.

        Parameters
        ----------
        text:
            The memory content, written to be useful standalone — a future
            session with no other context should understand it.  Prefer
            "User prefers tabs over spaces because of repo style" over
            "prefers tabs".
        image / video / audio:
            Optional media attached to the memory (URLs or data URLs).
            Rarely needed for coding memories; supported for completeness.
        metadata:
            Optional structured provenance, e.g.
            ``{"kind": "decision", "tags": ["auth", "refactor"],
            "session_id": "abc123"}``.  Stored alongside the memory in the
            vector payload for later filtering.  The ``session_id`` field is
            auto-populated from the ``X-Opencode-Session-ID`` request header
            or ``OPENCODE_SESSION_ID`` env var when not provided explicitly.

            When memories are created through the opencode
            ``memory-provenance`` plugin, the following fields are injected
            automatically (the plugin runs client-side, where the git repo
            lives, so the remote server cannot derive them):
              - ``git_before`` — HEAD commit at session start
                (``{"sha", "short", "subject"}``)
              - ``git_after``  — HEAD commit at memory-creation time
              - ``git_branch`` — branch name
              - ``git_repo``   — remote URL or repo root
              - ``git_dirty``  — whether the working tree had uncommitted
                changes
              - ``git_diff_stat`` — one-line summary of uncommitted changes
              - ``session_title``, ``session_created_at``, ``project_dir``
            Explicit metadata keys always take precedence over auto-injected
            ones.  ``search_memory`` surfaces these as a compact
            ``[Provenance: ...]`` line above each result.
        dataset_name:
            Memory dataset.  Optional — resolved from the
            ``X-Memory-Dataset`` request header or ``MEMORY_DATASET`` env
            var when omitted.  The model usually does NOT pass this.
        password:
            Password for the protected memory dataset.  Optional — resolved
            from the ``X-Dataset-Password`` request header when omitted.
        """

        def _impl() -> str:
            ds_name = _resolve_memory_dataset(dataset_name)
            pw = _resolve_memory_password(password)

            # Auto-tag the opencode session ID if one is available (from a
            # request header, env var, or the caller's metadata).  An explicit
            # session_id in metadata always wins; otherwise we fill it in so
            # memories are traceable to the session that created them without
            # the LLM having to pass it manually.
            meta = dict(metadata) if metadata else {}
            resolved_sid = _resolve_session_id(meta.get("session_id"))
            if resolved_sid:
                meta["session_id"] = resolved_sid

            dm = get_manager()
            try:
                dm.get_dataset(ds_name)
            except FileNotFoundError:
                raise ToolError(
                    f"Memory dataset '{ds_name}' not found. Create it first via "
                    "the REST API (POST /api/datasets) or the HTML frontend."
                )
            _resolve_and_unlock(dm, ds_name, pw)

            # A session-history memory is replaced in place: delete any prior
            # chunks for this session so the store keeps ONE current history
            # per session instead of accumulating copies on every re-flush.
            sid = meta.get("session_id")
            if meta.get("kind") == "session_history" and sid:
                try:
                    dm.delete_session_history(ds_name, str(sid))
                except Exception as exc:
                    raise ToolError(f"Failed to replace existing session history: {exc}") from exc

            # Build the document(s).  In MultimodalRAG._to_documents every
            # non-'text' key becomes Qdrant payload metadata, so the
            # provenance fields below are stored queryably alongside the
            # embedding.  Long memories are split into chunk-sized documents,
            # each prefixed with the header, so the session info is retained
            # in every split (like dataset-side text splitting).
            max_tokens = _memory_max_tokens()
            chunks, was_split = _split_memory_text(text, max_tokens)
            if not chunks:
                chunks = [""]

            base: dict[str, Any] = {"source": "opencode:memory"}
            if image:
                base["image"] = image
            if video:
                base["video"] = video
            if audio:
                base["audio"] = audio
            base["memory_kind"] = meta.get("kind", "note")
            base["memory_ts"] = datetime.now(UTC).isoformat()
            base["memory_chunks"] = len(chunks)
            base["memory_truncated"] = was_split
            if meta:
                tags = meta.get("tags")
                if tags:
                    base["memory_tags"] = tags
                sid = meta.get("session_id")
                if sid:
                    base["session_id"] = sid
                # Carry any other metadata keys through verbatim.
                for k, v in meta.items():
                    if k not in ("kind", "tags", "session_id") and k not in base:
                        base[k] = v

            docs: list[str | dict[str, Any]] = []
            for i, chunk_text in enumerate(chunks):
                doc = dict(base)
                doc["text"] = chunk_text
                doc["chunk_index"] = i
                doc["chunk_total"] = len(chunks)
                doc["memory_split"] = f"{i + 1}/{len(chunks)}"
                docs.append(doc)

            try:
                ids = dm.add_documents(ds_name, docs)
            except Exception as exc:
                raise ToolError(f"Failed to store memory: {exc}") from exc

            meta = dm.get_dataset(ds_name)
            count = meta.get("document_count", 0) if isinstance(meta, dict) else 0
            dedup_thr = os.environ.get("RAG_DEDUP_THRESHOLD", "0.995")
            return json.dumps(
                {
                    "status": "stored",
                    "dataset": ds_name,
                    "document_count": count,
                    "stored_ids": ids,
                    "note": (
                        "Memory stored. Near-duplicates (cosine >= "
                        + dedup_thr
                        + ") are auto-skipped, so this may be a no-op if an "
                        "identical memory already exists."
                    ),
                },
                indent=2,
                default=str,
            )

        return await _offload(_impl)

    @mcp.tool()
    async def search_memory(
        query: str,
        image: str | None = None,
        video: str | None = None,
        audio: str | None = None,
        top_k: int = 5,
        use_reranker: bool = False,
        reranker_top_k: int = 3,
        base_llm_modalities: list[str] | None = None,
        dataset_name: str | None = None,
        password: str | None = None,
    ) -> str:
        """Recall relevant memories from your personal long-term memory store.

        Use at the start of a non-trivial task to check whether past work,
        decisions, or preferences are relevant, or whenever the user
        references prior work.  The memory dataset and password are normally
        supplied by the MCP client via request headers, so the model does
        NOT need to pass ``dataset_name`` / ``password``.

        Parameters
        ----------
        query:
            Natural-language description of what you are looking for.
        image / video / audio:
            Optional media to search by similarity.
        top_k:
            Number of memories to retrieve (default 5).
        use_reranker / reranker_top_k:
            Cross-encoder reranking (off by default for speed).
        base_llm_modalities:
            Modalities the calling LLM supports, e.g. ``["text"]``.
        dataset_name / password:
            Optional — resolved from request headers / env var.  The model
            usually does NOT pass these.
        """

        # Sync setup — cheap (cached singleton, meta.json read, cached RAG).
        # Offloaded so it never blocks the MCP event loop.
        def _setup() -> tuple:
            ds_name = _resolve_memory_dataset(dataset_name)
            pw = _resolve_memory_password(password)

            dm = get_manager()
            try:
                dm.get_dataset(ds_name)
            except FileNotFoundError:
                raise ToolError(f"Memory dataset '{ds_name}' not found.")
            verified_pw = _resolve_and_unlock(dm, ds_name, pw)
            rag = dm._get_rag(ds_name)
            return rag, ds_name, verified_pw

        rag, ds_name, verified_pw = await _offload(_setup)

        # Clamp paging/ranking params to safe bounds (see _clamp_tool_limit).
        top_k = _clamp_tool_limit(top_k, "top_k", maximum=100)
        reranker_top_k = _clamp_tool_limit(reranker_top_k, "reranker_top_k", maximum=min(50, top_k))

        # -- Build multimodal query dict --
        query_dict: str | dict[str, Any] = query
        if image or video or audio:
            # Write-time SSRF guard: stored media URLs are fetched
            # server-side later (at retrieval/VLM/ASR time), so reject
            # policy-violating hosts up front rather than at read time.
            try:
                from multimodal_rag.dataset_manager import _check_media_url_policy

                for media_value in (image, video, audio):
                    if media_value:
                        _check_media_url_policy(media_value)
            except ValueError as exc:
                raise ToolError(f"Memory media rejected: {exc}")
            query_dict = {}
            if query:
                query_dict["text"] = query
            if image:
                query_dict["image"] = image
            if video:
                query_dict["video"] = video
            if audio:
                query_dict["audio"] = audio

        # Async retrieval directly on the MCP event loop — concurrent
        # embed + Qdrant I/O across requests instead of being serialised
        # through the single background event loop in sync_wrapper_safe.
        # media_base_url=None lets _arun_retrieval fall back to the global
        # MEDIA_BASE_URL env var so any media memories also get clickable
        # HTTP URLs (short-lived HMAC ?token= for protected datasets).
        return await _arun_retrieval(
            rag,
            ds_name,
            query_dict,
            query,
            top_k,
            use_reranker,
            reranker_top_k,
            base_llm_modalities,
            verified_pw,
            None,
        )

    def _memory_tool_dataset(dataset_name: str | None, password: str | None):
        """Shared setup for the memory-management tools: resolve + unlock.

        Returns ``(dm, ds_name)`` after verifying the dataset exists and the
        caller may access it — the same header-based identity resolution the
        other memory tools use, so memory headers can never unlock another
        dataset.
        """
        ds_name = _resolve_memory_dataset(dataset_name)
        pw = _resolve_memory_password(password)
        dm = get_manager()
        try:
            dm.get_dataset(ds_name)
        except FileNotFoundError:
            raise ToolError(f"Memory dataset '{ds_name}' not found.")
        _resolve_and_unlock(dm, ds_name, pw)
        return dm, ds_name

    @mcp.tool()
    async def delete_memory(
        memory_ids: list[str],
        dataset_name: str | None = None,
        password: str | None = None,
    ) -> str:
        """Delete memories from your personal long-term memory store by point ID.

        Pass the ``memory_id`` values reported by ``search_memory`` /
        ``list_memories``.  Explicit-IDs-only **by design** — there is no
        query/similarity-directed deletion, so an LLM can never delete
        anything it has not first seen listed.  Deleting one chunk of a split
        memory (``memory_chunks`` > 1) leaves the other chunks; pass every
        chunk id to remove the memory completely.

        Parameters
        ----------
        memory_ids:
            Non-empty list of point IDs to delete.
        dataset_name / password:
            Optional — resolved from request headers / env var.  The model
            usually does NOT pass these.
        """

        def _impl() -> str:
            dm, ds_name = _memory_tool_dataset(dataset_name, password)

            ids = [str(m).strip() for m in (memory_ids or []) if str(m).strip()]
            if not ids:
                raise ToolError(
                    "memory_ids must be a non-empty list of point IDs "
                    "(copy them from search_memory / list_memories results)."
                )

            # Audit trail: fetch payloads first so the response states exactly
            # what was removed (and unknown IDs are visible as "not found").
            previews: list[dict[str, Any]] = []
            try:
                rag = dm._get_rag(ds_name)
                vs = rag.vector_store
                if vs is not None and not isinstance(vs, dict):
                    client = vs._client  # type: ignore[attr-defined]
                    pts = client.retrieve(
                        vs.collection_name,  # type: ignore[attr-defined]
                        ids=ids,
                        with_payload=True,
                        with_vectors=False,
                    )
                    found = {str(p.id) for p in pts}
                    for pt in pts:
                        payload = pt.payload or {}
                        meta = payload.get("metadata", {}) or {}
                        text = str(payload.get("page_content", ""))
                        previews.append(
                            {
                                "memory_id": str(pt.id),
                                "kind": meta.get("memory_kind"),
                                "preview": " ".join(text.split())[:120],
                            }
                        )
                    missing = [i for i in ids if i not in found]
                else:
                    missing = []
            except Exception:
                previews, missing = [], []

            deleted = dm.delete_documents(ds_name, ids)
            return json.dumps(
                {
                    "status": "deleted",
                    "dataset": ds_name,
                    "requested": len(ids),
                    "deleted": deleted,
                    "not_found": missing,
                    "deleted_memories": previews,
                },
                indent=2,
                default=str,
            )

        return await _offload(_impl)

    @mcp.tool()
    async def list_memories(
        limit: int = 20,
        kind: str | None = None,
        tags: list[str] | None = None,
        include_session_history: bool = False,
        dataset_name: str | None = None,
        password: str | None = None,
    ) -> str:
        """List your stored memories so you can review or delete them.

        Returns the newest memories first, each with its ``memory_id`` (pass
        it to ``delete_memory`` to remove one), ``kind``, timestamp, tags and
        a text preview.  Session histories are hidden unless requested —
        they are machine-managed by the session-history plugin.

        Parameters
        ----------
        limit:
            Max memories to list (default 20, max 200).
        kind:
            Only list memories of this ``kind`` (e.g. ``decision``,
            ``preference``, ``gotcha``, ``fact``, ``note``).
        tags:
            Only list memories carrying ANY of these tags.
        include_session_history:
            Include ``session_history`` entries (default false).
        dataset_name / password:
            Optional — resolved from request headers / env var.
        """

        def _impl() -> str:
            dm, ds_name = _memory_tool_dataset(dataset_name, password)
            max_rows = _clamp_tool_limit(limit, "limit", maximum=200)

            from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

            must: list[Any] = []
            must_not: list[Any] = []
            if kind:
                must.append(FieldCondition(key="metadata.memory_kind", match=MatchValue(value=kind)))
            if tags:
                must.append(FieldCondition(key="metadata.memory_tags", match=MatchAny(any=[str(t) for t in tags])))
            if not include_session_history and not kind:
                must_not.append(FieldCondition(key="metadata.memory_kind", match=MatchValue(value="session_history")))
            scroll_filter = Filter(must=must or None, must_not=must_not or None)

            rows = dm.scroll_documents(ds_name, scroll_filter, limit=max_rows)

            def _ts(row: tuple[str, dict[str, Any]]) -> str:
                meta = row[1].get("metadata", {}) or {}
                return str(meta.get("memory_ts") or "")

            rows.sort(key=_ts, reverse=True)  # newest first

            if not rows:
                scope = f" (kind={kind})" if kind else ""
                return f"0 memories in '{ds_name}'{scope} — the store is empty or nothing matches."

            lines = [f"{len(rows)} memory(ies) in '{ds_name}' (newest first):", ""]
            for pid, payload in rows:
                meta = payload.get("metadata", {}) or {}
                text = " ".join(str(payload.get("page_content", "")).split())
                lines.append(f"- memory_id: {pid}")
                lines.append(
                    f"  kind: {meta.get('memory_kind', 'note')} | ts: {meta.get('memory_ts', '?')} "
                    f"| tags: {meta.get('memory_tags') or []} "
                    f"| session: {meta.get('session_id') or '-'}"
                )
                lines.append(f"  preview: {text[:200]}{'…' if len(text) > 200 else ''}")
            return "\n".join(lines)

        return await _offload(_impl)

    @mcp.tool()
    async def forget_session(
        session_id: str,
        dataset_name: str | None = None,
        password: str | None = None,
    ) -> str:
        """Delete the stored session-history memory for one session.

        Session histories are written automatically by the session-memory
        plugin (``kind: session_history``) and replaced in place when a
        session is re-flushed; this tool removes one entirely — e.g. after a
        session containing sensitive material.  It only ever touches
        ``session_history`` documents, never curated memories.

        Parameters
        ----------
        session_id:
            The session whose stored history should be deleted.
        dataset_name / password:
            Optional — resolved from request headers / env var.
        """

        def _impl() -> str:
            dm, ds_name = _memory_tool_dataset(dataset_name, password)
            try:
                deleted = dm.delete_session_history(ds_name, str(session_id))
            except Exception as exc:
                raise ToolError(f"Failed to delete session history: {exc}") from exc
            return json.dumps(
                {
                    "status": "deleted" if deleted else "nothing to delete",
                    "dataset": ds_name,
                    "session_id": str(session_id),
                    "deleted_chunks": deleted,
                },
                indent=2,
                default=str,
            )

        return await _offload(_impl)

    @mcp.tool()
    async def get_dataset_files(
        dataset_name: str,
        file_path: str | None = None,
        limit: int = 100,
        offset: int = 0,
        password: str | None = None,
    ) -> str:
        """List files in a dataset or retrieve a specific file's content.

        Parameters
        ----------
        dataset_name:
            Name of the dataset.
        file_path:
            Relative path of a specific file to retrieve.
            If omitted, lists files in the dataset (paginated).
            For text-based files (txt, md, json, xml, yaml, log, html, code)
            the content is returned inline.  Binary files (images, video,
            audio, PDF, office docs, etc.) return metadata plus a URL to
            the REST API endpoint for download.
        limit:
            Maximum number of files to list (default 100, max 500).
            Only applies when listing (``file_path`` is None).  Use
            with ``offset`` for pagination.  Do NOT set a large limit
            to list all files — datasets can contain tens of thousands
            of files, which will produce a huge response and waste
            context.  Use ``search_dataset`` to find specific files.
        offset:
            Number of files to skip for pagination (default 0).
        password:
            Optional if the dataset was previously unlocked with
            ``unlock_dataset``; required otherwise.  When provided this
            also acts as an implicit unlock for future calls.
        """

        def _impl() -> str:
            dm = get_manager()
            try:
                dm.get_dataset(dataset_name)
            except FileNotFoundError:
                raise ToolError(f"Dataset '{dataset_name}' not found.")
            _check_unlocked_or_password(dm, dataset_name, password)

            files_dir = dm._dataset_dir(dataset_name) / "files"
            if not files_dir.exists():
                return json.dumps({"files": [], "message": "No files in dataset."})

            # -- List files (paginated) --
            if file_path is None:
                lim = max(1, min(limit, 500))
                off = max(0, offset)
                all_files = sorted(f for f in files_dir.iterdir() if f.is_file() and not f.name.startswith("."))
                total = len(all_files)
                page = all_files[off : off + lim]
                entries: list[dict[str, Any]] = []
                from multimodal_rag.dataset_manager import _classify_file

                for f in page:
                    file_type = _classify_file(f.name)
                    stat = f.stat()
                    entries.append(
                        {
                            "name": f.name,
                            "path": f.name,
                            "size_bytes": stat.st_size,
                            "type": file_type,
                            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        }
                    )
                return json.dumps(
                    {
                        "dataset": dataset_name,
                        "file_count": total,
                        "returned": len(entries),
                        "offset": off,
                        "limit": lim,
                        "has_more": (off + lim) < total,
                        "files": entries,
                    },
                    indent=2,
                    default=str,
                )

            # -- Retrieve a specific file --
            target = files_dir / file_path
            # Resolve to prevent directory traversal
            try:
                target = target.resolve()
                target.relative_to(files_dir.resolve())
            except ValueError:
                raise ToolError("Invalid file path.")

            if not target.exists() or not target.is_file():
                raise ToolError(f"File '{file_path}' not found in dataset '{dataset_name}'.")

            stat = target.stat()
            from multimodal_rag.dataset_manager import _classify_file

            file_type = _classify_file(file_path)
            import mimetypes

            mime_type, _ = mimetypes.guess_type(file_path)

            # Text-based types that can be returned inline
            _TEXT_TYPES = frozenset(
                {
                    "text",
                    "json",
                    "code",
                    "xml",
                    "yaml",
                    "log",
                    "html",
                    "notebook",
                }
            )
            if file_type in _TEXT_TYPES:
                try:
                    content = target.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    content = None
                if content is not None:
                    return json.dumps(
                        {
                            "dataset": dataset_name,
                            "file": file_path,
                            "size_bytes": stat.st_size,
                            "type": file_type,
                            "mime_type": mime_type,
                            "content": content,
                        },
                        indent=2,
                        default=str,
                    )

            # Binary or unreadable file — return metadata + API download URL
            api_url = f"/api/datasets/{dataset_name}/files/{file_path}"
            return json.dumps(
                {
                    "dataset": dataset_name,
                    "file": file_path,
                    "size_bytes": stat.st_size,
                    "type": file_type,
                    "mime_type": mime_type,
                    "note": "Binary file — use the REST API to download.",
                    "download_url": api_url,
                },
                indent=2,
                default=str,
            )

        return await _offload(_impl)

    @mcp.tool()
    async def get_dataset_info(dataset_name: str, password: str | None = None) -> str:
        """Return metadata (including document count and password status) for a dataset.

        Parameters
        ----------
        dataset_name:
            Name of the dataset.
        password:
            Optional if the dataset was previously unlocked with
            ``unlock_dataset``; required otherwise.  When provided this
            also acts as an implicit unlock for future calls.
        """

        def _impl() -> str:
            dm = get_manager()
            try:
                dm.get_dataset(dataset_name)
            except FileNotFoundError:
                raise ToolError(f"Dataset '{dataset_name}' not found.")
            _check_unlocked_or_password(dm, dataset_name, password)
            meta = dm.get_dataset(dataset_name)
            return json.dumps(meta, indent=2, default=str)

        return await _offload(_impl)

    # ------------------------------------------------------------------
    # Standalone media understanding tools (no dataset required)
    # ------------------------------------------------------------------

    def _preprocess_media_url(media_url: str, dataset_manager) -> str:
        """Preprocess local media files to model-friendly caps.

        Applies the same resizing / truncation as the staging endpoint so
        the VLM / ASR endpoints never receive oversized payloads.  Remote
        and data URLs are returned unchanged (the model server fetches
        them directly).  Returns the (possibly new) URL.
        """
        path: str | None = None
        if media_url.startswith("file://"):
            path = media_url[7:]
        elif media_url.startswith("/") and os.path.exists(media_url):
            path = media_url
        else:
            return media_url  # http(s)://, data:, s3:// — can't preprocess

        if not os.path.exists(path):
            return media_url
        if not _media_path_allowed(path):
            # Fail closed: refuse to read a local file outside the allowed
            # prefixes (previously this only skipped preprocessing, so the
            # file was still read by the model endpoint).
            raise ToolError(f"Media path is outside MEDIA_ALLOW_PATH_PREFIXES: {path}")

        from pathlib import Path

        from multimodal_rag.dataset_manager import (
            _classify_file,
            _preprocess_image_file,
            _preprocess_video_file,
            _truncate_audio_file,
        )

        file_type = _classify_file(path)
        mpk = dataset_manager.embedder.mm_processor_kwargs
        max_pixels = mpk.get("max_pixels", 720 * 720)
        fps = mpk.get("fps", 1.0)

        src_path = Path(path)
        try:
            if file_type == "image":
                result = _preprocess_image_file(src_path, max_pixels=max_pixels)
            elif file_type == "video":
                result = _preprocess_video_file(src_path, max_pixels=max_pixels, max_fps=fps)
            elif file_type == "audio":
                result = _truncate_audio_file(src_path, max_seconds=60.0)
            else:
                return media_url
        except Exception:
            logger.warning("media preprocess failed for %s", path[-60:], exc_info=True)
            return media_url

        if result == src_path:
            return media_url  # no resize needed

        logger.info("media preprocess: %s → %s", path[-60:], result.name)
        return f"file://{result}"

    @mcp.tool()
    async def describe_media(
        media_url: str,
        query: str = "",
        media_type: str = "",
    ) -> str:
        """Describe an image or video using the vision-language model (VLM).

        Useful when the user wants a description of media **without**
        searching a dataset — e.g. "what's in this image?" or "describe
        this video".

        Parameters
        ----------
        media_url:
            URL or path to the image/video.  Accepts ``file://`` paths
            (staged uploads, dataset files), ``http(s)://`` URLs, and
            ``data:`` base64 URLs.
        query:
            Optional question about the media — the VLM will answer it
            instead of giving a generic description.
        media_type:
            Optional expected modality hint: ``"image"`` or ``"video"``.
            When empty (default) the tool detects the type automatically
            from the URL/path (extension, Content-Type/magic bytes) and,
            failing that, infers it from *query* wording (e.g. "describe
            the image").  Pass this explicitly when you *know* the media
            type and the URL/path is ambiguous.
        """

        def _impl() -> str:
            from multimodal_rag.rag_system import (
                _describe_doc,
                _media_url_to_displayable,
            )
            from multimodal_rag.utils.general_tools import sync_wrapper_safe

            # Query-time SSRF guard: a remote URL here is fetched server-side
            # by the VLM endpoint, so apply the same host policy as ingest
            # (loopback is allowed — clients legitimately hand back the
            # server's own media URLs).  file:// paths are separately
            # allowlisted in _preprocess_media_url; data: URLs are inert.
            try:
                from multimodal_rag.dataset_manager import _check_media_url_policy

                _check_media_url_policy(media_url)
            except ValueError as exc:
                raise ToolError(f"media_url rejected: {exc}")

            dm = get_manager()
            vlm = dm.vlm
            if vlm is None:
                raise ToolError("No VLM model configured on this server.")

            # Preprocess local files (resize images, transcode video) so the
            # VLM endpoint doesn't receive oversized payloads.
            processed_url = _preprocess_media_url(media_url, dm)

            # Build a doc dict in the format _describe_doc expects
            path = processed_url
            path = path.removeprefix("file://")

            # Detect modality.  The caller's *explicit* ``media_type`` hint wins; then
            # URL/path detection (extension → Content-Type/magic bytes); then a
            # low-confidence inference from *query* wording ("describe the
            # image"); finally the legacy "image"/"video" substring heuristic.
            hint = media_type.strip().lower()
            if hint not in ("", "image", "video"):
                raise ToolError("'media_type' must be 'image' or 'video' when provided.")

            file_type = hint if hint else _classify_media_type(path)

            if file_type is None:
                file_type = _infer_media_type_from_query(query)
            if file_type is None:
                if "image" in processed_url.lower():
                    file_type = "image"
                elif "video" in processed_url.lower():
                    file_type = "video"

            if file_type == "image":
                doc_dict: dict[str, Any] = {"text": "", "image": processed_url}
            elif file_type == "video":
                doc_dict = {"text": "", "video": processed_url}
            else:
                raise ToolError(
                    f"Could not determine if '{processed_url[:60]}' is an image or video. "
                    "Ensure the URL has a recognisable extension (.jpg, .png, .mp4, …), "
                    "pass media_type='image'/'video', or phrase the query to say what it is."
                )

            system_prompt = (
                "You are a detailed image and video captioning assistant. "
                "Describe the media thoroughly: include visible text, objects, "
                "people, scene context, and any relationships between them. "
                "Be precise and factual."
            )
            try:
                description = sync_wrapper_safe(
                    _describe_doc,
                    {"doc_dict": doc_dict, "query": query or None, "vlm": vlm, "system_prompt": system_prompt},
                )
            except Exception as exc:
                raise ToolError(f"VLM description failed: {exc}")

            displayable = _media_url_to_displayable(media_url)
            is_image = "image" in media_url.lower() or file_type == "image"
            return json.dumps(
                {
                    "description": description,
                    "media_url": displayable,
                    "media_type": file_type,
                    "markdown": f"![media]({displayable})" if is_image else "",
                },
                indent=2,
                default=str,
            )

        return await _offload(_impl)

    @mcp.tool()
    async def transcribe_audio(
        audio_url: str,
        max_seconds: float = 60.0,
    ) -> str:
        """Transcribe an audio file using the speech recognition model (ASR).

        Useful when the user wants a transcription **without** searching a
        dataset — e.g. "what's said in this recording?".

        Parameters
        ----------
        audio_url:
            URL or path to the audio file.  Accepts ``file://`` paths
            (staged uploads, dataset files), ``http(s)://`` URLs, and
            ``data:`` base64 URLs.
        max_seconds:
            Maximum duration (in seconds) to transcribe.  Audio longer
            than this is truncated from the start.  Default 60s to stay
            within the ASR endpoint's payload cap.
        """

        def _impl() -> str:
            from multimodal_rag.rag_system import (
                _media_url_to_displayable,
                _transcribe_media,
            )
            from multimodal_rag.utils.general_tools import sync_wrapper_safe

            # Query-time SSRF guard (same rationale as describe_media): a
            # remote URL here is fetched server-side by the ASR endpoint.
            try:
                from multimodal_rag.dataset_manager import _check_media_url_policy

                _check_media_url_policy(audio_url)
            except ValueError as exc:
                raise ToolError(f"audio_url rejected: {exc}")

            dm = get_manager()
            asr = dm.asr
            if asr is None:
                raise ToolError("No ASR model configured on this server.")

            # Preprocess (truncate local audio to max_seconds) — same helper
            # as describe_media, ensures the ASR endpoint never receives an
            # oversized payload.
            processed_url = _preprocess_media_url(audio_url, dm)

            # Check transcript cache (by original file hash)
            cache_key: str | None = None
            orig_path = audio_url
            orig_path = orig_path.removeprefix("file://")
            if os.path.exists(orig_path):
                file_hash = _hash_file(orig_path)
                cache_key = f"asr_tool:{file_hash}"
                with _asr_transcript_cache_lock:
                    cached = _asr_transcript_cache.get(cache_key)
                if cached is not None:
                    logger.info("ASR tool: cache HIT for %s", orig_path[-60:])
                    displayable = _media_url_to_displayable(audio_url)
                    return json.dumps(
                        {"transcript": cached, "audio_url": displayable},
                        indent=2,
                        default=str,
                    )

            try:
                transcript = sync_wrapper_safe(
                    _transcribe_media,
                    {"url": processed_url, "asr": asr},
                )
            except Exception as exc:
                raise ToolError(f"ASR transcription failed: {exc}")

            if not transcript:
                raise ToolError("ASR returned an empty transcript.")

            # Cache for future calls
            if cache_key:
                with _asr_transcript_cache_lock:
                    _bounded_cache_put(_asr_transcript_cache, cache_key, transcript, _MAX_ASR_TRANSCRIPT_CACHE)

            displayable = _media_url_to_displayable(audio_url)
            return json.dumps(
                {
                    "transcript": transcript,
                    "audio_url": displayable,
                    "duration_limit_seconds": max_seconds if cache_key else None,
                },
                indent=2,
                default=str,
            )

        return await _offload(_impl)

except ImportError:
    logger.error("MCP package not installed. Run: pip install mcp>=1.0.0")
    raise


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _with_mcp_health(app: ASGIApp) -> ASGIApp:
    """Attach ``/healthz`` (liveness) and ``/readyz`` (readiness) routes.

    ``FastMCP`` returns the wrapped Starlette app, which supports adding
    routes directly.  ``/healthz`` is pure process liveness (no model
    checks — the embedder is always a remote vLLM/SGLang service, and
    restarting this pod cannot bring it back).  ``/readyz`` is pure
    readiness — it does not gate on the embedder either, for the same
    reason (an embedder outage is a transient condition that a pod
    restart cannot fix; it is surfaced via ``/api/admin/health``).
    """
    from starlette.responses import JSONResponse

    async def _healthz(request: object) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def _readyz(request: object) -> JSONResponse:
        return JSONResponse({"status": "ready"})

    app.add_route("/healthz", _healthz)  # type: ignore[attr-defined]
    app.add_route("/readyz", _readyz)  # type: ignore[attr-defined]
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Multimodal RAG MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="streamable-http",
    )
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--data-path", default="/data")
    parser.add_argument("--qdrant-host", default="")
    parser.add_argument("--qdrant-port", type=int, default=6333)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    os.environ["DATA_PATH"] = args.data_path
    os.environ["QDRANT_HOST"] = args.qdrant_host
    os.environ["QDRANT_PORT"] = str(args.qdrant_port)

    setup_logger(level=args.log_level)

    # Media URLs are secured with an HMAC token that requires a shared secret.
    # Refuse to start without it rather than falling back to leaking the
    # dataset password inside ?password= URLs.
    if not _MEDIA_TOKEN_SECRET:
        logger.error(
            "MEDIA_TOKEN_SECRET is required: media URLs are signed with a short-lived "
            "HMAC token (the legacy ?password= suffix was removed for security). "
            "Set the shared MEDIA_TOKEN_SECRET env var (helm: security.mediaTokenSecret)."
        )
        raise SystemExit(1)

    import uvicorn

    if args.transport == "stdio":
        logger.info("Starting MCP stdio server")
        _start_config_watcher()
        mcp.run(transport="stdio")
    elif args.transport == "sse":
        logger.info("Starting MCP SSE server on %s:%s", args.host, args.port)
        app = mcp.sse_app(transport_security=_mcp_transport_security)
        app.add_middleware(_MemoryHeaderMiddleware)
        _start_config_watcher()
        _start_model_health_thread()
        uvicorn.run(_with_mcp_health(app), host=args.host, port=args.port)
    elif args.transport == "streamable-http":
        logger.info("Starting MCP streamable-http server on %s:%s", args.host, args.port)
        # Stateless + JSON-response mode: each HTTP request is self-contained
        # (no in-memory session tracking), so any pod in a multi-replica
        # Deployment can handle any request. This is required for horizontal
        # scaling — the default stateful mode stores sessions in-process, so a
        # request routed to a different pod than the one that initialized the
        # session fails with "Session not found". (In MCP 2.0 the protocol is
        # natively stateless; these flags keep 2025-era clients consistent.)
        app = mcp.streamable_http_app(
            json_response=True,
            stateless_http=True,
            transport_security=_mcp_transport_security,
        )
        app.add_middleware(_MemoryHeaderMiddleware)
        _start_config_watcher()
        _start_model_health_thread()
        uvicorn.run(_with_mcp_health(app), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
