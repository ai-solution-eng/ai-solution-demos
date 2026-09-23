import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.audio.ffmpeg import ffmpeg_convert_to_pcm_wav, wav_bytesio_from_pcm16
from app.config import SAMPLE_RATE
from app.schemas.api import TranscriptItem
from app.services.persistence import persist_recording_chunk_metadata, sync_room_persisted_session
from app.state.rooms import (
    ROOMS,
    ROOMS_LOCK,
    build_room_state,
    cleanup_recording_dir,
    normalize_client_session_id,
    normalize_room_id,
    recording_extension_for_name,
    room_recording_dir,
    room_recording_segments,
    room_segments_to_transcript_items_for_segments,
    serialize_room_state,
)


async def start_or_resume_room_recording(
    room_id: str, *, client_session_id: str
) -> dict[str, Any]:
    normalized = normalize_room_id(room_id)
    chunk_dir = ""
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            room = build_room_state(normalized)
            ROOMS[normalized] = room
        if room.get("recording_state") == "stopped":
            raise HTTPException(
                status_code=409,
                detail="Clear the session before starting a brand-new recording in this room.",
            )
        if room.get("recording_state") == "idle":
            chunk_dir = room.get("recording_chunk_dir") or ""
            room["recording_session_id"] = uuid.uuid4().hex
            room["recording_chunk_dir"] = str(
                room_recording_dir(normalized, room["recording_session_id"])
            )
            room["recording_chunk_paths"] = []
            room["used_translation_languages"] = []
            room["recording_file_path"] = ""
            room["recording_audio_bytes"] = b""
            room["recording_filename"] = ""
            room["recording_mime_type"] = ""
            room["recording_transcript_items"] = []
            room["recording_segment_ids"] = set()
            room["documents_source_items"] = []
        elif not room.get("recording_session_id"):
            room["recording_session_id"] = uuid.uuid4().hex
            room["recording_chunk_dir"] = str(
                room_recording_dir(normalized, room["recording_session_id"])
            )
        room["recording_state"] = "recording"
        room["recording_owner_session_id"] = normalize_client_session_id(
            client_session_id
        )
        room["updated_at"] = time.time()
        payload = serialize_room_state(room)
    if chunk_dir:
        await asyncio.to_thread(cleanup_recording_dir, chunk_dir)
    await asyncio.to_thread(
        lambda: room_recording_dir(normalized, payload["recording_session_id"]).mkdir(
            parents=True, exist_ok=True
        )
    )
    await sync_room_persisted_session(normalized)
    return payload


async def pause_room_recording(
    room_id: str, *, client_session_id: str | None = None
) -> dict[str, Any] | None:
    normalized = normalize_room_id(room_id)
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            return None
        if room.get("recording_state") != "recording":
            return serialize_room_state(room)
        owner_session_id = room.get("recording_owner_session_id") or ""
        normalized_session = normalize_client_session_id(client_session_id)
        if (
            client_session_id
            and owner_session_id
            and owner_session_id != normalized_session
        ):
            return serialize_room_state(room)
        room["recording_state"] = "paused"
        room["recording_owner_session_id"] = ""
        room["updated_at"] = time.time()
        payload = serialize_room_state(room)

    await sync_room_persisted_session(normalized)
    return payload


async def append_room_recording_chunk(
    room_id: str,
    *,
    recording_session_id: str,
    client_session_id: str,
    filename: str,
    mime_type: str,
    chunk_bytes: bytes,
) -> dict[str, Any]:
    normalized = normalize_room_id(room_id)
    client_session_id = normalize_client_session_id(client_session_id)
    if not chunk_bytes:
        raise HTTPException(status_code=400, detail="Recording chunk is empty.")

    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found.")
        if room.get("recording_state") != "recording":
            raise HTTPException(
                status_code=409,
                detail="Recording is not currently active for this room.",
            )
        if (room.get("recording_session_id") or "") != (recording_session_id or ""):
            raise HTTPException(
                status_code=409,
                detail="This recording session is no longer active.",
            )
        if (room.get("recording_owner_session_id") or "") != client_session_id:
            raise HTTPException(
                status_code=409,
                detail="Only the active presenter recording session can upload audio chunks.",
            )
        chunk_dir = room.get("recording_chunk_dir") or str(
            room_recording_dir(normalized, recording_session_id)
        )
        room["recording_chunk_dir"] = chunk_dir
        chunk_index = len(room.get("recording_chunk_paths") or [])
        chunk_ext = recording_extension_for_name(filename, mime_type)
        chunk_path = str(Path(chunk_dir) / f"chunk-{chunk_index:06d}{chunk_ext}")
        if mime_type:
            room["recording_mime_type"] = mime_type
        room["updated_at"] = time.time()

    target_path = Path(chunk_path)
    await asyncio.to_thread(lambda: target_path.parent.mkdir(parents=True, exist_ok=True))
    await asyncio.to_thread(target_path.write_bytes, chunk_bytes)

    should_keep_chunk = False
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is not None and (room.get("recording_session_id") or "") == (
            recording_session_id or ""
        ):
            committed_paths = list(room.get("recording_chunk_paths") or [])
            if chunk_path not in committed_paths:
                committed_paths.append(chunk_path)
                committed_paths.sort()
                room["recording_chunk_paths"] = committed_paths
            room["updated_at"] = time.time()
            should_keep_chunk = True

    if not should_keep_chunk:
        try:
            await asyncio.to_thread(target_path.unlink)
        except Exception:
            pass
        raise HTTPException(
            status_code=409,
            detail="This recording session is no longer active.",
        )

    await persist_recording_chunk_metadata(
        normalized,
        sequence_no=chunk_index,
        chunk_path=chunk_path,
        mime_type=(mime_type or "").strip() or "application/octet-stream",
        size_bytes=len(chunk_bytes),
    )

    return {"ok": True, "chunk_count": chunk_index + 1}


async def append_room_recording_transcript_item(
    room_id: str, *, segment_id: str, item: TranscriptItem
) -> None:
    normalized = normalize_room_id(room_id)
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None or room.get("recording_state") != "recording":
            return
        segment_ids = room.setdefault("recording_segment_ids", set())
        if segment_id in segment_ids:
            return
        segment_ids.add(segment_id)
        room.setdefault("recording_transcript_items", []).append(item.model_dump())
        room["updated_at"] = time.time()


def reconstruct_recording_media_bytes(
    chunk_paths: list[str], mime_type: str | None = None
) -> tuple[bytes, str]:
    if not chunk_paths:
        raise RuntimeError("No recording chunks are available.")
    ordered_paths = sorted(chunk_paths)
    media_bytes = b"".join(Path(path).read_bytes() for path in ordered_paths)
    if not media_bytes:
        raise RuntimeError("No recording media bytes were reconstructed.")
    suffix = recording_extension_for_name(ordered_paths[0], mime_type)
    return media_bytes, suffix


async def finalize_room_recording(
    room_id: str, *, recording_session_id: str
) -> dict[str, Any]:
    normalized = normalize_room_id(room_id)
    chunk_dir = ""
    recording_mime_type = ""
    final_recording_path = ""
    documents_source_items: list[TranscriptItem] = []
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found.")
        current_session_id = room.get("recording_session_id") or ""
        if current_session_id != (recording_session_id or ""):
            raise HTTPException(
                status_code=409,
                detail="This recording session is no longer active.",
            )
        if room.get("recording_state") not in {"paused", "recording"}:
            raise HTTPException(
                status_code=409,
                detail="Only a paused or active recording can be stopped.",
            )
        chunk_paths = list(room.get("recording_chunk_paths") or [])
        if not chunk_paths:
            raise HTTPException(
                status_code=409,
                detail="No recorded audio is available for this room yet.",
            )
        recorded_items = [
            TranscriptItem.model_validate(item)
            for item in room.get("recording_transcript_items") or []
        ]
        recording_segments = room_recording_segments(room)
        documents_source_items = room_segments_to_transcript_items_for_segments(
            room, recording_segments
        )
        if not documents_source_items:
            documents_source_items = list(recorded_items)
        chunk_dir = room.get("recording_chunk_dir") or ""
        recording_mime_type = room.get("recording_mime_type") or ""
        final_recording_path = str(Path(chunk_dir) / "meeting_audio.wav")

    media_bytes, media_suffix = await asyncio.to_thread(
        reconstruct_recording_media_bytes, chunk_paths, recording_mime_type
    )
    pcm16, _ = await asyncio.to_thread(ffmpeg_convert_to_pcm_wav, media_bytes, media_suffix)
    wav_bytes = wav_bytesio_from_pcm16(pcm16, SAMPLE_RATE).getvalue()
    target_path = Path(final_recording_path)
    await asyncio.to_thread(lambda: target_path.parent.mkdir(parents=True, exist_ok=True))
    await asyncio.to_thread(target_path.write_bytes, wav_bytes)

    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found.")
        if (room.get("recording_session_id") or "") != (recording_session_id or ""):
            raise HTTPException(
                status_code=409,
                detail="This recording session is no longer active.",
            )
        room["recording_audio_bytes"] = b""
        room["recording_file_path"] = final_recording_path
        room["recording_filename"] = "meeting_audio.wav"
        room["recording_mime_type"] = "audio/wav"
        room["recording_state"] = "stopped"
        room["recording_owner_session_id"] = ""
        room["recording_chunk_paths"] = list(chunk_paths)
        room["recording_transcript_items"] = [item.model_dump() for item in recorded_items]
        room["documents_source_items"] = [
            item.model_dump() for item in documents_source_items
        ]
        room["updated_at"] = time.time()
        payload = serialize_room_state(room)

    await sync_room_persisted_session(normalized)
    return payload


async def store_room_recording(
    room_id: str,
    *,
    audio_bytes: bytes,
    audio_filename: str,
    mime_type: str,
    transcript_items: list[TranscriptItem],
    documents_source_items: list[TranscriptItem],
) -> None:
    normalized = normalize_room_id(room_id)
    chunk_dir = ""
    recording_path = ""
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            room = build_room_state(normalized)
            ROOMS[normalized] = room
        chunk_dir = room.get("recording_chunk_dir") or ""
        recording_session_id = room.get("recording_session_id") or uuid.uuid4().hex
        room["recording_session_id"] = recording_session_id
        room["recording_chunk_dir"] = str(room_recording_dir(normalized, recording_session_id))
        recording_path = str(
            Path(room["recording_chunk_dir"]) / (audio_filename or "meeting_audio.webm")
        )
        room["recording_audio_bytes"] = b""
        room["recording_file_path"] = recording_path
        room["recording_filename"] = Path(recording_path).name
        room["recording_mime_type"] = mime_type
        room["recording_state"] = "stopped"
        room["recording_owner_session_id"] = ""
        room["recording_chunk_paths"] = [recording_path]
        room["recording_transcript_items"] = [
            item.model_dump() for item in transcript_items
        ]
        room["recording_segment_ids"] = set()
        room["documents_source_items"] = [
            item.model_dump() for item in documents_source_items
        ]
        room["updated_at"] = time.time()
    target_path = Path(recording_path)
    await asyncio.to_thread(lambda: target_path.parent.mkdir(parents=True, exist_ok=True))
    await asyncio.to_thread(target_path.write_bytes, audio_bytes)
    if chunk_dir and chunk_dir != str(target_path.parent):
        await asyncio.to_thread(cleanup_recording_dir, chunk_dir)
    await sync_room_persisted_session(normalized)
    await persist_recording_chunk_metadata(
        normalized,
        sequence_no=0,
        chunk_path=recording_path,
        mime_type=(mime_type or "").strip() or "application/octet-stream",
        size_bytes=len(audio_bytes),
    )
