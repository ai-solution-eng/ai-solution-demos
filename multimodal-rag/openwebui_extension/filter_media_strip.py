"""
title: Image/Video Strip Filter
author: HPE Multimodal RAG
description: >
  Lightweight filter that ONLY removes images/videos (and optionally audio)
  from the LLM context for models that cannot handle them natively.
  No RAG staging, no MCP hints, no dataset listing, no long-term memory.
  Media attached by the user is simply stripped out so a text-only LLM
  never receives image_url / video_url / audio_url parts that would cause
  "not a multimodal model" errors. For vision-capable models (NOT in
  STRIP_MODELS) media is left untouched.
required_open_webui_version: 0.3.0
version: 1.0.0
licence: MIT
"""

import logging
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Take control of file processing so OWUI does not try to read images/videos
# as text (media is stripped for text-only models; kept for vision models).
file_handler = True

_MODALITY_MAP: dict[str, str] = {
    "image/png": "image",
    "image/jpeg": "image",
    "image/jpg": "image",
    "image/gif": "image",
    "image/webp": "image",
    "image/bmp": "image",
    "image/tiff": "image",
    "image/svg+xml": "image",
    "video/mp4": "video",
    "video/webm": "video",
    "video/x-msvideo": "video",
    "video/quicktime": "video",
    "video/x-matroska": "video",
    "video/mpeg": "video",
    "audio/mpeg": "audio",
    "audio/mp3": "audio",
    "audio/wav": "audio",
    "audio/wave": "audio",
    "audio/x-wav": "audio",
    "audio/flac": "audio",
    "audio/ogg": "audio",
    "audio/opus": "audio",
    "audio/aac": "audio",
    "audio/x-m4a": "audio",
}

_MODALITY_PREFIXES: dict[str, list[str]] = {
    "image": ["image/"],
    "video": ["video/"],
    "audio": ["audio/"],
}


def _modality_from_mime(mime: str) -> Optional[str]:
    m = (mime or "").lower()
    exact = _MODALITY_MAP.get(m)
    if exact:
        return exact
    for mod, prefixes in _MODALITY_PREFIXES.items():
        for p in prefixes:
            if m.startswith(p):
                return mod
    return None


def _mime_from_data_url(url: str) -> Optional[str]:
    if url.startswith("data:"):
        return url.split(";")[0].replace("data:", "").strip()
    return None


def _is_media_type(part: Any, strip_audio: bool) -> bool:
    """True when *part* is an inline media block that should be removed."""
    if not isinstance(part, dict):
        return False
    t = part.get("type", "")
    if t == "image_url":
        return True
    if t == "video_url":
        return True
    if t == "audio_url":
        return strip_audio
    return False


def _extract_user_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
        return "\n".join(texts).strip()
    return str(content) if content else ""


class Filter:

    class Valves(BaseModel):
        STRIP_MODELS: str = Field(
            default="",
            description="Comma-separated substrings matched against the "
            "model ID or name (case-insensitive). Image/video (and audio, "
            "see STRIP_AUDIO) parts are REMOVED from the LLM context when "
            "any substring matches (e.g. 'deepseek' matches "
            "'deepseek-ai/DeepSeek-V4-Flash' and its clones). Models with "
            "NO match keep media in context (vision models like Gemma). "
            "Empty = strip for ALL models (safe default for text-only LLMs).",
        )
        STRIP_AUDIO: bool = Field(
            default=True,
            description="Also remove audio parts alongside images/videos. "
            "Set to False to keep audio in context (e.g. if the model "
            "supports audio natively).",
        )
        PRIORITY: int = Field(
            default=0,
            description="Filter priority (lower = runs first)",
        )

    def __init__(self):
        self.valves = self.Valves()
        self.toggle = False
        self.icon = "🧩"

    # ── helpers ───────────────────────────────────────────────────────────

    def _should_strip(self, model: Optional[dict]) -> bool:
        """True when media should be stripped for *model*.

        Models listed in ``STRIP_MODELS`` (text-only LLMs) get media
        stripped. Models NOT in the list (vision LLMs) keep media. Empty
        ``STRIP_MODELS`` = strip for ALL models (safe default).
        """
        raw = self.valves.STRIP_MODELS.strip()
        if not raw:
            return True  # safe default: strip for all
        listed = {s.strip().lower() for s in raw.split(",") if s.strip()}
        if not listed:
            return True
        if model is None:
            return True  # can't identify model → safe default
        model_id = (model.get("id") or "").lower()
        model_name = (model.get("name") or "").lower()
        return any(s in model_id or s in model_name for s in listed)

    # ══════════════════════════════════════════════════════════════════════
    #  inlet  —  called BEFORE the LLM request
    # ══════════════════════════════════════════════════════════════════════

    async def inlet(
        self,
        body: dict,
        __model__: Optional[dict] = None,
        **kwargs,
    ) -> dict:
        """Strip image/video (and optionally audio) parts from the messages
        for models that cannot handle them. No other processing occurs."""
        messages: list[dict] = body.get("messages", [])
        if not messages:
            return body

        should_strip = self._should_strip(__model__)
        if not should_strip:
            return body  # vision model → leave media in place

        strip_audio = self.valves.STRIP_AUDIO
        removed = 0
        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            has_media = any(_is_media_type(p, strip_audio) for p in content)
            if has_media:
                remaining = _extract_user_text(content)
                msg["content"] = remaining if remaining else ""
                removed += 1

        if removed:
            logger.info("Stripped media from %d user message(s)", removed)
        return body