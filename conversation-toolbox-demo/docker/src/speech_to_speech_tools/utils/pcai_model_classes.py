import asyncio
import base64
import io
import os
import re
import threading
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import cached_property, partial
from typing import Any, Literal

import httpx
from openai import AsyncOpenAI, OpenAI

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
from .model_adapters import MultiModalEmbeddings, MultiModalReranker
from .token_text_splitter import TokenTextSplitter

logger = logging.getLogger(__name__)

_MLIS_PAGE = "https://mlis.pcai-se-ai-application.hst.rdlabs.hpecorp.net/ui/deployments"
_NOT_DEPLOYED = ""


def _pool_limits_from_env() -> httpx.Limits:
    """Build httpx connection-pool limits from env vars.

    Defaults (30 / 10) match the original single-replica chart.  The
    scale chart raises these so the embedder — on every search's critical
    path — is not the bottleneck at high concurrency.
    """
    try:
        max_conn = int(os.environ.get("MODEL_POOL_MAX_CONNECTIONS", "30"))
    except (TypeError, ValueError):
        max_conn = 30
    try:
        max_keepalive = int(os.environ.get("MODEL_POOL_MAX_KEEPALIVE_CONNECTIONS", "10"))
    except (TypeError, ValueError):
        max_keepalive = 10
    return httpx.Limits(
        max_connections=max(1, max_conn),
        max_keepalive_connections=max(1, max_keepalive),
        keepalive_expiry=120.0,
    )


# Sync clients are thread-safe and can be shared globally.
_SHARED_HTTP_CLIENT = httpx.Client()
_SHARED_REMOTE_HTTP_CLIENT = httpx.Client(
    verify=False,
    limits=_pool_limits_from_env(),
)

# Async clients must be bound to a single event loop.
# We cache one per running loop (keyed by the loop object itself, not
# id(loop), to avoid id() recycling after GC hands a stale client to a
# fresh loop).  WeakKeyDictionary auto-evicts when the loop is GC'd.
_async_client_cache: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_async_remote_client_cache: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_async_client_lock = threading.Lock()

# Cache of discovered model names keyed by the ``{base_url}/models`` URL.
# Only successful lookups are stored so a temporarily-down endpoint is
# retried on the next construction. Sessions rebuild ASR/LLM/TTS frequently,
# so this avoids redundant /v1/models round-trips per session.
_discovered_model_names: dict[str, str] = {}
_discovered_model_lock = threading.Lock()


def discover_model_name(base_url: str, api_key: str = "", remote: bool = True) -> str:
    """Best-effort lookup of the served model id via ``GET {base_url}/models``.

    ``base_url`` is the OpenAI base URL (root + ``"/v1"``).  PCAI/vLLM endpoints
    serve a single model, so the first ``data[].id`` is returned.  Returns
    ``""`` on any failure (network, non-200, empty list) -- keeping this
    best-effort so a transient blip never hard-fails the caller.  Successful
    lookups are cached per URL; failures are not, so a recovered endpoint is
    retried on the next call.
    """
    url = f"{base_url}/models"
    cached = _discovered_model_names.get(url)
    if cached:
        return cached
    client = _SHARED_REMOTE_HTTP_CLIENT if remote else _SHARED_HTTP_CLIENT
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        resp = client.get(url, headers=headers, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        models = data.get("data", []) if isinstance(data, dict) else []
        ids = [str(m["id"]) for m in models if isinstance(m, dict) and m.get("id")]
        if ids:
            logger.info(
                "Auto-discovered model name '%s' from %s (available: %s)",
                ids[0],
                url,
                ids,
            )
            with _discovered_model_lock:
                _discovered_model_names[url] = ids[0]
            return ids[0]
        logger.warning("No models listed at %s; leaving model_name empty.", url)
    except Exception as e:
        logger.warning(
            "Could not auto-discover model name from %s: %s. "
            "Set model_name explicitly if the endpoint lacks /v1/models.",
            url,
            e,
        )
    return ""


def _get_async_client(remote: bool = False) -> httpx.AsyncClient:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        # No running loop — create a short-lived client.  Caller is
        # responsible for closing it.
        return httpx.AsyncClient(
            verify=not remote,
            timeout=httpx.Timeout(300.0, connect=30.0),
            limits=_pool_limits_from_env(),
        )
    cache = _async_remote_client_cache if remote else _async_client_cache
    with _async_client_lock:
        client = cache.get(loop)
        if client is None:
            client = httpx.AsyncClient(
                verify=not remote,
                timeout=httpx.Timeout(300.0, connect=30.0),
                limits=_pool_limits_from_env(),
            )
            cache[loop] = client
        return client


__all__ = [
    "BaseModel",
    "ChatModel",
    "EmbeddingModel",
    "RerankerModel",
    "ToolDefinition",
    "VoiceModel",
    "discover_model_name",
    "strip_tool_markers",
]

_TOOL_CALL_BLOCK = re.compile(r"<\|?\s*tool_call\s*\|?>.*$", re.DOTALL)
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
                # TLS-bypass httpx factory (PCAI ingress serves self-signed
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
            # and a 5s httpx request timeout (**) unless overridden. Real tool
            # calls (SQL queries, k8s ops, ...) routinely exceed that, surfacing
            # as "Timed out while waiting for response to ClientRequest. Waited
            # 5.0 seconds." Allow an optional per-server `timeout` (seconds) in
            # the tool config, falling back to a saner default. We apply it to
            # both the ClientSession read timeout and the underlying httpx
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
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """httpx client factory for MCPServerStreamableHttp that disables TLS
    verification - required for the PCAI ingress which serves self-signed
    certs. Mirrors the default factory signature so it can be dropped in
    via params['httpx_client_factory']."""
    kwargs: dict = {"follow_redirects": False, "verify": False}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if headers is not None:
        kwargs["headers"] = headers
    if auth is not None:
        kwargs["auth"] = auth
    return httpx.AsyncClient(**kwargs)


class _NamedBytesIO(io.BytesIO):
    """BytesIO with a ``name`` attribute, required by the OpenAI
    transcription API to infer the audio format."""

    def __init__(self, content: bytes, name: str = "audio"):
        super().__init__(content)
        self.name = name


@dataclass
class BaseModel:
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
    model_client_class: Callable = OpenAI
    model_async_client_class: Callable = AsyncOpenAI

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
    def from_config(cls, config: dict[str, Any], api_key: str = "") -> "BaseModel":
        """Construct from a DB config dict + resolved api_key.

        Only fields that exist on the dataclass are mapped.  Callable
        fields (``preprocessor``, ``model_instantiation_class``) are
        skipped when the JSON value is a string — they can't be
        deserialized.  Lists are converted to tuples/sets where the
        dataclass expects them.
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
        new_path = path.replace("https", "http")
        return new_path[: new_path.find(".serving.")] + ".svc.cluster.local"

    @cached_property
    def client(self) -> OpenAI:
        assert self.currently_deployed, (
            f"Model {self.model_name} is not currently deployed. "
            f"See {_MLIS_PAGE} and change flag `currently_deployed` to enable."
        )
        return self.model_client_class(
            api_key=self.api_key,
            base_url=self.base_url,
            http_client=self.http_client,
        )

    @cached_property
    def async_client(self) -> AsyncOpenAI:
        assert self.currently_deployed, (
            f"Model {self.model_name} is not currently deployed. "
            f"See {_MLIS_PAGE} and change flag `currently_deployed` to enable."
        )
        return self.model_async_client_class(
            api_key=self.api_key,
            base_url=self.base_url,
            http_client=self.http_async_client,
        )

    @cached_property
    def url_local(self) -> str:
        return self._convert_remote_url_to_local(self.url_remote)

    @cached_property
    def base_url(self) -> str:
        return (self.url_local if self.model_usage == "local" else self.url_remote) + "/v1"

    @cached_property
    def http_client(self):
        return _SHARED_REMOTE_HTTP_CLIENT if self.model_usage == "remote" else _SHARED_HTTP_CLIENT

    @cached_property
    def http_async_client(self):
        return _get_async_client(remote=self.model_usage == "remote")

    @cached_property
    def model(self):
        return self.build_model()

    def build_model(self, **kwargs):
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
        self.model_usage = "remote"
        self._clear_cached_class_elements()

    def local(self) -> None:
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
            # Mask long API keys
            if f.name == "api_key" and isinstance(val, str) and len(val) > 20:
                val = val[:12] + "..." + val[-4:]
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

    @cached_property
    def model(self):
        m = super().model
        return m

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
            if "role" in m and ("content" in m or "image" in m or "text" in m):
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
                # role-less input: wrap as a user message.
                fixed.append({"role": "user", "content": _parts_for(m)})
        return fixed

    @cached_property
    def llm_chat_function(self) -> Callable:
        return partial(self.client.chat.completions.create, model=self.model_name)

    def llm_chat_function_call(self, messages: messages_dtype, **chat_kwargs):

        return self.llm_chat_function(messages=self._fix_chat_inputs(messages), **chat_kwargs)

    @cached_property
    def llm_async_chat_function(self) -> Callable:
        return partial(self.async_client.chat.completions.create, model=self.model_name)

    def llm_async_chat_function_call(self, messages: str | dict[str, Any] | list[dict[str, Any]], **chat_kwargs):
        return self.llm_async_chat_function(messages=self._fix_chat_inputs(messages), **chat_kwargs)

    @cached_property
    def llm_response_function(self) -> Callable:
        return partial(self.client.responses.create, model=self.model_name)

    def llm_response_function_call(self, input: str | dict[str, Any] | list[dict[str, Any]], **chat_kwargs):
        return self.llm_response_function(input=input, **chat_kwargs)

    @cached_property
    def llm_async_response_function(self) -> Callable:
        return partial(self.async_client.responses.create, model=self.model_name)

    def llm_async_response_function_call(self, input: str | dict[str, Any] | list[dict[str, Any]], **chat_kwargs):
        return self.llm_async_response_function(input=input, **chat_kwargs)

    def agent(self, tool_json: dict[str, dict[str, Any]] | None = None):
        if Agent is None:
            raise ImportError("The `agents` SDK is not installed. Install with: pip install openai-agents")
        return sync_wrapper_safe(self.aagent, {"tool_json": tool_json})

    async def aagent(self, tool_json: dict[str, dict[str, Any]] | None = None) -> Agent:
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
    model_instantiation_class: Callable = OpenAI

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
        return self.tts_async_function(input=input, **chat_kwargs)

    @cached_property
    def asr_function(self) -> Callable:
        assert self.model_type != "TTS", "No `asr_function` is available for TTS model_type."
        return partial(self.client.audio.transcriptions.create, model=self.model_name)

    def asr_function_call(self, file, **chat_kwargs):
        return self.asr_function(file=file, **chat_kwargs)

    @cached_property
    def asr_async_function(self) -> Callable:
        assert self.model_type != "TTS", "No `asr_function` is available for TTS model_type."
        return partial(self.async_client.audio.transcriptions.create, model=self.model_name)

    def asr_async_function_call(self, file, **chat_kwargs):
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

            voices_str = ", ".join(sorted(self._get_available_voices())) or "default"
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
    asr_model: "VoiceModel | None" = field(default=None, init=False, repr=False)
    llm_model: "ChatModel | None" = field(default=None, init=False, repr=False)
    tts_model: "VoiceModel | None" = field(default=None, init=False, repr=False)

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

        from ..orchestration.context import get_current_context

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

    mm_processor_kwargs: dict[str, Any] = field(default_factory=dict)

    # If input should be preprocessed
    preprocessor: Callable | None = None

    allowable_modalities = ("text", "audio", "image", "video")

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
    # Model args w/ defaults
    model_instantiation_class: Callable = MultiModalReranker

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
