import asyncio
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response

from app.config import (
    DEFAULT_LLM_API_KEY,
    DEFAULT_LLM_BASE_URL,
    DEFAULT_LLM_MODEL,
    DEFAULT_SOURCE_LANGUAGE,
    DEFAULT_WHISPER_API_KEY,
    DEFAULT_WHISPER_BASE_URL,
    DEFAULT_WHISPER_MODEL,
)
from app.schemas.api import ExportLlmConfig, ExportRequest, ExportWhisperConfig, RoomExportRequest, TranscriptItem
from app.services.clients import make_client
from app.services.exports import (
    build_documents_from_export_segments,
    build_export_package_bytes,
    build_segments_from_room_segments_without_audio,
    generate_export_documents,
    run_export_job,
)
from app.services.persistence import load_room_persisted_export_data
from app.services.recordings import reconstruct_recording_media_bytes
from app.state.export_jobs import (
    cleanup_old_export_jobs,
    create_export_job,
    get_export_job,
    load_export_job_artifact,
)
from app.state.rooms import (
    ROOMS,
    ROOMS_LOCK,
    language_codes_from_room_segments_list,
    normalize_room_id,
    room_can_download,
    room_export_language_codes,
    room_has_resumable_recording,
    room_recording_segments,
)
from app.utils.text import safe_json_loads

router = APIRouter()


@router.post("/api/export-documents")
async def export_documents(payload: ExportRequest):
    items = payload.transcript or []
    if not items:
        return {"languages": ["en"], "documents": []}

    llm_base_url = (payload.llm.base_url if payload.llm else "") or DEFAULT_LLM_BASE_URL
    llm_api_key = (payload.llm.api_key if payload.llm else "") or DEFAULT_LLM_API_KEY
    llm_model = (payload.llm.model if payload.llm else "") or DEFAULT_LLM_MODEL
    llm_client = make_client(llm_base_url, llm_api_key)

    return await generate_export_documents(
        items=items,
        llm_client=llm_client,
        llm_model=llm_model,
    )


@router.post("/api/rooms/{room_id}/export-documents")
async def export_room_documents(room_id: str, payload: RoomExportRequest):
    normalized = normalize_room_id(room_id)
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found.")
        has_recorded_media = room_can_download(room) or room_has_resumable_recording(room)
        source_raw = list(room.get("recording_transcript_items") or [])
        room_export_segments = [
            {
                "segment_id": segment.get("segment_id"),
                "revision": int(segment.get("revision") or 0),
                "status": segment.get("status") or "listening",
                "is_final": bool(segment.get("is_final")),
                "original": segment.get("original") or "",
                "src": segment.get("src") or room.get("src") or DEFAULT_SOURCE_LANGUAGE,
                "ts_ms": segment.get("ts_ms"),
                "translations": dict(segment.get("translations") or {}),
            }
            for segment in room.get("segments") or []
        ]
        export_languages = room_export_language_codes(room)
        room_llm = dict(room.get("llm") or {})
        documents_raw = list(room.get("documents_source_items") or [])

    persisted = await load_room_persisted_export_data(normalized)
    if persisted and (persisted.get("audio_file_path") or persisted.get("chunk_paths")):
        room_export_segments = list(persisted["room_export_segments"])
        export_languages = list(persisted["export_languages"])
        source_items = list(persisted["source_items"])
        documents_items = list(persisted["documents_items"])
    else:
        if not has_recorded_media:
            raise HTTPException(
                status_code=409,
                detail="Meeting documents are only available after a recording has been captured.",
            )
        source_items = [TranscriptItem.model_validate(item) for item in source_raw]
        documents_items = [TranscriptItem.model_validate(item) for item in documents_raw]
        if not documents_items:
            documents_items = source_items

    if not source_items:
        raise HTTPException(
            status_code=409,
            detail="Meeting documents are only available after a recording has been captured.",
        )

    llm_cfg = ExportLlmConfig(
        base_url=(
            payload.llm.base_url
            if payload.llm and payload.llm.base_url
            else room_llm.get("base_url") or DEFAULT_LLM_BASE_URL
        ),
        api_key=(
            payload.llm.api_key
            if payload.llm and payload.llm.api_key
            else room_llm.get("api_key") or DEFAULT_LLM_API_KEY
        ),
        model=(
            payload.llm.model
            if payload.llm and payload.llm.model
            else room_llm.get("model") or DEFAULT_LLM_MODEL
        ),
    )
    llm_client = make_client(
        llm_cfg.base_url or DEFAULT_LLM_BASE_URL, llm_cfg.api_key or DEFAULT_LLM_API_KEY
    )
    segments = build_segments_from_room_segments_without_audio(room_export_segments)
    result = await build_documents_from_export_segments(
        segments=segments,
        transcript_items=source_items,
        documents_source_items=documents_items,
        export_languages=export_languages,
        llm_client=llm_client,
        llm_model=llm_cfg.model or DEFAULT_LLM_MODEL,
    )
    return result.model_dump()


@router.post("/api/export-package")
async def export_package(
    audio: UploadFile = File(...),
    transcript_json: str = Form("[]"),
    documents_transcript_json: str = Form("[]"),
    llm_json: str = Form("{}"),
    whisper_json: str = Form("{}"),
):
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="The uploaded recording file is empty.")

    transcript_items = [
        TranscriptItem.model_validate(item) for item in safe_json_loads(transcript_json, [])
    ]
    documents_transcript_items = [
        TranscriptItem.model_validate(item)
        for item in safe_json_loads(documents_transcript_json, [])
    ]
    if not documents_transcript_items:
        documents_transcript_items = transcript_items
    llm_cfg = ExportLlmConfig.model_validate(safe_json_loads(llm_json, {}))
    whisper_cfg = ExportWhisperConfig.model_validate(safe_json_loads(whisper_json, {}))

    llm_client = make_client(
        llm_cfg.base_url or DEFAULT_LLM_BASE_URL, llm_cfg.api_key or DEFAULT_LLM_API_KEY
    )
    whisper_client = make_client(
        whisper_cfg.base_url or DEFAULT_WHISPER_BASE_URL,
        whisper_cfg.api_key or DEFAULT_WHISPER_API_KEY,
    )

    zip_bytes, archive_name = await build_export_package_bytes(
        audio_bytes=audio_bytes,
        audio_filename=audio.filename or "meeting_audio.webm",
        transcript_items=transcript_items,
        documents_source_items=documents_transcript_items,
        whisper_client=whisper_client,
        whisper_model=whisper_cfg.model or DEFAULT_WHISPER_MODEL,
        llm_client=llm_client,
        llm_model=llm_cfg.model or DEFAULT_LLM_MODEL,
    )
    headers = {"Content-Disposition": f'attachment; filename="{archive_name}"'}
    return Response(content=zip_bytes, media_type="application/zip", headers=headers)


@router.post("/api/export-package/start")
async def export_package_start(
    audio: UploadFile = File(...),
    transcript_json: str = Form("[]"),
    documents_transcript_json: str = Form("[]"),
    llm_json: str = Form("{}"),
    whisper_json: str = Form("{}"),
):
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="The uploaded recording file is empty.")

    transcript_items = [
        TranscriptItem.model_validate(item) for item in safe_json_loads(transcript_json, [])
    ]
    documents_transcript_items = [
        TranscriptItem.model_validate(item)
        for item in safe_json_loads(documents_transcript_json, [])
    ]
    if not documents_transcript_items:
        documents_transcript_items = transcript_items
    llm_cfg = ExportLlmConfig.model_validate(safe_json_loads(llm_json, {}))
    whisper_cfg = ExportWhisperConfig.model_validate(safe_json_loads(whisper_json, {}))

    await cleanup_old_export_jobs()
    job_id = await create_export_job()
    asyncio.create_task(
        run_export_job(
            job_id=job_id,
            audio_bytes=audio_bytes,
            audio_filename=audio.filename or "meeting_audio.webm",
            transcript_items=transcript_items,
            documents_transcript_items=documents_transcript_items,
            llm_cfg=llm_cfg,
            whisper_cfg=whisper_cfg,
        )
    )
    return JSONResponse({"job_id": job_id, "status": "queued"})


@router.get("/api/export-package/status/{job_id}")
async def export_package_status(job_id: str):
    job = await get_export_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Export job not found.")
    return JSONResponse(
        {
            "job_id": job_id,
            "status": job.get("status"),
            "progress": job.get("progress", 0),
            "stage": job.get("stage", "Queued"),
            "detail": job.get("detail", ""),
            "archive_name": job.get("archive_name"),
            "error": job.get("error"),
        }
    )


@router.get("/api/export-package/download/{job_id}")
async def export_package_download(job_id: str):
    artifact = await load_export_job_artifact(job_id)
    if artifact is None:
        job = await get_export_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Export job not found.")
        raise HTTPException(
            status_code=409, detail="Export job is not ready for download yet."
        )
    archive_name, zip_bytes = artifact
    headers = {"Content-Disposition": f'attachment; filename="{archive_name}"'}
    return Response(content=zip_bytes, media_type="application/zip", headers=headers)


@router.post("/api/rooms/{room_id}/export-package/start")
async def start_room_export_package(room_id: str, payload: RoomExportRequest):
    normalized = normalize_room_id(room_id)
    resumable_chunk_paths: list[str] = []
    recording_file_path = ""
    recording_mime_type = ""
    export_languages: list[str] = []
    room_export_segments: list[dict[str, object]] = []
    persisted_session_id = None
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found.")
        if not room_can_download(room) and not room_has_resumable_recording(room):
            raise HTTPException(
                status_code=409,
                detail="The full package is only available after a recording has been captured.",
            )
        recording_segments = room_recording_segments(room)
        source_raw = list(room.get("recording_transcript_items") or [])
        documents_raw = list(room.get("documents_source_items") or [])
        audio_bytes = bytes(room.get("recording_audio_bytes") or b"")
        audio_filename = room.get("recording_filename") or "meeting_audio.webm"
        recording_file_path = room.get("recording_file_path") or ""
        resumable_chunk_paths = list(room.get("recording_chunk_paths") or [])
        recording_mime_type = room.get("recording_mime_type") or ""
        export_languages = (
            language_codes_from_room_segments_list(recording_segments)
            if recording_segments
            else room_export_language_codes(room)
        )
        room_export_segments = [
            {
                "segment_id": segment.get("segment_id"),
                "revision": int(segment.get("revision") or 0),
                "status": segment.get("status") or "listening",
                "is_final": bool(segment.get("is_final")),
                "original": segment.get("original") or "",
                "src": segment.get("src") or room.get("src") or DEFAULT_SOURCE_LANGUAGE,
                "ts_ms": segment.get("ts_ms"),
                "translations": dict(segment.get("translations") or {}),
            }
            for segment in recording_segments
        ]
        room_llm = dict(room.get("llm") or {})
        room_whisper = dict(room.get("whisper") or {})
        persisted_session_id = str(room.get("persisted_session_id") or "").strip() or None

    persisted = await load_room_persisted_export_data(normalized)
    if persisted:
        persisted_session_id = persisted.get("session_id") or persisted_session_id
        if not source_raw:
            source_raw = [item.model_dump() for item in persisted["source_items"]]
        if not documents_raw:
            documents_raw = [item.model_dump() for item in persisted["documents_items"]]
        if not room_export_segments:
            room_export_segments = list(persisted["room_export_segments"])
        if not export_languages:
            export_languages = list(persisted["export_languages"])
        if not resumable_chunk_paths:
            resumable_chunk_paths = list(persisted["chunk_paths"])
        if not recording_file_path:
            recording_file_path = str(persisted.get("audio_file_path") or "")
        if not recording_mime_type:
            recording_mime_type = str(persisted.get("recording_mime_type") or "")
        if not audio_filename:
            audio_filename = str(persisted.get("audio_filename") or audio_filename)

    if not audio_bytes and recording_file_path:
        audio_bytes = await asyncio.to_thread(Path(recording_file_path).read_bytes)
        if not audio_filename:
            audio_filename = Path(recording_file_path).name

    if not audio_bytes and resumable_chunk_paths:
        audio_bytes, media_suffix = await asyncio.to_thread(
            reconstruct_recording_media_bytes,
            resumable_chunk_paths,
            recording_mime_type,
        )
        audio_filename = f"meeting_audio{media_suffix}"

    if not audio_bytes:
        raise HTTPException(
            status_code=409,
            detail="No finished recording package is available for this room.",
        )

    if not source_raw and documents_raw:
        source_raw = list(documents_raw)
    if not documents_raw and source_raw:
        documents_raw = list(source_raw)
    if not documents_raw and source_raw:
        documents_raw = list(source_raw)

    llm_cfg = ExportLlmConfig(
        base_url=(
            payload.llm.base_url
            if payload.llm and payload.llm.base_url
            else room_llm.get("base_url") or DEFAULT_LLM_BASE_URL
        ),
        api_key=(
            payload.llm.api_key
            if payload.llm and payload.llm.api_key
            else room_llm.get("api_key") or DEFAULT_LLM_API_KEY
        ),
        model=(
            payload.llm.model
            if payload.llm and payload.llm.model
            else room_llm.get("model") or DEFAULT_LLM_MODEL
        ),
    )
    whisper_cfg = ExportWhisperConfig(
        base_url=(
            payload.whisper.base_url
            if payload.whisper and payload.whisper.base_url
            else room_whisper.get("base_url") or DEFAULT_WHISPER_BASE_URL
        ),
        api_key=(
            payload.whisper.api_key
            if payload.whisper and payload.whisper.api_key
            else room_whisper.get("api_key") or DEFAULT_WHISPER_API_KEY
        ),
        model=(
            payload.whisper.model
            if payload.whisper and payload.whisper.model
            else room_whisper.get("model") or DEFAULT_WHISPER_MODEL
        ),
    )

    source_items = [TranscriptItem.model_validate(item) for item in source_raw]
    documents_items = [TranscriptItem.model_validate(item) for item in documents_raw]

    await cleanup_old_export_jobs()
    if persisted_session_id:
        job_id = await create_export_job(session_id=persisted_session_id)
    else:
        job_id = await create_export_job()
    asyncio.create_task(
        run_export_job(
            job_id=job_id,
            audio_bytes=audio_bytes,
            audio_filename=audio_filename,
            transcript_items=source_items,
            documents_transcript_items=documents_items,
            export_languages=export_languages,
            room_segments=room_export_segments,
            room_id=normalized,
            llm_cfg=llm_cfg,
            whisper_cfg=whisper_cfg,
        )
    )
    return JSONResponse({"job_id": job_id, "status": "queued"})
