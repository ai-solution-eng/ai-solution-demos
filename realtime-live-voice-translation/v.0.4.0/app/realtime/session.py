from typing import Any

from openai import AsyncOpenAI

from app.config import DEFAULT_SOURCE_LANGUAGE, DEFAULT_TARGET_LANGUAGE, code_to_language
from app.schemas.api import TranscriptItem
from app.services.translation import ensure_room_translation
from app.state.rooms import (
    ROOMS,
    ROOMS_LOCK,
    build_room_state,
    normalize_room_id,
    room_can_download,
)


async def build_room_snapshot(
    room_id: str,
    *,
    role: str,
    target_language: str,
    llm_client: AsyncOpenAI,
    llm_model: str,
) -> dict[str, Any]:
    normalized = normalize_room_id(room_id)
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            room = build_room_state(normalized)
            ROOMS[normalized] = room
        room_copy = {
            "room_id": room["room_id"],
            "src": room.get("src") or DEFAULT_SOURCE_LANGUAGE,
            "presenter_tgt": room.get("presenter_tgt") or DEFAULT_TARGET_LANGUAGE,
            "recording_state": room.get("recording_state") or "idle",
            "recording_session_id": room.get("recording_session_id") or "",
            "can_download_package": room_can_download(room),
            "segments": [
                {
                    "segment_id": segment.get("segment_id"),
                    "revision": int(segment.get("revision") or 0),
                    "status": segment.get("status") or "listening",
                    "is_final": bool(segment.get("is_final")),
                    "original": segment.get("original") or "",
                    "src": segment.get("src") or room.get("src") or DEFAULT_SOURCE_LANGUAGE,
                    "ts_ms": segment.get("ts_ms"),
                    "translations": dict(segment.get("translations") or {}),
                    "fact_check": dict(segment.get("fact_check") or {}),
                }
                for segment in room.get("segments") or []
            ],
        }

    requested_target = target_language or room_copy["presenter_tgt"] or DEFAULT_TARGET_LANGUAGE
    requested_target = (
        requested_target if requested_target in code_to_language else room_copy["presenter_tgt"]
    )
    serialized_segments: list[dict[str, Any]] = []
    for segment in room_copy["segments"]:
        translation = segment["translations"].get(requested_target, "")
        if segment["original"] and requested_target != segment["src"] and not translation:
            translation = await ensure_room_translation(
                normalized,
                segment_id=segment["segment_id"],
                revision=segment["revision"],
                original=segment["original"],
                src=segment["src"],
                target_language=requested_target,
                llm_client=llm_client,
                llm_model=llm_model,
            )
        elif requested_target == segment["src"]:
            translation = segment["original"]
        serialized_segments.append(
            {
                "segment_id": segment["segment_id"],
                "revision": segment["revision"],
                "status": segment["status"],
                "is_final": segment["is_final"],
                "original": segment["original"],
                "translation": translation,
                "src": segment["src"],
                "tgt": requested_target,
                "ts_ms": segment["ts_ms"],
                "fact_check": dict(segment.get("fact_check") or {}),
            }
        )

    return {
        "type": "snapshot",
        "role": role,
        "room_id": room_copy["room_id"],
        "src": room_copy["src"],
        "tgt": requested_target,
        "presenter_tgt": room_copy["presenter_tgt"],
        "recording_state": room_copy["recording_state"],
        "recording_session_id": room_copy["recording_session_id"],
        "can_download_package": room_copy["can_download_package"],
        "segments": serialized_segments,
    }
