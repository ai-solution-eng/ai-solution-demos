import asyncio
import time
from typing import Any

from fastapi import WebSocket

from app.config import DEFAULT_SOURCE_LANGUAGE, code_to_language
from app.services.persistence import end_room_persisted_session
from app.state.rooms import (
    ROOMS,
    ROOMS_LOCK,
    build_room_state,
    cleanup_recording_dir,
    get_or_create_room,
    normalize_room_id,
    remember_room_translation_language,
    serialize_room_state,
)


async def register_room_connection(
    room_id: str,
    *,
    websocket: WebSocket,
    role: str,
    target_language: str,
    send_lock: asyncio.Lock,
) -> dict[str, Any]:
    normalized, room = await get_or_create_room(room_id)
    connection = {
        "websocket": websocket,
        "role": role,
        "target_language": target_language,
        "send_lock": send_lock,
    }
    async with ROOMS_LOCK:
        room = ROOMS[normalized]
        remaining = []
        for existing in room["connections"]:
            if existing.get("websocket") is websocket:
                continue
            if role == "presenter" and existing.get("role") == "presenter":
                continue
            remaining.append(existing)
        remaining.append(connection)
        room["connections"] = remaining
        room["updated_at"] = time.time()
    return connection


async def unregister_room_connection(room_id: str | None, websocket: WebSocket) -> None:
    if not room_id:
        return
    normalized = normalize_room_id(room_id)
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            return
        room["connections"] = [
            conn for conn in room["connections"] if conn.get("websocket") is not websocket
        ]
        room["updated_at"] = time.time()


async def send_room_connection_payload(
    connection: dict[str, Any], payload: dict[str, Any]
) -> bool:
    websocket = connection.get("websocket")
    send_lock = connection.get("send_lock")
    if websocket is None or send_lock is None:
        return False
    try:
        async with send_lock:
            await websocket.send_json(payload)
        return True
    except Exception:
        return False


async def broadcast_room_state(room_id: str) -> None:
    normalized = normalize_room_id(room_id)
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            return
        payload = serialize_room_state(room)
        connections = list(room.get("connections") or [])
    failed: list[WebSocket] = []
    for connection in connections:
        ok = await send_room_connection_payload(connection, payload)
        if not ok and connection.get("websocket") is not None:
            failed.append(connection["websocket"])
    for websocket in failed:
        await unregister_room_connection(normalized, websocket)


async def update_room_segment(
    room_id: str,
    *,
    segment_id: str,
    revision: int,
    status: str,
    is_final: bool,
    original: str,
    src: str,
    ts_ms: int,
    tgt: str,
    translation: str,
) -> dict[str, Any]:
    normalized = normalize_room_id(room_id)
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            room = build_room_state(normalized)
            ROOMS[normalized] = room
        index = room["segment_index"].get(segment_id)
        if index is None:
            segment = {
                "segment_id": segment_id,
                "revision": revision,
                "status": status,
                "is_final": is_final,
                "original": original,
                "src": src,
                "ts_ms": ts_ms,
                "translations": {},
                "fact_check": {},
            }
            room["segment_index"][segment_id] = len(room["segments"])
            room["segments"].append(segment)
        else:
            segment = room["segments"][index]
            existing_revision = int(segment.get("revision") or 0)
            if revision < existing_revision:
                room["updated_at"] = time.time()
                return {
                    "segment_id": segment.get("segment_id"),
                    "revision": existing_revision,
                    "status": segment.get("status") or "listening",
                    "is_final": bool(segment.get("is_final")),
                    "original": segment.get("original") or "",
                    "src": segment.get("src")
                    or room.get("src")
                    or DEFAULT_SOURCE_LANGUAGE,
                    "ts_ms": segment.get("ts_ms"),
                    "translations": dict(segment.get("translations") or {}),
                    "fact_check": dict(segment.get("fact_check") or {}),
                }
            if revision > int(segment.get("revision") or 0):
                segment["translations"] = {}
                segment["fact_check"] = {}
            segment.update(
                {
                    "revision": revision,
                    "status": status,
                    "is_final": is_final,
                    "original": original,
                    "src": src,
                    "ts_ms": ts_ms,
                }
            )
        cleaned_translation = (translation or "").strip()
        if tgt in code_to_language and cleaned_translation:
            segment.setdefault("translations", {})[tgt] = cleaned_translation
            remember_room_translation_language(room, tgt)
        room["src"] = src or room.get("src") or DEFAULT_SOURCE_LANGUAGE
        room["updated_at"] = time.time()
        return {
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


async def update_room_segment_fact_check(
    room_id: str,
    *,
    segment_id: str,
    revision: int,
    fact_check: dict[str, object] | None,
) -> dict[str, Any] | None:
    normalized = normalize_room_id(room_id)
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            return None
        index = room["segment_index"].get(segment_id)
        if index is None:
            return None
        segment = room["segments"][index]
        if int(segment.get("revision") or 0) != int(revision or 0):
            return None
        segment["fact_check"] = dict(fact_check or {})
        room["updated_at"] = time.time()
        return {
            "segment_id": segment.get("segment_id"),
            "revision": int(segment.get("revision") or 0),
            "fact_check": dict(segment.get("fact_check") or {}),
        }


async def remove_room_segment(room_id: str, *, segment_id: str) -> bool:
    normalized = normalize_room_id(room_id)
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            return False
        index = room["segment_index"].pop(segment_id, None)
        if index is None:
            return False
        segments = room.get("segments") or []
        if not (0 <= index < len(segments)):
            room["segment_index"] = {
                str(segment.get("segment_id") or ""): idx
                for idx, segment in enumerate(segments)
                if str(segment.get("segment_id") or "")
            }
            room["updated_at"] = time.time()
            return False
        segments.pop(index)
        room["segment_index"] = {
            str(segment.get("segment_id") or ""): idx
            for idx, segment in enumerate(segments)
            if str(segment.get("segment_id") or "")
        }
        room["updated_at"] = time.time()
        return True


async def clear_room_session(room_id: str) -> None:
    normalized = normalize_room_id(room_id)
    chunk_dir = ""
    await end_room_persisted_session(normalized, status="ended")
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            return
        room["segments"] = []
        room["segment_index"] = {}
        room["used_translation_languages"] = []
        room["recording_state"] = "idle"
        room["persisted_session_id"] = ""
        room["recording_owner_session_id"] = ""
        room["recording_session_id"] = ""
        chunk_dir = room.get("recording_chunk_dir") or ""
        room["recording_chunk_dir"] = ""
        room["recording_chunk_paths"] = []
        room["recording_file_path"] = ""
        room["recording_audio_bytes"] = b""
        room["recording_filename"] = ""
        room["recording_mime_type"] = ""
        room["recording_transcript_items"] = []
        room["recording_segment_ids"] = set()
        room["documents_source_items"] = []
        room["updated_at"] = time.time()
    await asyncio.to_thread(cleanup_recording_dir, chunk_dir)
