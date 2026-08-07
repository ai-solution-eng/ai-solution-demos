"""
End-to-end test of ChatModel.aagent + Runner.run_streamed with all 4 MCP
servers. Mirrors the conversation_manager streaming loop so we can see
exactly what would go to the websocket vs. the TTS queue.

Run:
    python src/speech_to_speech_tools/helper_scripts/test_agents_with_tools.py
"""

import asyncio
import sys
from pathlib import Path

import urllib3

# Make `utils.*` importable when run as a script.
_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from typing import Any

from agents import RawResponsesStreamEvent, Runner
from utils.audio_handling import SentenceBuffer
from utils.pcai_models import gemma4_31B

urllib3.disable_warnings()

# External MCP URLs (user-provided). sql_mcp carries the bearer the user
# shared on 2026-07-21; if expired, replace via get_some_tools.py.
PRESTO_BEARER = (
    "eyJhbGciOiJSUzI1NiIsInR5cCIgOiAiSldUIiwia2lkIiA6ICJWMV9IZjlqZFVRZGdTal9l"
    "bU8yeDNZcjdWN1Q2MG1KaDFMMUJjMTRuS3pBIn0.eyJleHAiOjE3ODAwNjkzNzYsImlhdCI6"
    "MTc3OTgxMDE3NiwiYXV0aF90aW1lIjoxNzc5NzQ0NDA4LCJqdGkiOiJjZDdlNGU1Ni02NGUz"
    "LTRhYjItOGYxNC1kNTZkZTFmY2RjZjIiLCJpc3MiOiJodHRwczovL2tleWNsb2FrLnBjYWkt"
    "c2UtYWktYXBwbGljYXRpb24uaHN0LnJkbGFicy5ocGVjb3JwLm5ldC9yZWFsbXMvVUEiLCJz"
    "dWIiOiJhOThkNDkzNi1mODExLTQwZjctYWI0NC04NDlhNDE4MTgzYjkiLCJ0eXAiOiJCZWFy"
    "ZXIiLCJhenAiOiJ1YSIsIm5vbmNlIjoiRW0xNlF5b2VMZlc4T0JNYm96RnJxT1oyajNnMl9v"
    "NTFIUzVJVmZnRU5STSIsInNlc3Npb25fc3RhdGUiOiI5MTFkODZiNi0wMmFmLTRhMzYtOGFj"
    "Yi0yZDI5Zjc1M2NjNjMiLCJhY3IiOiIxIiwic2NvcGUiOiJvcGVuaWQgZW1haWwgcHJvZmls"
    "ZSBvZmZsaW5lX2FjY2VzcyBwY2Fpc29sdXRpOnVhIiwic2lkIjoiOTExZDg2YjYtMDJhZi00"
    "YTM2LThhY2ItMmQyOWY3NTNjYzYzIiwidWlkIjoiMTAwMDAwMjkiLCJlbWFpbF92ZXJpZmll"
    "ZCI6ZmFsc2UsImdpZCI6IjEwMDEiLCJuYW1lIjoiRnJhbmNlc2NvIENhbGl2YSIsIm5hbWVz"
    "cGFjZSI6InByb2plY3QtdXNlci1mcmFuY2VzY28tY2FsaXZhIiwiZ3JvdXBzIjpbInVhLWVu"
    "YWJsZWQiLCJvZmZsaW5lX2FjY2VzcyIsImFkbWluIiwidW1hX2F1dGhvcml6YXRpb24iLCJk"
    "ZWZhdWx0LXJvbGVzLXVhIl0sInByZWZlcnJlZF91c2VybmFtZSI6ImZyYW5jZXNjby1jYWxp"
    "dmEiLCJnaXZlbl9uYW1lIjoiRnJhbmNlc2NvIiwiZmFtaWx5X25hbWUiOiJDYWxpdmEiLCJl"
    "bWFpbCI6ImZyYW5jZXNjby5jYWxpdmFAaHBlLmNvbSJ9.QEcFkP_JNk72BoopWNLbexh0goW"
    "CQAbWNnEUqcfWs3x31CV9QWKkKTiMheCOOHubVLfE447dKhsvneIK5XUDhyseHhuJjzg8k_H"
    "B6E33qwKJTPItKYgLCPAraZuZmfF1gST5dwFjw2Dr4LscW_whQx-IiBjzvkDCn27oQMZLzk6"
    "qaoWekLrFI5mOkHvedImw1tGCz1x6vOsX5QVCu91wDPjr5xQ15QQcuQ-F2eK5l944oFzgIGx"
    "0J8cXmhlB1BJIDQUgDF8KAU1XBtnjtpuvKSzVzk00BwCYkEXCOpBqUdv0URxXR8LNevc0Jb31"
    "jTAKNLK6vIKp1YfvC6ypGb7Vpw"
)

# tool_json shape mirrors get_some_tools.py output, but with external URLs
# so the spike can reach them from a laptop.
TOOL_JSON: dict[str, dict[str, Any]] = {
    "sql_mcp": {
        "url": "https://mcp-ezpresto-server.pcai-se-ai-application.hst.rdlabs.hpecorp.net/mcp",
        "headers": {"Authorization": f"Bearer {PRESTO_BEARER}"},
        "transport": "streamable-http",
    },
    "k8s_opts": {
        "url": "https://mcp-k8s-server.pcai-se-ai-application.hst.rdlabs.hpecorp.net/mcp",
        "transport": "streamable-http",
    },
    "ddgs_mcp": {
        "url": "https://ddgs-lite.pcai-se-ai-application.hst.rdlabs.hpecorp.net/mcp",
        "transport": "streamable-http",
    },
    "seaborn": {
        "url": "https://mcp-seaborn-server.pcai-se-ai-application.hst.rdlabs.hpecorp.net/mcp",
        "transport": "streamable-http",
    },
}


def _tool_call_for_tts(content: str) -> str:
    """Same cleanup the original conversation_manager applied before queuing
    a tool-call announcement for TTS — strip trailing JSON args and the
    'with arguments' suffix so the user doesn't hear raw JSON read aloud."""
    import re

    cleaned = re.sub(r"\s*\{.*\}\s*$", "", content, flags=re.DOTALL)
    cleaned = re.sub(r"\s+with arguments\s*$", "", cleaned)
    return cleaned.strip()


async def main() -> None:
    # --- 1. Instantiate ChatModel.aagent with all 4 MCP servers ---
    print("=" * 80)
    print("STEP 1: Instantiate ChatModel.aagent(tool_json=4 external MCP servers)")
    print("=" * 80)
    m = gemma4_31B
    m.remote()
    agent = await m.aagent(tool_json=TOOL_JSON)
    print(f"  Agent model: {m.model_name}")
    print(f"  MCP servers loaded: {[srv.name for srv in agent.mcp_servers]}")
    for srv in agent.mcp_servers:
        try:
            srv_tools = await srv.list_tools()
            print(f"    {srv.name}: {len(srv_tools)} tools ({[t.name for t in srv_tools][:3]}...)")
        except Exception as e:
            print(f"    {srv.name}: list_tools failed: {e!r}")

    # --- 2. Run a stream that mirrors conversation_manager ---
    print()
    print("=" * 80)
    print("STEP 2: Runner.run_streamed with a tool-triggering prompt")
    print("=" * 80)
    prompt = "Search the web for the latest openai agents sdk release and one-line summary."
    print(f"  Prompt: {prompt!r}")
    print()

    websocket_messages: list[str] = []
    tts_queue: list[str] = []
    sentence_buffer = SentenceBuffer()
    event_type_counts: dict[str, int] = {}

    streamed = Runner.run_streamed(agent, input=[{"role": "user", "content": prompt}])
    async for ev in streamed.stream_events():
        if not isinstance(ev, RawResponsesStreamEvent):
            continue
        raw = ev.data
        raw_type = getattr(raw, "type", "?")
        event_type_counts[raw_type] = event_type_counts.get(raw_type, 0) + 1

        # Mirror conversation_manager._process_llm_response handlers:
        if raw_type == "response.output_text.delta":
            delta = getattr(raw, "delta", None)
            if not delta:
                continue
            websocket_messages.append(delta)
            for sentence in sentence_buffer.add_text(delta):
                if len(sentence.strip()) >= 3:
                    tts_queue.append(sentence.strip())

        elif raw_type == "response.output_item.added":
            item = getattr(raw, "item", None)
            if item and getattr(item, "type", None) == "function_call":
                tool_name = getattr(item, "name", "tool")
                tool_str = f"Calling tool {tool_name}."
                websocket_messages.append(tool_str)
                # Original code queued this for TTS too (regression check).
                tts_text = _tool_call_for_tts(tool_str)
                if tts_text:
                    for sentence in sentence_buffer.add_text(tts_text):
                        if len(sentence.strip()) >= 3:
                            tts_queue.append(sentence.strip())

        elif raw_type == "response.output_item.done":
            item = getattr(raw, "item", None)
            if item and getattr(item, "type", None) == "function_call_output":
                output = getattr(item, "output", "")
                if isinstance(output, str) and output:
                    tool_str = f"Tool Output:\n\n{output}\n\n"
                    websocket_messages.append(tool_str)
                    # Original: ToolMessages were NOT queued for TTS.
                    # Confirming we match that here.

    # Flush any remaining buffered text
    for sentence in sentence_buffer.flush():
        if len(sentence.strip()) >= 3:
            tts_queue.append(sentence.strip())

    # --- 3. Report what the user would see vs. hear ---
    print()
    print("=" * 80)
    print("STEP 3: What would the user SEE (websocket stream_text events)?")
    print("=" * 80)
    for i, msg in enumerate(websocket_messages):
        if len(msg) > 200:
            print(f"  [{i}] {msg[:200]}... ({len(msg)} chars)")
        else:
            print(f"  [{i}] {msg!r}")
    print(f"  total stream events sent to websocket: {len(websocket_messages)}")

    print()
    print("=" * 80)
    print("STEP 4: What would the user HEAR (TTS queue)?")
    print("=" * 80)
    for i, sentence in enumerate(tts_queue):
        print(f"  [{i}] {sentence!r}")
    print(f"  total sentences queued for TTS: {len(tts_queue)}")
    has_tool_announce = any("Calling tool" in s for s in tts_queue)
    print(f"  tool call announcement in TTS queue? {has_tool_announce}")

    print()
    print("=" * 80)
    print("STEP 5: Raw event type breakdown")
    print("=" * 80)
    for t, n in sorted(event_type_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {t:<40} {n}")

    # Cleanup
    for s in agent.mcp_servers:
        try:
            await s.cleanup()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
