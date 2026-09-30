from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from app.schemas.api import RecordingControlRequest, RecordingFinalizeRequest, TranscriptItem
from app.services.recordings import (
    append_room_recording_chunk,
    finalize_room_recording,
    pause_room_recording,
    start_or_resume_room_recording,
    store_room_recording,
)
from app.services.rooms import broadcast_room_state
from app.state.rooms import get_room_or_404, serialize_room_state
from app.utils.text import safe_json_loads

router = APIRouter()


@router.post("/api/rooms/{room_id}/recording/start")
async def start_room_recording(room_id: str, payload: RecordingControlRequest):
    room_state = await start_or_resume_room_recording(
        room_id, client_session_id=payload.client_session_id
    )
    await broadcast_room_state(room_id)
    return JSONResponse(room_state)


@router.post("/api/rooms/{room_id}/recording/pause")
async def pause_room_recording_api(room_id: str, payload: RecordingControlRequest):
    room_state = await pause_room_recording(
        room_id, client_session_id=payload.client_session_id
    )
    room = await get_room_or_404(room_id)
    payload_state = room_state or serialize_room_state(room)
    await broadcast_room_state(room_id)
    return JSONResponse(payload_state)


@router.post("/api/rooms/{room_id}/recording/chunk")
async def upload_room_recording_chunk(
    room_id: str,
    audio: UploadFile = File(...),
    recording_session_id: str = Form(""),
    client_session_id: str = Form(""),
    mime_type: str = Form(""),
):
    chunk_bytes = await audio.read()
    result = await append_room_recording_chunk(
        room_id,
        recording_session_id=recording_session_id,
        client_session_id=client_session_id,
        filename=audio.filename or "chunk.webm",
        mime_type=(mime_type or audio.content_type or "").strip(),
        chunk_bytes=chunk_bytes,
    )
    return JSONResponse(result)


@router.post("/api/rooms/{room_id}/recording/finalize")
async def finalize_room_recording_api(room_id: str, payload: RecordingFinalizeRequest):
    room_state = await finalize_room_recording(
        room_id, recording_session_id=payload.recording_session_id
    )
    await broadcast_room_state(room_id)
    return JSONResponse(room_state)


@router.post("/api/rooms/{room_id}/recording")
async def upload_room_recording(
    room_id: str,
    audio: UploadFile = File(...),
    transcript_json: str = Form("[]"),
    documents_transcript_json: str = Form("[]"),
    mime_type: str = Form(""),
):
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(
            status_code=400, detail="The uploaded recording file is empty."
        )

    transcript_items = [
        TranscriptItem.model_validate(item) for item in safe_json_loads(transcript_json, [])
    ]
    documents_transcript_items = [
        TranscriptItem.model_validate(item)
        for item in safe_json_loads(documents_transcript_json, [])
    ]
    if not documents_transcript_items:
        documents_transcript_items = transcript_items
    if not transcript_items:
        transcript_items = documents_transcript_items

    await store_room_recording(
        room_id,
        audio_bytes=audio_bytes,
        audio_filename=audio.filename or "meeting_audio.webm",
        mime_type=(mime_type or audio.content_type or "").strip(),
        transcript_items=transcript_items,
        documents_source_items=documents_transcript_items,
    )
    await broadcast_room_state(room_id)
    room = await get_room_or_404(room_id)
    return serialize_room_state(room)
