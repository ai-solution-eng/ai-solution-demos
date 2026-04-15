from __future__ import annotations

import traceback
from pathlib import Path

from app.db import CallSegment, database_enabled, session_scope
from app.config import RECORDING_STORAGE_ROOT, SESSION_STORAGE_ROOT
from app.repositories.chunks import ChunkRepository
from app.repositories.segments import SegmentRepository
from app.repositories.sessions import SessionRepository
from app.schemas.api import TranscriptItem
from app.state.rooms import ROOMS, ROOMS_LOCK, normalize_room_id


async def ensure_room_persisted_session(room_id: str) -> str | None:
    if not database_enabled():
        return None

    normalized = normalize_room_id(room_id)
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            return None
        existing_session_id = str(room.get("persisted_session_id") or "").strip()
        if existing_session_id:
            return existing_session_id
        src_language = str(room.get("src") or "en").strip().lower() or "en"
        presenter_target_language = (
            str(room.get("presenter_tgt") or "es").strip().lower() or "es"
        )
        recording_state = str(room.get("recording_state") or "idle").strip().lower() or "idle"

    try:
        async with session_scope() as db_session:
            repo = SessionRepository(db_session)
            record = await repo.create(
                room_id=normalized,
                src_language=src_language,
                presenter_target_language=presenter_target_language,
                recording_state=recording_state,
            )
            persisted_session_id = record.id
    except Exception:
        print("Failed to ensure persisted session")
        traceback.print_exc()
        return None

    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is not None and not room.get("persisted_session_id"):
            room["persisted_session_id"] = persisted_session_id
    return persisted_session_id


async def sync_room_persisted_session(room_id: str) -> str | None:
    if not database_enabled():
        return None

    session_id = await ensure_room_persisted_session(room_id)
    if not session_id:
        return None

    normalized = normalize_room_id(room_id)
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            return session_id
        src_language = str(room.get("src") or "en").strip().lower() or "en"
        presenter_target_language = (
            str(room.get("presenter_tgt") or "es").strip().lower() or "es"
        )
        recording_state = str(room.get("recording_state") or "idle").strip().lower() or "idle"

    try:
        async with session_scope() as db_session:
            repo = SessionRepository(db_session)
            await repo.update(
                session_id,
                room_id=normalized,
                src_language=src_language,
                presenter_target_language=presenter_target_language,
                recording_state=recording_state,
            )
    except Exception:
        print("Failed to sync persisted session")
        traceback.print_exc()
    return session_id


async def end_room_persisted_session(room_id: str, *, status: str = "ended") -> str | None:
    if not database_enabled():
        return None

    normalized = normalize_room_id(room_id)
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            return None
        session_id = str(room.get("persisted_session_id") or "").strip()
        room["persisted_session_id"] = ""
    if not session_id:
        return None

    try:
        async with session_scope() as db_session:
            repo = SessionRepository(db_session)
            await repo.mark_ended(session_id, status=status)
    except Exception:
        print("Failed to end persisted session")
        traceback.print_exc()
    return session_id


async def persist_finalized_segment(
    room_id: str,
    *,
    segment_id: str,
    revision: int,
    included_in_recording: bool,
    source_text: str,
    source_language: str,
    translations_json: dict[str, str],
    fact_check_json: dict[str, object] | None = None,
    ts_ms: int | None,
) -> str | None:
    if not database_enabled():
        return None
    if not (source_text or "").strip():
        return None

    session_id = await sync_room_persisted_session(room_id)
    if not session_id:
        return None

    try:
        async with session_scope() as db_session:
            repo = SegmentRepository(db_session)
            await repo.add(
                session_id=session_id,
                segment_id=segment_id,
                revision=revision,
                is_final=True,
                included_in_recording=included_in_recording,
                source_text=source_text,
                source_language=source_language,
                translations_json=translations_json,
                fact_check_json=fact_check_json,
                ts_ms=ts_ms,
            )
    except Exception:
        print("Failed to persist finalized segment")
        traceback.print_exc()
    return session_id


async def persist_segment_fact_check(
    room_id: str,
    *,
    segment_id: str,
    revision: int,
    fact_check_json: dict[str, object] | None,
) -> str | None:
    if not database_enabled():
        return None

    session_id = await sync_room_persisted_session(room_id)
    if not session_id:
        return None

    try:
        async with session_scope() as db_session:
            repo = SegmentRepository(db_session)
            await repo.update_fact_check(
                session_id=session_id,
                segment_id=segment_id,
                revision=revision,
                fact_check_json=fact_check_json,
            )
    except Exception:
        print("Failed to persist fact-check result")
        traceback.print_exc()
    return session_id


async def persist_recording_chunk_metadata(
    room_id: str,
    *,
    sequence_no: int,
    chunk_path: str,
    mime_type: str,
    size_bytes: int,
) -> str | None:
    if not database_enabled():
        return None

    session_id = await sync_room_persisted_session(room_id)
    if not session_id:
        return None

    try:
        relative_path = str(Path(chunk_path).resolve().relative_to(SESSION_STORAGE_ROOT.resolve()))
    except Exception:
        relative_path = str(Path(chunk_path).name)

    try:
        async with session_scope() as db_session:
            repo = ChunkRepository(db_session)
            await repo.add(
                session_id=session_id,
                sequence_no=sequence_no,
                relative_path=relative_path,
                mime_type=mime_type or "application/octet-stream",
                size_bytes=size_bytes,
            )
    except Exception:
        print("Failed to persist recording chunk metadata")
        traceback.print_exc()
    return session_id


async def load_room_persisted_export_data(room_id: str) -> dict[str, object] | None:
    if not database_enabled():
        return None

    normalized = normalize_room_id(room_id)
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        session_id = str((room or {}).get("persisted_session_id") or "").strip()

    try:
        async with session_scope() as db_session:
            session_repo = SessionRepository(db_session)
            segment_repo = SegmentRepository(db_session)
            chunk_repo = ChunkRepository(db_session)

            session_record = await session_repo.get(session_id) if session_id else None
            if session_record is None:
                session_record = await session_repo.get_by_room_id(normalized)
            if session_record is None:
                return None

            segments = await segment_repo.list_for_session(session_record.id)
            chunks = await chunk_repo.list_for_session(session_record.id)
    except Exception:
        print("Failed to load room persisted export data")
        traceback.print_exc()
        return None

    latest_by_segment: dict[str, CallSegment] = {}
    for segment in segments:
        prior = latest_by_segment.get(segment.segment_id)
        if prior is None or segment.revision >= prior.revision:
            latest_by_segment[segment.segment_id] = segment

    ordered_segments = sorted(
        latest_by_segment.values(),
        key=lambda seg: ((seg.ts_ms or 0), seg.id),
    )
    chunk_paths = [
        str((SESSION_STORAGE_ROOT / chunk.relative_path).resolve())
        for chunk in sorted(chunks, key=lambda item: (item.sequence_no, item.id))
    ]
    final_audio_path = (RECORDING_STORAGE_ROOT / session_record.room_id / session_record.id / "meeting_audio.wav").resolve()
    audio_file_path = str(final_audio_path) if final_audio_path.exists() else ""
    if not audio_file_path and len(chunk_paths) == 1:
        audio_file_path = chunk_paths[0]
    audio_filename = Path(audio_file_path).name if audio_file_path else "meeting_audio.webm"
    recording_mime_type = "audio/wav" if audio_file_path.endswith(".wav") else ""
    has_recorded_media = bool(audio_file_path or chunk_paths)
    recorded_segments = [
        segment for segment in ordered_segments if bool(segment.included_in_recording)
    ]
    room_export_segments = [
        {
            "segment_id": segment.segment_id,
            "revision": segment.revision,
            "status": "final" if segment.is_final else "listening",
            "is_final": segment.is_final,
            "original": segment.source_text,
            "src": segment.source_language,
            "ts_ms": segment.ts_ms,
            "translations": dict(segment.translations_json or {}),
            "fact_check": dict(getattr(segment, "fact_check_json", None) or {}),
        }
        for segment in recorded_segments
        if has_recorded_media and segment.is_final and (segment.source_text or "").strip()
    ]
    source_items = [
        TranscriptItem(
            original=segment["original"],
            translation=(segment["translations"].get(session_record.presenter_target_language) or ""),
            src=segment["src"],
            tgt=session_record.presenter_target_language,
            ts_ms=segment["ts_ms"],
        )
        for segment in room_export_segments
    ]

    languages = ["en"]
    for segment in room_export_segments:
        for code, text in (segment.get("translations") or {}).items():
            normalized_code = str(code or "").strip().lower()
            if normalized_code and normalized_code not in languages and str(text or "").strip():
                languages.append(normalized_code)

    return {
        "session_id": session_record.id,
        "audio_filename": audio_filename,
        "audio_file_path": audio_file_path,
        "recording_mime_type": recording_mime_type,
        "chunk_paths": chunk_paths,
        "source_items": source_items,
        "documents_items": source_items,
        "room_export_segments": room_export_segments,
        "export_languages": languages,
        "src_language": session_record.src_language,
        "presenter_target_language": session_record.presenter_target_language,
    }
