from __future__ import annotations

import asyncio
import base64
import inspect
import io
import os
import re
import sys
import threading
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import cached_property, partial
from typing import TYPE_CHECKING, Any, Literal

import httpx2

try:
    from agents import Agent, set_tracing_disabled
    from agents.mcp import MCPServerStreamableHttp
    from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
    from agents.models.openai_responses import OpenAIResponsesModel

except ImportError:
    Agent = None  # type: ignore[assignment, misc]
    set_tracing_disabled = None  # type: ignore[assignment]
    MCPServerStreamableHttp = None  # type: ignore[assignment, misc]
    OpenAIResponsesModel = None  # type: ignore[assignment, misc]
    OpenAIChatCompletionsModel = None  # type: ignore[assignment, misc]

from .general_tools import sync_wrapper_safe
from .logging_utils import logging
from .token_text_splitter import TokenTextSplitter

if TYPE_CHECKING:  # names used in annotations only (from __future__ annotations)
    from openai import AsyncOpenAI, OpenAI

    from .model_adapters import MultiModalReranker

logger = logging.getLogger(__name__)


def _openai_client_class(name: str) -> Callable:
    """Resolve an OpenAI SDK client class on first use.

    openai costs ~0.5s to import and pulls in its whole generated types tree.
    Deferring it keeps a DecisionModel-only caller (which never speaks the SDK)
    from paying that, without changing the dataclass defaults callers see.
    """
    import openai

    return getattr(openai, name)


def _multi_modal_reranker() -> Callable:
    """The reranker adapter, imported on first use (same reason as above)."""
    from .model_adapters import MultiModalReranker

    return MultiModalReranker


def _no_openai_client(*_args: Any, **_kwargs: Any) -> Any:
    """Stand-in client for models that have none (DecisionModel)."""
    raise ValueError(
        "A decision model has no OpenAI client: it serves POST /v1/systemone and "
        "never /v1/chat/completions. Use system_one(...), system_one_async(...), or "
        "the helpers decide(...) / choice(...) / yes_no(...) / score(...)."
    )


_MLIS_PAGE = "https://mlis.pcai-se-ai-application.hst.rdlabs.hpecorp.net/ui/deployments"
_NOT_DEPLOYED = ""


def _pool_limits_from_env() -> httpx2.Limits:
    """Build httpx2 connection-pool limits from env vars.

    Default (128) tracks the benchmark target: concurrency levels are chosen
    so the client is never the bottleneck, and SGLang servers are typically
    configured with ``--max-running-requests`` up to 128.  Override with
    ``MODEL_POOL_MAX_CONNECTIONS`` (and ``MODEL_POOL_MAX_KEEPALIVE_CONNECTIONS``)
    for other deployments (e.g. the original single-replica chart used 30/10).
    """
    return httpx2.Limits(
        max_connections=model_pool_max_connections(),
        max_keepalive_connections=model_pool_max_keepalive_connections(),
        keepalive_expiry=120.0,
    )


def model_pool_max_connections() -> int:
    """Effective ``MODEL_POOL_MAX_CONNECTIONS`` (default 128, min 1)."""
    try:
        return max(1, int(os.environ.get("MODEL_POOL_MAX_CONNECTIONS", "128")))
    except (TypeError, ValueError):
        return 128


def model_pool_max_keepalive_connections() -> int:
    """Effective ``MODEL_POOL_MAX_KEEPALIVE_CONNECTIONS`` (default 32, min 1)."""
    try:
        return max(1, int(os.environ.get("MODEL_POOL_MAX_KEEPALIVE_CONNECTIONS", "32")))
    except (TypeError, ValueError):
        return 32


# Single source of truth for model-request timeouts.  NOTE: the openai SDK
# resolves a *per-request* timeout from the OpenAI/AsyncOpenAI ``timeout``
# kwarg — falling back to its own DEFAULT_TIMEOUT (connect=5s, read=600s) —
# and IGNORES the timeout configured on an injected httpx2 client.  So the
# SDK clients MUST be given this explicitly (see _openai_client_kwargs) or
# the httpx2-level value is dead config: a 5s connect timeout turns slow
# TLS handshakes at high concurrency into spurious request failures.
_MODEL_REQUEST_TIMEOUT = httpx2.Timeout(600.0, connect=30.0)


# TLS verification for "remote" model endpoints.  PCAI endpoints use
# self-signed certs, so the legacy default is verify=False; operators who
# can pin the platform CA should set REMOTE_CA_BUNDLE to a .crt/.pem bundle
# and verification will be performed against it (strictly better — it also
# protects the bearer API keys in flight).
_REMOTE_CA_BUNDLE = os.environ.get("REMOTE_CA_BUNDLE", "")
_REMOTE_VERIFY_WARNED = False


def _warn_once_insecure_tls(reason: str) -> None:
    """One-time stderr notice that remote TLS verification is disabled.

    Printed (not logged) because CLI entry points commonly raise the root
    logger level above WARNING, which would silence a logger.warning here.
    """
    global _REMOTE_VERIFY_WARNED
    if _REMOTE_VERIFY_WARNED:
        return
    _REMOTE_VERIFY_WARNED = True
    print(
        f"[security] {reason} Bearer tokens are unauthenticated against MITM "
        "on untrusted networks. Set REMOTE_CA_BUNDLE=/path/to/ca-bundle.pem "
        "(platform CA) to enable certificate verification.",
        file=sys.stderr,
    )


def _remote_verify() -> str | bool:
    """TLS verification setting for *remote* model endpoints.

    Returns the pinned CA bundle when ``REMOTE_CA_BUNDLE`` is set and exists
    (verification ON against that bundle), else ``False`` (the legacy
    self-signed-PCAI behaviour, warned about once per process).  Local
    in-cluster endpoints keep normal system-CA verification.
    """
    if _REMOTE_CA_BUNDLE:
        if os.path.exists(_REMOTE_CA_BUNDLE):
            return _REMOTE_CA_BUNDLE
        _warn_once_insecure_tls(
            f"REMOTE_CA_BUNDLE={_REMOTE_CA_BUNDLE!r} does not exist — falling back to "
            "TLS verification DISABLED for remote model endpoints."
        )
        return False
    _warn_once_insecure_tls(
        "TLS certificate verification is DISABLED for remote model endpoints (self-signed PCAI ingress)."
    )
    return False


def _client_verify(remote: bool) -> str | bool:
    """TLS verification for a client talking to a model endpoint."""
    return _remote_verify() if remote else True


def _new_http_client(**kwargs: Any) -> httpx2.Client:
    """Build a shared httpx client without letting the proxy environment kill the process.

    httpx reads ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY`` / ``NO_PROXY``
    at CONSTRUCTION time (``trust_env`` defaults to True).  A ``NO_PROXY`` entry
    in bracketed IPv6 form -- ``[::1]``, a very common corporate/desktop default
    -- makes httpx2's URL parser raise ``InvalidURL: Invalid port: ':1]'``.  At
    module scope that aborted the import of the whole package (2026-11-10 audit:
    reproduced, 65 pytest collection errors, app would not start).

    Retry once with the environment ignored rather than dying: losing proxy
    support for model calls is a degraded network path, an unimportable package
    is an outage.
    """
    try:
        return httpx2.Client(**kwargs)
    except Exception as exc:  # retried once below, then raised
        logger.warning(
            "httpx client construction failed with the process proxy environment (%s: %s) - "
            "retrying with trust_env=False; check HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY",
            type(exc).__name__,
            exc,
        )
        kwargs["trust_env"] = False
        return httpx2.Client(**kwargs)


# Sync clients are thread-safe and can be shared globally.  Both are built on
# first use (never at import): construction reads the proxy environment and,
# for the remote client, configures TLS -- neither belongs in module import.
_SHARED_HTTP_CLIENT: httpx2.Client  # built on first use; see below
_shared_client_lock = threading.Lock()
# The remote client is built on first use by the module __getattr__ below, so
# importing this module never configures TLS nor prints the "verification
# disabled" notice -- that now fires when a remote model is actually used.
_SHARED_REMOTE_HTTP_CLIENT: httpx2.Client  # built on first use; see below
_shared_remote_client_lock = threading.Lock()


def _shared_http_client() -> httpx2.Client:
    """The process-wide client for in-cluster (non-remote) model endpoints.

    Built lazily for the same reason as the remote client: an import-time
    ``httpx2.Client()`` turns a malformed proxy variable into an unimportable
    package (see :func:``_new_http_client``).
    """
    client = globals().get("_SHARED_HTTP_CLIENT")
    if client is not None:
        return client
    with _shared_client_lock:
        client = globals().get("_SHARED_HTTP_CLIENT")
        if client is None:
            client = _new_http_client()
            globals()["_SHARED_HTTP_CLIENT"] = client
    return client


def _shared_remote_http_client() -> httpx2.Client:
    """The process-wide client for remote endpoints, built on first use.

    Built lazily because constructing it calls _client_verify(remote=True),
    which emits the one-time "TLS verification disabled" notice. At import that
    notice fired for every process that merely imported the module -- including
    ones that only ever talk to in-cluster endpoints.
    """
    client = globals().get("_SHARED_REMOTE_HTTP_CLIENT")
    if client is not None:
        return client
    with _shared_remote_client_lock:
        client = globals().get("_SHARED_REMOTE_HTTP_CLIENT")
        if client is None:
            client = _new_http_client(
                verify=_client_verify(remote=True),
                limits=_pool_limits_from_env(),
            )
            globals()["_SHARED_REMOTE_HTTP_CLIENT"] = client
    return client


def __getattr__(name: str) -> Any:
    """Keep the shared-client globals readable as module attributes (PEP 562).

    Both are lazy now, so a bare attribute read must still resolve to the
    constructed client (legacy access path).  New code should call
    _shared_http_client() / _shared_remote_http_client().
    """
    if name == "_SHARED_REMOTE_HTTP_CLIENT":
        return _shared_remote_http_client()
    if name == "_SHARED_HTTP_CLIENT":
        return _shared_http_client()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Async clients must be bound to a single event loop.
# We cache one per running loop (keyed by the loop object itself, not
# id(loop), to avoid id() recycling after GC hands a stale client to a
# fresh loop).  WeakKeyDictionary auto-evicts when the loop is GC'd.
_async_client_cache: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_async_remote_client_cache: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_async_client_lock = threading.Lock()

# Fallback async client used when no event loop is running.  A single
# process-global instance is reused (and lives for the process) so this
# rare path never leaks a fresh client+sockets per call.
_async_client_fallback: httpx2.AsyncClient | None = None
_async_client_fallback_lock = threading.Lock()

# Cache of discovered model names keyed by the ``{base_url}/models`` URL.
# Only successful lookups are stored so a temporarily-down endpoint is
# retried on the next construction. Sessions rebuild ASR/LLM/TTS frequently,
# so this avoids redundant /v1/models round-trips per session.  Bounded to
# prevent unbounded growth across many distinct endpoints.
_discovered_model_names: dict[str, str] = {}
_discovered_model_lock = threading.Lock()
_MAX_DISCOVERED_MODEL_NAMES = max(64, int(os.environ.get("DISCOVERED_MODEL_CACHE_MAX", "512")))
# Negative cache: a *failed* discovery is retried only after this TTL.
# Without it, every model construction against a down endpoint repeats the
# full 10s timeout — historically on the event loop itself.
_DISCOVERY_FAILURE_TTL_S = 60.0
_discovery_failures: dict[str, float] = {}

# Same negative-cache idea for DecisionModel model-card reads: the card is
# fetched lazily on first capability access, and a down endpoint must not be
# re-probed on every question_types/supports call.
_MODEL_CARD_FAILURE_TTL_S = 60.0
_model_card_failures: dict[str, float] = {}


def _http_status_error_detail(exc: httpx2.HTTPStatusError) -> str:
    """A one-line reason from a non-2xx discovery answer: status + the API's
    own ``detail``/``error`` text. The PCAI LLM gateway says exactly why a
    key was refused ("invalid, revoked, or expired key"), which is the whole
    diagnosis for a 401 -- raise_for_status() alone would hide it."""
    response = exc.response
    message = f"HTTP {response.status_code} {response.reason_phrase}".strip()
    detail = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        raw = body.get("detail") or body.get("error") or body.get("message")
        if isinstance(raw, str):
            detail = raw
    if not detail:
        detail = (response.text or "").strip()[:200]
    return f"{message}: {detail}" if detail else message


def _fetch_model_ids(
    base_url: str, api_key: str = "", remote: bool = True, timeout: float = 10.0
) -> tuple[list[str], str]:
    """One ``GET {base_url}/models`` probe: (model ids, error reason).

    ``base_url`` is the OpenAI base URL (root + ``"/v1"``). Never raises: a
    down endpoint or a non-200 answer comes back as an empty list plus a
    human-readable reason; a reachable endpoint that lists no models returns
    ``([], "")`` (an empty answer is not a failure). Auto-discovery (which
    wants the first id, and caches it) and the webapp's model dropdown (which
    wants the whole list, uncached) share this one probe so both always see
    exactly the same models.
    """
    url = f"{base_url}/models"
    client = _shared_remote_http_client() if remote else _shared_http_client()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        resp = client.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        models = data.get("data", []) if isinstance(data, dict) else []
        ids = [str(m["id"]) for m in models if isinstance(m, dict) and m.get("id")]
        return ids, ""
    except httpx2.HTTPStatusError as e:
        return [], _http_status_error_detail(e)
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


def discover_model_name(base_url: str, api_key: str = "", remote: bool = True) -> str:
    """Best-effort lookup of the served model id via ``GET {base_url}/models``.

    ``base_url`` is the OpenAI base URL (root + ``"/v1"``).  PCAI/vLLM endpoints
    serve a single model, so the first ``data[].id`` is returned.  Returns
    ``""`` on any failure (network, non-200, empty list) -- keeping this
    best-effort so a transient blip never hard-fails the caller.  Successful
    lookups are cached per URL; failures carry a short negative-TTL so a
    down endpoint is not re-probed on every single construction.
    """
    url = f"{base_url}/models"
    cached = _discovered_model_names.get(url)
    if cached:
        return cached
    with _discovered_model_lock:
        last_failure = _discovery_failures.get(url)
    if last_failure is not None and (time.monotonic() - last_failure) < _DISCOVERY_FAILURE_TTL_S:
        return ""
    ids, error = _fetch_model_ids(base_url, api_key, remote=remote)
    if error:
        logger.warning(
            "Could not auto-discover model name from %s: %s. "
            "Set model_name explicitly if the endpoint lacks /v1/models.",
            url,
            error,
        )
        with _discovered_model_lock:
            _discovery_failures[url] = time.monotonic()
        return ""
    if not ids:
        logger.warning("No models listed at %s; leaving model_name empty.", url)
        with _discovered_model_lock:
            _discovery_failures[url] = time.monotonic()
        return ""
    logger.info(
        "Auto-discovered model name '%s' from %s (available: %s)",
        ids[0],
        url,
        ids,
    )
    with _discovered_model_lock:
        _discovered_model_names[url] = ids[0]
        _discovery_failures.pop(url, None)
        if len(_discovered_model_names) > _MAX_DISCOVERED_MODEL_NAMES:
            _discovered_model_names.pop(next(iter(_discovered_model_names)))
    return ids[0]


def discover_model_ids(
    base_url: str, api_key: str = "", remote: bool = True, timeout: float = 10.0
) -> tuple[list[str], str]:
    """Every model id advertised by ``GET {base_url}/models``: (ids, error).

    The uncached full-list counterpart of :func:`discover_model_name` -- for
    callers that present a CHOICE (the webapp's chat-page model dropdown)
    rather than resolving the one served model. Same client, TLS posture and
    auth header as auto-discovery, so the list matches what a run would see.
    """
    return _fetch_model_ids(base_url, api_key, remote=remote, timeout=timeout)


def _get_async_client(remote: bool = False) -> httpx2.AsyncClient:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        # No running loop — reuse a single process-global client instead of
        # leaking a fresh client (and its sockets) on every call.
        global _async_client_fallback
        with _async_client_fallback_lock:
            if _async_client_fallback is None:
                _async_client_fallback = httpx2.AsyncClient(
                    verify=_client_verify(remote),
                    timeout=_MODEL_REQUEST_TIMEOUT,
                    limits=_pool_limits_from_env(),
                )
            return _async_client_fallback
    cache = _async_remote_client_cache if remote else _async_client_cache
    with _async_client_lock:
        client = cache.get(loop)
        if client is None:
            client = httpx2.AsyncClient(
                verify=_client_verify(remote),
                timeout=_MODEL_REQUEST_TIMEOUT,
                limits=_pool_limits_from_env(),
            )
            cache[loop] = client
        return client


__all__ = [
    "BaseModel",
    "ChatModel",
    "DecisionModel",
    "EmbeddingModel",
    "RerankerModel",
    "SpeechFlowModel",
    "ToolDefinition",
    "VoiceModel",
    "discover_model_name",
    "prefetch_model_cards",
    "strip_tool_markers",
]

# Strip from a tool-call marker through its closing `</tool_call>` (when
# present) so a real answer emitted after a *closed* wrapper survives.
# Unterminated wrappers (the common case) still strip to end-of-text.
_TOOL_CALL_BLOCK = re.compile(r"<\|?\s*tool_call\s*\|?>.*?(?:</tool_call\s*>|$)", re.DOTALL)
_INTERNAL_FILE_REF = re.compile(r"(?im)^[ \t]*input_files?\.[0-9\w\-.]+[.\w]*\s*$")
_GENERIC_ANGLE = re.compile(r"<\|[a-z_]+\|>")


def strip_tool_markers(text: str) -> str:
    """Remove agent-token artifacts left by tool-calling models.

    Models tuned for function calling (e.g. the Gemma VLM) sometimes emit
    synthetic ``<|tool_call|>…`` wrappers and internal file refs such as
    ``input_file_0.png`` instead of a plain answer.  Strip those before the
    text reaches the caller so a "summarize the image" call returns clean
    prose.
    """
    if not text:
        return text
    text = _TOOL_CALL_BLOCK.sub("", text)
    text = _INTERNAL_FILE_REF.sub("", text)
    text = _GENERIC_ANGLE.sub("", text)
    return text.strip()


input_modalities = Literal["text", "audio", "image", "video"]
messages_dtype = str | dict[str, Any] | list[dict[str, Any]]


@dataclass
class ToolDefinition:
    """Describes a single MCP tool exposed by a model endpoint.

    The ``handler`` is an async callable taking a single ``dict[str, Any]``
    of arguments (matching ``input_schema``) and returning a JSON-serializable
    dict.  The orchestration layer wraps handlers with depth/budget
    instrumentation before they reach the MCP server.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[Any]]


async def _get_mcp_servers(
    mcp_servers: dict[str, dict[str, Any]],
) -> list[MCPServerStreamableHttp]:
    """Build and connect one MCPServerStreamableHttp per entry in the
    {name: {url, headers, transport}} config dict. Each server is returned
    already connected; the caller is responsible for calling cleanup() on
    each when the owning session ends.
    """
    servers: list[MCPServerStreamableHttp] = []
    for name, cfg in mcp_servers.items():
        try:
            params: dict[str, Any] = {
                "url": cfg["url"],
                # TLS-bypass httpx2 factory (PCAI ingress serves self-signed
                # certs); defined below.
                "httpx_client_factory": _streamable_http_factory,
                # Skip the session-terminate DELETE on cleanup - the PCAI
                # istio ingress doesn't support DELETE on /mcp and the call
                # hangs until asyncio tears the loop down. MCP servers will
                # reap idle sessions by TTL on their side.
                "terminate_on_close": False,
            }
            if cfg.get("headers"):
                params["headers"] = cfg["headers"]
            # The openai-agents SDK hardcodes a 5s MCP session read timeout (*)
            # and a 5s httpx2 request timeout (**) unless overridden. Real tool
            # calls (SQL queries, k8s ops, ...) routinely exceed that, surfacing
            # as "Timed out while waiting for response to ClientRequest. Waited
            # 5.0 seconds." Allow an optional per-server `timeout` (seconds) in
            # the tool config, falling back to a saner default. We apply it to
            # both the ClientSession read timeout and the underlying httpx2
            # request so neither layer cancels slow tool calls.
            #   (*)  MCPServerStreamableHttp.client_session_timeout_seconds
            #   (**) MCPServerStreamableHttp.params["timeout"]
            request_timeout = float(cfg.get("timeout", 30))
            params.setdefault("timeout", request_timeout)
            server = MCPServerStreamableHttp(
                params=params,
                name=name,
                client_session_timeout_seconds=request_timeout,
            )  # type: ignore[arg-type]
            await server.connect()
            servers.append(server)
        except Exception as e:
            logger.warning("Failed to load MCP server %s: %s", name, e)
    return servers


def _streamable_http_factory(
    headers: dict[str, str] | None = None,
    timeout: httpx2.Timeout | None = None,
    auth: httpx2.Auth | None = None,
) -> httpx2.AsyncClient:
    """httpx2 client factory for MCPServerStreamableHttp (MCP Python SDK v2).
    TLS verification follows the same REMOTE_CA_BUNDLE toggle as the main
    remote clients (PCAI ingress serves self-signed certs, so it defaults
    to disabled with a one-time warning). Mirrors the default factory
    signature so it can be dropped in via params['httpx_client_factory']."""
    kwargs: dict = {"follow_redirects": False, "verify": _remote_verify()}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if headers is not None:
        kwargs["headers"] = headers
    if auth is not None:
        kwargs["auth"] = auth
    return httpx2.AsyncClient(**kwargs)


class _NamedBytesIO(io.BytesIO):
    """BytesIO with a ``name`` attribute, required by the OpenAI
    transcription API to infer the audio format."""

    def __init__(self, content: bytes, name: str = "audio"):
        super().__init__(content)
        self.name = name


@dataclass
class BaseModel:
    """Shared plumbing for every PCAI model wrapper in the catalog.

    A BaseModel is a dataclass describing ONE deployed endpoint plus the lazy
    machinery to talk to it: an OpenAI-compatible sync/async client, the
    resolved base URL, and (in subclasses) a higher-level adapter.

    Fields
    ------
    model_name:
        The served model id, e.g. "Qwen/Qwen3.8-27B-FP8". May be left empty:
        when currently_deployed is True it is discovered at construction time
        from GET {base_url}/models.
    url_remote:
        The externally reachable serving URL (the PCAI ingress hostname).
    description:
        Free text used for tool descriptions; a generated one is used if empty.
    model_instantiation_class / model_instantiation_kwargs:
        Optional callable (plus kwargs) that builds the high-level adapter
        returned by .model. When None, .model raises and callers use .client
        or .async_client directly.
    model_usage:
        "local" (in-cluster URL, plain HTTP) or "remote" (ingress URL, TLS).
        Switch with .local() / .remote(); both drop the memoized clients.
    api_key:
        Bearer token for the endpoint. Empty means a key-less endpoint.
    currently_deployed:
        False marks a catalog placeholder: construction skips model-name
        discovery and .client / .async_client refuse to build.
    allowable_modalities:
        Which of text/audio/image/video this model accepts.

    Attributes
    ----------
    base_url:
        {url_local or url_remote}/v1, chosen by model_usage.
    client / async_client:
        OpenAI / AsyncOpenAI bound to base_url, sharing one pooled HTTP client.
    model:
        The high-level adapter built by build_model(); subclass-specific.
    """

    # Model args. ``model_name`` is optional: when left empty (and the model
    # is deployed) it is auto-discovered at construction time from the
    # OpenAI-compatible ``GET {base_url}/models`` endpoint -- the same trick
    # Open WebUI uses. Pass an explicit name to pin/override it.
    model_name: str = ""
    url_remote: str = ""

    # Model args w/ defaults
    description: str = ""
    model_instantiation_class: Callable | None = None
    model_instantiation_kwargs: dict[str, Any] = field(default_factory=dict)

    # OpenAI clients
    model_client_class: Callable = field(default_factory=lambda: _openai_client_class("OpenAI"))
    model_async_client_class: Callable = field(default_factory=lambda: _openai_client_class("AsyncOpenAI"))

    model_usage: Literal["local", "remote"] = "local"
    api_key: str = ""

    _cached_properties: tuple[str, ...] = field(
        default=(
            "client",
            "async_client",
            "base_url",
            "http_client",
            "http_async_client",
            "model",
        ),
        init=False,
        repr=False,
    )
    _cached_functions: tuple[str, ...] = field(default=(), init=False, repr=False)

    currently_deployed: bool = True

    allowable_modalities: tuple[input_modalities, ...] = ("text",)

    @classmethod
    def from_config(cls, config: dict[str, Any], api_key: str = "") -> BaseModel:
        """Build a model from a stored config dict and a resolved API key.

        The counterpart of dumping a catalog entry to JSON: unknown keys are
        ignored, so a config written for a newer or older catalog still loads.

        Parameters
        ----------
        config:
            Field values keyed by dataclass field name (the shape stored in
            the app DB). Only keys that exist on cls are used. Callable fields
            (preprocessor, model_instantiation_class, model_client_class,
            model_async_client_class) are skipped when the value is a string,
            because a callable cannot be deserialized from JSON. allowable_modalities
            lists become tuples and tts_supported_voices lists become sets.
        api_key:
            The bearer token for the endpoint, resolved by the caller (env
            var, secret, or DB). Overrides any api_key present in config.

        Returns
        -------
        BaseModel
            An instance of cls, so ChatModel.from_config gives a ChatModel.

        Raises
        ------
        TypeError
            config carries a value of the wrong type for the dataclass field.
        """
        from dataclasses import fields as dc_fields

        field_names = {f.name for f in dc_fields(cls)}
        _CALLABLE_FIELDS = frozenset(
            {
                "preprocessor",
                "model_instantiation_class",
                "model_client_class",
                "model_async_client_class",
            }
        )
        kwargs: dict[str, Any] = {}
        for k, v in config.items():
            if k not in field_names:
                continue
            if k in _CALLABLE_FIELDS and not callable(v):
                continue
            if k == "allowable_modalities" and isinstance(v, list):
                v = tuple(v)
            if k == "tts_supported_voices" and isinstance(v, list):
                v = set(v)
            kwargs[k] = v
        kwargs["api_key"] = api_key
        return cls(**kwargs)  # type: ignore[arg-type]

    def __post_init__(self) -> None:
        # Auto-discover the model name from the serving endpoint when none was
        # supplied.  This mirrors Open WebUI, which lists models via the
        # OpenAI ``/v1/models`` API rather than requiring a hardcoded name.
        # Disabled models are skipped so the catalog can hold placeholders.
        if not self.model_name and self.currently_deployed:
            self.model_name = self._discover_model_name()
        if self.model_usage == "remote":
            # One-time notice that remote TLS is unverified. Emitted here, not
            # at import, so it only fires for processes that actually go remote.
            _remote_verify()

    def _discover_model_name(self) -> str:
        """Look up the served model id via ``GET {base_url}/models``.

        Thin instance wrapper around :func:`discover_model_name` that passes
        this model's resolved base URL, API key, and transport.
        """
        return discover_model_name(
            self.base_url,
            self.api_key,
            remote=self.model_usage == "remote",
        )

    def _clear_cached_class_elements(self) -> None:
        for property in self._cached_properties + self._cached_functions:
            if property in self.__dict__:
                self.__dict__.pop(property)
                logger.info(f"Removed property {property}")

    @staticmethod
    def _convert_remote_url_to_local(path: str) -> str:
        """Rewrite a remote PCAI serving URL to its in-cluster form.

        Only URLs containing the ``.serving.`` marker (PCAI serving hostnames)
        are rewritten; any other URL (e.g. a localhost test endpoint passed via
        ``--url``) is returned unchanged instead of being mangled.
        """
        new_path = path.replace("https", "http")
        marker = new_path.find(".serving.")
        if marker == -1:
            return new_path
        return new_path[:marker] + ".svc.cluster.local"

    def _openai_client_kwargs(self, client_cls: Callable, http_client: Any) -> dict[str, Any]:
        """Keyword args shared by the sync/async OpenAI client constructors.

        When ``api_key`` is empty (a key-less OpenAI-compatible endpoint) and
        the client class supports it, relax credential enforcement
        (``_enforce_credentials=False``) so a client can still be built — the
        SDK otherwise raises "Missing credentials" on an empty key. Requests
        then simply carry no Authorization header (the SDK's ``auth_headers``
        already returns ``{}`` for an empty key); callers targeting openai>=2
        additionally pass ``extra_headers={"Authorization": Omit()}`` per
        request so the header is explicitly omitted.
        """
        kwargs: dict[str, Any] = {
            "api_key": self.api_key,
            "base_url": self.base_url,
            "http_client": http_client,
            # The SDK ignores the injected client's timeout and applies its own
            # DEFAULT_TIMEOUT (connect=5s, read=600s) per request unless one
            # is given here — pass ours so connect=30s survives.
            "timeout": _MODEL_REQUEST_TIMEOUT,
        }
        if not self.api_key and "_enforce_credentials" in inspect.signature(client_cls.__init__).parameters:
            kwargs["_enforce_credentials"] = False
        return kwargs

    @cached_property
    def client(self) -> OpenAI:
        assert self.currently_deployed, (
            f"Model {self.model_name} is not currently deployed. "
            f"See {_MLIS_PAGE} and change flag `currently_deployed` to enable."
        )
        return self.model_client_class(**self._openai_client_kwargs(self.model_client_class, self.http_client))

    @cached_property
    def async_client(self) -> AsyncOpenAI:
        assert self.currently_deployed, (
            f"Model {self.model_name} is not currently deployed. "
            f"See {_MLIS_PAGE} and change flag `currently_deployed` to enable."
        )
        return self.model_async_client_class(
            **self._openai_client_kwargs(self.model_async_client_class, self.http_async_client)
        )

    @cached_property
    def url_local(self) -> str:
        return self._convert_remote_url_to_local(self.url_remote)

    @cached_property
    def base_url(self) -> str:
        return (self.url_local if self.model_usage == "local" else self.url_remote) + "/v1"

    @cached_property
    def http_client(self):
        return _shared_remote_http_client() if self.model_usage == "remote" else _shared_http_client()

    @cached_property
    def http_async_client(self):
        return _get_async_client(remote=self.model_usage == "remote")

    @cached_property
    def model(self):
        return self.build_model()

    def build_model(self, **kwargs):
        """Instantiate the high-level adapter for this endpoint.

        Parameters
        ----------
        **kwargs:
            Extra keyword arguments forwarded to model_instantiation_class on
            top of model / api_key / base_url / http_client /
            http_async_client and the configured model_instantiation_kwargs.

        Returns
        -------
        Any
            Whatever model_instantiation_class returns (for example a
            MultiModalEmbeddings for an EmbeddingModel).

        Raises
        ------
        ValueError
            model_instantiation_class is None: this catalog entry has no
            adapter, so use .client or .async_client instead.
        """
        if self.model_instantiation_class is None:
            raise ValueError(
                "model_instantiation_class is not set. The langchain-based defaults "
                "(ChatOpenAI / OpenAIEmbeddings) were removed; pass an explicit "
                "callable (e.g. MultiModalEmbeddings) or use `.client` / "
                "`.async_client` for direct OpenAI API access."
            )
        return self.model_instantiation_class(
            model=self.model_name,
            api_key=self.api_key,
            base_url=self.base_url,
            http_client=self.http_client,
            http_async_client=self.http_async_client,
            **kwargs,
            **self.model_instantiation_kwargs,
        )

    def remote(self) -> None:
        """Switch this model to its externally reachable ingress URL.

        Sets model_usage to "remote" and drops every cached client/property, so
        the next access rebuilds against url_remote (TLS-verified unless
        REMOTE_CA_BUNDLE points at a platform CA bundle).
        """
        self.model_usage = "remote"
        _remote_verify()  # one-time notice: remote TLS is unverified
        self._clear_cached_class_elements()

    def local(self) -> None:
        """Switch this model to its in-cluster URL (plain HTTP).

        Sets model_usage to "local" and drops every cached client/property, so
        the next access rebuilds against the rewritten .svc.cluster.local
        address. Only meaningful when running inside the cluster.
        """
        self.model_usage = "local"
        self._clear_cached_class_elements()

    _REPR_SKIP = frozenset(
        {
            "_cached_properties",
            "_cached_functions",
            "model_client_class",
            "model_async_client_class",
            "model_instantiation_class",
            "model_instantiation_kwargs",
        }
    )

    def __repr__(self) -> str:
        fields = []
        from dataclasses import fields as dc_fields

        for f in dc_fields(self):
            if f.name in self._REPR_SKIP or not f.repr:
                continue
            val = getattr(self, f.name)
            # Mask API keys (anything non-empty — short tokens must not leak either)
            if f.name == "api_key" and isinstance(val, str) and val:
                val = val[:4] + "..." if len(val) > 8 else "***"
            # Truncate long URLs
            if f.name == "url_remote" and isinstance(val, str) and len(val) > 80:
                val = val[:60] + "..." + val[-17:]
            # Pretty-print modality tuples
            if f.name == "allowable_modalities" and isinstance(val, tuple):
                val = "(" + ", ".join(val) + ")"
            fields.append(f"  {f.name}={val}")
        return self.__class__.__name__ + "(\n" + "\n".join(fields) + "\n)"


@dataclass(repr=False)
class ChatModel(BaseModel):
    """A chat/completion model (LLM or VLM) on a PCAI serving endpoint.

    Adds a normalising chat call on top of BaseModel: whatever you pass as
    messages is rewritten into valid OpenAI chat messages, including the
    {text, image, video} dict form that would otherwise be sent to the model as
    a tool call instead of being read as an image.

    Attributes
    ----------
    transport:
        Which Agents SDK transport to use: "chat-completions" (default; the
        only reliable tool-calling path on SGLang/vLLM) or "responses"
        (/v1/responses, for the real OpenAI API).

    Examples
    --------
        out = model.llm_chat_function_call("Summarise this log line.")
        print(out.choices[0].message.content)
    """

    _cached_functions = (
        "llm_chat_function",
        "llm_async_chat_function",
        "llm_response_function",
        "llm_async_response_function",
    )

    # Which OpenAI-compatible transport the Agents SDK should use.
    #   "chat-completions" (default) — /chat/completions; the only reliable
    #        tool-calling path on SGLang/vLLM (their /v1/responses rejects
    #        tool-result round-trips with a 400 validation error).
    #   "responses" — /v1/responses via OpenAIResponsesModel (e.g. real OpenAI).
    transport: str = "chat-completions"

    @staticmethod
    def _fix_chat_inputs(messages: messages_dtype) -> list[dict[str, Any]]:
        """Normalise arbitrary caller input into OpenAI chat messages.

        Handles:
        * a plain string  -> ``[{"role":"user","content": str}]``
        * message dicts (with role/content) -> kept, empty/Nones fixed
        * tool-style dicts like ``{"text": ..., "image": ...}`` -> wrapped as
          a user message with a proper ``content`` array (so the VLM actually
          sees the image instead of re-emitting a tool call for it).
        """
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        elif isinstance(messages, dict):
            messages = [messages]

        def _parts_for(raw: Any) -> Any:
            """Convert a text/str-or-{text,image,...} value into content parts."""
            if isinstance(raw, str):
                return raw
            if isinstance(raw, dict):
                has_img = any(k in raw for k in ("image", "image_url", "video"))
                has_text = bool(raw.get("text"))
                if has_img or has_text:
                    parts: list[dict[str, Any]] = []
                    if has_text:
                        parts.append({"type": "text", "text": str(raw["text"])})
                    urls: list[str] = []
                    for k in ("image", "image_url"):
                        v = raw.get(k)
                        if v is None:
                            continue
                        items = v if isinstance(v, list) else [v]
                        for it in items:
                            if isinstance(it, dict):
                                it = it.get("url", "")
                            if it:
                                urls.append(str(it))
                    for u in urls:
                        parts.append({"type": "image_url", "image_url": {"url": u}})
                    return parts
                return raw
            return raw

        fixed: list[dict[str, Any]] = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            if "role" in m or "content" in m:
                # A dict carrying `role` and/or `content` is a message; a
                # missing role defaults to "user".  Handles OpenAI-style
                # messages that were passed in without a role.
                m2 = {**m, "role": m.get("role") or "user"}
                content = m2.get("content")
                if content is None or (isinstance(content, list) and not content):
                    m2["content"] = ""
                elif isinstance(content, dict):
                    m2["content"] = _parts_for(content)
                elif "image" in m2 or "image_url" in m2 or "text" in m2:
                    m2["content"] = _parts_for(m2)
                    for k in ("image", "image_url", "text"):
                        m2.pop(k, None)
                fixed.append(m2)
            else:
                # role-less, content-less input: wrap as a user message.
                fixed.append({"role": "user", "content": _parts_for(m)})
        return fixed

    @cached_property
    def llm_chat_function(self) -> Callable:
        return partial(self.client.chat.completions.create, model=self.model_name)

    def llm_chat_function_call(self, messages: messages_dtype, **chat_kwargs):
        """Run one blocking Chat Completions call.

        Parameters
        ----------
        messages:
            A plain string (wrapped as a single user message), one message
            dict, or a list of message dicts. Multimodal content is accepted
            either in OpenAI content-parts form or as a {text, image,
            image_url, video} dict, which is converted to content parts.
        **chat_kwargs:
            Any extra Chat Completions parameter (temperature, max_tokens,
            tools, response_format, ...), passed straight to the SDK.

        Returns
        -------
        openai.types.chat.ChatCompletion
            The raw SDK response: choices[0].message.content holds the text and
            usage carries the token counts.

        Raises
        ------
        AssertionError
            The catalog entry is marked currently_deployed = False.

        Examples
        --------
        A text prompt, then a vision prompt::

            r = model.llm_chat_function_call("Name this colour: #ff0000")
            r = model.llm_chat_function_call(
                {"text": "What is in this image?", "image": "data:image/png;base64,..."}
            )
        """
        return self.llm_chat_function(messages=self._fix_chat_inputs(messages), **chat_kwargs)

    @cached_property
    def llm_async_chat_function(self) -> Callable:
        return partial(self.async_client.chat.completions.create, model=self.model_name)

    def llm_async_chat_function_call(self, messages: str | dict[str, Any] | list[dict[str, Any]], **chat_kwargs):
        """Awaitable form of llm_chat_function_call.

        Takes and returns the same things, but must be awaited from async code:

            r = await model.llm_async_chat_function_call("Summarise this.")

        Returns
        -------
        openai.types.chat.ChatCompletion
            The raw SDK response.
        """
        return self.llm_async_chat_function(messages=self._fix_chat_inputs(messages), **chat_kwargs)

    @cached_property
    def llm_response_function(self) -> Callable:
        return partial(self.client.responses.create, model=self.model_name)

    def llm_response_function_call(self, input: str | dict[str, Any] | list[dict[str, Any]], **chat_kwargs):
        """Run one blocking Responses API call (/v1/responses).

        Unlike the chat helpers this does NOT normalise multimodal dicts; the
        Responses API takes input items directly.

        Parameters
        ----------
        input:
            A prompt string, a response input item dict, or a list of them.
        **chat_kwargs:
            Extra Responses parameters (instructions, max_output_tokens,
            temperature, tools, ...).

        Returns
        -------
        openai.types.responses.Response
            The raw response; output_text holds the concatenated text.

        Raises
        ------
        openai.BadRequestError
            The endpoint does not implement /v1/responses. SGLang and vLLM
            reject tool-result round-trips here, which is why transport
            defaults to "chat-completions".
        """
        return self.llm_response_function(input=input, **chat_kwargs)

    @cached_property
    def llm_async_response_function(self) -> Callable:
        return partial(self.async_client.responses.create, model=self.model_name)

    def llm_async_response_function_call(self, input: str | dict[str, Any] | list[dict[str, Any]], **chat_kwargs):
        """Awaitable form of llm_response_function_call.

        Parameters
        ----------
        input:
            A prompt string, a response input item dict, or a list of them.
            Unlike the chat helpers, multimodal dicts are not normalised here.
        **chat_kwargs:
            Extra Responses parameters (instructions, max_output_tokens,
            temperature, tools, ...).

        Returns
        -------
        openai.types.responses.Response
            The raw response; output_text holds the concatenated text.
        """
        return self.llm_async_response_function(input=input, **chat_kwargs)

    def agent(self, tool_json: dict[str, dict[str, Any]] | None = None):
        """Build an Agents SDK Agent bound to this model (blocking wrapper).

        Parameters
        ----------
        tool_json:
            Optional mapping of MCP server name -> connection config. Each
            entry is attached as a streamable-HTTP MCP server and becomes the
            agent tools. Omit for a plain model agent.

        Returns
        -------
        agents.Agent
            The SDK agent, ready for Runner.run(...).

        Raises
        ------
        ImportError
            The openai-agents package is not installed.
        """
        if Agent is None:
            raise ImportError("The `agents` SDK is not installed. Install with: pip install openai-agents")
        return sync_wrapper_safe(self.aagent, {"tool_json": tool_json})

    async def aagent(self, tool_json: dict[str, dict[str, Any]] | None = None) -> Agent:
        """Awaitable form of agent(); use it when already inside an event loop.

        Tracing is disabled process-wide on first call, because the SDK
        otherwise phones home to api.openai.com, which fails behind the PCAI
        firewall.

        Parameters
        ----------
        tool_json:
            Optional MCP server configs, as for agent().

        Returns
        -------
        agents.Agent
            The SDK agent.

        Raises
        ------
        ImportError
            The openai-agents package is not installed.
        """
        if Agent is None:
            raise ImportError("The `agents` SDK is not installed. Install with: pip install openai-agents")
        # Tracing off: the SDK phones home to api.openai.com by default,
        # which fails behind the PCAI firewall and adds noise to logs.
        # set_tracing_disabled is process-global; safe to call repeatedly.
        set_tracing_disabled(True)
        # SGLang/vLLM tool-calling is battle-tested over Chat Completions;
        # their Responses API (/v1/responses) cannot round-trip tool results
        # — follow-up turns that include output_text/input_text content parts
        # get rejected with "26 validation errors for ChatCompletionRequest"
        # (HTTP 400). Default to the Chat Completions model; set
        # `llmTransport: "responses"` in config to fall back to the Responses
        # API (e.g. against the real OpenAI API).
        if self.transport == "chat-completions":
            model_obj = OpenAIChatCompletionsModel(model=self.model_name, openai_client=self.async_client)
        else:
            model_obj = OpenAIResponsesModel(model=self.model_name, openai_client=self.async_client)
        if not tool_json:
            return Agent(name=self.model_name, model=model_obj)
        servers = await _get_mcp_servers(tool_json)
        return Agent(name=self.model_name, model=model_obj, mcp_servers=servers)  # type: ignore[arg-type]

    def to_mcp_tools(self) -> list[ToolDefinition]:
        """Expose this chat model as a single ``respond`` tool.

        Uses the Chat Completions API (``/chat/completions``) — compatible
        with the PCAI/vLLM serving endpoints — instead of the Responses API,
        which those endpoints reject.

        The handler always does a single non-looping call — tool-calling
        agent loops are handled by the orchestration layer, not here.
        """

        async def _respond(arguments: dict[str, Any]) -> dict[str, Any]:
            messages = self._fix_chat_inputs(arguments["input"])
            if arguments.get("instructions"):
                messages = [{"role": "system", "content": arguments["instructions"]}] + messages
            params: dict[str, Any] = {
                "model": self.model_name,
                "messages": messages,
            }
            for k in ("temperature", "max_output_tokens", "top_p"):
                if k in arguments:
                    params[k] = arguments[k]

            response = await self.async_client.chat.completions.create(**params)
            usage = None
            if hasattr(response, "usage") and response.usage:
                usage = {
                    "input_tokens": getattr(response.usage, "prompt_tokens", None),
                    "output_tokens": getattr(response.usage, "completion_tokens", None),
                }
            text = response.choices[0].message.content if response.choices else ""
            return {
                "output": strip_tool_markers(text or ""),
                "model": self.model_name,
                "usage": usage,
            }

        modalities_str = ", ".join(self.allowable_modalities)
        return [
            ToolDefinition(
                name="respond",
                description=self.description
                or f"Generate a response using {self.model_name}. Supports {modalities_str} inputs.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "input": {
                            "type": ["string", "array"],
                            "description": (
                                "The input text, or a structured message array for multi-turn / multimodal input."
                            ),
                        },
                        "instructions": {
                            "type": "string",
                            "description": "Optional system-level instructions to guide the model's behavior.",
                        },
                        "temperature": {
                            "type": "number",
                            "description": "Sampling temperature (0-2). Higher = more random.",
                        },
                        "max_output_tokens": {
                            "type": "integer",
                            "description": "Maximum number of tokens to generate.",
                        },
                    },
                    "required": ["input"],
                },
                handler=_respond,
            )
        ]


@dataclass(repr=False)
class VoiceModel(BaseModel):
    """A speech model: ASR (transcription), TTS (synthesis) or JOINT.

    Fields
    ------
    model_type:
        "ASR" exposes the asr_* helpers only, "TTS" the tts_* helpers only,
        "JOINT" exposes both.
    tts_voice:
        Default voice name used by tts_function_call and the synthesize tool.
    tts_supported_voices:
        Voices known for this model. Seeded from config; otherwise fetched
        once from GET {base_url}/audio/voices and memoized only on success, so
        an unreachable endpoint is retried on the next access.
    tts_skip_verify:
        Skip the voices lookup entirely (offline or air-gapped use).

    Note that .model raises on purpose: speech calls do not go through an
    adapter. Use .client, or the *_function_call helpers below.
    """

    model_instantiation_class: Callable = field(default_factory=lambda: _openai_client_class("OpenAI"))

    model_type: Literal["ASR", "TTS", "JOINT"] = "ASR"

    tts_supported_voices: set[str] = field(default_factory=set)
    tts_voice: str = "alys"
    tts_skip_verify: bool = False

    _cached_properties = (
        "client",
        "async_client",
        "base_url",
        "http_client",
        "http_async_client",
        "model",
        "tts_supported_voices",
    )
    _cached_functions = (
        "tts_function",
        "tts_async_function",
        "asr_function",
        "asr_async_function",
    )

    allowable_modalities = (
        "text",
        "audio",
    )

    def __post_init__(self) -> None:
        # Resolve model_name first (BaseModel auto-discovery via /v1/models).
        # NOTE: ``tts_supported_voices`` is a plain dataclass field — do NOT
        # shadow it with a cached_property of the same name (that returns the
        # descriptor object on access, breaking any iteration over the voices).
        super().__post_init__()
        # Seed: the init-provided voices.  ``_clear_cached_class_elements``
        # pops ``tts_supported_voices`` (it's listed in ``_cached_properties``)
        # — but it's a plain field, not a cached_property, so the pop leaves a
        # dangling attribute that raises AttributeError on the next access.
        # Keep the seed so the override below can restore the field.
        self._tts_voices_seed: set[str] = set(self.tts_supported_voices)

    def _clear_cached_class_elements(self) -> None:
        super()._clear_cached_class_elements()
        # Restore ``tts_supported_voices`` after the generic clear popped it
        # (it is a plain dataclass field listed in ``_cached_properties``).
        # Restoring to the seed means: with init-provided voices they survive a
        # transport switch; without them, the next ``_get_available_voices``
        # re-fetches using the updated endpoint.
        self.__dict__["tts_supported_voices"] = set(self._tts_voices_seed)

    def _get_available_voices(self) -> set[str]:
        """Return the TTS voices available for this model.

        Uses voices configured at init time if any; otherwise (and only when
        verification isn't skipped) fetches them from ``{base_url}/audio/voices``.
        Only a *resolved* (non-empty) list is memoized into the
        ``tts_supported_voices`` field — a failed/unreachable fetch stays
        unmemoized so the next request/access re-checks.  ``.remote()`` /
        ``.local()`` also clears the field (it's in ``_cached_properties``)
        so the next access re-fetches with the updated transport.
        """
        if self.tts_supported_voices:
            return self.tts_supported_voices
        if self.tts_skip_verify:
            return set()
        voices: set[str] = set()
        url = ""
        try:
            url = f"{self.base_url}/audio/voices"
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            resp = self.http_client.get(url, headers=headers, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            for v in data.get("voices", []):
                voices.add(v if isinstance(v, str) else v.get("name", ""))
            for v in data.get("uploaded_voices", []):
                voices.add(v.get("name", "") if isinstance(v, dict) else str(v))
            voices.discard("")
            if voices:
                logger.info(f"Dynamically fetched {len(voices)} voices from {url}: {sorted(voices)}")
                # Only memoize a successful, non-empty result.  Empty here
                # means it should be re-tried on the next access.
                self.tts_supported_voices = voices
        except Exception as e:
            logger.warning(f"Could not fetch voices from {url}: {e}. Keeping unsettled.")
        return voices

    @cached_property
    def model(self) -> OpenAI:
        raise ValueError(
            "Use `client` instead of `model`, or make calls directly with `tts_function` or `asr_function`."
        )

    @cached_property
    def tts_function(self) -> Callable:
        assert self.model_type != "ASR", "No `tts_function` is available for ASR model_type."
        return partial(self.client.audio.speech.create, model=self.model_name, voice=self.tts_voice)

    def tts_function_call(self, input: str, **chat_kwargs):
        """Synthesise speech from text (blocking).

        Parameters
        ----------
        input:
            The text to speak.
        **chat_kwargs:
            Extra Audio.speech.create parameters: voice (overrides tts_voice),
            response_format ("mp3", "wav", ...), speed.

        Returns
        -------
        openai._legacy_response.HttpxBinaryResponseContent
            The audio payload; .content holds the raw bytes and
            .write_to_file(path) saves them.

        Raises
        ------
        AssertionError
            model_type is "ASR": this model cannot synthesise.
        """
        return self.tts_function(input=input, **chat_kwargs)

    @cached_property
    def tts_async_function(self) -> Callable:
        assert self.model_type != "ASR", "No `tts_function` is available for ASR model_type."
        return partial(
            self.async_client.audio.speech.create,
            model=self.model_name,
            voice=self.tts_voice,
        )

    def tts_async_function_call(self, input: str, **chat_kwargs):
        """Awaitable form of tts_function_call.

        Parameters
        ----------
        input:
            The text to speak.
        **chat_kwargs:
            Extra Audio.speech.create parameters: voice (overrides tts_voice),
            response_format ("mp3", "wav", ...), speed.

        Returns
        -------
        openai._legacy_response.HttpxBinaryResponseContent
            The audio payload; await it, then read .content for the bytes.

        Raises
        ------
        AssertionError
            model_type is "ASR": this model cannot synthesise.
        """
        return self.tts_async_function(input=input, **chat_kwargs)

    @cached_property
    def asr_function(self) -> Callable:
        assert self.model_type != "TTS", "No `asr_function` is available for TTS model_type."
        return partial(self.client.audio.transcriptions.create, model=self.model_name)

    def asr_function_call(self, file, **chat_kwargs):
        """Transcribe audio to text (blocking).

        Parameters
        ----------
        file:
            The audio to transcribe: a file path, a file-like object, or a
            (filename, bytes) tuple, as accepted by the OpenAI SDK.
        **chat_kwargs:
            Extra Audio.transcriptions.create parameters: language, prompt,
            response_format, temperature.

        Returns
        -------
        openai.types.audio.Transcription
            The transcription; .text holds the transcript.

        Raises
        ------
        AssertionError
            model_type is "TTS": this model cannot transcribe.
        """
        return self.asr_function(file=file, **chat_kwargs)

    @cached_property
    def asr_async_function(self) -> Callable:
        assert self.model_type != "TTS", "No `asr_function` is available for TTS model_type."
        return partial(self.async_client.audio.transcriptions.create, model=self.model_name)

    def asr_async_function_call(self, file, **chat_kwargs):
        """Awaitable form of asr_function_call.

        Parameters
        ----------
        file:
            The audio to transcribe: a file path, a file-like object, or a
            (filename, bytes) tuple, as accepted by the OpenAI SDK.
        **chat_kwargs:
            Extra Audio.transcriptions.create parameters: language, prompt,
            response_format, temperature.

        Returns
        -------
        openai.types.audio.Transcription
            The transcription; .text holds the transcript.

        Raises
        ------
        AssertionError
            model_type is "TTS": this model cannot transcribe.
        """
        return self.asr_async_function(file=file, **chat_kwargs)

    def to_mcp_tools(self) -> list[ToolDefinition]:
        """Expose TTS as ``synthesize`` and/or ASR as ``transcribe``,
        depending on ``model_type``."""

        tools: list[ToolDefinition] = []

        if self.model_type != "ASR":

            async def _synthesize(arguments: dict[str, Any]) -> dict[str, Any]:
                text = arguments["text"]
                voice = arguments.get("voice", self.tts_voice)
                response = await self.tts_async_function_call(input=text, voice=voice)
                return {
                    "audio_base64": base64.b64encode(response.content).decode(),
                    "model": self.model_name,
                    "voice": voice,
                }

            # No network I/O at tool-listing time: _get_available_voices()
            # performs a blocking GET ({base_url}/audio/voices) that used to
            # stall the event loop on every tools/list.  Advertise voices
            # already resolved (init-time config or memoized from a prior
            # call); otherwise advertise the configured default voice — the
            # handler validates the requested voice against the endpoint.
            voices_str = ", ".join(sorted(self.tts_supported_voices)) or (self.tts_voice or "default")
            tools.append(
                ToolDefinition(
                    name="synthesize",
                    description=self.description or f"Synthesize speech from text using {self.model_name}.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": "The text to synthesize.",
                            },
                            "voice": {
                                "type": "string",
                                "description": f"Voice to use. Available: {voices_str}.",
                            },
                        },
                        "required": ["text"],
                    },
                    handler=_synthesize,
                )
            )

        if self.model_type != "TTS":

            async def _transcribe(arguments: dict[str, Any]) -> dict[str, Any]:
                audio_b64 = arguments.get("audio_base64")
                audio_url = arguments.get("audio_url")

                if audio_b64:
                    audio_bytes = base64.b64decode(audio_b64)
                elif audio_url:
                    resp = await self.http_async_client.get(audio_url, follow_redirects=True)
                    resp.raise_for_status()
                    audio_bytes = resp.content
                else:
                    return {"error": "Either audio_base64 or audio_url must be provided."}

                buf = _NamedBytesIO(audio_bytes)
                result = await self.asr_async_function_call(file=buf)
                return {
                    "text": result.text,
                    "model": self.model_name,
                }

            tools.append(
                ToolDefinition(
                    name="transcribe",
                    description=self.description or f"Transcribe audio to text using {self.model_name}.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "audio_base64": {
                                "type": "string",
                                "description": "Base64-encoded audio data.",
                            },
                            "audio_url": {
                                "type": "string",
                                "description": "URL of the audio file to transcribe.",
                            },
                        },
                    },
                    handler=_transcribe,
                )
            )

        return tools


@dataclass
class SpeechFlowModel(BaseModel):
    """Base-to-speech flow: ASR → chat LLM → TTS.

    Instead of pointing at a single endpoint, this source references
    three other model sources (by slug): an ASR ``VOICE`` source, a
    ``CHAT`` source, and a TTS ``VOICE`` source.  It
    is exposed to agents as one ``audio_chat`` tool that runs the whole
    pipeline in a single MCP call.

    Result handling (see the ``audio_chat`` tool):
    * **Default:** the synthesized audio is written to the artifact store
      and the tool returns a small ``artifact://`` URI + transcript/reply.
      This keeps large binary blobs out of the caller's context and out of
      the agent-loop result truncation path.
    * **Opt-in:** passing ``return_audio_base64=true`` returns the audio
      inline as base64 instead (for direct consumers that need the bytes).
    """

    # Slugs of the three model sources this flow composes.
    asr_slug: str = ""
    llm_slug: str = ""
    tts_slug: str = ""
    # Prompt used for the middle LLM step (override per-call via `system`).
    system_prompt: str = (
        "You are a helpful speech assistant. Answer the user's spoken "
        "request concisely, in the same language they spoke."
    )
    # Default TTS voice; the per-call `voice` argument takes precedence.
    tts_voice: str = ""
    # MIME type of the synth audio (and thus the artifact handle).
    content_type: str = "audio/mpeg"

    # Resolved sub-models.  Populated by
    # orchestration.adapters.build_model_from_source at build time, not
    # from the stored config (init=False).
    asr_model: VoiceModel | None = field(default=None, init=False, repr=False)
    llm_model: ChatModel | None = field(default=None, init=False, repr=False)
    tts_model: VoiceModel | None = field(default=None, init=False, repr=False)

    _cached_properties: tuple[str, ...] = ()
    _cached_functions: tuple[str, ...] = ()

    allowable_modalities = ("audio",)

    def __post_init__(self) -> None:
        # A flow composes other models; it has no endpoint/name of its own,
        # so skip model-name auto-discovery (BaseModel would otherwise try
        # GET /v1/models against an empty URL).
        self.model_name = self.model_name or "speech-flow"
        super().__post_init__()

    def to_mcp_tools(self) -> list[ToolDefinition]:
        """Expose the speech flow as a single ``audio_chat`` tool."""

        async def _audio_chat(arguments: dict[str, Any]) -> dict[str, Any]:
            return await self._run_audio_chat(arguments)

        return [
            ToolDefinition(
                name="audio_chat",
                description=self.description
                or "Speech-to-speech assistant: transcribes the audio, "
                "answers with an LLM, and synthesizes the reply as speech.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "audio_base64": {
                            "type": "string",
                            "description": "Base64-encoded input audio.",
                        },
                        "audio_url": {
                            "type": "string",
                            "description": "URL of the input audio to transcribe.",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "Extra instruction to the LLM, on top of the transcription.",
                        },
                        "system": {
                            "type": "string",
                            "description": "Override the flow's system prompt for the LLM step.",
                        },
                        "voice": {
                            "type": "string",
                            "description": "TTS voice override.",
                        },
                        "return_audio_base64": {
                            "type": "boolean",
                            "description": (
                                "If true, return the reply audio inline as base64 instead "
                                "of an artifact:// reference (default: false)."
                            ),
                        },
                    },
                },
                handler=_audio_chat,
            )
        ]

    async def _run_audio_chat(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run ASR → LLM → TTS over the input audio.

        Returns a JSON-serializable dict.  Step failures are returned as
        structured ``{"error": ...}`` results rather than raised, so the
        caller always gets a traceable outcome.
        """
        if self.asr_model is None or self.llm_model is None or self.tts_model is None:
            return {
                "error": "flow_unresolved",
                "message": (
                    "Speech flow sub-models were not resolved at build time. "
                    "Check that 'asr_slug', 'llm_slug' and 'tts_slug' point at "
                    "existing, compatible model sources."
                ),
            }

        # -- Input audio -----------------------------------------------------
        audio_bytes: bytes | None = None
        audio_b64 = arguments.get("audio_base64")
        audio_url = arguments.get("audio_url")
        if audio_b64:
            try:
                audio_bytes = base64.b64decode(audio_b64)
            except (ValueError, TypeError) as exc:
                return {
                    "error": "invalid_audio",
                    "message": f"audio_base64 is not valid base64: {exc}",
                }
        elif audio_url:
            audio_bytes = await self._fetch_audio(audio_url)
        if not audio_bytes:
            return {
                "error": "missing_audio",
                "message": "Provide one of 'audio_base64' or 'audio_url' with valid audio.",
            }

        # -- 1. ASR ---------------------------------------------------------
        try:
            buf = _NamedBytesIO(audio_bytes)
            asr_res = await self.asr_model.asr_async_function_call(file=buf)
            transcript = (asr_res.text or "").strip()
        except Exception as exc:
            return {"error": "asr_failed", "message": f"ASR step failed: {exc}"}
        if not transcript:
            return {
                "error": "asr_empty",
                "message": "No speech was recognized in the audio.",
            }

        # -- 2. LLM ---------------------------------------------------------
        system = arguments.get("system") or self.system_prompt
        user_content = transcript
        extra = arguments.get("prompt")
        if extra:
            user_content = f"{extra}\n\nThe user said:\n{transcript}"
        try:
            llm_res = await self.llm_model.async_client.chat.completions.create(
                model=self.llm_model.model_name,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_content},
                ],
            )
            reply = (llm_res.choices[0].message.content if llm_res.choices else None) or ""
            reply = reply.strip()
        except Exception as exc:
            return {
                "error": "llm_failed",
                "message": f"LLM step failed: {exc}",
                "transcript": transcript,
            }
        if not reply:
            return {
                "error": "llm_empty",
                "message": "The LLM returned an empty reply.",
                "transcript": transcript,
            }

        # -- 3. TTS ---------------------------------------------------------
        voice = arguments.get("voice") or self.tts_voice
        try:
            tts_res = await self.tts_model.tts_async_function_call(input=reply, voice=voice)
            audio_bytes = tts_res.content
        except Exception as exc:
            return {
                "error": "tts_failed",
                "message": f"TTS step failed: {exc}",
                "transcript": transcript,
                "text": reply,
            }

        models = {
            "asr": self.asr_model.model_name,
            "llm": self.llm_model.model_name,
            "tts": self.tts_model.model_name,
        }

        # -- Return path: inline base64 (opt-in) or artifact handle --------
        if bool(arguments.get("return_audio_base64", False)):
            return {
                "transcript": transcript,
                "text": reply,
                "audio_base64": base64.b64encode(audio_bytes).decode(),
                "content_type": self.content_type,
                "models": models,
            }

        # Lazy import: `orchestration.context` only exists when this module is
        # vendored into a real package tree (src/<pkg>/utils/), never in the
        # flat pcai_utils checkout — hence the mypy suppression.
        from ..orchestration.context import get_current_context  # type: ignore[misc]

        ctx = get_current_context()
        artifact_store = getattr(ctx, "artifact_store", None)
        if artifact_store is None:
            return {
                "error": "artifact_store_unavailable",
                "message": (
                    "The artifact store is not available in this context. "
                    "Either enable it or set return_audio_base64=true."
                ),
                "transcript": transcript,
                "text": reply,
            }

        try:
            uri = await artifact_store.write(
                content=audio_bytes,
                content_type=self.content_type,
                operation_id=ctx.operation_id,
                filename=f"audio_chat-{ctx.operation_id[:8]}{_audio_extension(self.content_type)}",
            )
        except Exception as exc:
            return {
                "error": "artifact_write_failed",
                "message": f"Could not persist audio output: {exc}",
                "transcript": transcript,
                "text": reply,
            }

        return {
            "transcript": transcript,
            "text": reply,
            "artifact": str(uri),
            "content_type": self.content_type,
            "size_bytes": len(audio_bytes),
            "models": models,
        }

    async def _fetch_audio(self, audio_url: str) -> bytes | None:
        """Download the input audio via the ASR model's async HTTP client."""
        if self.asr_model is None:
            return None
        try:
            resp = await self.asr_model.http_async_client.get(audio_url, follow_redirects=True)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            logger.warning("Could not fetch audio_url %r: %s", audio_url, exc)
            return None


def _audio_extension(content_type: str) -> str:
    import mimetypes

    ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
    if not ext:
        ext = ".bin"
    return ext


@dataclass(repr=False)
class EmbeddingModel(BaseModel):
    """An embedding model: text and/or multimodal inputs to dense vectors.

    Adds chunking knobs and the dataset fingerprint guard on top of BaseModel.

    Fields
    ------
    embedding_dim:
        Vector width, so callers can size an index without a probe call.
    chunk_size / chunk_overlap:
        Token (or character) budget for text chunking.
    code_chunk_size / code_chunk_overlap:
        Separate budget for code and structured data.
    tokenizer_name / tokenizer_type:
        Set tokenizer_type = "HuggingFace" to chunk by real token counts;
        otherwise chunking falls back to characters.
    alias:
        Other model ids that must count as THE SAME embedder for the dataset
        fingerprint guard (for example an FP8 redeploy of the same base
        model), so datasets indexed under the alias keep working without a
        rebuild. One name, a list, or a comma-separated string.
    preprocessor:
        Optional callable applied to inputs before they are embedded.

    Attributes
    ----------
    model:
        A MultiModalEmbeddings adapter (unless model_instantiation_class
        overrides it), exposing embed_documents / aembed_documents.
    text_splitter / code_text_splitter:
        TokenTextSplitter instances, or None when tokenizer_type is not
        "HuggingFace" (callers then chunk by characters).
    """

    # Model args w/ defaults
    model_instantiation_class: Callable | None = None

    # Optional RAG args
    embedding_dim: int = 4096
    chunk_size: int = 2048
    chunk_overlap: int = 256
    code_chunk_size: int = 8192
    code_chunk_overlap: int = 512

    # For enabling splitting by token
    tokenizer_name: str | None = None
    tokenizer_type: Literal["HuggingFace", "TikToken"] | None = None

    # Other model id(s) that must be treated as THE SAME embedder by the
    # dataset fingerprint guard — e.g. a quantized (FP8) redeploy of the same
    # base model: datasets indexed under the alias keep working without a
    # rebuild.  Accepts one name or a list (a comma-separated string is also
    # normalized).  Set via values ``models.embedder.extra.alias``.
    alias: str | list[str] | tuple[str, ...] | None = None

    mm_processor_kwargs: dict[str, Any] = field(default_factory=dict)

    # If input should be preprocessed
    preprocessor: Callable | None = None

    allowable_modalities = ("text", "audio", "image", "video")

    @property
    def alias_names(self) -> tuple[str, ...]:
        """Normalized alias ids considered identical to :attr:`model_name`."""
        if not self.alias:
            return ()
        if isinstance(self.alias, str):
            return tuple(a.strip() for a in self.alias.split(",") if a.strip())
        return tuple(str(a).strip() for a in self.alias if str(a).strip())

    @cached_property
    def text_splitter(self) -> TokenTextSplitter | None:
        """Return a token-count-aware text splitter, or ``None`` if the
        tokenizer file is not available (fall back to character-based)."""
        if self.tokenizer_type != "HuggingFace":
            logger.info(
                "Text chunking: character-based (tokenizer_type=%r; set "
                "tokenizer_type=HuggingFace to enable token counts)",
                self.tokenizer_type,
            )
            return None
        splitter = TokenTextSplitter.from_bundled(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
        )
        if splitter is not None:
            logger.info(
                "Text chunking: token-based (chunk_size=%d, chunk_overlap=%d)",
                self.chunk_size,
                self.chunk_overlap,
            )
        else:
            logger.warning(
                "Text chunking: character-based fallback (tokenizer file not found despite tokenizer_type=HuggingFace)",
            )
        return splitter

    @cached_property
    def code_text_splitter(self) -> TokenTextSplitter | None:
        """Return a token-count-aware text splitter for code/structured data,
        using ``code_chunk_size`` / ``code_chunk_overlap``."""
        if self.tokenizer_type != "HuggingFace":
            logger.info(
                "Code chunking: character-based (tokenizer_type=%r; set "
                "tokenizer_type=HuggingFace to enable token counts)",
                self.tokenizer_type,
            )
            return None
        splitter = TokenTextSplitter.from_bundled(
            chunk_size=self.code_chunk_size,
            chunk_overlap=self.code_chunk_overlap,
        )
        if splitter is not None:
            logger.info(
                "Code chunking: token-based (chunk_size=%d, chunk_overlap=%d)",
                self.code_chunk_size,
                self.code_chunk_overlap,
            )
        else:
            logger.warning(
                "Code chunking: character-based fallback (tokenizer file not found despite tokenizer_type=HuggingFace)",
            )
        return splitter

    @cached_property
    def model(self):
        from .model_adapters import MultiModalEmbeddings

        if self.model_instantiation_class is None or self.model_instantiation_class is MultiModalEmbeddings:
            return MultiModalEmbeddings(self)
        return super().model

    def to_mcp_tools(self) -> list[ToolDefinition]:
        """Expose this embedder as a single ``embed`` tool."""

        async def _embed(arguments: dict[str, Any]) -> dict[str, Any]:
            texts = arguments["texts"]
            embeddings = await self.model.aembed_documents(texts)
            return {
                "embeddings": embeddings,
                "model": self.model_name,
                "dim": self.embedding_dim,
                "count": len(embeddings),
            }

        modalities_str = ", ".join(self.allowable_modalities)
        return [
            ToolDefinition(
                name="embed",
                description=self.description
                or f"Generate embeddings using {self.model_name}. Supports {modalities_str} inputs.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "texts": {
                            "type": "array",
                            "items": {"type": ["string", "object"]},
                            "description": (
                                "List of texts (strings) or multimodal dicts ({text, image, video, audio}) to embed."
                            ),
                        },
                    },
                    "required": ["texts"],
                },
                handler=_embed,
            )
        ]


@dataclass(repr=False)
class RerankerModel(BaseModel):
    """A reranker: scores query/document pairs so a retriever can reorder them.

    allowable_modalities defaults to text, audio, image and video, so the same
    class serves text-only rerankers and multimodal ones.

    Fields
    ------
    mm_processor_kwargs:
        Extra processor arguments forwarded to the underlying model.
    preprocessor:
        Optional callable applied to inputs before they are scored.

    Attributes
    ----------
    model:
        A MultiModalReranker adapter exposing rerank / arerank. For each query
        it returns the documents ordered by descending relevance, with scores.
    """

    # Model args w/ defaults
    model_instantiation_class: Callable = field(default_factory=_multi_modal_reranker)

    mm_processor_kwargs: dict[str, Any] = field(default_factory=dict)

    # If input should be preprocessed
    preprocessor: Callable | None = None

    allowable_modalities = ("text", "audio", "image", "video")

    @cached_property
    def model(self) -> MultiModalReranker:
        return self.model_instantiation_class(self)  # type: ignore[return-value]

    def to_mcp_tools(self) -> list[ToolDefinition]:
        """Expose this reranker as a single ``rerank`` tool."""

        async def _rerank(arguments: dict[str, Any]) -> dict[str, Any]:
            query = arguments["query"]
            documents = arguments["documents"]
            results = await self.model.arerank(query, documents)
            # arerank returns list[list[dict]] (one inner list per query).
            # For a single query (the common case), unwrap.
            if isinstance(query, (str, dict)):
                return {
                    "results": results[0] if results else [],
                    "model": self.model_name,
                }
            return {
                "results": results,
                "model": self.model_name,
            }

        return [
            ToolDefinition(
                name="rerank",
                description=self.description or f"Rerank documents by relevance to a query using {self.model_name}.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": ["string", "object"],
                            "description": "The search query (string or multimodal dict).",
                        },
                        "documents": {
                            "type": "array",
                            "items": {"type": ["string", "object"]},
                            "description": "List of documents to rank.",
                        },
                    },
                    "required": ["query", "documents"],
                },
                handler=_rerank,
            )
        ]


@dataclass(repr=False)
class DecisionModel(BaseModel):
    """A *decision* model served by the vLLM-SR native runtime (System One API).

    This is the analog of ChatModel for the ``decision3`` model family
    (vllm-sr/d3-turbo, d3, d3-flash, d3-mini, d3-nano, d3-lite, d3-edge).
    The two classes differ at the protocol level, not just in naming:

    ============  =========================================================
    ChatModel     POST /v1/chat/completions -- autoregressive text out.
    DecisionModel POST /v1/systemone       -- calibrated probabilities out.
    ============  =========================================================

    A decision model never generates text: it reads a *state* (text/JSON, plus
    optional images and videos) once and answers a set of typed *questions*,
    returning a probability per candidate answer.  usage.output_tokens is
    always 0.  There is NO /v1/chat/completions and NO /v1/responses on the
    endpoint, which is why this class does not use the OpenAI SDK the way
    ChatModel does -- it speaks the decision wire format over the shared
    httpx2 clients.

    Question types (what a given checkpoint supports is advertised by
    GET /v1/models -> question_types):

    * choice -- pick one of N labelled options (criteria: name -> description)
    * noul   -- yes/no (criteria: optional true/false descriptions)
    * score  -- ordered rubric (criteria: list of level names, 2..10)
    * box    -- one bounding box in an image, or at a given second of a video
    * track  -- a box per moment across one video (rate: 1 or 2 per second)

    box/track are available on d3-turbo only within the family.

    Example
    -------
    >>> m = DecisionModel(
    ...     model_name="vllm-sr/d3-turbo",
    ...     url_remote="https://vllmsr-d3-turbo-router.<ns>.serving.<domain>",
    ...     api_key=KEY,
    ... )
    >>> m.choice(
    ...     "Ticket: the invoice total is wrong after the plan change.",
    ...     "Which team handles this?",
    ...     {"billing": "Billing", "tech": "Technical support"},
    ... )
    {'type': 'choice', 'choice': 'billing',
     'probabilities': {'billing': 0.9998, 'tech': 0.0002},
     'confidence': 0.9997}
    """

    model_instantiation_class: Callable | None = None

    # A decision model never speaks the OpenAI SDK (the client/async_client
    # properties below raise), so it overrides the inherited OpenAI defaults
    # with a stand-in. That keeps openai out of the import path entirely for a
    # decision-only caller.
    model_client_class: Callable = field(default_factory=lambda: _no_openai_client)
    model_async_client_class: Callable = field(default_factory=lambda: _no_openai_client)

    # Advertised by GET /v1/models (best-effort; empty when unreachable).
    # Can also be PASSED IN, which skips that call entirely -- bake the card
    # into a catalog entry and no request is ever made for it:
    #     DecisionModel(..., model_card={"question_types": [...], "limits": {...}})
    # A non-empty card is authoritative: nothing is fetched for it.
    # NOTE: reading this field never fetches -- call load_model_card() (or any
    # capability accessor) to populate it on demand.
    # Kept as a plain dict rather than one field per attribute: a plain field
    # that is ALSO named in _cached_properties gets popped by
    # _clear_cached_class_elements and raises AttributeError on next access
    # (the trap VoiceModel documents for tts_supported_voices).
    model_card: dict[str, Any] = field(default_factory=dict)

    _cached_properties = (
        "client",
        "async_client",
        "base_url",
        "http_client",
        "http_async_client",
        "model",
        "system_one_function",
        "system_one_async_function",
    )
    _cached_functions = (
        "system_one_function",
        "system_one_async_function",
    )

    allowable_modalities = ("text", "image", "video")

    # -- construction -----------------------------------------------------

    def __post_init__(self) -> None:
        super().__post_init__()
        # The model card is deliberately NOT fetched here. Every catalog entry
        # is constructed at import time, so a fetch here costs one blocking
        # DNS+HTTP round-trip per entry -- seconds on a machine outside the
        # cluster, where the in-cluster *.svc.cluster.local name never
        # resolves. It is read on first use of a capability accessor instead
        # (see _ensure_model_card), which keeps construction offline.

    def _ensure_model_card(self) -> dict[str, Any]:
        """Fetch this model card on first capability read, with negative caching.

        Best-effort: a failed fetch leaves the card empty and is not retried for
        _MODEL_CARD_FAILURE_TTL_S, so a down endpoint costs one attempt per
        minute rather than one per question_types access.
        """
        if self.model_card or not self.currently_deployed:
            return self.model_card
        url = f"{self.base_url}/models"
        last_failure = _model_card_failures.get(url)
        if last_failure is not None and (time.monotonic() - last_failure) < _MODEL_CARD_FAILURE_TTL_S:
            return self.model_card
        card = self._fetch_model_card()
        if card:
            self.model_card = card
            _model_card_failures.pop(url, None)
        else:
            _model_card_failures[url] = time.monotonic()
        return self.model_card

    def load_model_card(self) -> dict[str, Any]:
        """Return this model's card, reading it from the endpoint on first use.

        model_card is a plain dataclass field, so READING IT NEVER FETCHES: it
        stays {} until something asks for a capability. The capability
        accessors (question_types, max_input_tokens, validated_input_tokens,
        supports) call this internally; call it directly when you want the whole
        card. Calling it again is free -- the card is memoized, and a failure is
        negative-cached for _MODEL_CARD_FAILURE_TTL_S.

        Returns
        -------
        dict[str, Any]
            The card exactly as the endpoint served it (family, surfaces,
            question_types, limits, ...), or {} when it could not be read.
        """
        return self._ensure_model_card()

    def _fetch_model_card(self) -> dict[str, Any]:
        """Best-effort read of this model entry from GET {base_url}/models.

        Returns {} on any failure -- same contract as discover_model_name, so an
        unreachable endpoint degrades to "unknown capabilities" instead of
        failing the caller.
        """
        url = f"{self.base_url}/models"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            resp = self.http_client.get(url, headers=headers, timeout=10.0)
            resp.raise_for_status()
            body = resp.json()
            data = body.get("data", []) if isinstance(body, dict) else []
            for entry in data:
                if isinstance(entry, dict) and entry.get("id") == self.model_name:
                    return entry
            if data and isinstance(data[0], dict):
                return data[0]
        except Exception as exc:
            logger.warning("Could not read the model card from %s: %s", url, exc)
        return {}

    # -- capabilities (read from the model card) --------------------------

    @property
    def question_types(self) -> tuple[str, ...]:
        """Question types this checkpoint accepts, e.g. (choice, noul, score, box, track)."""
        return tuple(self._ensure_model_card().get("question_types") or ())

    @property
    def max_input_tokens(self) -> int:
        """Architectural ceiling per question (e.g. 262144); 0 when unknown."""
        return int((self._ensure_model_card().get("limits") or {}).get("max_input_tokens") or 0)

    @property
    def validated_input_tokens(self) -> int:
        """Length the checkpoint was validated at (e.g. 32768); 0 when unknown."""
        return int((self._ensure_model_card().get("limits") or {}).get("validated_input_tokens") or 0)

    def supports(self, question_type: str) -> bool:
        """True when the served checkpoint advertises question_type.

        Unknown capabilities (unreachable /v1/models) return True rather than
        blocking the call: the server is the authority and answers 400 with a
        precise reason if the type is genuinely unsupported.
        """
        types = self.question_types
        return True if not types else question_type in types

    # -- transport --------------------------------------------------------

    @cached_property
    def client(self):
        raise ValueError(
            "A decision model has no OpenAI client: it serves POST /v1/systemone "
            "and never /v1/chat/completions. Use system_one(...), system_one_async(...), "
            "or the helpers decide(...) / choice(...) / yes_no(...) / score(...)."
        )

    @cached_property
    def async_client(self):
        raise ValueError(
            "A decision model has no OpenAI client: it serves POST /v1/systemone "
            "and never /v1/chat/completions. Use system_one_async(...)."
        )

    @cached_property
    def model(self):
        raise ValueError(
            "Use system_one(...) / system_one_async(...); a decision model is not instantiated as a chat client."
        )

    @property
    def decisions_url(self) -> str:
        """{base_url}/systemone -- base_url already ends in /v1."""
        return f"{self.base_url}/systemone"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    # -- request building -------------------------------------------------

    @staticmethod
    def _check_questions(questions: dict[str, Any]) -> None:
        """Validate a questions payload before it is sent.

        The server remains the authority; this mirrors its documented limits so
        a typo fails locally with a clear message instead of a 400 round-trip.

        Parameters
        ----------
        questions:
            Mapping of {question_id: spec}. Each spec is a dict with a "type"
            (choice, noul, score, box or track) and non-empty "instructions".
            A choice question also needs "criteria" as {option: description}
            with 2..255 options; a score question needs "criteria" as a list of
            2..10 levels; a track question accepts an optional "rate" of 1 or 2.
            noul and box questions need no criteria.

        Returns
        -------
        None
            Returns nothing; raises on invalid input.

        Raises
        ------
        TypeError
            A value has the wrong type: questions is not a dict, a question id
            is not a str, a spec is not a dict, or criteria is not the shape the
            question type requires. Treat this as a programming error
            (ruff TRY004).
        ValueError
            A value has the right type but an unusable value: empty questions,
            more than 64 questions, an empty id, an unknown type, missing
            instructions, or an out-of-range criteria/rate.

        Examples
        --------
        A well-formed choice question passes silently::

            DecisionModel._check_questions(
                {"answer": {"type": "choice", "instructions": "Which one?",
                            "criteria": {"a": "the first", "b": "the second"}}}
            )

        A noul question needs no criteria at all::

            DecisionModel._check_questions(
                {"answer": {"type": "noul", "instructions": "Is this text?"}}
            )
        """
        if not isinstance(questions, dict):
            raise TypeError(f"questions must be a dict of {{id: question}}; got {type(questions).__name__}.")
        if not questions:
            raise ValueError("questions must be a non-empty dict of {id: question}.")
        if len(questions) > 64:
            raise ValueError(f"At most 64 questions per request; got {len(questions)}.")
        for qid, spec in questions.items():
            if not isinstance(qid, str):
                raise TypeError(f"Question ids must be strings; got {type(qid).__name__}.")
            if not qid:
                raise ValueError("Each question id must be a non-empty string.")
            if not isinstance(spec, dict):
                raise TypeError(f"Question {qid!r} must be a dict; got {type(spec).__name__}.")
            qtype = spec.get("type")
            if qtype not in ("choice", "noul", "score", "box", "track"):
                raise ValueError(
                    f"Question {qid!r} has type {qtype!r}; expected one of choice, noul, score, box, track."
                )
            if not spec.get("instructions"):
                raise ValueError(f"Question {qid!r} needs non-empty 'instructions'.")
            criteria = spec.get("criteria")
            if qtype == "choice":
                if not isinstance(criteria, dict):
                    raise TypeError(
                        f"Question {qid!r} (choice) 'criteria' must be a dict of options; "
                        f"got {type(criteria).__name__}."
                    )
                if len(criteria) < 2:
                    raise ValueError(f"Question {qid!r} (choice) needs 'criteria' with >= 2 options.")
                if len(criteria) > 255:
                    raise ValueError(f"Question {qid!r} (choice) has {len(criteria)} options; max 255.")
            if qtype == "score":
                if not isinstance(criteria, (list, tuple)):
                    raise TypeError(
                        f"Question {qid!r} (score) 'criteria' must be a list of 2..10 levels; "
                        f"got {type(criteria).__name__}."
                    )
                if not 2 <= len(criteria) <= 10:
                    raise ValueError(f"Question {qid!r} (score) needs 'criteria' as a list of 2..10 levels.")
            if qtype == "track" and spec.get("rate") not in (None, 1, 2):
                raise ValueError(f"Question {qid!r} (track) 'rate' must be 1 or 2.")

    def _payload(
        self,
        state: Any,
        questions: dict[str, Any],
        *,
        images: list[str] | None = None,
        videos: list[str] | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._check_questions(questions)
        payload: dict[str, Any] = {
            "model": self.model_name,
            "state": state,
            "questions": questions,
        }
        if images:
            payload["images"] = list(images)
        if videos:
            payload["videos"] = list(videos)
        if options:
            payload["options"] = dict(options)
        return payload

    # -- the call ---------------------------------------------------------

    def system_one(
        self,
        state: Any,
        questions: dict[str, Any],
        *,
        images: list[str] | None = None,
        videos: list[str] | None = None,
        options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Answer every question about one state in a single request (blocking).

        The System One API answers typed questions about a state instead of
        generating text: choice (pick one option), noul (probability of yes),
        score (expected level on a rubric), box (bounding box) and track.

        Parameters
        ----------
        state:
            What to judge: usually a string (a document, a log line, a prompt);
            the server also accepts structured state.
        questions:
            Mapping of {question_id: spec}. Each spec carries "type" plus
            "instructions", and "criteria" for choice (a dict of options) or
            score (an ordered list of levels). Validated client-side first:
            TypeError for a wrong type, ValueError for a bad value.
        images, videos:
            Optional base64 data: URLs, or http(s) URLs the server can reach.
        options:
            Server options: long_context (bool) and deadline_ms (int).
        **kwargs:
            Forwarded to the underlying async call.

        Returns
        -------
        dict[str, Any]
            The raw response envelope::

                {"model": ..., "answers": {qid: answer, ...},
                 "usage": {"input_tokens": n, "output_tokens": 0}}

            The answer shape depends on the question type. Use decide(),
            choice(), yes_no() or score() to get the answer alone.

        Raises
        ------
        TypeError
            A question is structurally wrong (see _check_questions).
        ValueError
            A question value is out of range (see _check_questions).
        httpx2.HTTPStatusError
            The endpoint rejected the request.

        Examples
        --------
        Two questions, one round-trip::

            out = model.system_one(
                "the invoice total is 42.00 USD",
                {"currency": {"type": "choice", "instructions": "Which currency?",
                              "criteria": {"usd": "US dollars", "eur": "Euros"}},
                 "paid": {"type": "noul", "instructions": "Is it paid?"}},
            )
            print(out["answers"])
        """
        return sync_wrapper_safe(
            partial(self.system_one_async, **kwargs),
            {
                "state": state,
                "questions": questions,
                "images": images,
                "videos": videos,
                "options": options,
            },
        )

    async def system_one_async(
        self,
        state: Any,
        questions: dict[str, Any],
        *,
        images: list[str] | None = None,
        videos: list[str] | None = None,
        options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Awaitable form of system_one; takes and returns the same things.

        Prefer this inside an event loop: system_one() is a thin wrapper that
        runs this method on a worker thread.

        Returns
        -------
        dict[str, Any]
            The raw response envelope, as described on system_one.
        """
        payload = self._payload(state, questions, images=images, videos=videos, options=options)
        resp = await self.http_async_client.post(
            self.decisions_url,
            json=payload,
            headers=self._headers(),
            timeout=_MODEL_REQUEST_TIMEOUT,
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json()

    # -- convenience wrappers --------------------------------------------

    def decide(
        self,
        state: Any,
        *,
        instructions: str,
        criteria: dict[str, Any] | list[str] | None = None,
        type: str = "choice",
        images: list[str] | None = None,
        videos: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Ask exactly one question and return its answer, not the envelope.

        Parameters
        ----------
        state:
            What to judge (see system_one).
        instructions:
            The question, in plain language.
        criteria:
            choice: {option: description} with 2..255 options.
            score: an ordered list of 2..10 levels, low to high.
            noul, box, track: omit it.
        type:
            The wire question type: "choice" (default), "noul", "score",
            "box" or "track".
        images, videos:
            Optional data: URLs, or http(s) URLs the server can reach.
        **kwargs:
            at and rate are lifted into the question spec (used by track);
            everything else is forwarded to system_one.

        Returns
        -------
        dict[str, Any]
            The answer for that one question, for example {"choice": "usd",
            "confidence": 0.93} for a choice, {"noul": 0.87} for yes/no, or
            {"score": 2.4} for a score. Empty when the server answered
            nothing.

        Examples
        --------
            ans = model.decide(
                "The user wants a refund.",
                instructions="Which team should handle this?",
                criteria={"billing": "money questions", "support": "how-to"},
            )
            print(ans["choice"])
        """
        spec: dict[str, Any] = {"type": type, "instructions": instructions}
        if criteria is not None:
            spec["criteria"] = criteria
        for extra in ("at", "rate"):
            if extra in kwargs:
                spec[extra] = kwargs.pop(extra)
        out = self.system_one(
            state,
            {"answer": spec},
            images=images,
            videos=videos,
            **kwargs,
        )
        return (out.get("answers") or {}).get("answer", {})

    def choice(
        self,
        state: Any,
        instructions: str,
        criteria: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Pick one of the given options (decide with type="choice").

        Parameters
        ----------
        state:
            What to judge.
        instructions:
            The question, in plain language.
        criteria:
            {option: description}; at least 2 and at most 255 options.
        **kwargs:
            Forwarded to decide (images, videos, at/rate, ...).

        Returns
        -------
        dict[str, Any]
            The answer, typically {"choice": <option>, "confidence": <float>}.

        Examples
        --------
        # Input
        initial_message = "Hello Friend"

        state = json.dumps({            # the compact summary, NOT the raw text
            "request": {"estimated_prompt_tokens": 5000, "message_count": 1,
            "user_message": initial_message, "has_image": False, "current_model": "None",
            "switch_cost": "None", "tool_count": 28, "max_tokens": 2**15, "stream": True},
        }, sort_keys=True)

        instructions = open("PCAI_LLM/documentation/routing_text_v2.md").read()
        instructions = instructions.replace("{{model_list}}",
        "Qwen3.8-27B, GLM-5.3-Flash, Deepseek-V4.1-Flash-1017")

        criteria = {                    # {option: description}, >= 2 options
            "Qwen3.8-27B": (
                "pick only for simpler tasks: a greeting, lookups, reformat, classification. "
                "Okay for smaller coding projects too. 256k token limit. "
                "{gpu_util: 0.0%, low_demand: true}"
            ),
            "GLM-5.3-Flash": (
                "pick when the deliverable IS judgement: reviewing a diff, verifying a claim, "
                "fact-checking, careful summarisation, second opinions, contract and consistency "
                "checks. {gpu_util: 6.3%, low_demand: true}"
            ),
            "Deepseek-V4.1-Flash-1017": (
                "the default for agent work. Coding, refactors, multi-file changes, debugging, "
                "multi-step plans, tool-driven loops, long context, anything expensive to get "
                "wrong. Strong coding default. {gpu_util: 16.3%, low_demand: true}"
            ),
        }

        m.choice(state, instructions, criteria)

        # Output
        {'type': 'choice',
        'choice': 'Qwen3.8-27B',
        'probabilities': {'Qwen3.8-27B': 0.9933191919330562,
        'GLM-5.3-Flash': 0.0011650887531313096,
        'Deepseek-V4.1-Flash-1017': 0.00551571931381253},
        'confidence': 0.9899787878995843}
        """
        return self.decide(state, instructions=instructions, criteria=criteria, type="choice", **kwargs)

    def yes_no(self, state: Any, instructions: str, **kwargs: Any) -> float:
        """Probability that the answer is yes (decide with type="noul").

        Parameters
        ----------
        state:
            What to judge.
        instructions:
            A yes/no question, phrased so that "yes" is the affirmative case.
        **kwargs:
            Forwarded to decide.

        Returns
        -------
        float
            P(yes) in 0..1. A missing answer also yields 0.0, so treat a zero
            as "no, or no answer" unless you inspect the raw envelope too.
        """
        ans = self.decide(state, instructions=instructions, type="noul", **kwargs)
        return float(ans.get("noul", 0.0))

    def score(
        self,
        state: Any,
        instructions: str,
        levels: list[str],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Expected level on an ordered rubric (decide with type="score").

        Parameters
        ----------
        state:
            What to judge.
        instructions:
            What to rate, and what the scale means.
        levels:
            The rubric, ordered low to high, 2..10 entries.
        **kwargs:
            Forwarded to decide.

        Returns
        -------
        dict[str, Any]
            The answer, typically {"score": <expected level index>, ...}.
        """
        return self.decide(state, instructions=instructions, criteria=levels, type="score", **kwargs)

    # -- agent integration ------------------------------------------------

    def to_mcp_tools(self) -> list[ToolDefinition]:
        """Expose this model as a single decide tool for agent workflows."""

        async def _decide(arguments: dict[str, Any]) -> dict[str, Any]:
            questions = arguments.get("questions")
            if questions:
                out = await self.system_one_async(
                    arguments["state"],
                    questions,
                    images=arguments.get("images"),
                    videos=arguments.get("videos"),
                )
                return out.get("answers", {})
            spec: dict[str, Any] = {
                "type": arguments.get("type", "choice"),
                "instructions": arguments["instructions"],
            }
            if arguments.get("criteria") is not None:
                spec["criteria"] = arguments["criteria"]
            for extra in ("at", "rate"):
                if extra in arguments:
                    spec[extra] = arguments[extra]
            out = await self.system_one_async(
                arguments["state"],
                {"answer": spec},
                images=arguments.get("images"),
                videos=arguments.get("videos"),
            )
            return (out.get("answers") or {}).get("answer", {})

        types = self.question_types or ("choice", "noul", "score")
        return [
            ToolDefinition(
                name="decide",
                description=self.description
                or (
                    "Answer typed questions about a state with calibrated "
                    f"probabilities using {self.model_name} "
                    f"(types: {', '.join(types)}). Returns a probability per "
                    "candidate answer; never generates text."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "state": {
                            "type": "string",
                            "description": "What the questions are about (text or JSON).",
                        },
                        "questions": {
                            "type": "object",
                            "description": (
                                "Map of question id -> {type, instructions, criteria}. "
                                "Use this instead of the single-question fields "
                                "to ask several at once."
                            ),
                        },
                        "type": {
                            "type": "string",
                            "enum": ["choice", "noul", "score", "box", "track"],
                            "description": "Question type for a single-question call.",
                        },
                        "instructions": {
                            "type": "string",
                            "description": "The question, in plain language.",
                        },
                        "criteria": {
                            "description": ("choice: {option: description}; score: ordered list of levels."),
                        },
                        "images": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "base64 data URLs (or reachable http(s) URLs).",
                        },
                        "videos": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "base64 data URLs (or reachable http(s) URLs).",
                        },
                    },
                    "required": ["state"],
                },
                handler=_decide,
            )
        ]


def prefetch_model_cards(models: Any, *, max_workers: int = 8) -> dict[str, dict[str, Any]]:
    """Read several decision-model cards concurrently, blocking until done.

    The alternative to relying on the per-model lazy fetch: call this once at
    application startup and every card is warm afterwards. Off-cluster each
    fetch is a DNS timeout, so N models cost N timeouts sequentially but one
    timeout in parallel.

    Parameters
    ----------
    models:
        DecisionModel instances (or any iterable of them). Anything that is not
        a DecisionModel is ignored, so a mixed list is fine.
    max_workers:
        Upper bound on concurrent fetches.

    Returns
    -------
    dict[str, dict[str, Any]]
        model_name -> model card. Entries whose endpoint could not be read map
        to an empty dict (the same best-effort contract as the lazy path).

    Examples
    --------
        from pcai_utils.pcai_models import vllmsr_d3_turbo_27b
        from pcai_utils.pcai_model_classes import prefetch_model_cards

        prefetch_model_cards([vllmsr_d3_turbo_27b])   # one round-trip, once
    """
    from concurrent.futures import ThreadPoolExecutor

    targets = [m for m in models if isinstance(m, DecisionModel)]
    if not targets:
        return {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(targets))) as pool:
        cards = list(pool.map(lambda m: m._ensure_model_card(), targets))
    return {m.model_name: card for m, card in zip(targets, cards)}
