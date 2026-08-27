"""
title: Multimodal RAG Bridge (no memory)
author: HPE Multimodal RAG
description: >
  Handles unsupported modalities (images, video, audio) by handing them
  off to the Multimodal RAG MCP tool instead of injecting raw media into
  the LLM context window. The filter uploads each file to the RAG
  API's staging endpoint and injects only a short ``file://`` URL hint
  plus the live list of available datasets, so the LLM can call the
  ``search_dataset`` MCP tool itself with no base64 data in its context
  and choose whichever dataset is most relevant. When the RAG MCP is
  not enabled (``DEFER_TO_MCP = false``) the filter warns the user and
  strips the unsupported media type instead. For vision-capable LLMs
  (NOT listed in ``STRIP_MODELS``), media stays in the LLM context
  while also being staged for the MCP tool. Text uploads are still
  read inline and injected into the context.

  This variant drops the long-term memory features (recall + distillation)
  found in the full "Multimodal RAG Bridge" function — every other part of
  the media routing pipeline is identical.
required_open_webui_version: 0.3.0
version: 0.7.0
licence: MIT
"""

import base64
import logging
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ── Module-level flag ──────────────────────────────────────────────────────
# Take full control of file processing so we can route multimodal files
# through the RAG system and prevent the built-in text-only RAG from
# trying to process images/video/audio as text.
file_handler = True

# ── Text-based MIME types (can be read inline without special libs) ────────
_TEXT_MIMES: frozenset = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/html",
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        # Code
        "text/x-python",
        "text/x-javascript",
        "text/x-typescript",
        "text/x-java",
        "text/x-c",
        "text/x-c++",
        "text/x-go",
        "text/x-ruby",
        "text/x-rust",
        "text/x-sh",
        "text/x-bash",
        "text/x-script.python",
        # Logs
        "text/x-log",
        "application/x-log",
    }
)


def _is_text_mime(mime: str) -> bool:
    if mime in _TEXT_MIMES:
        return True
    if mime.startswith("text/"):
        return True
    return False


# ── Known media MIME-type to modality mapping ──────────────────────────────
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
    m = mime.lower()
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


# ═══════════════════════════════════════════════════════════════════════════
# Filter
# ═══════════════════════════════════════════════════════════════════════════


class Filter:

    class Valves(BaseModel):
        # -- RAG API connection --------------------------------------------
        RAG_API_URL: str = Field(
            default="http://rag-mcp-server-api.mm-rag-mcp.svc.cluster.local",
            description="Base URL of the Multimodal RAG API server " "(used for the staging endpoint)",
        )
        DATASET_NAME: str = Field(
            default="default",
            description="Fallback dataset injected into the hint when the "
            "filter cannot fetch the live dataset list from the RAG API; "
            "otherwise the hint includes the full list and the LLM picks "
            "the most relevant one",
        )
        # -- Modality routing toggles --------------------------------------
        ROUTE_IMAGES: bool = Field(
            default=True,
            description="Hand images off to the RAG MCP tool instead "
            "of the LLM (False = leave for the LLM if it supports them)",
        )
        ROUTE_VIDEO: bool = Field(
            default=True,
            description="Hand video off to the RAG MCP tool instead "
            "of the LLM (False = leave for the LLM if it supports it)",
        )
        ROUTE_AUDIO: bool = Field(
            default=True,
            description="Hand audio off to the RAG MCP tool instead "
            "of the LLM (False = leave for the LLM if it supports it)",
        )
        # -- Per-model strip vs keep ---------------------------------------
        STRIP_MODELS: str = Field(
            default="",
            description="Comma-separated substrings matched against the "
            "model ID or name (case-insensitive). Media is stripped from "
            "the LLM context when any substring matches (e.g. 'deepseek' "
            "matches 'deepseek-ai/DeepSeek-V4-Flash' and its clones). "
            "Models with NO match keep media in context (vision models "
            "like Gemma) while also staging it for the MCP tool. "
            "Empty = strip for ALL models (safe default for text-only LLMs).",
        )
        # -- MCP deferral --------------------------------------------------
        DEFER_TO_MCP: bool = Field(
            default=True,
            description="When True, stage media on the RAG API and inject "
            "a URL hint so the LLM can call the search_dataset MCP tool "
            "itself. For text-only models (in STRIP_MODELS) the media is "
            "stripped from context; for vision models it stays. When "
            "False, no staging — text-only models warn+drop, vision "
            "models pass media through natively.",
        )
        STAGING_PATH: str = Field(
            default="/api/staging",
            description="Path on the RAG API server used to stage " "uploaded media for the MCP tool",
        )
        MCP_TOOL_HINT: str = Field(
            default=(
                "The user attached media. The following MCP tools are "
                "available — choose the right one based on what the user "
                "is asking for:\n\n"
                "1. `describe_media` — Call this when the user wants you "
                "to DESCRIBE or ANALYSE the uploaded media itself (e.g. "
                '"what\'s in this image?", "describe this video"). Pass '
                "the media URL from the list below as `media_url`. No "
                "dataset needed. Returns a VLM description + markdown.\n\n"
                "2. `transcribe_audio` — Call this when the user wants a "
                "TRANSCRIPTION of uploaded audio (e.g. \"what's said in "
                'this recording?"). Pass the audio URL as `audio_url`. '
                "No dataset needed. Returns the transcript text.\n\n"
                "3. `search_dataset` — Call this when the user wants to "
                'FIND SIMILAR content in a dataset (e.g. "find images '
                'like this", "search for related documents"). Pass the '
                "dataset name and the matching media URL from the list "
                "below. The tool result's `context` field contains "
                "ready-to-paste markdown — include it VERBATIM:\n"
                "  - Images: `![alt](url)` markdown image links\n"
                "  - Audio: `<audio controls src=url></audio>` HTML5 "
                "players (with a markdown link fallback)\n"
                "  - Documents: `[label](url)` clickable links\n"
                "Do NOT replace these with plain text links, and do NOT "
                "omit them. For video matches, present the URL from the "
                "tool result's `video` field as a clickable link.\n\n"
                "CRITICAL — URL USAGE:\n"
                "- You MUST use the EXACT URLs listed in the 'Staged "
                "media' block below. These are the only valid media URLs.\n"
                "- Do NOT construct your own URLs, prepend `file://` to "
                "file IDs, or use any other URL you may have seen.\n"
                "- The staged URLs begin with `file:///data/staging/` "
                "and include the actual filename.\n"
                "- Do NOT fetch these URLs yourself — they are only "
                "resolvable by the MCP tools.\n\n"
                "Do NOT call `get_dataset_files` to list all files — "
                "datasets can contain tens of thousands of files and "
                "listing them wastes context. Use `search_dataset` to "
                "find relevant content; only use `get_dataset_files` "
                "with a specific `file_path` to read an individual file."
            ),
            description="Header prepended to the staged-media hint " "injected into the LLM context",
        )
        # -- Context injection ---------------------------------------------
        INJECT_AS_SYSTEM: bool = Field(
            default=True,
            description="Inject context as a system message " "(False = prepend to user message)",
        )
        MAX_CONTEXT_CHARS: int = Field(
            default=4000,
            ge=100,
            le=64000,
            description="Max characters of injected context " "(hint + text file contents)",
        )
        CONTEXT_HEADER: str = Field(
            default="Context accompanying the user's uploaded file(s):",
            description="Header before injected context " "(hint block, text file contents, file references)",
        )
        # -- Advanced ------------------------------------------------------
        PRIORITY: int = Field(
            default=0,
            description="Filter priority (lower = runs first)",
        )

    def __init__(self):
        self.valves = self.Valves()
        self.toggle = False
        self.icon = "🧩"

    # ── helpers ───────────────────────────────────────────────────────────

    def _is_routable(self, modality: str) -> bool:
        return {
            "image": self.valves.ROUTE_IMAGES,
            "video": self.valves.ROUTE_VIDEO,
            "audio": self.valves.ROUTE_AUDIO,
        }.get(modality, False)

    def _should_strip(self, model: Optional[dict]) -> bool:
        """Return True if media should be stripped from the LLM context.

        Models listed in ``STRIP_MODELS`` (text-only LLMs) get media
        stripped.  Models NOT in the list (vision LLMs) keep media in
        context while also staging it for the MCP tool.

        Empty ``STRIP_MODELS`` = strip for ALL models (backward-
        compatible safe default for text-only LLMs).
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
        # Substring match: 'deepseek' matches 'deepseek-ai/deepseek-v4-flash'
        # and any clones (e.g. 'deepseek-ai/deepseek-v4-flash:clone').
        return any(s in model_id or s in model_name for s in listed)

    @staticmethod
    def _bytes_to_data_url(data: bytes, mime: str) -> str:
        b64 = base64.b64encode(data).decode("utf-8")
        return f"data:{mime};base64,{b64}"

    @staticmethod
    async def _emit_status(
        emitter: Optional[callable],
        description: str,
        status: str = "in_progress",
        done: bool = False,
    ):
        if emitter is None:
            return
        try:
            await emitter(
                event_type="status" if not done else "meta",
                data={"description": description, "status": status, "done": done},
            )
        except Exception:
            logger.debug("_emit_status failed", exc_info=True)

    # ── file content fetching ─────────────────────────────────────────────

    @staticmethod
    async def _fetch_file_bytes(file_ref: dict, request) -> Optional[bytes]:
        """Fetch a file's raw bytes from Open WebUI's internal file storage.

        Returns ``None`` when the request object is unavailable, the URL is
        missing, or the fetch fails (logged as warning).
        """
        if request is None:
            logger.warning("No __request__ available — cannot fetch file bytes")
            return None
        url = file_ref.get("url", "")
        if not url:
            return None
        try:
            base = str(request.base_url).rstrip("/")
        except Exception:
            logger.warning("Could not extract base_url from request")
            return None
        full_url = f"{base}{url}" if url.startswith("/") else url
        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                resp = await client.get(full_url)
                resp.raise_for_status()
                return resp.content
        except Exception:
            logger.warning("Failed to fetch file from %s", full_url, exc_info=True)
            return None

    @staticmethod
    def _try_decode_text(data: bytes) -> Optional[str]:
        for enc in ("utf-8", "latin-1", "cp1252"):
            try:
                return data.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return None

    # ── media handoff (staging → MCP tool) ────────────────────────────────

    _EXT_FOR_MIME: dict[str, str] = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "image/svg+xml": ".svg",
        "image/tiff": ".tiff",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "video/quicktime": ".mov",
        "video/x-matroska": ".mkv",
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/wav": ".wav",
        "audio/flac": ".flac",
        "audio/ogg": ".ogg",
        "audio/aac": ".aac",
    }

    @staticmethod
    def _data_url_to_bytes(url: str) -> Optional[bytes]:
        """Decode a ``data:<mime>;base64,..`` URL into raw bytes."""
        if not url.startswith("data:"):
            return None
        try:
            _, b64 = url.split(",", 1)
            return base64.b64decode(b64)
        except Exception:
            logger.debug("Failed to decode data URL", exc_info=True)
            return None

    @classmethod
    def _ext_for_mime(cls, mime: str) -> str:
        return cls._EXT_FOR_MIME.get((mime or "").lower(), ".bin")

    async def _upload_to_staging(
        self,
        data: bytes,
        mime: str,
        filename: str,
    ) -> Optional[str]:
        """Upload media bytes to the RAG API staging endpoint.

        Returns a short ``file://`` (or ``http://``) URL the LLM can pass
        to the ``search_dataset`` MCP tool, or ``None`` on failure.
        """
        api = self.valves.RAG_API_URL.rstrip("/")
        url = f"{api}{self.valves.STAGING_PATH}"
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                files = {"file": (filename, data, mime or "application/octet-stream")}
                resp = await client.post(url, files=files)
                resp.raise_for_status()
                payload = resp.json()
                # Prefer the file:// URL (MCP shares the PVC); fall back
                # to the HTTP URL for non-colocated MCP deployments.
                return payload.get("file_url") or payload.get("http_url")
        except Exception:
            logger.warning("Staging upload failed for %s", filename, exc_info=True)
            return None

    async def _list_datasets(self) -> Optional[list[dict]]:
        """Fetch the list of datasets from the RAG API.

        Returns a list of metadata dicts (name, description,
        document_count, has_password, unlocked) or ``None`` on failure.
        Used to inform the LLM which datasets it can pass to
        ``search_dataset`` so the filter does not hardcode one.
        """
        api = self.valves.RAG_API_URL.rstrip("/")
        url = f"{api}/api/datasets"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.json().get("datasets")
        except Exception:
            logger.warning("Failed to list datasets from %s", url, exc_info=True)
            return None

    def _build_mcp_hint(
        self,
        hints: list[dict],
        datasets: Optional[list[dict]] = None,
        should_strip: bool = True,
    ) -> str:
        """Build the short text hint injected so the LLM can call the
        ``search_dataset`` MCP tool — no base64, just URLs + dataset names.
        """
        by_mod: dict[str, list[dict]] = {}
        for h in hints:
            by_mod.setdefault(h["modality"], []).append(h)
        lines: list[str] = []
        for mod, items in by_mod.items():
            lines.append(f"{mod} ({len(items)}):")
            for it in items:
                lines.append(f"  - {it['url']}  [{it['filename']}]")
        media_block = "\n".join(lines)

        # Dataset block — prefer the live dataset list from the API so
        # the LLM can pick the most relevant one (or all, via multiple
        # calls).  Fall back to the configured DATASET_NAME valve when
        # the list call failed.
        if datasets:
            ds_lines: list[str] = []
            for ds in datasets:
                name = ds.get("name", "?")
                count = ds.get("document_count", 0)
                desc = ds.get("description", "")
                locked = " [password-protected]" if ds.get("has_password") else ""
                unlocked = " [unlocked]" if ds.get("unlocked") else ""
                desc_str = f" — {desc}" if desc else ""
                ds_lines.append(f"  - {name} ({count} docs{locked}{unlocked}){desc_str}")
            dataset_block = (
                "Available datasets (pick the most relevant; call "
                "`unlock_dataset` first for password-protected ones):\n" + "\n".join(ds_lines)
            )
        else:
            dataset_block = f"Dataset: {self.valves.DATASET_NAME}"

        # Tell the LLM its own modalities so the MCP tool converts
        # result images/video/audio to text descriptions via VLM/ASR.
        # Text-only models get ["text"] (full conversion); vision models
        # get ["text","image"] (images kept as URLs).
        if should_strip:
            modalities_block = (
                'base_llm_modalities: ["text"]\n'
                '(IMPORTANT: always pass base_llm_modalities=["text"] '
                "to search_dataset. You are a TEXT-ONLY model — the tool "
                "will use a VLM to describe result images as text so you "
                'can understand them. Never pass ["text","image"].)'
            )
        else:
            modalities_block = (
                'base_llm_modalities: ["text", "image"]\n'
                "(You natively support images, so result images will be "
                'returned as viewable URLs. Pass ["text","image"] to '
                "search_dataset.)"
            )

        return (
            f"{self.valves.MCP_TOOL_HINT}\n\n"
            f"{dataset_block}\n\n"
            f"{modalities_block}\n\n"
            f"Staged media (use these EXACT URLs — do NOT modify them "
            f"or construct your own) — pass the relevant URL to the "
            f"`media_url` / `audio_url` parameter of `describe_media` / "
            f"`transcribe_audio`, or the matching `image` / `video` / "
            f"`audio` parameter of `search_dataset`:\n"
            f"{media_block}"
        )

    # ── inline media detection / stripping ────────────────────────────────

    @staticmethod
    def _extract_inline_media(content: Any) -> list[dict]:
        if not isinstance(content, list):
            return []
        found: list[dict] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            t = part.get("type", "")
            if t == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if not url:
                    continue
                mime = _mime_from_data_url(url) or "image/png"
                mod = _modality_from_mime(mime)
                found.append({"modality": mod, "url": url, "mime": mime, "part": part})
            elif t in ("video_url", "audio_url"):
                key = t.replace("_url", "")
                url = (part.get(f"{key}_url") or {}).get("url", "")
                if not url:
                    continue
                mime = _mime_from_data_url(url) or f"{key}/mp4"
                mod = _modality_from_mime(mime)
                found.append({"modality": mod, "url": url, "mime": mime, "part": part})
        return found

    @staticmethod
    def _extract_user_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            return "\n".join(texts).strip()
        return str(content) if content else ""

    # ══════════════════════════════════════════════════════════════════════
    #  inlet  —  called BEFORE the LLM request
    # ══════════════════════════════════════════════════════════════════════

    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __model__: Optional[dict] = None,
        __files__: Optional[list] = None,
        __event_emitter__: Optional[callable] = None,
        __request__: Optional[Any] = None,
        **kwargs,
    ) -> dict:
        await self._emit_status(
            __event_emitter__,
            "Multimodal RAG Bridge: scanning for files…",
        )

        messages: list[dict] = body.get("messages", [])
        if not messages:
            return body

        last_msg = messages[-1]
        if last_msg.get("role") != "user":
            return body

        content: Any = last_msg.get("content", "")
        user_text = self._extract_user_text(content)

        # Determine early whether media should be stripped for this model.
        # This must be known before the early return below so that
        # historical user messages — whose re-injected media would crash
        # text-only LLMs on follow-up turns — can be cleaned even when the
        # *current* message carries no new media of its own.
        should_strip = self._should_strip(__model__)

        # ── 1. Collect ALL file references ────────────────────────────────
        # Sources (in priority order): __files__, body["files"],
        # body["metadata"]["files"]
        file_refs: list[dict] = []
        if __files__ is not None:
            file_refs = list(__files__)
        if not file_refs:
            file_refs = body.get("files") or []
        if not file_refs:
            file_refs = body.get("metadata", {}).get("files") or []
        # Inline media from message content (pasted images, etc.)
        inline_media = self._extract_inline_media(content)

        # ── 1b. Strip media from historical user messages ───────────────
        # Open WebUI stores uploaded files on user messages in its DB and
        # re-injects them as image_url / video_url / audio_url parts on
        # every follow-up turn.  For text-only models this causes "not a
        # multimodal model" errors on the second (and later) message of a
        # conversation — even when the current message has no media of its
        # own.  Strip these parts from ALL user messages before the early
        # return so a media-less follow-up turn cannot bypass cleanup.
        if should_strip:
            for msg in messages:
                if msg.get("role") != "user":
                    continue
                c = msg.get("content")
                if not isinstance(c, list):
                    continue
                has_media = any(
                    isinstance(p, dict) and p.get("type") in ("image_url", "video_url", "audio_url") for p in c
                )
                if has_media:
                    msg["content"] = self._extract_user_text(c) or ""

        has_any_media = bool(file_refs) or bool(inline_media)
        if not has_any_media:
            return body  # nothing more to process

        # ── 2. Categorise files: media ↔ text ↔ other ────────────────────
        media_for_handoff: list[dict] = []  # media to stage for the MCP tool
        reinject_parts: list[dict] = []  # image_url parts for vision LLMs
        text_parts: list[str] = []  # text content to inject directly
        other_files: list[str] = []  # non-text, non-media (name only)

        # Process file attachments
        for fref in file_refs:
            fname = fref.get("filename", "file")
            mime = fref.get("type", "") or "application/octet-stream"
            modality = _modality_from_mime(mime)

            if modality and self._is_routable(modality):
                # Media file → stage for the MCP tool (if enabled) and/or
                # re-inject for vision LLMs (if not stripping)
                data = await self._fetch_file_bytes(fref, __request__)
                if data:
                    media_for_handoff.append(
                        {
                            "modality": modality,
                            "data": data,
                            "mime": mime,
                            "filename": fname,
                        }
                    )
                    if not should_strip:
                        # Vision LLM: also keep the image in context
                        reinject_parts.append(
                            {
                                "type": f"{modality}_url",
                                f"{modality}_url": {"url": self._bytes_to_data_url(data, mime)},
                            }
                        )
                else:
                    other_files.append(fname)
            elif _is_text_mime(mime):
                # Text file → read and include directly
                data = await self._fetch_file_bytes(fref, __request__)
                if data:
                    decoded = self._try_decode_text(data)
                    if decoded:
                        text_parts.append(f"[File: {fname}]\n{decoded}")
                    else:
                        other_files.append(fname)
                else:
                    other_files.append(fname)
            else:
                other_files.append(fname)

        # Process inline media (image_url / video_url / audio_url) that are
        # routable. Non-routable inline media is left in place so a
        # vision-capable LLM can consume it natively (ROUTE_*=False).
        # When NOT stripping (vision LLM), inline media stays in the
        # content — we just don't strip it in step 4.
        for item in inline_media:
            mod = item.get("modality")
            if mod and self._is_routable(mod):
                data = self._data_url_to_bytes(item.get("url", ""))
                if data:
                    mime = item.get("mime") or f"{mod}/octet-stream"
                    fname = f"pasted-{mod}{self._ext_for_mime(mime)}"
                    media_for_handoff.append(
                        {
                            "modality": mod,
                            "data": data,
                            "mime": mime,
                            "filename": fname,
                        }
                    )

        # ── 3. Build context from all sources ─────────────────────────────
        context_sections: list[str] = []
        staged_hints: list[dict] = []
        dropped_media = 0

        # 3a. Media handoff
        if media_for_handoff:
            if self.valves.DEFER_TO_MCP:
                await self._emit_status(
                    __event_emitter__,
                    f"Staging {len(media_for_handoff)} media file(s) for the search_dataset MCP tool…",
                )
                for item in media_for_handoff:
                    url = await self._upload_to_staging(
                        item["data"],
                        item["mime"],
                        item["filename"],
                    )
                    if url:
                        staged_hints.append(
                            {
                                "modality": item["modality"],
                                "url": url,
                                "filename": item["filename"],
                            }
                        )
                    else:
                        dropped_media += 1
                        other_files.append(item["filename"])
                if staged_hints:
                    # Fetch the live dataset list so the LLM can choose
                    # which dataset(s) to search (falls back to the
                    # configured DATASET_NAME valve if the call fails).
                    datasets = await self._list_datasets()
                    context_sections.append(
                        self._build_mcp_hint(staged_hints, datasets=datasets, should_strip=should_strip)
                    )
            else:
                # DEFER_TO_MCP off.
                if should_strip:
                    # Text-only LLM + no MCP → warn + skip media
                    dropped_media = len(media_for_handoff)
                    other_files.extend(it["filename"] for it in media_for_handoff)
                # else: vision LLM + no MCP → media stays in context
                # via reinject_parts / inline media. No staging.

        # 3b. Direct text file content
        if text_parts:
            context_sections.append("Uploaded file contents:\n\n" + "\n\n---\n\n".join(text_parts))

        # 3c. Other file references
        if other_files:
            context_sections.append("Uploaded files (not readable as text): " + ", ".join(other_files))

        # ── 4. Manage inline media in message content ────────────────────
        if should_strip:
            # Text-only LLM: remove image_url/video_url/audio_url parts so
            # no base64 reaches the LLM. Non-routable inline media
            # (ROUTE_*=False) is preserved.
            parts_to_remove = [
                m["part"] for m in inline_media if m.get("part") is not None and self._is_routable(m.get("modality"))
            ]
            if parts_to_remove and isinstance(content, list):
                remaining_text = self._extract_user_text(content)
                last_msg["content"] = remaining_text or user_text
        elif reinject_parts:
            # Vision LLM: add file-attachment media back into the content
            # (file_handler=True prevents OWUI from injecting them).
            # Inline media (pasted images) is already in the content and
            # was not stripped, so it stays untouched.
            if isinstance(last_msg["content"], str):
                last_msg["content"] = [{"type": "text", "text": last_msg["content"]}] if last_msg["content"] else []
            if not isinstance(last_msg["content"], list):
                last_msg["content"] = []
            last_msg["content"].extend(reinject_parts)

        # ── 4b. Historical user-message media is stripped in step 1b ──────
        # (above), before the media-less early return, so that follow-up
        # text turns in an existing conversation don't leak prior media
        # into text-only LLMs.

        # ── 5. Inject combined context ────────────────────────────────────
        if context_sections:
            combined = "\n\n".join(context_sections)
            if len(combined) > self.valves.MAX_CONTEXT_CHARS:
                combined = combined[: self.valves.MAX_CONTEXT_CHARS] + "\n\n[Context truncated...]"

            full_context = f"{self.valves.CONTEXT_HEADER}\n\n{combined}"

            if self.valves.INJECT_AS_SYSTEM:
                messages.insert(0, {"role": "system", "content": full_context})
            elif isinstance(last_msg["content"], list):
                # Content has media parts (vision LLM) — prepend text
                # instead of overwriting and destroying them.
                last_msg["content"].insert(0, {"type": "text", "text": full_context})
            else:
                last_msg["content"] = f"{full_context}\n\n{user_text}"

            summary_bits: list[str] = []
            if staged_hints:
                summary_bits.append(f"{len(staged_hints)} media staged for MCP")
            if reinject_parts:
                summary_bits.append(f"{len(reinject_parts)} media kept in context")
            if text_parts:
                summary_bits.append(f"{len(text_parts)} text files")
            if dropped_media:
                summary_bits.append(f"{dropped_media} media dropped")
            summary = ", ".join(summary_bits)
            await self._emit_status(
                __event_emitter__,
                f"Injected context ({summary})" if summary else "Injected context",
                status="success" if not dropped_media else "warning",
                done=True,
            )
        else:
            if dropped_media:
                await self._emit_status(
                    __event_emitter__,
                    f"{dropped_media} media file(s) skipped — "
                    "DEFER_TO_MCP is off or staging failed. "
                    "Enable the RAG MCP server and set DEFER_TO_MCP=true.",
                    status="warning",
                    done=True,
                )
            elif reinject_parts:
                await self._emit_status(
                    __event_emitter__,
                    f"{len(reinject_parts)} media file(s) kept in context " "(vision LLM, no MCP staging)",
                    status="success",
                    done=True,
                )
            else:
                await self._emit_status(
                    __event_emitter__,
                    "No context to inject (files processed but no content extracted)",
                    status="warning",
                    done=True,
                )

        # file_handler = True → Open WebUI strips body["files"] after
        # inlet() returns. No manual cleanup needed.

        return body
