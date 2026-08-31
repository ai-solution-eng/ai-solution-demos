"""
title: Multimodal RAG Bridge
author: HPE Multimodal RAG
description: >
  Handles unsupported modalities (images, video, audio) by handing them
  off to the Multimodal RAG MCP tool instead of injecting raw media into
  the LLM context window. The filter uploads each media file to the RAG
  API's staging endpoint and injects only a short ``file://`` URL hint
  plus the live list of available datasets, so the LLM can call the
  ``search_dataset`` MCP tool itself with no base64 data in its context
  and choose whichever dataset is most relevant. When the RAG MCP is
  not enabled (``DEFER_TO_MCP = false``) the filter warns the user and
  strips the unsupported modality instead. For vision-capable LLMs
  (NOT listed in ``STRIP_MODELS``), media stays in the LLM context
  while also being staged for the MCP tool. Text uploads are still
  read inline and injected directly.
required_open_webui_version: 0.3.0
version: 0.6.3
licence: MIT
"""

import base64
import hashlib
import hmac
import logging
import re
from datetime import datetime, timezone
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
        # -- Long-term memory (recall at conversation start) -----------------
        MEMORY_ENABLED: bool = Field(
            default=True,
            description="Enable long-term memory: recall relevant memories at "
            "conversation start (inlet) and distil/store memories after each "
            "turn (outlet). Per-user: the dataset name is derived from the "
            "OWUI user id (see MEMORY_DATASET_PREFIX).",
        )
        MEMORY_DATASET_PREFIX: str = Field(
            default="owui-memory-",
            description="Per-user memory datasets are named "
            "'{PREFIX}{user_id}'. The filter builds this at runtime from the "
            "OWUI __user__ identity, so each user gets an isolated dataset "
            "without per-user valve config. The admin pre-creates each "
            "user's dataset with the shared MEMORY_PASSWORD (or set "
            "MEMORY_AUTO_CREATE=true to create on first write).",
        )
        MEMORY_AUTO_CREATE: bool = Field(
            default=False,
            description="If true, the filter creates a user's memory dataset "
            "(with MEMORY_PASSWORD) on first write when it doesn't exist. "
            "Convenient; needs the shared password to also satisfy any "
            "dataset-name policy on the server.",
        )
        MEMORY_PASSWORD: str = Field(
            default="",
            description="Shared password for ALL per-user memory datasets "
            "(every 'owui-memory-{user_id}' uses this same password). Stored "
            "in the filter Valves (server-side) — never injected into the LLM "
            "context. Used as the FALLBACK when MEMORY_SECRET is empty. "
            "Per-user isolation is dataset-NAME based, not crypto: "
            "if this password leaks, all users' memories are exposed.",
        )
        MEMORY_SECRET: str = Field(
            default="",
            description="Server-side HMAC key for deriving a UNIQUE per-user "
            "memory password from the SSO-authenticated __user__ id. When set, "
            "each user's dataset password = HMAC-SHA256(MEMORY_SECRET, user_id) "
            "(base64url, 24 chars) — unpredictable per user, so a single leak "
            "exposes only that user. Leverages the fact that OWUI's __user__ "
            "identity is trusted post-SSO. Leave empty to fall back to the "
            "shared MEMORY_PASSWORD (weaker, name-based isolation only).",
        )
        MEMORY_RECALL_TOP_K: int = Field(
            default=5,
            ge=1,
            le=20,
            description="Number of memories to recall and inject at conversation " "start.",
        )
        MEMORY_RECALL_FIRST_ONLY: bool = Field(
            default=True,
            description="Recall only on the first user message of each chat. "
            "False = recall on every turn (more context, more tokens).",
        )
        MEMORY_INJECT_AS_SYSTEM: bool = Field(
            default=True,
            description="Inject recalled memories as a system message (True) "
            "or prepend to the user message (False).",
        )
        # -- Distillation LLM (for LLM-curated memory writes) ---------------
        DISTILL_LLM_URL: str = Field(
            default="",
            description="OpenAI-compatible base URL for the distillation LLM "
            "(e.g. https://vllm.example.com/v1). When empty, no memories are "
            "written (recall still works if MEMORY_DATASET_PREFIX is set).",
        )
        DISTILL_LLM_MODEL: str = Field(
            default="",
            description="Model name for distillation (e.g. 'deepseek-v4-flash').",
        )
        DISTILL_LLM_API_KEY: str = Field(
            default="",
            description="API key for the distillation LLM endpoint.",
        )
        DISTILL_MIN_REPLY_CHARS: int = Field(
            default=200,
            ge=0,
            description="Skip distillation for assistant replies shorter than "
            "this (trivial exchanges aren't worth storing). 0 = distil all.",
        )
        # -- SQL lessons (self-improving SQL agent loop) ---------------------
        SQL_LESSONS_ENABLED: bool = Field(
            default=False,
            description="Enable the SQL-lesson loop: at inlet, recall the top-k "
            "curated lessons from the SQL_LESSONS_DATASET dataset and inject "
            "them as 'follow if applicable' context before the agent writes "
            "SQL. This is the recall half of resolve -> distill -> store -> "
            "promote -> recall.",
        )
        SQL_LESSONS_DATASET: str = Field(
            default="sql-lessons",
            description="Curated SQL-lesson dataset the agent recalls from. "
            "Written ONLY by the promotion gate (candidate -> curated). The "
            "separate 'sql-lessons-candidates' dataset holds unvalidated "
            "distilled lessons and is never read directly.",
        )
        SQL_LESSONS_PASSWORD: str = Field(
            default="",
            description="Password for the SQL-lesson datasets (shared, not "
            "per-user). Sent as X-Dataset-Password to the RAG API; never "
            "injected into the LLM context.",
        )
        SQL_LESSONS_RECALL_TOP_K: int = Field(
            default=3,
            ge=1,
            le=10,
            description="Number of curated SQL lessons to recall and inject "
            "at inlet.",
        )
        SQL_LESSONS_INJECT_AS_SYSTEM: bool = Field(
            default=True,
            description="Inject recalled SQL lessons as a system message "
            "(True) or prepend to the user message (False).",
        )
        SQL_LESSONS_DISTILL_ENABLED: bool = Field(
            default=False,
            description="Enable the write half of the SQL-lesson loop: after "
            "each turn, ask the distillation LLM to extract 0-3 candidate "
            "lessons from the exchange and store them in "
            "SQL_LESSONS_CANDIDATES_DATASET (NEVER the curated set). The "
            "prompt only derives lessons from positive evidence (SQL ran and "
            "the result was accepted/plausible, or a concrete fix for a "
            "concrete error). Requires DISTILL_LLM_URL/MODEL. Promotion "
            "candidate -> curated is a separate manual/gated step.",
        )
        SQL_LESSONS_CANDIDATES_DATASET: str = Field(
            default="sql-lessons-candidates",
            description="Write-only staging dataset for distilled SQL lesson "
            "candidates. The agent NEVER reads this directly — the promotion "
            "gate reviews candidates here before promoting to the curated "
            "sql-lessons dataset.",
        )
        SQL_LESSONS_DISTILL_MIN_REPLY_CHARS: int = Field(
            default=200,
            ge=0,
            description="Skip SQL-lesson distillation for assistant replies "
            "shorter than this (trivial exchanges aren't worth lessons).",
        )
        # -- Media URL repair -----------------------------------------------
        REPAIR_MEDIA_URLS: bool = Field(
            default=True,
            description="Repair garbled media URLs in the model's reply: "
            "match every ``/api/datasets/{name}/files/{file}`` URL the model "
            "printed against the exact URLs returned by the RAG MCP tool "
            "results, and substitute the correct host/token.  LLMs often "
            "mistype long signed URLs; this restores them deterministically.",
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

    # ── long-term memory helpers ─────────────────────────────────────────

    def _memory_enabled(self) -> bool:
        return bool(self.valves.MEMORY_ENABLED and self.valves.MEMORY_DATASET_PREFIX)

    def _memory_password_for_user(self, user: Optional[dict]) -> str:
        """Return the memory-dataset password for *user*.

        - When ``MEMORY_SECRET`` is set: HMAC-SHA256(MEMORY_SECRET, user_id)
          → base64url, 24 chars.  Unique and unpredictable per SSO-authenticated
          user, so one user's password leaking cannot expose another's.
        - Otherwise: the shared ``MEMORY_PASSWORD`` valve (name-based isolation
          only).  May be empty ( callers must handle that — the RAG API then
          treats the dataset as unprotected).
        """
        secret = self.valves.MEMORY_SECRET
        if secret:
            uid = self._user_identifier(user) or ""
            digest = hmac.new(secret.encode("utf-8"), uid.encode("utf-8"), hashlib.sha256).digest()
            # 18 bytes → 24 base64url chars, no padding.  Plenty of entropy,
            # URL-safe, safe in an HTTP header value.
            return base64.urlsafe_b64encode(digest[:18]).decode("ascii").rstrip("=")
        return self.valves.MEMORY_PASSWORD

    def _memory_headers(self, user: Optional[dict]) -> dict[str, str]:
        pw = self._memory_password_for_user(user)
        return {"X-Dataset-Password": pw} if pw else {}

    @staticmethod
    def _user_identifier(user: Optional[dict]) -> Optional[str]:
        """Extract a stable, sanitised identifier from the OWUI __user__ dict.

        Preference: ``id`` → ``email`` → ``name``.  The value is lowercased
        and reduced to ``[a-z0-9_-]`` so it is safe to embed in a dataset
        name.  Returns ``None`` when no usable identity is present (the
        caller skips memory in that case).
        """
        if not isinstance(user, dict):
            return None
        raw = user.get("id") or user.get("email") or user.get("name") or ""
        raw = str(raw).strip().lower()
        if not raw:
            return None
        import re

        sanitised = re.sub(r"[^a-z0-9_-]+", "-", raw).strip("-")
        return sanitised or None

    def _memory_dataset_for_user(self, user: Optional[dict]) -> Optional[str]:
        """Return the per-user memory dataset name, or None if unidentified."""
        uid = self._user_identifier(user)
        if not uid:
            return None
        prefix = self.valves.MEMORY_DATASET_PREFIX
        # AWS-style trailing hyphen is ugly; collapse any doubled hyphens.
        import re

        name = re.sub(r"-+", "-", f"{prefix}{uid}").strip("-")
        return name or None

    async def _ensure_dataset_exists(self, dataset_name: str, user: Optional[dict]) -> bool:
        """Create *dataset_name* (with the user's password) if missing.

        Returns True when the dataset exists (pre-existing or just created),
        False on failure.  Used by the outlet when MEMORY_AUTO_CREATE=true.
        """
        api = self.valves.RAG_API_URL.rstrip("/")
        user_pw = self._memory_password_for_user(user)
        # Check existence first to avoid creating duplicates / noisy logs.
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{api}/api/datasets/{dataset_name}",
                    headers=self._memory_headers(user),
                )
                if resp.status_code == 200:
                    return True
        except Exception:
            logger.debug("dataset existence check failed", exc_info=True)
        # Create it.
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{api}/api/datasets",
                    json={
                        "name": dataset_name,
                        "description": "Open WebUI long-term memory (auto-created)",
                        "password": user_pw or None,
                    },
                )
                if resp.status_code in (200, 201):
                    logger.info("Auto-created memory dataset '%s'", dataset_name)
                    return True
                # 409 Conflict = already exists (race) — also fine.
                if resp.status_code == 409:
                    return True
                logger.warning(
                    "Auto-create of '%s' failed: %s %s",
                    dataset_name,
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception:
            logger.warning("Auto-create of '%s' failed", dataset_name, exc_info=True)
        return False

    @staticmethod
    def _is_first_user_message(messages: list[dict]) -> bool:
        """True when the last message is the first user message in the chat."""
        user_count = sum(1 for m in messages if m.get("role") == "user")
        return user_count <= 1

    async def _recall_memory(self, dataset_name: str, user: Optional[dict], query: str) -> str:
        """Search the memory dataset and return formatted context (or '')."""
        if not query.strip():
            return ""
        api = self.valves.RAG_API_URL.rstrip("/")
        url = f"{api}/api/datasets/{dataset_name}/search"
        params = {"q": query[:500], "top_k": self.valves.MEMORY_RECALL_TOP_K}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, params=params, headers=self._memory_headers(user))
                resp.raise_for_status()
                results = resp.json().get("results", [])
        except Exception:
            logger.warning("Memory recall failed", exc_info=True)
            return ""
        if not results:
            return ""
        lines = ["Relevant memories from past conversations:"]
        for i, r in enumerate(results):
            content = r.get("content", "") if isinstance(r, dict) else str(r)
            score = r.get("score", 0) if isinstance(r, dict) else 0
            if content:
                lines.append(f"[Memory {i + 1}] (score: {score:.4f})\n{content}")
        context = "\n\n".join(lines)
        logger.info("Memory recall: %d hit(s) for query '%.40s…'", len(results), query)
        return context

    def _inject_memory_context(self, messages: list[dict], context: str) -> None:
        """Inject recalled memory context as a system or user-prefix message."""
        full = (
            "The following are relevant memories from this user's past "
            "conversations. Use them as background context — do not mention "
            "'memory' or 'recall' to the user unless they ask.\n\n" + context
        )
        if self.valves.MEMORY_INJECT_AS_SYSTEM:
            messages.insert(0, {"role": "system", "content": full})
        else:
            last = messages[-1]
            content = last.get("content", "")
            if isinstance(content, str):
                last["content"] = f"{full}\n\n{content}"
            elif isinstance(content, list):
                content.insert(0, {"type": "text", "text": full})

    # ── SQL lessons (self-improving SQL agent loop: recall half) ──────────
    # Recall the top-k curated lessons from the sql-lessons dataset at inlet
    # so the agent follows them before writing SQL. Storage/distillation is
    # handled separately (outlet + promotion gate), keeping this half read-only.

    async def _recall_sql_lessons(self, dataset_name: str, query: str) -> str:
        """Search the curated SQL-lesson dataset; return formatted context (or '')."""
        if not self.valves.SQL_LESSONS_ENABLED or not query.strip():
            return ""
        api = self.valves.RAG_API_URL.rstrip("/")
        url = f"{api}/api/datasets/{dataset_name}/search"
        params = {"q": query[:500], "top_k": self.valves.SQL_LESSONS_RECALL_TOP_K}
        headers = {"Content-Type": "application/json"}
        if self.valves.SQL_LESSONS_PASSWORD:
            headers["X-Dataset-Password"] = self.valves.SQL_LESSONS_PASSWORD
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, params=params, headers=headers)
                resp.raise_for_status()
                results = resp.json().get("results", [])
        except Exception:
            logger.warning("SQL-lesson recall failed for '%s'", dataset_name, exc_info=True)
            return ""
        if not results:
            return ""
        lines: list[str] = []
        for i, r in enumerate(results):
            content = r.get("text") or r.get("content") or ""
            if isinstance(content, dict):
                content = content.get("text", "")
            score = r.get("score", 0)
            kind = r.get("kind", "")
            if content:
                head = f"[SQL LESSON {i + 1}] (score: {score:.4f})"
                if kind:
                    head += f" — {kind}"
                lines.append(f"{head}\n{content}")
        if not lines:
            return ""
        context = "\n\n".join(lines)
        logger.info("SQL-lesson recall: %d hit(s) for '%.40s…'", len(lines), query)
        return context

    def _inject_sql_lessons(self, messages: list[dict], context: str) -> None:
        """Inject recalled SQL lessons as 'follow if applicable' context."""
        full = (
            "The following are SQL lessons from past, validated resolutions. "
            "Follow any that apply to the current question — they encode "
            "intent-to-schema mappings and performance guards. Do not mention "
            "'lessons' or 'recall' to the user unless they ask.\n\n" + context
        )
        if self.valves.SQL_LESSONS_INJECT_AS_SYSTEM:
            messages.insert(0, {"role": "system", "content": full})
        else:
            last = messages[-1]
            content = last.get("content", "")
            if isinstance(content, str):
                last["content"] = f"{full}\n\n{content}"
            elif isinstance(content, list):
                content.insert(0, {"type": "text", "text": full})

    # ── SQL lessons (self-improving SQL agent loop: write half) ───────────
    # Distill 0-3 candidate lessons from a resolved exchange and store them
    # in the CANDIDATES dataset (never the curated one). The distillation
    # prompt only emits lessons on positive evidence; the promotion gate
    # later moves candidates -> curated.

    _SQL_DISTILL_SYSTEM_PROMPT = (
        "You are an SQL lesson curator for an LLM SQL agent. Given a "
        "user-question/assistant-resolution exchange, decide whether a "
        "durable, reusable lesson was established that would help FUTURE "
        "agents answer similar questions — an intent-to-schema mapping "
        "(what the user means maps to which table/column/pattern), a "
        "performance guard (bounded IN-list, trailing-12-month window, "
        "LIMIT, never ILIKE raw serials), or a fix for a repeated failure "
        "(e.g. serial lives in the mapping table, not the raw columns).\n\n"
        "Only derive lessons from POSITIVE evidence: the SQL executed and the "
        "result was accepted/plausible, OR a concrete fix for a concrete "
        "error. If nothing durable (trivial Q&A, no SQL run, transient), "
        "respond with exactly: NOTHING\n\n"
        "Otherwise respond with 1-3 concise, standalone lessons, each:\n"
        "- kind: schema-map | perf-guard | resolution-pattern | fail-fix\n"
        "- content: the imperative instruction (1-3 sentences), standalone — "
        "a future session with zero other context must understand it\n"
        "- trigger: the class of question this applies to\n"
        "- tables: comma-separated table/dataset names\n"
        "- tags: comma-separated keywords\n"
        'Format as JSON: {"lessons": [{kind, content, trigger, tables, tags}, ...]}'
    )

    async def _distill_and_store_sql_lessons(
        self,
        user_text: str,
        assistant_text: str,
    ) -> Optional[str]:
        """Ask the distillation LLM for SQL lessons and store them as candidates.

        Returns the number of candidates stored (as a string) or None if the
        exchange was not worth storing (or distillation is disabled/failed).
        """
        if not self.valves.SQL_LESSONS_DISTILL_ENABLED:
            return None
        if not self.valves.DISTILL_LLM_URL or not self.valves.DISTILL_LLM_MODEL:
            return None
        if len(assistant_text) < self.valves.SQL_LESSONS_DISTILL_MIN_REPLY_CHARS:
            return None

        url = self.valves.DISTILL_LLM_URL.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.valves.DISTILL_LLM_API_KEY:
            headers["Authorization"] = f"Bearer {self.valves.DISTILL_LLM_API_KEY}"
        payload = {
            "model": self.valves.DISTILL_LLM_MODEL,
            "messages": [
                {"role": "system", "content": self._SQL_DISTILL_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"User: {user_text[:2000]}\n\nAssistant: {assistant_text[:4000]}",
                },
            ],
            "max_tokens": 512,
            "temperature": 0.1,
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                raw = resp.json()["choices"][0]["message"]["content"].strip()
        except Exception:
            logger.warning("SQL-lesson distillation LLM call failed", exc_info=True)
            return None

        if not raw or raw.upper().strip() == "NOTHING":
            return None
        import json

        try:
            data = json.loads(raw)
            lessons = data.get("lessons", []) if isinstance(data, dict) else data
            lessons = [les for les in lessons if isinstance(les, dict) and les.get("content")]
        except Exception:
            logger.warning("SQL-lesson distillation: unparseable JSON: %.120s…", raw)
            return None

        if not lessons:
            return None

        # Store each lesson as a candidate in the WRITE-ONLY candidates dataset.
        dataset_name = (self.valves.SQL_LESSONS_CANDIDATES_DATASET or "sql-lessons-candidates").strip()
        api = self.valves.RAG_API_URL.rstrip("/")
        store_url = f"{api}/api/datasets/{dataset_name}/documents"
        headers = {"Content-Type": "application/json"}
        if self.valves.SQL_LESSONS_PASSWORD:
            headers["X-Dataset-Password"] = self.valves.SQL_LESSONS_PASSWORD

        stored = 0
        for lesson in lessons:
            doc = {
                "text": lesson.get("content", ""),
                "kind": lesson.get("kind", "resolution-pattern"),
                "trigger": lesson.get("trigger", ""),
                "tables": lesson.get("tables", []),
                "tags": lesson.get("tags", []),
                "status": "candidate",
                "source": "openwebui:sql-lesson",
            }
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(store_url, json=[doc], headers=headers)
                    resp.raise_for_status()
                stored += 1
            except Exception:
                logger.warning("SQL-lesson candidate store failed", exc_info=True)
                break

        if stored:
            logger.info("Stored %d SQL-lesson candidate(s) in '%s'", stored, dataset_name)
            return str(stored)
        return None

    _DISTILL_SYSTEM_PROMPT = (
        "You are a memory curator for an LLM chat application. Given a "
        "user-assistant exchange, decide whether anything durable was "
        "established that would be useful in FUTURE conversations — a "
        "decision and its rationale, a confirmed user preference, a "
        "non-obvious fact about the user's project/setup, or a gotcha that "
        "took effort to resolve.\n\n"
        "If nothing notable (trivial Q&A, transient debugging, chitchat), "
        "respond with exactly: NOTHING\n\n"
        "Otherwise respond with 1-3 concise, standalone sentences that a "
        "future session with zero other context can understand. Do NOT "
        "prefix with 'The user' or 'Memory:' — write the fact directly. "
        "One memory only; if multiple facts stand out, pick the most durable."
    )

    async def _distill_and_store_memory(
        self,
        dataset_name: str,
        user: Optional[dict],
        user_text: str,
        assistant_text: str,
    ) -> Optional[str]:
        """Ask the distillation LLM to extract a memory, then store it.

        Returns the stored memory text, or ``None`` if the exchange was
        deemed not worth remembering (or distillation is disabled/failed).
        """
        if not self.valves.DISTILL_LLM_URL or not self.valves.DISTILL_LLM_MODEL:
            return None
        if len(assistant_text) < self.valves.DISTILL_MIN_REPLY_CHARS:
            return None

        url = self.valves.DISTILL_LLM_URL.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.valves.DISTILL_LLM_API_KEY:
            headers["Authorization"] = f"Bearer {self.valves.DISTILL_LLM_API_KEY}"
        payload = {
            "model": self.valves.DISTILL_LLM_MODEL,
            "messages": [
                {"role": "system", "content": self._DISTILL_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"User: {user_text[:2000]}\n\nAssistant: {assistant_text[:4000]}",
                },
            ],
            "max_tokens": 256,
            "temperature": 0.1,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                memory = resp.json()["choices"][0]["message"]["content"].strip()
        except Exception:
            logger.warning("Memory distillation LLM call failed", exc_info=True)
            return None

        if not memory or memory.upper().strip() == "NOTHING":
            return None

        # Optionally auto-create the per-user dataset on first write.
        if self.valves.MEMORY_AUTO_CREATE:
            if not await self._ensure_dataset_exists(dataset_name, user):
                # Dataset missing and couldn't be created — skip this write.
                return None

        # Store via the REST API (password in header, never in chat context).
        api = self.valves.RAG_API_URL.rstrip("/")
        store_url = f"{api}/api/datasets/{dataset_name}/documents"
        doc = {
            "text": memory,
            "source": "openwebui:memory",
            "memory_kind": "auto",
            "memory_ts": datetime.now(timezone.utc).isoformat(),
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(store_url, json=[doc], headers=self._memory_headers(user))
                resp.raise_for_status()
        except Exception:
            logger.warning("Memory store failed", exc_info=True)
            return None

        logger.info("Memory stored in '%s': %.80s…", dataset_name, memory)
        return memory

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

    # ── media URL repair ──────────────────────────────────────────────────
    # LLMs frequently garble long signed media URLs (host typos, dropped hex
    # chars, renamed query params).  The RAG MCP tool results in the same
    # conversation contain the *correct* URLs, so the outlet matches every
    # media URL the model printed against those and substitutes the exact
    # one — deterministic "copy and paste" independent of model behaviour.

    _MEDIA_URL_RE = re.compile(
        r"https?://[^\s\"'<>\)\]\}]+/api/datasets/[^/\s]+/files/[^\s\"'<>\)\]\}]+"
    )

    @classmethod
    def _collect_correct_media_urls(cls, messages: list[dict]) -> dict[str, str]:
        """Map ``{dataset}/{file}`` -> the correct full media URL, sourced
        from tool-result messages (the RAG MCP ``search_dataset`` output)."""
        correct: dict[str, str] = {}
        for msg in messages:
            role = str(msg.get("role", ""))
            content = msg.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and isinstance(p.get("text", ""), str)
                )
            if not isinstance(content, str):
                continue
            for url in cls._MEDIA_URL_RE.findall(content):
                url = url.rstrip(".,;:!?)]}")
                if "?" in url and "token" not in url:
                    continue  # non-media query (e.g. unrelated link)
                try:
                    path = url.split("/api/datasets/", 1)[1]
                    ds, _, file_part = path.split("/", 2) if path.count("/") >= 2 else (path, "", "")
                except ValueError:
                    continue
                if not file_part:
                    continue
                # Only the tool results carry ground-truth URLs.  Never
                # source from the assistant's own (possibly garbled) reply.
                if role == "tool":
                    key = f"{ds}/{file_part.split('?', 1)[0]}"
                    correct[key] = url
        return correct

    @classmethod
    def _repair_media_urls(cls, text: str, correct: dict[str, str]) -> str:
        """Replace every media URL in *text* whose dataset/file key is known
        with the exact correct URL (host + token) from the tool results."""
        if not correct or not text:
            return text

        def _replacer(match: re.Match) -> str:
            url = match.group(0).rstrip(".,")
            if "?" in url and "token" not in url:
                return match.group(0)
            path = url.split("?", 1)[0]
            try:
                rest = path.split("/api/datasets/", 1)[1]
                ds, _, file_part = rest.split("/", 2)
            except (ValueError, IndexError):
                return match.group(0)
            if not file_part:
                return match.group(0)
            key = f"{ds}/{file_part}"
            good = correct.get(key)
            if good and good != url:
                return good
            return url

        return cls._MEDIA_URL_RE.sub(_replacer, text)

    @classmethod
    def _repair_last_assistant_media_urls(cls, messages: list[dict]) -> None:
        """Rewrite the last assistant message's media URLs in place so the
        user always sees the exact, correctly-signed URLs from the tool
        results — no matter how the model garbled them."""
        if not messages:
            return
        correct = cls._collect_correct_media_urls(messages)
        if not correct:
            return
        for msg in reversed(messages):
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content", "")
            if isinstance(content, str):
                repaired = cls._repair_media_urls(content, correct)
                if repaired != content:
                    msg["content"] = repaired
            elif isinstance(content, list):
                changed = False
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        t = part.get("text", "")
                        repaired = cls._repair_media_urls(t, correct)
                        if repaired != t:
                            part["text"] = repaired
                            changed = True
                if changed:
                    msg["content"] = content
            break  # only the latest assistant message is user-visible

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

        # ── 0. Long-term memory recall (independent of media) ───────────
        # At conversation start (or every turn if configured), search the
        # user's PER-USER memory dataset (named from __user__) and inject
        # relevant past memories as context.  Runs before media processing
        # so it works even for text-only messages with no file uploads.
        if self._memory_enabled():
            memory_ds = self._memory_dataset_for_user(__user__)
            if memory_ds:
                should_recall = not self.valves.MEMORY_RECALL_FIRST_ONLY or self._is_first_user_message(messages)
                if should_recall and user_text:
                    memory_ctx = await self._recall_memory(memory_ds, __user__, user_text)
                    if memory_ctx:
                        self._inject_memory_context(messages, memory_ctx)

        # ── 0b. SQL-lesson recall (self-improving SQL agent loop) ──────────
        # If enabled, recall the top-k CURATED sql-lessons for the user's
        # question and inject them as 'follow if applicable' context before
        # the agent writes SQL. Recall is read-only; distillation/storage is
        # handled separately (outlet + promotion gate).
        if self.valves.SQL_LESSONS_ENABLED and user_text:
            sql_ds = (self.valves.SQL_LESSONS_DATASET or "").strip()
            if sql_ds:
                lesson_ctx = await self._recall_sql_lessons(sql_ds, user_text)
                if lesson_ctx:
                    self._inject_sql_lessons(messages, lesson_ctx)

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

    # ════════════════════════════════════════════════════════════════════════
    #  outlet  —  called AFTER the LLM reply
    # ════════════════════════════════════════════════════════════════════════

    async def outlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __model__: Optional[dict] = None,
        **kwargs,
    ) -> dict:
        """Repair garbled media URLs in the model's reply, then distil and
        store a memory from the completed exchange.

        Runs after the LLM has replied and the user has seen the answer.
        Media URLs are matched against the exact URLs in the RAG MCP tool
        results and corrected deterministically (models often mistype long
        signed URLs).  The memory distillation asks a second LLM to extract
        a durable memory; if one is produced it is stored in the memory
        dataset via the RAG REST API.  All of this is invisible to the user
        (no extra tool calls in the chat, no password in context).
        """
        messages: list[dict] = body.get("messages", [])
        if messages and self.valves.REPAIR_MEDIA_URLS:
            self._repair_last_assistant_media_urls(messages)

        # Extract the exchange once; both per-user memory and SQL lessons
        # distillation consume the same user/assistant text.
        messages = body.get("messages", [])
        if not messages:
            return body
        user_text = ""
        assistant_text = ""
        for msg in reversed(messages):
            role = msg.get("role", "")
            if role == "assistant" and not assistant_text:
                assistant_text = self._extract_user_text(msg.get("content", ""))
            elif role == "user" and not user_text:
                user_text = self._extract_user_text(msg.get("content", ""))
            if user_text and assistant_text:
                break

        if not user_text or not assistant_text:
            return body

        # ── Long-term memory (per-user, optional) ─────────────────────────
        if self._memory_enabled() and self.valves.DISTILL_LLM_URL and self.valves.DISTILL_LLM_MODEL:
            memory_ds = self._memory_dataset_for_user(__user__)
            if memory_ds:
                await self._distill_and_store_memory(memory_ds, __user__, user_text, assistant_text)

        # ── SQL lessons (self-improving SQL agent loop: write half) ───────
        # Ask the distillation LLM to extract 0-3 candidate lessons and store
        # them in sql-lessons-candidates. Independent of per-user memory; the
        # store is shared and password-protected, and the distill prompt only
        # emits on positive evidence.
        if self.valves.SQL_LESSONS_DISTILL_ENABLED:
            await self._distill_and_store_sql_lessons(user_text, assistant_text)

        return body
