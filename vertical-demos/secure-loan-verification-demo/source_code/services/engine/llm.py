"""Shared OpenAI-compatible LLM call helpers.

Centralizes the request-body options (reasoning effort, thinking-mode control)
so the assistant and the memo agent stay consistent and a single env var
(SLVD_LLM_REASONING_EFFORT) tunes latency for reasoning-model backends.
"""
from __future__ import annotations

import httpx

from .config import settings


def reasoning_params(effort: str | None = None) -> dict:
    """Request-body extras to cap/disable reasoning on reasoning-model backends.

    effort: "low" (default) | "none" | "" (send nothing).
    - "none": disable chain-of-thought entirely (fastest, content-only).
      On qwen3 this is enable_thinking=false; on OpenAI-compatible endpoints
      reasoning_effort=none.
    - "low": cap the thinking budget (still a little CoT, cheap).
    Unknown keys are ignored by providers that don't support them, so this is
    safe to send to any OpenAI-compatible /v1/chat/completions endpoint.
    """
    e = settings.llm_reasoning_effort if effort is None else effort
    if e == "none":
        return {"reasoning_effort": "none",
                "chat_template_kwargs": {"enable_thinking": False}}
    if e == "low":
        return {"reasoning_effort": "low"}
    return {}


def chat(messages: list[dict], *, max_tokens: int, temperature: float,
         timeout: float | None = None) -> str:
    """POST /v1/chat/completions and return the message content (or reasoning).

    Falls back to the `reasoning` field when `content` is empty (qwen3 thinking
    mode), and strips whitespace. Raises on HTTP/network errors.
    """
    body = {
        "model": settings.llm_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        **reasoning_params(),
    }
    r = httpx.post(
        f"{settings.llm_base_url}/chat/completions",
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        json=body,
        timeout=timeout or settings.llm_timeout_s,
    )
    r.raise_for_status()
    m = r.json()["choices"][0]["message"]
    return (m.get("content") or m.get("reasoning") or "").strip()
