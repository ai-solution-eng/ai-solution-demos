import asyncio
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.config import (
    DEFAULT_SOURCE_LANGUAGE,
    DEFAULT_TARGET_LANGUAGE,
    RECORDING_STORAGE_ROOT,
    build_default_runtime_config,
    code_to_language,
)
from app.schemas.api import TranscriptItem

ROOMS: dict[str, dict[str, Any]] = {}
ROOMS_LOCK = asyncio.Lock()


def normalize_room_id(room_id: str | None) -> str:
    raw = (room_id or "").strip().lower()
    if raw and re.fullmatch(r"[a-z0-9-]{8,64}", raw):
        return raw
    return uuid.uuid4().hex


def is_valid_room_id(room_id: str | None) -> bool:
    raw = (room_id or "").strip().lower()
    return bool(raw and re.fullmatch(r"[a-z0-9-]{8,64}", raw))


def normalize_client_session_id(client_session_id: str | None) -> str:
    raw = (client_session_id or "").strip().lower()
    return raw or uuid.uuid4().hex


def recording_extension_for_name(name: str | None, mime_type: str | None) -> str:
    ext = (Path(name or "").suffix or "").strip().lower()
    if ext:
        return ext
    normalized = (mime_type or "").strip().lower()
    if "ogg" in normalized:
        return ".ogg"
    if "mp4" in normalized or "aac" in normalized:
        return ".mp4"
    if "wav" in normalized:
        return ".wav"
    return ".webm"


def room_recording_dir(room_id: str, recording_session_id: str) -> Path:
    return RECORDING_STORAGE_ROOT / normalize_room_id(room_id) / recording_session_id


def cleanup_recording_dir(path_str: str | None) -> None:
    path = Path(path_str or "")
    if not path_str:
        return
    try:
        if path.exists():
            shutil.rmtree(path)
    except Exception:
        pass


def build_room_state(room_id: str) -> dict[str, Any]:
    cfg = build_default_runtime_config()
    now = time.time()
    return {
        "room_id": room_id,
        "persisted_session_id": "",
        "src": cfg["src"],
        "presenter_tgt": cfg["tgt"],
        "whisper": dict(cfg["whisper"]),
        "llm": dict(cfg["llm"]),
        "segments": [],
        "segment_index": {},
        "connections": [],
        "used_translation_languages": [],
        "recording_state": "idle",
        "recording_owner_session_id": "",
        "recording_session_id": "",
        "recording_chunk_dir": "",
        "recording_chunk_paths": [],
        "recording_file_path": "",
        "recording_audio_bytes": b"",
        "recording_filename": "",
        "recording_mime_type": "",
        "recording_transcript_items": [],
        "recording_segment_ids": set(),
        "documents_source_items": [],
        "created_at": now,
        "updated_at": now,
    }


def room_can_download(room: dict[str, Any]) -> bool:
    return room.get("recording_state") == "stopped" and bool(
        room.get("recording_file_path") or room.get("recording_chunk_paths")
    )


def room_has_resumable_recording(room: dict[str, Any]) -> bool:
    return bool(room.get("recording_session_id")) and bool(
        room.get("recording_file_path") or room.get("recording_chunk_paths")
    )


def remember_room_translation_language(room: dict[str, Any], language_code: str) -> None:
    normalized = (language_code or "").strip().lower()
    if normalized not in code_to_language or normalized == "en":
        return
    languages = list(room.get("used_translation_languages") or [])
    if normalized not in languages:
        languages.append(normalized)
        room["used_translation_languages"] = languages


def language_codes_from_room_segments_list(room_segments: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for segment in room_segments:
        if not segment.get("is_final"):
            continue
        for code, translated_text in (segment.get("translations") or {}).items():
            normalized = (code or "").strip().lower()
            if (
                normalized in code_to_language
                and (translated_text or "").strip()
                and normalized not in seen
            ):
                seen.append(normalized)
    if "en" not in seen:
        seen.insert(0, "en")
    return seen


def language_codes_from_room_segments(room: dict[str, Any]) -> list[str]:
    return language_codes_from_room_segments_list(room.get("segments") or [])


def room_export_language_codes(room: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for code in room.get("used_translation_languages") or []:
        normalized = (code or "").strip().lower()
        if normalized in code_to_language and normalized not in seen:
            seen.append(normalized)
    for code in language_codes_from_room_segments_list(room.get("segments") or []):
        if code in code_to_language and code not in seen:
            seen.append(code)
    if "en" not in seen:
        seen.insert(0, "en")
    return seen


async def get_or_create_room(room_id: str | None) -> tuple[str, dict[str, Any]]:
    normalized = normalize_room_id(room_id)
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            room = build_room_state(normalized)
            ROOMS[normalized] = room
        room["updated_at"] = time.time()
        return normalized, room


async def get_room_or_404(room_id: str) -> dict[str, Any]:
    normalized = normalize_room_id(room_id)
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found.")
        room["updated_at"] = time.time()
        return room


def serialize_room_state(room: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "room_state",
        "room_id": room["room_id"],
        "src": room.get("src") or DEFAULT_SOURCE_LANGUAGE,
        "presenter_tgt": room.get("presenter_tgt") or DEFAULT_TARGET_LANGUAGE,
        "recording_state": room.get("recording_state") or "idle",
        "recording_session_id": room.get("recording_session_id") or "",
        "can_download_package": room_can_download(room),
    }


def room_segments_to_transcript_items_for_segments(
    room: dict[str, Any], room_segments: list[dict[str, Any]]
) -> list[TranscriptItem]:
    presenter_tgt = (
        (room.get("presenter_tgt") or DEFAULT_TARGET_LANGUAGE).strip().lower()
    )
    items: list[TranscriptItem] = []
    for segment in room_segments:
        if not segment.get("is_final"):
            continue
        original = (segment.get("original") or "").strip()
        if not original:
            continue
        src = (
            (segment.get("src") or room.get("src") or DEFAULT_SOURCE_LANGUAGE)
            .strip()
            .lower()
        )
        translations = segment.get("translations") or {}
        items.append(
            TranscriptItem(
                original=original,
                translation=(translations.get(presenter_tgt) or "").strip(),
                src=src,
                tgt=presenter_tgt,
                ts_ms=segment.get("ts_ms"),
            )
        )
    return items


def room_segments_to_transcript_items(room: dict[str, Any]) -> list[TranscriptItem]:
    return room_segments_to_transcript_items_for_segments(room, room.get("segments") or [])


def room_recording_segments(room: dict[str, Any]) -> list[dict[str, Any]]:
    recorded_segment_ids = {
        str(segment_id or "").strip()
        for segment_id in (room.get("recording_segment_ids") or set())
        if str(segment_id or "").strip()
    }
    if not recorded_segment_ids:
        return []
    return [
        dict(segment)
        for segment in (room.get("segments") or [])
        if str(segment.get("segment_id") or "").strip() in recorded_segment_ids
    ]
