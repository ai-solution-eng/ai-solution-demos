"""
Spike: OpenAI Agents SDK against PCAI vLLM chat endpoints.

Goal: Verify openai-agents can replace langchain.agents.create_agent for
chat-only models via the /v1/responses API path. Disposable — delete after
evaluation. Read-only consumer of pcai_models and SentenceBuffer.

Three tests (each gates the migration differently):
  T2 - vLLM implements /v1/responses for these models.  PHASE-0 GATE.
  T6 - MCPServerStreamableHttp loads tools and round-trips a call.
       This is what would replace langchain_mcp_adapters.
  T4 - Runner.run_streamed text deltas plug into SentenceBuffer.

Run:
    python src/speech_to_speech_tools/helper_scripts/spike_agents_sdk.py
"""

import asyncio
import sys
from pathlib import Path

import urllib3

# Make `utils.*` / `main_components.*` importable when run as a script,
# matching app.py's import style.
_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent  # src/speech_to_speech_tools/
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

import agents
import httpx
from agents import (
    Agent,
    RawResponsesStreamEvent,
    Runner,
    set_default_openai_client,
    set_tracing_disabled,
)
from agents.mcp import MCPServerStreamableHttp
from agents.models.openai_responses import OpenAIResponsesModel
from openai import AsyncOpenAI
from utils.audio_handling import SentenceBuffer
from utils.pcai_models import deepseek_v4_flash_280B, gemma4_31B

# vLLM serves self-signed certs inside the cluster; we hit it over the
# remote ingress from the laptop, so silence the resulting warnings.
urllib3.disable_warnings()

MODELS = [gemma4_31B, deepseek_v4_flash_280B]
DDGS_URL = "https://ddgs-lite.pcai-se-ai-application.hst.rdlabs.hpecorp.net/mcp"


def _streamable_http_factory(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """httpx client factory for MCPServerStreamableHttp that disables TLS
    verification — required for the PCAI ingress which serves self-signed
    certs. Matches the default factory's signature but adds verify=False."""
    kwargs: dict = {"follow_redirects": False, "verify": False}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if headers is not None:
        kwargs["headers"] = headers
    if auth is not None:
        kwargs["auth"] = auth
    return httpx.AsyncClient(**kwargs)


def _build_model(model) -> OpenAIResponsesModel:
    """Force remote transport, build AsyncOpenAI bound to this model's endpoint,
    register it as the SDK default, disable tracing, and return an explicit
    OpenAIResponsesModel so Agent(model=...) bypasses prefix-routing
    (which would otherwise choke on `RedHatAI/...` / `deepseek-ai/...`)."""
    model.remote()
    client = AsyncOpenAI(
        base_url=model.base_url,
        api_key=model.api_key,
        http_client=httpx.AsyncClient(verify=False, timeout=httpx.Timeout(300.0, connect=30.0)),
    )
    # Still register as default so tracing-disabled state and any internal
    # SDK lookups behave; the explicit Model passed to Agent is what actually
    # gets used.
    set_default_openai_client(client, use_for_tracing=False)
    set_tracing_disabled(True)
    return OpenAIResponsesModel(model=model.model_name, openai_client=client)


def _fresh_client(model, timeout: float = 60.0) -> AsyncOpenAI:
    """Standalone client for the raw /v1/responses smoke (T2) — bypasses the
    SDK so a failure here is unambiguously vLLM, not the agent layer."""
    return AsyncOpenAI(
        base_url=model.base_url,
        api_key=model.api_key,
        http_client=httpx.AsyncClient(verify=False, timeout=timeout),
    )


async def t2_responses_endpoint() -> None:
    """Phase-0 gate: does vLLM implement /v1/responses for these models?"""
    print("\n=== T2: raw /v1/responses non-streaming (PHASE-0 GATE) ===")
    for m in MODELS:
        m.remote()
        name = m.model_name
        try:
            r = await _fresh_client(m).responses.create(model=name, input="Say hi in three words.")
            print(f"  PASS  {name:<40} {r.output_text[:60]!r}")
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", "?")
            print(f"  FAIL  {name:<40} HTTP {status}: {e}")
            if status == 404:
                print("        !!! /v1/responses not implemented - plan stops here.")


async def t6_mcp_via_responses() -> None:
    """Highest-value test: replaces langchain_mcp_adapters + create_agent's
    MCP translation layer. maps your tool_json shape 1:1 onto
    MCPServerStreamableHttp(params={'url':..., 'headers':...}, name=...)."""
    print("\n=== T6: MCP tool via MCPServerStreamableHttp ===")
    for m in MODELS:
        model_obj = _build_model(m)
        server = MCPServerStreamableHttp(
            params={
                "url": DDGS_URL,
                "httpx_client_factory": _streamable_http_factory,
            },
            name="ddgs",
        )
        agent = Agent(
            name="spike",
            model=model_obj,
            instructions="Use the search tool to answer concisely.",
            mcp_servers=[server],
        )
        try:
            async with server:
                tools = await server.list_tools()
                if not tools:
                    print(f"  FAIL  {m.model_name:<40} no tools exposed by MCP server")
                    continue
                tool_names = [t.name for t in tools]
                result = await Runner.run(agent, "Search the web for: openai agents sdk")
                out = (result.final_output or "").replace("\n", " ")[:80]
                print(f"  PASS  {m.model_name:<40} {len(tools)} tools {tool_names[:3]}")
                print(f"        output: {out!r}")
        except Exception as e:
            msg = repr(e)
            # Detect the cluster-DNS case so the user knows it's environment
            # and not a migration blocker.
            if "Connection timeout" in msg or "Name or service not known" in msg:
                print(f"  SKIP  {m.model_name:<40} cannot reach {DDGS_URL}")
                print("        (cluster-internal DNS - run from inside the cluster,")
                print("         or kubectl port-forward ddgs-lite-service:9090)")
            else:
                print(f"  FAIL  {m.model_name:<40} {msg}")


async def t4_streaming_into_sentence_buffer() -> None:
    """Verify response.output_text.delta events plug straight into SentenceBuffer.
    The Agents SDK wraps raw Responses-API events in RawResponsesStreamEvent;
    the actual text delta lives at ev.data.delta for events whose type is
    'response.output_text.delta'. A PASS here means conversation_manager's
    sentence buffer / TTS queue plumbing can be reproduced with zero adapter code."""
    print("\n=== T4: Runner.run_streamed -> SentenceBuffer ===")
    for m in MODELS:
        model_obj = _build_model(m)
        agent = Agent(
            name="spike",
            model=model_obj,
            instructions="Tell me a 3-sentence story about a robot.",
        )
        buf = SentenceBuffer()
        sentences: list[str] = []
        deltas = 0
        raw_event_types: dict[str, int] = {}
        try:
            streamed = Runner.run_streamed(agent, "Begin.")
            async for ev in streamed.stream_events():
                # RawResponsesStreamEvent.data holds the underlying
                # OpenAI Responses event (response.created,
                # response.output_text.delta, response.completed, ...).
                if not isinstance(ev, RawResponsesStreamEvent):
                    continue
                raw = ev.data
                raw_type = getattr(raw, "type", "?")
                raw_event_types[raw_type] = raw_event_types.get(raw_type, 0) + 1
                if raw_type == "response.output_text.delta":
                    delta = getattr(raw, "delta", None)
                    if delta:
                        deltas += 1
                        sentences.extend(buf.add_text(delta))
            sentences.extend(buf.flush())
            top = sorted(raw_event_types.items(), key=lambda kv: -kv[1])[:5]
            print(f"  {'PASS' if deltas else 'FAIL'}  {m.model_name:<40} {deltas} deltas -> {len(sentences)} sentences")
            print(f"        top raw event types: {dict(top)}")
            if sentences:
                print(f"        first sentence: {sentences[0][:70]!r}")
        except Exception as e:
            print(f"  FAIL  {m.model_name:<40} {e!r}")


async def main() -> None:
    print(f"openai-agents version: {agents.__version__}")
    try:
        import openai as _o

        print(f"openai version:        {_o.__version__}")
    except Exception:
        pass
    print(f"models under test:     {[m.model_name for m in MODELS]}")
    print(f"ddgs MCP URL:          {DDGS_URL}")

    await t2_responses_endpoint()
    await t6_mcp_via_responses()
    await t4_streaming_into_sentence_buffer()
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
