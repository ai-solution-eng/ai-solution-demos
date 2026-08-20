"""
Production-path test: exercises the REAL ConversationSession._process_llm_response
code (not a parallel implementation) with a mock websocket and a real vLLM model.

Closes the gap between "spike proves the SDK works" and "production code works":
  - Proves Runner.run_streamed(self.llm_function, input=self.history) accepts
    a history list that includes a {"role": "system"} message (the type mypy
    complained about and I silenced with # type: ignore).
  - Proves the actual streaming loop in conversation_manager.py produces
    websocket events and appends the assistant response to history.
  - Proves the real ChatModel.aagent() construction path via update_configs.

Run:
    python src/speech_to_speech_tools/helper_scripts/test_production_path.py
"""

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import urllib3

# Redirect transcript/audio dirs to temp before any code reads them.
# constants.py reads TRANSCRIPTS_DIR / AUDIO_DIR from env at import time.
_tmp = tempfile.mkdtemp(prefix="s2s_test_")
os.environ.setdefault("TRANSCRIPTS_DIR", _tmp)
os.environ.setdefault("AUDIO_DIR", _tmp)

# Make `utils.*` / `main_components.*` importable when run as a script.
_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

urllib3.disable_warnings()
logging.basicConfig(level=logging.WARNING)

from main_components.conversation_manager import ConversationSession
from utils.pcai_models import gemma4_31B

DDGS_URL = "https://ddgs-lite.pcai-se-ai-application.hst.rdlabs.hpecorp.net/mcp"


class MockWebSocket:
    """Stand-in for starlette.WebSocket. Satisfies the
    `hasattr(self.websocket, 'client_state') and self.websocket.client_state`
    checks in conversation_manager and records every send_json call."""

    def __init__(self) -> None:
        self.client_state = True  # truthy, so sends proceed
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


async def main() -> None:
    # --- Build a real config pointing at gemma4_31B ---
    config: dict[str, Any] = {
        "remote": True,  # use external ingress, not cluster-internal DNS
        "llmBaseUrl": gemma4_31B.url_remote,
        "llmApiKey": gemma4_31B.api_key,
        "llmModelName": gemma4_31B.model_name,
        "systemPrompt": "You are a helpful, concise voice assistant. Keep responses short.",
        "ttsBaseUrl": "",
        "ttsApiKey": "",
        "ttsModelName": "",
        "ttsVoice": "alys",
        "asrBaseUrl": "",
        "asrApiKey": "",
        "asrModelName": "",
        "asrHallucinationPatterns": "",
        "sampleRate": 16000,
        "vadAggression": 3,
        "rmsThreshold": 200,
        "frameDuration": 30,
        "language": "",
        "toolCalls": None,  # no-tools first; tool test below
    }

    # --- Instantiate ConversationSession (runs update_configs) ---
    ws = MockWebSocket()
    session = ConversationSession(websocket=ws)  # type: ignore[arg-type]

    # update_configs with our real config (this builds the ChatModel)
    session.update_configs(config)
    assert session.llm_class is not None, "LLM class was not created by update_configs"
    print(f"[setup] llm_class created: {session.llm_class.model_name}")
    print(f"[setup] history after system prompt: {session.history}")

    # Disable TTS so we don't need a real TTS endpoint. The TTS-queue logic
    # is verified separately in test_agents_with_tools.py.
    session.tts_enabled = False

    # ============================================================
    # TEST 1: No tools — does the production streaming loop work?
    # ============================================================
    print("\n" + "=" * 80)
    print("TEST 1: _process_llm_response — no tools, system+user history")
    print("=" * 80)

    session.history.append({"role": "user", "content": "Say hi in one short sentence."})
    history_len_before = len(session.history)
    ws.sent.clear()

    await session._process_llm_response()

    stream_texts = [e for e in ws.sent if e.get("type") == "stream_text"]
    status_events = [e for e in ws.sent if e.get("type") == "status_update"]
    complete_events = [e for e in ws.sent if e.get("type") in ("response_end", "response_complete")]
    full_response = "".join(e["text"] for e in stream_texts)

    print(f"  websocket events sent:     {len(ws.sent)}")
    print(f"    status_update:           {len(status_events)}")
    print(f"    stream_text:             {len(stream_texts)}")
    print(f"    response_complete/end:   {len(complete_events)}")
    print(f"  full assistant response:   {full_response!r}")
    print(f"  history before:            {history_len_before} msgs")
    print(f"  history after:             {len(session.history)} msgs")
    if len(session.history) > history_len_before:
        print(f"  appended assistant msg:    {session.history[-1]!r}")

    t1_pass = (
        len(stream_texts) > 0
        and len(session.history) > history_len_before
        and session.history[-1].get("role") == "assistant"
        and len(session.history[-1].get("content", "")) > 0
    )
    print(f"  RESULT: {'PASS' if t1_pass else 'FAIL'}")

    # ============================================================
    # TEST 2: With tools — does MCP load + tool-call announce work
    # in the production code path?
    # ============================================================
    print("\n" + "=" * 80)
    print("TEST 2: _process_llm_response — with ddgs MCP tool")
    print("=" * 80)

    tool_config: dict[str, Any] = dict(config)
    import json

    tool_config["toolCalls"] = json.dumps(
        {
            "ddgs_mcp": {
                "url": DDGS_URL,
                "transport": "streamable-http",
            }
        }
    )
    session.update_configs(tool_config)
    session.llm_function = None  # force re-init with tools
    session.history = [
        {"role": "system", "content": config["systemPrompt"]},
        {
            "role": "user",
            "content": "Search the web for: openai agents sdk. Be very concise — one sentence.",
        },
    ]
    ws.sent.clear()

    await session._process_llm_response()

    stream_texts_2 = [e for e in ws.sent if e.get("type") == "stream_text"]
    full_response_2 = "".join(e["text"] for e in stream_texts_2)
    tool_announcements = [e for e in stream_texts_2 if e.get("text", "").startswith("Calling ")]
    tool_results = [e for e in stream_texts_2 if e.get("text", "").startswith("Result:")]

    print(f"  stream_text events:        {len(stream_texts_2)}")
    print(f"  tool announcements:        {len(tool_announcements)}")
    for ta in tool_announcements:
        print(f"    {ta['text']!r}")
    print(f"  tool results:              {len(tool_results)}")
    for tr in tool_results:
        print(f"    {tr['text'][:120]!r}")
    print(f"  full response:             {full_response_2[:200]!r}")
    print(f"  history last msg role:     {session.history[-1].get('role') if session.history else 'none'}")

    t2_pass = len(tool_announcements) > 0 and len(tool_results) > 0 and len(full_response_2) > 0
    print(f"  RESULT: {'PASS' if t2_pass else 'FAIL'}")

    # --- Cleanup ---
    # Mock save_state — Redis is cluster-internal, unreachable from laptop.
    async def _noop_save_state():
        pass

    session.save_state = _noop_save_state  # type: ignore[method-assign]
    try:
        await session.close()
    except Exception as e:
        print(f"  [cleanup warning] {e!r}")

    # --- Summary ---
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"  TEST 1 (no tools, production path):     {'PASS' if t1_pass else 'FAIL'}")
    print(f"  TEST 2 (with tools, production path):   {'PASS' if t2_pass else 'FAIL'}")
    if t1_pass and t2_pass:
        print("\n  The actual ConversationSession._process_llm_response code works")
        print("  end-to-end against a live vLLM model via the Agents SDK.")
        print("  The system-message-in-history concern (mypy arg-type) is resolved:")
        print("  the Responses API accepts [{role: system}, {role: user}] input.")


if __name__ == "__main__":
    asyncio.run(main())
