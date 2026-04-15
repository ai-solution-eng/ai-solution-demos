from __future__ import annotations

from pathlib import Path

from app.config import RECORDING_STORAGE_ROOT, SESSION_STORAGE_ROOT
from app.db import CallSegment, database_enabled, session_scope
from app.repositories.chunks import ChunkRepository
from app.repositories.segments import SegmentRepository
from app.repositories.sessions import SessionRepository
from app.schemas.api import TranscriptItem
from app.state.rooms import ROOMS, ROOMS_LOCK, build_room_state, normalize_room_id


def hydrate_room_from_persisted_session(
    session_record,
    segments: list[CallSegment],
    chunks,
) -> dict[str, object]:
    latest_by_segment: dict[str, CallSegment] = {}
    for segment in segments:
        prior = latest_by_segment.get(segment.segment_id)
        if prior is None or segment.revision >= prior.revision:
            latest_by_segment[segment.segment_id] = segment

    ordered_segments = sorted(
        latest_by_segment.values(),
        key=lambda seg: ((seg.ts_ms or 0), seg.id),
    )
    room = build_room_state(session_record.room_id)
    room["persisted_session_id"] = session_record.id
    room["src"] = session_record.src_language
    room["presenter_tgt"] = session_record.presenter_target_language
    room["recording_state"] = (
        "paused"
        if (session_record.recording_state or "").lower() == "recording"
        else session_record.recording_state
    )
    room["recording_session_id"] = session_record.id if chunks else ""
    final_audio_path = (
        RECORDING_STORAGE_ROOT / session_record.room_id / session_record.id / "meeting_audio.wav"
    ).resolve()
    room["recording_file_path"] = str(final_audio_path) if final_audio_path.exists() else ""
    room["recording_chunk_paths"] = [
        str(Path(chunk.relative_path).resolve())
        if Path(chunk.relative_path).is_absolute()
        else str((SESSION_STORAGE_ROOT / chunk.relative_path).resolve())
        for chunk in sorted(chunks, key=lambda item: (item.sequence_no, item.id))
        if (Path(chunk.relative_path).exists() or (SESSION_STORAGE_ROOT / chunk.relative_path).exists())
    ]
    room["recording_filename"] = (
        final_audio_path.name if room["recording_file_path"] else "meeting_audio.webm"
    )
    room["recording_mime_type"] = (
        "audio/wav" if room["recording_file_path"] else (chunks[0].mime_type if chunks else "")
    )

    room_segments: list[dict[str, object]] = []
    transcript_items: list[dict[str, object]] = []
    recorded_segment_ids: set[str] = set()
    for idx, segment in enumerate(ordered_segments):
        segment_payload = {
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
        room_segments.append(segment_payload)
        room["segment_index"][segment.segment_id] = idx
        if bool(segment.included_in_recording):
            recorded_segment_ids.add(segment.segment_id)
            if segment.is_final and (segment.source_text or "").strip():
                transcript_items.append(
                    TranscriptItem(
                        original=segment.source_text,
                        translation=(segment.translations_json or {}).get(
                            session_record.presenter_target_language, ""
                        ),
                        src=segment.source_language,
                        tgt=session_record.presenter_target_language,
                        ts_ms=segment.ts_ms,
                    ).model_dump()
                )

    room["segments"] = room_segments
    room["recording_segment_ids"] = recorded_segment_ids
    room["recording_transcript_items"] = list(transcript_items)
    room["documents_source_items"] = list(transcript_items)
    return room


async def load_or_recover_room(room_id: str) -> dict[str, object] | None:
    normalized = normalize_room_id(room_id)
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is not None:
            return room

    if not database_enabled():
        return None

    async with session_scope() as db_session:
        session_repo = SessionRepository(db_session)
        segment_repo = SegmentRepository(db_session)
        chunk_repo = ChunkRepository(db_session)
        session_record = await session_repo.get_by_room_id(normalized)
        if session_record is None:
            return None
        segments = await segment_repo.list_for_session(session_record.id)
        chunks = await chunk_repo.list_for_session(session_record.id)
        room = hydrate_room_from_persisted_session(session_record, segments, chunks)
        async with ROOMS_LOCK:
            ROOMS[normalized] = room
        if (session_record.recording_state or "").lower() == "recording":
            await session_repo.update(
                session_record.id,
                status="interrupted",
                recording_state="paused",
            )
        return room


async def recover_persisted_rooms() -> int:
    if not database_enabled():
        return 0

    recovered = 0
    async with session_scope() as db_session:
        session_repo = SessionRepository(db_session)
        segment_repo = SegmentRepository(db_session)
        chunk_repo = ChunkRepository(db_session)
        sessions = await session_repo.list_recoverable()

        for session_record in sessions:
            segments = await segment_repo.list_for_session(session_record.id)
            chunks = await chunk_repo.list_for_session(session_record.id)
            room = hydrate_room_from_persisted_session(session_record, segments, chunks)

            async with ROOMS_LOCK:
                ROOMS[session_record.room_id] = room

            if (session_record.recording_state or "").lower() == "recording":
                await session_repo.update(
                    session_record.id,
                    status="interrupted",
                    recording_state="paused",
                )
            recovered += 1

    return recovered
