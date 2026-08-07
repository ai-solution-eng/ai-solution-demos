import asyncio
import io
import os
import shutil
import tempfile
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from openai import APIError
from pydantic import BaseModel
from pydub import AudioSegment

from app.services.clients import make_client
from app.services.rooms import update_room_tts_default_voice
from app.services.tts import (
    TTSCache,
    delete_voice,
    list_voices,
    synthesize_speech,
    upload_voice,
)
from app.state.rooms import ROOMS, ROOMS_LOCK, get_room_or_404, normalize_room_id

router = APIRouter()


def _resolve_tts_config(room_id: str) -> dict[str, str]:
    normalized = normalize_room_id(room_id)
    room = ROOMS.get(normalized)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found.")
    tts = room.get("tts") or {}
    base_url = (tts.get("base_url") or "").strip()
    api_key = (tts.get("api_key") or "").strip()
    model = (tts.get("model") or "").strip()
    voice = (tts.get("voice") or "").strip()
    if not base_url:
        raise HTTPException(status_code=400, detail="TTS is not configured for this room.")
    return {"base_url": base_url, "api_key": api_key, "model": model, "voice": voice}


def _get_room_tts_cache(room_id: str) -> TTSCache:
    normalized = normalize_room_id(room_id)
    room = ROOMS.get(normalized)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found.")
    cache = room.get("tts_cache")
    if cache is None:
        cache = TTSCache()
        room["tts_cache"] = cache
    return cache


async def _transcribe_audio(
    room_id: str, audio_bytes: bytes, filename: str, timeout: float = 30.0
) -> str:
    normalized = normalize_room_id(room_id)
    room = ROOMS.get(normalized)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found.")
    asr_cfg = room.get("asr") or {}
    asr_base_url = (asr_cfg.get("base_url") or "").strip()
    asr_api_key = (asr_cfg.get("api_key") or "").strip()
    asr_model = (asr_cfg.get("model") or "").strip()
    if not asr_base_url or not asr_model:
        raise HTTPException(status_code=400, detail="ASR is not configured for this room.")

    temp_dir = tempfile.mkdtemp()
    try:
        ext = os.path.splitext(filename or "sample.webm")[1].lower() or ".webm"
        temp_in = os.path.join(temp_dir, f"sample{ext}")
        with open(temp_in, "wb") as f:
            f.write(audio_bytes)

        audio = AudioSegment.from_file(temp_in)
        audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
        wav_buf = io.BytesIO()
        audio.export(wav_buf, format="wav")
        wav_buf.seek(0)

        asr_client = make_client(asr_base_url, asr_api_key)
        result = await asyncio.wait_for(
            asr_client.audio.transcriptions.create(model=asr_model, file=wav_buf),
            timeout=timeout,
        )

        if hasattr(result, "text"):
            text = (result.text or "").strip()
        elif isinstance(result, dict):
            text = (result.get("text", "") or "").strip()
        else:
            text = ""
        print(f"ASR transcription ({len(text)} chars): {text[:100]}")
        return text
    except asyncio.TimeoutError:
        raise HTTPException(status_code=400, detail="ASR transcription timed out")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not transcribe audio: {exc}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


class TTSGenerateRequest(BaseModel):
    segment_id: str
    lang: str
    voice: str = ""
    text: str


class TTSVoicePreviewRequest(BaseModel):
    voice: str
    text: str = "Hello, this is a voice profile preview."


class TTSDefaultVoiceRequest(BaseModel):
    voice: str


@router.get("/api/rooms/{room_id}/tts/voices")
async def get_tts_voices(room_id: str):
    cfg = _resolve_tts_config(room_id)
    result = await list_voices(base_url=cfg["base_url"], api_key=cfg["api_key"])
    if result["status"] != 200:
        raise HTTPException(status_code=result["status"], detail=str(result["data"]))
    return JSONResponse(result["data"])


@router.post("/api/rooms/{room_id}/tts/voices")
async def upload_tts_voice(
    room_id: str,
    name: str = Form(...),
    consent: str = Form(...),
    ref_text: str = Form(default=""),
    speaker_description: str = Form(default=""),
    audio_sample: UploadFile = File(...),
):
    cfg = _resolve_tts_config(room_id)
    audio_bytes = await audio_sample.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Audio sample is empty")
    if len(audio_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Audio sample exceeds 10MB limit")

    resolved_ref_text = (ref_text or "").strip()

    if not resolved_ref_text:
        try:
            resolved_ref_text = await _transcribe_audio(
                room_id, audio_bytes, audio_sample.filename or "sample.webm"
            )
        except HTTPException as exc:
            raise exc

    if not resolved_ref_text:
        raise HTTPException(
            status_code=400,
            detail=(
                "A reference transcript (ref_text) is required to clone a voice. "
                "Please provide the transcript of the audio sample, or use a sample "
                "that can be automatically transcribed."
            ),
        )

    result = await upload_voice(
        base_url=cfg["base_url"],
        api_key=cfg["api_key"],
        audio_bytes=audio_bytes,
        filename=audio_sample.filename or "sample.webm",
        name=name,
        consent=consent,
        ref_text=resolved_ref_text,
        speaker_description=speaker_description,
    )
    if result["status"] not in (200, 201):
        raise HTTPException(status_code=result["status"], detail=str(result["data"]))
    return JSONResponse(result["data"])


@router.post("/api/rooms/{room_id}/tts/voices/transcribe")
async def transcribe_tts_voice(room_id: str, audio_sample: UploadFile = File(...)):
    audio_bytes = await audio_sample.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Audio sample is empty")
    if len(audio_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Audio sample exceeds 10MB limit")

    text = await _transcribe_audio(room_id, audio_bytes, audio_sample.filename or "sample.webm")
    return JSONResponse({"text": text})


@router.delete("/api/rooms/{room_id}/tts/voices/{name}")
async def delete_tts_voice(room_id: str, name: str):
    cfg = _resolve_tts_config(room_id)
    result = await delete_voice(base_url=cfg["base_url"], api_key=cfg["api_key"], name=name)
    if result["status"] != 200:
        raise HTTPException(status_code=result["status"], detail=str(result["data"]))
    return JSONResponse(result["data"])


@router.post("/api/rooms/{room_id}/tts/voices/preview")
async def preview_tts_voice(room_id: str, payload: TTSVoicePreviewRequest):
    cfg = _resolve_tts_config(room_id)
    voice = (payload.voice or "").strip()
    if not voice:
        raise HTTPException(status_code=400, detail="No voice specified.")
    text = payload.text or "Hello, this is a voice profile preview."

    tts_client = make_client(cfg["base_url"], cfg["api_key"])
    try:
        audio_bytes = await synthesize_speech(
            text=text,
            voice=voice,
            tts_client=tts_client,
            tts_model=cfg["model"],
        )
    except APIError as exc:
        body = getattr(exc, "body", None) or {}
        message = body.get("message") or body.get("detail") or str(exc)
        raise HTTPException(status_code=502, detail=f"TTS preview failed: {message}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TTS preview failed: {exc}")

    if not audio_bytes:
        raise HTTPException(status_code=422, detail="TTS produced no audio.")
    return Response(content=audio_bytes, media_type="audio/wav")


@router.post("/api/rooms/{room_id}/tts/generate")
async def generate_tts(room_id: str, payload: TTSGenerateRequest):
    cfg = _resolve_tts_config(room_id)
    voice = (payload.voice or cfg["voice"]).strip()
    if not voice:
        raise HTTPException(status_code=400, detail="No voice specified.")

    cache = _get_room_tts_cache(room_id)
    cached = await cache.get(payload.segment_id, payload.lang, voice)
    if cached:
        print(f"TTS cache hit for segment={payload.segment_id} lang={payload.lang} voice={voice}")
        return Response(content=cached, media_type="audio/wav")

    tts_client = make_client(cfg["base_url"], cfg["api_key"])
    try:
        audio_bytes = await synthesize_speech(
            text=payload.text,
            voice=voice,
            tts_client=tts_client,
            tts_model=cfg["model"],
        )
    except APIError as exc:
        body = getattr(exc, "body", None) or {}
        message = body.get("message") or body.get("detail") or str(exc)
        raise HTTPException(status_code=502, detail=f"TTS synthesis failed: {message}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TTS synthesis failed: {exc}")

    if not audio_bytes:
        raise HTTPException(status_code=422, detail="TTS produced no audio.")

    await cache.set(payload.segment_id, payload.lang, voice, audio_bytes)
    return Response(content=audio_bytes, media_type="audio/wav")


@router.put("/api/rooms/{room_id}/tts/default-voice")
async def set_tts_default_voice(room_id: str, payload: TTSDefaultVoiceRequest):
    await update_room_tts_default_voice(room_id, voice=payload.voice)
    return JSONResponse({"room_id": room_id, "tts_default_voice": payload.voice})
