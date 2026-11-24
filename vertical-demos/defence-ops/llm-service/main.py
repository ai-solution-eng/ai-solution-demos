"""LLM Service: models from LiteLLM or another OpenAI-compatible endpoint, and VLM chat."""

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any, Literal

import structlog
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import prompts
from common.demo_config import load_demo_config
from common.feeds import VideoName
from common.logging_config import setup_logging
from litellm_client import (
    AssumedCapabilities,
    LiteLLMClient,
    LiteLLMError,
    LiteLLMNotConfiguredError,
    ModelCapabilities,
)
from media import MediaError, fetch_clip, fetch_frames
from prompts import Alert
from settings import get_settings

setup_logging("llm-service")
logger = structlog.get_logger(__name__)
settings = get_settings()

ContextMode = Literal["video", "frames", "none"]
THINK_BLOCK = re.compile(r"<think>(.*?)</think>", re.DOTALL)
# Statuses that mean the clip itself was refused, so frames are worth a retry: the model says so
# (vLLM: 400), or LiteLLM does (its token counter fails on video_url parts for Ollama models with a
# 500, seen 2026-11-02). Auth, budget, rate, unreachable and timeout errors are not retried.
VIDEO_REFUSED = frozenset({400, 413, 415, 422, 500})


class HealthCheckFilter(logging.Filter):
    """Drops access-log lines for successful health checks."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Keeps every record except a 200 on /health."""
        msg = record.getMessage()
        return not ("/health" in msg and " 200" in msg)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Installs the health-check filter once uvicorn's loggers exist."""
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        log = logging.getLogger(name)
        if not any(isinstance(f, HealthCheckFilter) for f in log.filters):
            log.addFilter(HealthCheckFilter())
    yield


app = FastAPI(title="LLM Service", lifespan=lifespan)
client = LiteLLMClient(
    settings.litellm_base_url,
    settings.litellm_api_key.get_secret_value() if settings.litellm_api_key else None,
    settings.request_timeout_seconds,
    settings.models_cache_seconds,
    assumed=AssumedCapabilities(
        vision=settings.llm_supports_vision,
        video=settings.llm_supports_video,
        reasoning=settings.llm_supports_reasoning,
    ),
)


class ChatRequest(BaseModel):
    """A question about the selected feeds.

    Attributes:
        message: The question.
        system_prompt: The assistant instructions from the dashboard.
        videos: Feeds to show the model; empty for a text-only question.
        kafka_context: Recent alerts to include.
        enable_thinking: Ask for the model's own reasoning, when it supports it.
        temperature: Sampling temperature.
    """

    message: str = Field(min_length=1, max_length=4000)
    system_prompt: str | None = Field(default=None, max_length=4000)
    videos: list[VideoName] = Field(default_factory=list, max_length=4)
    kafka_context: list[Alert] = Field(default_factory=list, max_length=50)
    enable_thinking: bool = False
    temperature: float = Field(default=0.7, ge=0, le=2)


def _http_error(e: LiteLLMError | LiteLLMNotConfiguredError) -> HTTPException:
    if isinstance(e, LiteLLMNotConfiguredError):
        return HTTPException(status_code=503, detail=str(e))
    status = e.status_code if e.status_code in (400, 404, 422, 429, 504) else 502
    return HTTPException(status_code=status, detail=f"Model endpoint: {e}")


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy"}


@app.get("/api/v1/llm/models")
async def list_models(refresh: bool = False) -> dict[str, Any]:
    """Lists the models the app's key may use on the model endpoint, and what each accepts.

    Args:
        refresh: Re-read the endpoint instead of the 60 s cache.

    Returns:
        The models and the one selected on System Config.
    """
    try:
        models = await client.list_models(refresh=refresh)
    except (LiteLLMError, LiteLLMNotConfiguredError) as e:
        raise _http_error(e) from e
    selected = (await load_demo_config()).get("llm_model") or None
    return {
        "status": "success",
        "data": {"models": [asdict(m) for m in models], "selected": selected},
    }


def _initial_mode(caps: ModelCapabilities, notices: list[str]) -> ContextMode:
    if caps.video is True:
        return "video"
    if caps.vision is False:
        notices.append(
            f"{caps.id} does not accept images, so it answered from the text and alerts only."
        )
        return "none"
    return "frames"


async def _visual_parts(
    videos: list[str], mode: ContextMode, notices: list[str]
) -> tuple[list[dict[str, Any]], ContextMode, str]:
    """Builds the imagery for the request and the note that tells the model what it shows."""
    if mode == "video":
        try:
            clip = await fetch_clip(settings.video_service_url, videos)
            return (
                [{"type": "video_url", "video_url": {"url": clip}}],
                "video",
                prompts.clip_note(videos),
            )
        except MediaError as e:
            logger.warning("video_clip_failed", error=str(e))
            notices.append("The video clip could not be built, so still frames were sent instead.")
            mode = "frames"
    if mode == "frames":
        try:
            feeds = await fetch_frames(settings.video_service_url, videos, settings.max_images)
            parts = [
                {"type": "image_url", "image_url": {"url": frame}}
                for _, frames in feeds
                for frame in frames
            ]
            note = prompts.frames_note([(video, len(frames)) for video, frames in feeds])
            return parts, "frames", note
        except MediaError as e:
            logger.warning("frame_capture_failed", error=str(e))
            notices.append("Frames could not be captured, so the model answered from text only.")
    return [], "none", prompts.no_imagery_note(videos)


def _thinking_params(enabled: bool, caps: ModelCapabilities) -> dict[str, Any]:
    """Request fields that switch the model's own reasoning on or off.

    Tested through a PCAI cluster's LiteLLM on 2026-11-02: reasoning_effort reaches Ollama (as its
    think switch) and vLLM; chat_template_kwargs reaches only vLLM (Nemotron VL), and Ollama
    ignores it. So "none" is what turns thinking off on Ollama's gemma4 and qwen3.8. Ollama's
    qwen3-vl thinks whatever is sent. Sent straight to vLLM (an MLIS Nemotron VL deployment,
    2026-11-03), the same fields switch thinking on and off. Both go only to models that support
    reasoning, as LiteLLM's model_info or llm.capabilities.reasoning says.
    """
    if caps.reasoning is not True:
        return {}
    if enabled:
        return {"reasoning_effort": "medium", "chat_template_kwargs": {"enable_thinking": True}}
    return {"reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False}}


def _payload(
    req: ChatRequest,
    caps: ModelCapabilities,
    parts: list[dict[str, Any]],
    imagery: str,
    thinking: bool,
) -> dict[str, Any]:
    text = prompts.user_text(req.message, imagery, req.kafka_context)
    content: str | list[dict[str, Any]] = (
        [{"type": "text", "text": text}, *parts] if parts else text
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompts.system_message(req.system_prompt)},
        {"role": "user", "content": content},
    ]
    return {
        "model": caps.id,
        "messages": messages,
        "temperature": req.temperature,
        **_thinking_params(thinking, caps),
    }


def _split_reasoning(result: dict[str, Any]) -> tuple[str, str | None]:
    """Separates the answer from the model's reasoning, wherever the model put it."""
    choices = result.get("choices") or [{}]
    message = choices[0].get("message") or {}
    content: Any = message.get("content") or ""
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    # LiteLLM returns reasoning_content; vLLM, called directly, returns reasoning.
    reasoning_parts = [
        str(message.get("reasoning_content") or message.get("reasoning") or "").strip()
    ]

    blocks = THINK_BLOCK.findall(content)
    if blocks:
        reasoning_parts += [b.strip() for b in blocks]
        content = THINK_BLOCK.sub("", content)
    elif "</think>" in content:
        # Templates that open <think> in the prompt return only the closing tag.
        head, _, content = content.partition("</think>")
        reasoning_parts.append(head.strip())

    reasoning = "\n\n".join(p for p in reasoning_parts if p)
    return str(content).strip(), reasoning or None


@app.post("/api/v1/llm/chat")
async def chat(req: ChatRequest) -> dict[str, Any]:
    """Answers a question about the selected feeds with the model chosen on System Config.

    Video-capable models get one clip of all the feeds; image models get still frames;
    others get text only. Thinking mode is used only when the model supports it, and every
    such decision is returned as a notice. One frame to the 120B Nemotron VL took 4.3 s
    (2026-11-02); a clip adds about 1 s per feed to build, and thinking adds model time.

    Returns:
        The reply, the reasoning if any, the model, how the feeds were sent, and notices.
    """
    model = (await load_demo_config()).get("llm_model")
    if not isinstance(model, str) or not model:
        raise HTTPException(
            status_code=400,
            detail="No model selected. An administrator picks one on System Config, or sets "
            "llm.model in the app's Helm values.",
        )
    try:
        caps = await client.capabilities(model)
    except (LiteLLMError, LiteLLMNotConfiguredError) as e:
        raise _http_error(e) from e

    notices: list[str] = []
    thinking = req.enable_thinking and caps.reasoning is True
    if req.enable_thinking and not thinking:
        notices.append(f"{model} does not support thinking mode, so it answered without it.")

    mode: ContextMode = _initial_mode(caps, notices) if req.videos else "none"
    parts, mode, imagery = await _visual_parts(req.videos, mode, notices)
    logger.info("llm_chat", model=model, context=mode, feeds=len(req.videos), thinking=thinking)

    try:
        result = await client.chat(_payload(req, caps, parts, imagery, thinking))
    except LiteLLMNotConfiguredError as e:
        raise _http_error(e) from e
    except LiteLLMError as e:
        if mode != "video" or e.status_code not in VIDEO_REFUSED:
            logger.error("llm_chat_failed", model=model, status_code=e.status_code, error=str(e))
            raise _http_error(e) from e
        logger.warning("video_refused", model=model, status_code=e.status_code, error=str(e))
        client.mark_video_refused(model)
        notices.append(f"{model} refused the video clip, so still frames were sent instead.")
        parts, mode, imagery = await _visual_parts(req.videos, "frames", notices)
        try:
            result = await client.chat(_payload(req, caps, parts, imagery, thinking))
        except LiteLLMError as retry_error:
            raise _http_error(retry_error) from retry_error

    reply, reasoning = _split_reasoning(result)
    return {
        "status": "success",
        "data": {
            "reply": reply,
            "reasoning": reasoning,
            "model": model,
            "context": mode,
            "thinking": thinking,
            "notices": notices,
        },
    }
