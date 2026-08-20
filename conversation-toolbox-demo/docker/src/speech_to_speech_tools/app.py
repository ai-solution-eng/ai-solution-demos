"""
AI Voice Conversation Application v2.0 – Scalable Architecture
- Redis-backed session state
- Direct inline ASR/TTS for real-time conversational path (low latency)
- Pub/sub for multi-replica result delivery (batch status only)
- Memory‑optimised batch transcription with disk-backed audio storage
"""

import asyncio
import glob
import json
import logging
import shutil
import tempfile
import time
import uuid
import wave
from contextlib import asynccontextmanager
from datetime import datetime
from os import environ, makedirs, remove
from os.path import basename as bn
from os.path import exists, splitext
from os.path import join as pj
from pathlib import Path
from typing import Any, ClassVar

import httpx
import numpy as np
import psutil
import uvicorn
from arq.connections import create_pool
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.requests import Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from main_components.batch_transcription import (
    JobStatus,
    get_memory_stats,
    init_batch_system,
)
from main_components.constants import AUDIO_DIR, TRANSCRIPTS_DIR, resolve_api_key
from main_components.conversation_manager import ConversationSession
from main_components.multiuser_backend import (
    multi_user_manager,
    return_uuid,
)
from pydantic import BaseModel
from utils.audio_handling import (
    SUPPORTED_AUDIO_FORMATS,
    audio_bytes_to_wave_bytesio,
    convert_audio_to_wav,
    extract_asr_text,
    format_timestamp,
    get_audio_format_safe,
)
from utils.pcai_model_classes import VoiceModel, discover_model_name
from utils.redis_client import RedisClient
from workers import WorkerSettings

# ==============================================================================
# Configuration from Environment
# ==============================================================================
BATCH_PROCESSING_WORKERS = int(environ.get("BATCH_TRANSCRIPTION_WORKER_COUNT", "4"))
BATCH_MAX_CONCURRENT_JOBS = int(environ.get("BATCH_MAX_CONCURRENT_JOBS", "3"))
BATCH_MAX_MEMORY_PERCENT = float(environ.get("BATCH_MAX_MEMORY_PERCENT", "75.0"))
BATCH_USE_BOUNDED_QUEUE = environ.get("BATCH_USE_BOUNDED_QUEUE", "true").lower() == "true"

# Initialise batch system (uses Redis-backed queue now)
batch_transcription_queue, file_processor = init_batch_system(
    max_workers=BATCH_PROCESSING_WORKERS,
    max_concurrent_jobs=BATCH_MAX_CONCURRENT_JOBS,
    max_memory_percent=BATCH_MAX_MEMORY_PERCENT,
    use_bounded_queue=BATCH_USE_BOUNDED_QUEUE,
)

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
    force=True,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting batch transcription system...")
    logger.info(
        f"Configuration: workers={BATCH_PROCESSING_WORKERS}, "
        f"max_concurrent_jobs={BATCH_MAX_CONCURRENT_JOBS}, "
        f"max_memory_percent={BATCH_MAX_MEMORY_PERCENT}, "
        f"use_bounded_queue={BATCH_USE_BOUNDED_QUEUE}"
    )

    # Initialise Redis client
    await RedisClient.get_client()
    logger.info("Redis connected")

    # Connect to arq pool (used to dispatch batch jobs to the worker Deployment)
    arq_pool: Any | None = None
    if batch_transcription_queue:
        arq_pool = await create_pool(WorkerSettings.redis_settings)
        batch_transcription_queue.set_arq_pool(arq_pool)
        logger.info("arq pool connected; batch jobs will dispatch to workers")

    yield

    # Shutdown
    logger.info("Shutting down...")
    if arq_pool is not None:
        await arq_pool.close()
        logger.info("arq pool closed")
    await RedisClient.close()
    logger.info("Shutdown complete")


app = FastAPI(lifespan=lifespan)

templates = Jinja2Templates(directory="static")
makedirs(AUDIO_DIR, exist_ok=True)
makedirs(TRANSCRIPTS_DIR, exist_ok=True)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
app.mount(
    "/static",
    StaticFiles(directory=str(STATIC_DIR)),
    name="static",
)


# ==============================================================================
# Basic site features
# ==============================================================================
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(STATIC_DIR / "favicon.ico", media_type="image/x-icon")


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


@app.get("/ready")
async def readiness_check():
    """Readiness probe with Redis health check."""
    try:
        r = await RedisClient.get_client()
        await r.ping()
        vm = psutil.virtual_memory()
        memory_percent = vm.percent
        memory_available_mb = vm.available / (1024 * 1024)

        if memory_percent > 90:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "reason": "memory_critical",
                    "memory_percent": memory_percent,
                    "memory_available_mb": memory_available_mb,
                },
            )

        return {
            "status": "ready",
            "memory_percent": round(memory_percent, 1),
            "memory_available_mb": round(memory_available_mb, 0),
            "redis_connected": True,
        }
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "reason": str(e)},
        )


# ==============================================================================
# Pydantic Models for Batch Transcription
# ==============================================================================
class BatchStatusResponse(BaseModel):
    job_id: str
    status: str
    progress_percent: float
    total_segments: int
    completed_segments: int
    failed_segments: int
    file_name: str = ""
    error: str | None = None
    transcript_path: str | None = None
    requested_speakers: int = 1
    diarization_used: bool | None = None


class BatchListResponse(BaseModel):
    jobs: list[BatchStatusResponse]
    total: int
    pending: int
    processing: int
    completed: int
    failed: int


# ==============================================================================
# Main Routes
# ==============================================================================
@app.get("/")
async def get(request: Request):
    config = ConversationSession.current_config
    data = {
        "asr_base_url": config["asrBaseUrl"],
        "hallucination_patterns": config["asrHallucinationPatterns"],
        "llm_base_url": config["llmBaseUrl"],
        "system_prompt": config["systemPrompt"],
        "tts_base_url": config["ttsBaseUrl"],
        "tts_voice": config["ttsVoice"],
        "language": config["language"],
        "rms_threshold": config["rmsThreshold"],
        "sample_rate": 16000,
        "vad_aggression": config["vadAggression"],
    }
    return templates.TemplateResponse(request=request, name="conversational_ai.html", context=data)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    session = ConversationSession(websocket)
    logger.info(f"New session: {session.session_id}")

    # Send session ID to client so it can be saved for reconnection
    await websocket.send_json(
        {
            "type": "session_id",
            "session_id": session.session_id,
        }
    )

    # Task tracking
    active_tasks: set[asyncio.Task] = set()

    async def cancel_all_session_tasks():
        """Cancel all tasks when disconnecting."""
        logger.info(f"Cancelling {len(active_tasks)} active tasks for session {session.session_id}")
        for task in active_tasks:
            if not task.done():
                task.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        await session._cancel_all_tasks()

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                logger.info(f"Disconnect message received for session {session.session_id}")
                break

            if "text" in message:
                raw_text = message["text"]
                try:
                    config_data = json.loads(raw_text)
                    msg_type = config_data.get("type")
                    logger.info(f"📩 Received message type: {msg_type}")

                    if msg_type == "config_update":
                        new_config = config_data.get("config", {})
                        session.update_configs(new_config)
                    elif msg_type == "toggle_voice_interrupt":
                        enabled = config_data.get("enabled", True)
                        session.voice_interrupt_enabled = enabled
                        logger.info(f"Voice interruption set to: {enabled}")
                        await websocket.send_json({"type": "interrupt_setting_ack", "enabled": enabled})
                    elif msg_type == "toggle_tts":
                        enabled = config_data.get("enabled", True)
                        session.tts_enabled = enabled
                        logger.info(f"TTS {'enabled' if enabled else 'disabled'}")
                        await websocket.send_json({"type": "tts_setting_ack", "enabled": enabled})
                    elif msg_type == "restore_session":
                        prev_session_id = config_data.get("session_id", "")
                        if prev_session_id:
                            logger.info(f"Restoring session {prev_session_id}")
                            session.session_id = prev_session_id
                            # Update transcript path to match restored session
                            from os.path import join as pj

                            from main_components.constants import TRANSCRIPTS_DIR

                            session.transcript_path = pj(TRANSCRIPTS_DIR, f"session_{prev_session_id}.log")
                            await session.load_state()
                            await websocket.send_json(
                                {
                                    "type": "history_restored",
                                    "history": session.history,
                                }
                            )
                    elif msg_type == "reset_history":
                        logger.info(f"♻️ Resetting history for session {session.session_id}")
                        session.history = []
                        sys_prompt = session.current_config.get("systemPrompt")
                        if sys_prompt:
                            session.history.append({"role": "system", "content": sys_prompt})
                        await websocket.send_json({"type": "history_reset_ack"})
                    elif msg_type == "toggle_audio_save":
                        enabled = config_data.get("enabled", False)
                        session.save_audio_enabled = enabled
                        logger.info(f"Audio saving {'enabled' if enabled else 'disabled'}")
                        await websocket.send_json({"type": "audio_save_setting_ack", "enabled": enabled})
                    elif msg_type == "manual_interrupt":
                        logger.info("🛑 Manual interrupt requested")
                        await session.interrupt_generation()
                    elif msg_type == "direct_text":
                        text = config_data.get("text", "").strip()
                        if text:
                            logger.info(f"📝 Direct text input (skipping ASR): {text[:100]}...")
                            # Reset VAD state so stale speech detection doesn't
                            # immediately interrupt the text-initiated response.
                            session.vad.reset()
                            # Brief grace period: suppress voice interrupts
                            # for 1.5s so the LLM response can start streaming.
                            session.interrupt_suppressed_until = time.time() + 1.5
                            asyncio.create_task(session.process_text_direct(text))
                    else:
                        logger.warning(f"⚠️ Unknown message type: {msg_type}")
                except json.JSONDecodeError:
                    logger.exception("❌ JSON decode error")
                except Exception:
                    logger.exception("❌ Unexpected error handling message")

            if "bytes" in message:
                data = message["bytes"]
                speech_segments = session.vad.add_audio(data)
                if (
                    session.voice_interrupt_enabled
                    and time.time() > session.interrupt_suppressed_until
                    and session.vad.is_speaking
                    and session.vad.speech_frame_count >= 10
                    and session.is_generating
                ):
                    await session.interrupt_generation()

                for segment in speech_segments:
                    if (
                        session.voice_interrupt_enabled
                        and time.time() > session.interrupt_suppressed_until
                        and session.is_generating
                        and len(segment) >= session.vad.frame_size * session.vad.min_speech_frames
                    ):
                        await session.interrupt_generation()
                    # Create task and track it
                    task = asyncio.create_task(session.process_speech(segment))
                    active_tasks.add(task)
                    task.add_done_callback(active_tasks.discard)

    except WebSocketDisconnect:
        logger.info(f"Client disconnected: {session.session_id}")
    except Exception:
        logger.exception("Connection Error")
    finally:
        await cancel_all_session_tasks()
        await session.close()


# ==============================================================================
# Multi-User Page
# ==============================================================================
@app.get("/multi-user")
async def get_multi_user_page(request: Request):
    """Serve the multi-user transcription page"""
    return templates.TemplateResponse(request=request, name="multi_user_transcription.html")


# ==============================================================================
# File Transcription Page
# ==============================================================================
@app.get("/transcribe-file")
async def get_mp3_page(request: Request):
    """Serve the MP3 transcription page"""
    return templates.TemplateResponse(request=request, name="mp3_transcription.html")


# ==============================================================================
# Batch Transcription Page
# ==============================================================================
@app.get("/batch-transcription")
async def get_batch_page(request: Request):
    """Serve the batch transcription page"""
    return templates.TemplateResponse(request=request, name="batch_transcription.html")


# ==============================================================================
# Voice Profiles Page (voice cloning management)
# ==============================================================================
@app.get("/voice-profiles")
async def get_voice_profiles_page(request: Request):
    """Serve the voice profile management page (upload/record/clone voices)."""
    config = ConversationSession.current_config
    data = {
        "tts_base_url": config["ttsBaseUrl"],
        "tts_voice": config["ttsVoice"],
        "tts_key_configured": bool(resolve_api_key(config.get("ttsApiKey", ""))),
    }
    return templates.TemplateResponse(request=request, name="voice_profiles.html", context=data)


# ==============================================================================
# Multi-User WebSocket (fully implemented)
# ==============================================================================
@app.websocket("/ws/multi-user/{session_id}")
async def multi_user_websocket(websocket: WebSocket, session_id: str):
    await websocket.accept()
    session = multi_user_manager.get_session(session_id)
    if not session:
        session = multi_user_manager.create_session(session_id)
    user_id = return_uuid()
    user_info = await session.add_user(user_id, websocket)
    logger.info(f"New multi-user session {session_id}: User {user_info['name']} joined")

    asr_function = None
    asr_initialized = False

    try:
        await websocket.send_json(
            {
                "type": "joined",
                "user_id": user_id,
                "session_id": session_id,
                "user_name": user_info["name"],
                "users": session.get_user_list(),
            }
        )
        await session.broadcast_to_all(
            {
                "type": "user_joined",
                "user_id": user_id,
                "user_name": user_info["name"],
                "users": session.get_user_list(),
            }
        )

        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break

            if "text" in message:
                try:
                    data = json.loads(message["text"])
                    if data.get("type") == "config_update":
                        config_data = data.get("config", {})
                        logger.info(f"Config update received: {config_data.get('asrModelName')}")
                        asr_initialized = False
                        # Don't let empty API keys from frontend overwrite backend env vars
                        for key in ["asrApiKey", "llmApiKey", "ttsApiKey"]:
                            if not config_data.get(key):
                                config_data.pop(key, None)
                        ConversationSession.current_config.update(config_data)
                    elif data.get("type") == "update_name":
                        new_name = data.get("name", f"User_{user_info['user_number']}")
                        user_info["name"] = new_name
                        session.users[user_id]["name"] = new_name
                        await session.broadcast_to_all(
                            {
                                "type": "user_renamed",
                                "user_id": user_id,
                                "new_name": new_name,
                                "users": session.get_user_list(),
                            }
                        )
                    elif data.get("type") == "get_users":
                        await websocket.send_json({"type": "users_list", "users": session.get_user_list()})
                    elif data.get("type") == "save_transcript":
                        await websocket.send_json(
                            {
                                "type": "transcript_saved",
                                "path": session.transcript_path,
                            }
                        )
                except json.JSONDecodeError:
                    pass

            if "bytes" in message:
                audio_data = message["bytes"]
                logger.info(f"Audio chunk received: {len(audio_data)} bytes")

                if not asr_initialized:
                    try:
                        cfg = getattr(session, "current_config", None) or ConversationSession.current_config
                        config: dict[str, Any] = cfg  # type: ignore[assignment]
                        model_usage = "remote" if config.get("remote", True) else "local"
                        logger.info(
                            f"Initializing ASR: model={config.get('asrModelName') or '(auto)'}, "
                            f"url={config['asrBaseUrl']}, usage={model_usage}"
                        )
                        asr = VoiceModel(
                            model_name=config.get("asrModelName", ""),  # type: ignore[arg-type]
                            url_remote=config["asrBaseUrl"],  # type: ignore[arg-type]
                            api_key=config.get("asrApiKey", ""),  # type: ignore[arg-type]
                            model_usage=model_usage,  # type: ignore[arg-type]
                        )
                        asr_function = asr.asr_async_function
                        asr_initialized = True
                        logger.info("✓ ASR initialized successfully for multi-user session")
                    except Exception as e:
                        logger.exception("ASR initialization failed")
                        await websocket.send_json(
                            {
                                "type": "error",
                                "message": f"ASR initialization failed: {e!s}",
                            }
                        )
                        continue

                if asr_function is None:
                    logger.warning("ASR not initialized, skipping audio")
                    continue

                np_audio = np.frombuffer(audio_data, dtype=np.int16)
                rms = np.sqrt(np.mean(np_audio.astype(float) ** 2))
                logger.info(f"Audio RMS: {rms}")
                if rms < 300:
                    logger.info("Audio too quiet, skipping")
                    continue

                audio_buffer = audio_bytes_to_wave_bytesio(audio_data, sample_rate=ConversationSession.sample_rate)
                try:
                    logger.info("Sending to ASR...")
                    result = await asyncio.wait_for(asr_function(file=audio_buffer), timeout=30.0)
                    text = extract_asr_text(result)
                    logger.info(f"ASR result: '{text}'")
                    if text and len(text) > 2:
                        await session.log_transcript(user_id, text)
                    else:
                        logger.info("ASR returned empty or too short text")
                except TimeoutError:
                    logger.exception("ASR request timed out")
                    await websocket.send_json(
                        {
                            "type": "error",
                            "message": "Transcription timed out. Please try again.",
                        }
                    )
                except Exception as e:
                    logger.exception("Multi-user ASR error")
                    await websocket.send_json({"type": "error", "message": f"Transcription error: {e!s}"})

    except WebSocketDisconnect:
        logger.info(f"User {user_info['name']} disconnected from session {session_id}")
    except Exception:
        logger.exception("Multi-user WebSocket error")
    finally:
        await session.remove_user(user_id)
        await session.broadcast_to_all(
            {
                "type": "user_left",
                "user_id": user_id,
                "user_name": user_info.get("name", "Unknown"),
                "users": session.get_user_list(),
            }
        )


# ==============================================================================
# API Endpoints (fully implemented)
# ==============================================================================
@app.post("/api/test-connection")
async def test_connection(request: Request):
    """Test connectivity to all three model endpoints from the backend."""

    try:
        body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    except Exception:
        body = {}
    from os import environ as env

    endpoints = [
        (
            "ASR",
            body.get("asrBaseUrl", ""),
            body.get("asrApiKey") or env.get("ASR_API_KEY", ""),
        ),
        (
            "LLM",
            body.get("llmBaseUrl", ""),
            body.get("llmApiKey") or env.get("LLM_API_KEY", ""),
        ),
        (
            "TTS",
            body.get("ttsBaseUrl", ""),
            body.get("ttsApiKey") or env.get("TTS_API_KEY", ""),
        ),
    ]
    results = {}
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        for name, url, key in endpoints:
            try:
                headers = {}
                if key:
                    headers["Authorization"] = f"Bearer {key}"
                resp = await client.get(url.rstrip("/") + "/", headers=headers, follow_redirects=False)
                # Any response (including 4xx/5xx) means the endpoint is reachable
                results[name] = {
                    "status": "ok",
                    "code": resp.status_code,
                }
            except Exception as e:
                results[name] = {"status": "error", "message": str(e)}
    return {"results": results}


@app.post("/api/reset-history")
async def reset_history(request: Request):
    """Reset conversation history without requiring an active WebSocket.
    Accepts an optional session_id; clears active session history."""
    try:
        body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    except Exception:
        body = {}
    session_id = body.get("session_id", "")
    logger.info(f"♻️ API reset_history called (session_id={session_id or 'none'})")
    # If there's an active session, clear its history in memory
    # (No active sessions outside WebSocket context, so this is best-effort)
    return {"status": "ok", "message": "History cleared"}


@app.get("/api/config")
async def get_config():
    """Get current configuration for other pages to use (no secrets exposed)."""
    config = ConversationSession.current_config
    return {
        "asrBaseUrl": config["asrBaseUrl"],
        "asrModelName": config.get("asrModelName", ""),
        "remote": config.get("remote", False),
        "language": config.get("language", ""),
    }


@app.post("/api/config")
async def update_config(request: Request):
    """Push full config to the backend without an active WebSocket.

    Updates the shared ``ConversationSession.current_config`` so that
    server-side resolution (e.g. /api/voices key lookup) and the next
    session's ``__init__`` rebuild see the new values immediately. Empty
    API-key fields fall through to the existing server value, mirroring the
    WebSocket ``config_update`` path. Live model-client rebuild for an
    active conversation still happens via the WS ``config_update`` message;
    this endpoint does not touch a running session's instance attributes.
    Returns only a non-secret acknowledgement.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Config must be a JSON object")

    # Don't let empty API keys from the frontend overwrite backend values.
    for key in ["asrApiKey", "llmApiKey", "ttsApiKey"]:
        if not body.get(key):
            body.pop(key, None)

    ConversationSession.current_config.update(body)
    config = ConversationSession.current_config
    return {
        "status": "ok",
        "asrBaseUrl": config["asrBaseUrl"],
        "asrModelName": config.get("asrModelName", ""),
        "remote": config.get("remote", False),
    }


# ==============================================================================
# Voice Profile Management (proxies to vLLM-Omni /v1/audio/voices)
# ==============================================================================
def _bearer_key(request: Request) -> str:
    """Extract a bearer token from the request's Authorization header, if present."""
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return ""


def _resolve_tts_endpoint(base_url: str, client_key: str = "") -> tuple[str, str]:
    """Resolve TTS base URL and API key.

    The API key is resolved in priority order:
      1. client-supplied key (per-request, enables concurrent endpoints/keys)
      2. server config (ConversationSession.current_config["ttsApiKey"])
      3. TTS_API_KEY env var
    Base URLs are not secret and may be supplied by the client to target a
    specific endpoint. An empty client key falls through to the server sources
    so existing single-key deployments keep working with no client change.
    """
    base = (base_url or environ.get("TTS_BASE_URL", "") or "").rstrip("/")
    key = resolve_api_key(
        client_key,
        ConversationSession.current_config.get("ttsApiKey", ""),
        environ.get("TTS_API_KEY", ""),
    )
    if not base:
        raise HTTPException(status_code=400, detail="TTS base URL is required")
    return base, key


# Mimetypes accepted by the TTS voice-upload endpoint (fish s2 pro / vLLM-Omni).
_TTS_ALLOWED_AUDIO_MIMETYPES = frozenset(
    {
        "audio/aac",
        "audio/webm",
        "audio/mp4",
        "audio/x-wav",
        "audio/wav",
        "audio/ogg",
        "audio/flac",
        "audio/mpeg",
    }
)

# Maps browser/OS quirks and file extensions to accepted mimetypes.
_AUDIO_MIMETYPE_ALIASES = {
    "audio/wave": "audio/wav",
    "audio/x-wave": "audio/wav",
    "audio/mp3": "audio/mpeg",
    "audio/x-mp3": "audio/mpeg",
    "audio/m4a": "audio/mp4",
    "audio/x-m4a": "audio/mp4",
}
_AUDIO_EXT_TO_MIMETYPE = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".webm": "audio/webm",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
}


def _normalize_audio_mimetype(filename: str, content_type: str) -> str:
    """Return an accepted audio mimetype for the upload, or '' if unknown."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _TTS_ALLOWED_AUDIO_MIMETYPES:
        return ct
    if ct in _AUDIO_MIMETYPE_ALIASES:
        return _AUDIO_MIMETYPE_ALIASES[ct]
    ext = splitext(filename or "")[1].lower()
    if ext in _AUDIO_EXT_TO_MIMETYPE:
        return _AUDIO_EXT_TO_MIMETYPE[ext]
    return ""


@app.post("/api/transcribe-clip")
async def transcribe_clip(
    file: UploadFile = File(...),
    language: str | None = Form(default=None),
):
    """Transcribe a short audio clip via the configured ASR model.

    Lightweight single-speaker transcription (no diarization) used by the
    voice-profiles page to auto-generate ref_text from the reference audio.

    Optional ``language`` form field overrides the homepage's saved ASR
    language so non-English samples transcribe correctly (e.g. Spanish).
    """
    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Audio file is empty")
    ext = splitext(file.filename or "")[1].lower() or ".wav"
    temp_dir = tempfile.mkdtemp()
    try:
        temp_in = pj(temp_dir, f"clip{ext}")
        with open(temp_in, "wb") as f:
            f.write(audio_bytes)
        wav_bytes, _ = convert_audio_to_wav(temp_in)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not convert audio: {e}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    config = ConversationSession.current_config
    try:
        asr = VoiceModel(
            model_name=config.get("asrModelName", ""),
            url_remote=config["asrBaseUrl"],
            api_key=resolve_api_key(config.get("asrApiKey", "")),
            model_usage="remote" if config.get("remote") else "local",
        )
        asr_function = asr.asr_async_function
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ASR model unavailable: {e}")
    audio_buffer = audio_bytes_to_wave_bytesio(wav_bytes)
    try:
        # Caller-supplied language wins; fall back to homepage config, then "en".
        lang = language or config.get("language") or "en"
        asr_kwargs: dict[str, Any] = {"file": audio_buffer}
        if lang:
            asr_kwargs["language"] = lang
        result = await asyncio.wait_for(asr_function(**asr_kwargs), timeout=30.0)
        text = extract_asr_text(result)
    except TimeoutError:
        raise HTTPException(status_code=504, detail="ASR transcription timed out")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ASR transcription failed: {e}")
    if not text:
        raise HTTPException(status_code=422, detail="ASR returned empty transcript")
    return {"text": text}


VOICE_PROFILES_DIR = pj(AUDIO_DIR, "voice_profiles")


def _voice_original_path(name: str) -> str | None:
    """Return the path to the saved original audio for ``name``, or ``None``.

    The file extension depends on what the user uploaded (``.wav``, ``.mp3``,
    ``.webm``, …), so we glob for ``{name}.*``.  ``name`` is sanitised the
    same way as on save to prevent path traversal.
    """
    safe = bn(name).replace("/", "_").replace("\\", "_")
    matches = [f for f in glob.glob(pj(VOICE_PROFILES_DIR, f"{safe}.*")) if Path(f).is_file()]
    return matches[0] if matches else None


def _save_voice_original(name: str, audio_bytes: bytes, ext: str) -> None:
    """Persist the original uploaded audio to the PVC for later download."""
    safe = bn(name).replace("/", "_").replace("\\", "_")
    dest_dir = VOICE_PROFILES_DIR
    try:
        makedirs(dest_dir, exist_ok=True)
        dest = pj(dest_dir, f"{safe}{ext}")
        with open(dest, "wb") as f:
            f.write(audio_bytes)
        logger.info("Saved voice original '%s' -> %s (%d bytes)", name, dest, len(audio_bytes))
    except OSError as e:
        logger.warning("Could not save voice original for '%s': %s", name, e)


@app.get("/api/voices")
async def list_voices(
    request: Request,
    tts_base_url: str = Query(default=""),
) -> JSONResponse:
    """List available and uploaded voices from the TTS endpoint.

    Each uploaded voice is annotated with ``has_original: true/false``
    indicating whether the original audio sample is available for download
    from this app's PVC.
    """
    base, key = _resolve_tts_endpoint(tts_base_url, _bearer_key(request))
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
            resp = await client.get(f"{base}/v1/audio/voices", headers=headers)
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach TTS endpoint: {e}")
    data = resp.json()
    # Annotate uploaded voices with availability of the original audio.
    if isinstance(data, dict) and isinstance(data.get("uploaded_voices"), list):
        for v in data["uploaded_voices"]:
            if isinstance(v, dict):
                vname = v.get("name") or v.get("voice_name_lower") or ""
                v["has_original"] = _voice_original_path(vname) is not None
    return JSONResponse(status_code=resp.status_code, content=data)


@app.post("/api/voices")
async def upload_voice(
    request: Request,
    tts_base_url: str = Form(default=""),
    name: str = Form(...),
    consent: str = Form(...),
    ref_text: str = Form(default=""),
    speaker_description: str = Form(default=""),
    audio_sample: UploadFile = File(...),
):
    """Upload a voice sample (file) to the TTS endpoint for voice cloning.

    The original audio file is also saved to the app's PVC
    (``{AUDIO_DIR}/voice_profiles/{name}{ext}``) so it can be downloaded
    later from the voice-profiles page.
    """
    base, key = _resolve_tts_endpoint(tts_base_url, _bearer_key(request))
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    audio_bytes = await audio_sample.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Audio sample is empty")
    # vLLM-Omni enforces a 10MB cap on uploaded voice samples.
    if len(audio_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Audio sample exceeds 10MB limit")
    filename = audio_sample.filename or "sample.webm"
    mimetype = _normalize_audio_mimetype(filename, audio_sample.content_type or "")
    if not mimetype:
        allowed = ", ".join(sorted(_TTS_ALLOWED_AUDIO_MIMETYPES))
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported audio format for '{filename}'. Allowed: {allowed}",
        )
    # Convert to 16kHz mono WAV — some TTS endpoints (e.g. fish s2 pro) list
    # audio/webm as accepted but their decoder (pydub) can't open webm/opus
    # containers. WAV is universally decodable and ideal for voice cloning.
    ext = splitext(filename)[1].lower() or ".webm"
    temp_dir = tempfile.mkdtemp()
    try:
        temp_in = pj(temp_dir, f"sample{ext}")
        with open(temp_in, "wb") as f:
            f.write(audio_bytes)
        wav_bytes, _ = convert_audio_to_wav(temp_in)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not convert audio to WAV: {e}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    # Save the original audio to the PVC so it can be downloaded later.
    _save_voice_original(name, audio_bytes, ext)

    files = {
        "audio_sample": (
            splitext(filename)[0] + ".wav",
            wav_bytes,
            "audio/wav",
        )
    }
    data: dict[str, str] = {"name": name, "consent": consent}
    if ref_text:
        data["ref_text"] = ref_text
    if speaker_description:
        data["speaker_description"] = speaker_description
    try:
        async with httpx.AsyncClient(verify=False, timeout=120.0) as client:
            resp = await client.post(f"{base}/v1/audio/voices", headers=headers, files=files, data=data)
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach TTS endpoint: {e}")
    return JSONResponse(status_code=resp.status_code, content=resp.json())


@app.delete("/api/voices/{name}")
async def delete_voice(
    name: str,
    request: Request,
    tts_base_url: str = Query(default=""),
):
    """Delete an uploaded voice profile from the TTS endpoint.

    Also removes the saved original audio from the app's PVC, if present.
    """
    base, key = _resolve_tts_endpoint(tts_base_url, _bearer_key(request))
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
            resp = await client.delete(f"{base}/v1/audio/voices/{name}", headers=headers)
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach TTS endpoint: {e}")
    # Best-effort: remove the saved original audio from the PVC so it
    # doesn't linger as an orphan after the voice profile is deleted.
    orig_path = _voice_original_path(name)
    if orig_path:
        try:
            remove(orig_path)
            logger.info("Removed voice original '%s' -> %s", name, orig_path)
        except OSError as e:
            logger.warning("Could not remove voice original '%s': %s", name, e)
    return JSONResponse(status_code=resp.status_code, content=resp.json())


@app.get("/api/voices/{name}/original")
async def download_voice_original(name: str) -> FileResponse:
    """Download the original audio sample saved for voice ``name``.

    Returns 404 if no saved original exists for that voice.
    """
    path = _voice_original_path(name)
    if not path:
        raise HTTPException(status_code=404, detail=f"No saved original audio for voice '{name}'.")
    return FileResponse(
        path=path,
        media_type="application/octet-stream",
        filename=bn(path),
    )


@app.post("/api/voices/test")
async def test_voice(request: Request):
    """Synthesize a short preview clip with a given voice (for the UI play button)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    base, key = _resolve_tts_endpoint(body.get("ttsBaseUrl", ""), _bearer_key(request))
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload: dict[str, Any] = {
        "input": body.get("input", "Hello, this is a voice profile preview."),
        "voice": body.get("voice", "vivian"),
        "response_format": body.get("response_format", "wav"),
    }
    model = body.get("ttsModelName")
    if not model and base:
        # Auto-discover from the TTS endpoint (mirrors BaseModel). This lets
        # the UI omit the model name entirely -- the server resolves it via
        # the OpenAI /v1/models listing, same as Open WebUI.
        model = discover_model_name(f"{base}/v1", key)
    if model:
        payload["model"] = model
    task_type = body.get("task_type")
    if task_type:
        payload["task_type"] = task_type
    try:
        async with httpx.AsyncClient(verify=False, timeout=120.0) as client:
            resp = await client.post(f"{base}/v1/audio/speech", headers=headers, json=payload)
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach TTS endpoint: {e}")
    if resp.status_code != 200:
        try:
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except Exception:
            raise HTTPException(status_code=resp.status_code, detail=resp.text[:500] or "TTS error")
    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "audio/wav"),
    )


@app.post("/api/transcribe-mp3")
async def transcribe_audio_file(
    file: UploadFile = File(...),
    num_speakers: int = 1,
    language: str = Form(default=""),
):
    """
    Transcribe an audio file with speaker diarization - supports multiple formats.
    Uses pyannote community-1 for diarization, then transcribes each speaker segment.
    When diarization is disabled/unavailable, falls back to whole-file transcription.
    """
    original_format = get_audio_format_safe(file.filename)
    if not original_format:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unsupported file format. Supported: {', '.join(SUPPORTED_AUDIO_FORMATS)}"},
        )

    temp_dir = tempfile.mkdtemp()
    temp_audio_path = pj(temp_dir, f"audio.{original_format}")
    temp_wav_path = pj(temp_dir, "audio.wav")

    try:
        with open(temp_audio_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        logger.info(f"Processing {original_format.upper()} file: {file.filename}")

        wav_data, duration = convert_audio_to_wav(temp_audio_path)
        logger.info(f"Converted to WAV: {len(wav_data)} bytes, duration: {duration:.1f}s")

        with open(temp_wav_path, "wb") as f:
            f.write(wav_data)

        with wave.open(temp_wav_path, "rb") as wf:
            sample_rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
        np_audio = np.frombuffer(frames, dtype=np.int16)

        diarized_segments: list | None = None
        diarization_used = False
        effective_speakers = num_speakers
        if num_speakers > 1:
            try:
                from utils.diarization_client import diarize_audio

                base_url = environ.get(
                    "DIARIZATION_BASE_URL",
                    "http://conversation-toolbox-diarization:8001",
                )
                diarization_result = await diarize_audio(temp_wav_path, num_speakers=num_speakers, base_url=base_url)
                diarized_segments = diarization_result.exclusive_segments or diarization_result.segments
                diarization_used = True
                logger.info(
                    f"Diarization: {len(diarized_segments)} segments, "
                    f"{diarization_result.num_speakers_detected} speakers detected"
                )
            except Exception as e:
                logger.warning(f"Diarization unavailable ({e}); falling back to whole-file transcription")
                diarized_segments = None
                effective_speakers = 1

        config = ConversationSession.current_config
        asr_language = language or config.get("language", "") or "en"
        asr = VoiceModel(
            model_name=config.get("asrModelName", ""),
            url_remote=config["asrBaseUrl"],
            api_key=config.get("asrApiKey", ""),
            model_usage="remote" if config.get("remote") else "local",
        )
        asr_function = asr.asr_async_function

        transcripts: list[dict[str, Any]] = []
        if diarized_segments:
            for seg in diarized_segments:
                start_sample = int(seg.start * sample_rate)
                end_sample = int(seg.end * sample_rate)
                end_sample = min(end_sample, len(np_audio))
                segment_data = np_audio[start_sample:end_sample].tobytes()
                if len(segment_data) < sample_rate * 0.3:
                    continue
                try:
                    segment_buffer = audio_bytes_to_wave_bytesio(segment_data, ConversationSession.sample_rate)
                    asr_kwargs: dict[str, Any] = {"file": segment_buffer}
                    if asr_language:
                        asr_kwargs["language"] = asr_language
                    result = await asr_function(**asr_kwargs)
                    text = extract_asr_text(result)
                    if text and len(text) > 2:
                        transcripts.append(
                            {
                                "speaker": seg.speaker,
                                "start": seg.start,
                                "end": seg.end,
                                "text": text,
                            }
                        )
                except Exception:
                    logger.exception("Segment transcription error")
                    continue
        else:
            try:
                segment_buffer = audio_bytes_to_wave_bytesio(np_audio.tobytes(), ConversationSession.sample_rate)
                asr_kwargs = {"file": segment_buffer}
                if asr_language:
                    asr_kwargs["language"] = asr_language
                result = await asr_function(**asr_kwargs)
                text = extract_asr_text(result)
                if text and len(text) > 2:
                    transcripts.append(
                        {
                            "speaker": "SPEAKER_00",
                            "start": 0.0,
                            "end": duration,
                            "text": text,
                        }
                    )
            except Exception:
                logger.exception("Whole-file transcription error")

        transcript_lines = []
        for t in transcripts:
            start_str = format_timestamp(t["start"])
            end_str = format_timestamp(t["end"])
            line = f"[{start_str} - {end_str}] {t['speaker']}: {t['text']}"
            transcript_lines.append(line)
        transcript_text = "\n".join(transcript_lines)

        original_name = file.filename or "mp3"
        if "." in original_name:
            base_name = original_name.rsplit(".", 1)[0]
        else:
            base_name = original_name

        transcript_filename = f"{base_name}_transcript.txt"
        counter = 1
        while exists(transcript_filename):
            transcript_filename = pj(TRANSCRIPTS_DIR, f"{base_name}_transcript_{counter}.txt")
            counter += 1

        with open(transcript_filename, "w", encoding="utf-8") as f:
            f.write(f"=== Audio Transcription: {file.filename} ===\n")
            f.write(f"=== Original Format: {original_format.upper()} ===\n")
            f.write("=== Converted to: 16kHz Mono WAV ===\n")
            f.write(f"=== Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            f.write(f"=== Estimated Speakers: {effective_speakers} ===\n")
            f.write(f"=== Duration: {duration:.1f} seconds ===\n\n")
            f.write(transcript_text)
            f.write("\n\n=== End of Transcription ===\n")

        return {
            "status": "success",
            "transcript_path": transcript_filename,
            "transcript": transcript_text,
            "segments": transcripts,
            "speaker_count": len({s["speaker"] for s in transcripts}),
            "duration_seconds": duration,
            "original_format": original_format,
            "requested_speakers": num_speakers,
            "diarization_used": diarization_used,
        }
    finally:
        try:
            shutil.rmtree(temp_dir)
        except Exception as e:
            logger.warning(f"Failed to clean up temp dir {temp_dir}: {e}")


@app.post("/api/transcribe-single-optimized")
async def transcribe_single_optimized(
    file: UploadFile = File(...),
    num_speakers: int = 1,
    max_concurrent: int = 5,
    language: str = Form(default=""),
):
    """
    Optimized single file transcription with parallel segment processing.
    Uses pyannote community-1 for diarization, asyncio.gather for concurrent ASR.
    When diarization is disabled/unavailable, falls back to whole-file transcription.
    """
    original_format = get_audio_format_safe(file.filename)
    if not original_format:
        raise HTTPException(status_code=400, detail="Unsupported format")

    temp_dir = tempfile.mkdtemp()
    temp_audio_path = pj(temp_dir, f"audio.{original_format}")
    temp_wav_path = pj(temp_dir, "audio.wav")

    try:
        with open(temp_audio_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        wav_data, duration = convert_audio_to_wav(temp_audio_path)
        with open(temp_wav_path, "wb") as f:
            f.write(wav_data)

        diarized_segments: list | None = None
        diarization_used = False
        if num_speakers > 1:
            try:
                from utils.diarization_client import diarize_audio

                base_url = environ.get(
                    "DIARIZATION_BASE_URL",
                    "http://conversation-toolbox-diarization:8001",
                )
                diarization_result = await diarize_audio(temp_wav_path, num_speakers=num_speakers, base_url=base_url)
                diarized_segments = diarization_result.exclusive_segments or diarization_result.segments
                diarization_used = True
                logger.info(
                    f"Diarization: {len(diarized_segments)} segments, "
                    f"{diarization_result.num_speakers_detected} speakers detected"
                )
            except Exception as e:
                logger.warning(f"Diarization unavailable ({e}); falling back to whole-file transcription")
                diarized_segments = None

        # Load WAV for segment extraction
        with wave.open(temp_wav_path, "rb") as wf:
            sample_rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
        np_audio = np.frombuffer(frames, dtype=np.int16)

        # Build audio segments with speaker labels
        if diarized_segments:
            audio_segments = []
            for seg in diarized_segments:
                start_sample = int(seg.start * sample_rate)
                end_sample = int(seg.end * sample_rate)
                end_sample = min(end_sample, len(np_audio))
                segment_data = np_audio[start_sample:end_sample].tobytes()
                if len(segment_data) < sample_rate * 0.3:
                    continue
                audio_buffer = audio_bytes_to_wave_bytesio(segment_data, sample_rate)
                audio_segments.append(
                    {
                        "speaker": seg.speaker,
                        "start": seg.start,
                        "end": seg.end,
                        "buffer": audio_buffer,
                    }
                )
        else:
            audio_segments = [
                {
                    "speaker": "SPEAKER_00",
                    "start": 0.0,
                    "end": duration,
                    "buffer": audio_bytes_to_wave_bytesio(np_audio.tobytes(), sample_rate),
                }
            ]

        config = ConversationSession.current_config
        asr_language = language or config.get("language", "") or "en"
        asr = VoiceModel(
            model_name=config.get("asrModelName", ""),
            url_remote=config["asrBaseUrl"],
            api_key=config.get("asrApiKey", ""),
            model_usage="remote" if config.get("remote") else "local",
        )
        asr_function = asr.asr_async_function

        transcripts: list[dict[str, Any]] = []
        semaphore = asyncio.Semaphore(max_concurrent)

        async def process_segment(seg: dict, idx: int) -> dict | None:
            async with semaphore:
                try:
                    asr_kwargs = {"file": seg["buffer"]}
                    if asr_language:
                        asr_kwargs["language"] = asr_language
                    result = await asyncio.wait_for(asr_function(**asr_kwargs), timeout=30.0)
                    text = result.text.strip() if hasattr(result, "text") else str(result).strip()
                    if text and len(text) > 2:
                        return {
                            "speaker": seg["speaker"],
                            "start": seg["start"],
                            "end": seg["end"],
                            "text": text,
                        }
                    return None
                except Exception:
                    logger.exception(f"Segment {idx} error")
                    return None

        tasks = [process_segment(seg, i) for i, seg in enumerate(audio_segments)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, dict):
                transcripts.append(result)

        transcripts.sort(key=lambda x: x["start"])

        transcript_lines = []
        for t in transcripts:
            start_str = format_timestamp(t["start"])
            end_str = format_timestamp(t["end"])
            line = f"[{start_str} - {end_str}] {t['speaker']}: {t['text']}"
            transcript_lines.append(line)
        transcript_text = "\n".join(transcript_lines)

        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        transcript_filename = pj(TRANSCRIPTS_DIR, f"single_{timestamp_str}.txt")
        with open(transcript_filename, "w", encoding="utf-8") as out_f:
            out_f.write(f"=== Audio Transcription: {file.filename} ===\n")
            out_f.write(f"=== Duration: {duration:.1f} seconds ===\n\n")
            out_f.write(transcript_text)

        return {
            "status": "success",
            "transcript_path": transcript_filename,
            "transcript": transcript_text,
            "segments": transcripts,
            "speaker_count": len({t["speaker"] for t in transcripts}),
            "duration_seconds": duration,
            "requested_speakers": num_speakers,
            "diarization_used": diarization_used,
        }
    finally:
        try:
            shutil.rmtree(temp_dir)
        except Exception as e:
            logger.warning(f"Failed to clean up temp dir {temp_dir}: {e}")


@app.get("/api/transcript/{filename}")
async def get_transcript(filename: str):
    """Download a transcript file"""
    transcript_path = pj("transcripts", filename)
    if not exists(transcript_path):
        raise HTTPException(status_code=404, detail="Transcript not found")
    with open(transcript_path, "r", encoding="utf-8") as f:
        content = f.read()
    return {"filename": filename, "content": content}


@app.post("/api/multi-user/create-session")
async def create_multi_user_session():
    """Create a new multi-user session and return the session ID"""
    session = multi_user_manager.create_session()
    return {
        "session_id": session.session_id,
        "transcript_path": session.transcript_path,
    }


# ==============================================================================
# Batch Transcription API Endpoints (modified to use Redis-backed queue)
# ==============================================================================

# Staging directory for uploaded files awaiting background processing.
# Lives on the PVC so files survive pod restarts.
_STAGED_DIR = pj(TRANSCRIPTS_DIR, "_staged")
makedirs(_STAGED_DIR, exist_ok=True)

# Background processor state. The processor is a continuous loop that
# drains the staging directory one file at a time. It starts on the first
# upload and exits when staging is empty. Subsequent uploads while the
# processor is running just add files to staging — the loop picks them up.
_batch_processing: dict[str, Any] = {
    "active": False,
    "num_speakers": 1,
    "language": "",
    "webhook_url": None,
    "processed": 0,
    "errors": [],
}

# Max concurrent file-processing tasks (WAV convert + split + enqueue).
# Each file holds ~57MB PCM in memory during processing, so 3 concurrent
# ≈ 171MB peak — well within the app pod's 4Gi limit.
_BATCH_PROCESS_CONCURRENCY = 3


async def _drain_staged_files():
    """Background loop: process staged files concurrently.

    Pipeline:
      1. Split up to 3 files concurrently (WAV convert + chunk).
      2. Enqueue each job immediately — no backpressure needed because
         the worker uses lazy audio loading (only 1 segment in memory
         per concurrent ASR call, ~2MB each). Even 20 queued jobs cost
         zero memory until the worker pulls them.
      3. HPA scales worker pods based on memory when the worker
         genuinely needs more capacity.

    Started by the first /api/batch/upload call. Runs concurrently with
    subsequent uploads — files appear in staging while earlier files are
    being WAV-converted and split.
    """
    import glob

    logger.info(f"Background processor started (split concurrency={_BATCH_PROCESS_CONCURRENCY})")
    sem = asyncio.Semaphore(_BATCH_PROCESS_CONCURRENCY)

    async def _process_one(staged_path: str) -> None:
        async with sem:
            staged_name = bn(staged_path)
            try:
                original_name = staged_name.split("_", 1)[1] if "_" in staged_name else staged_name
                original_format = get_audio_format_safe(original_name) or "mp3"
                if file_processor is None or batch_transcription_queue is None:
                    raise RuntimeError("Batch system not initialised")
                job = await file_processor.process_file(
                    staged_path,
                    original_format,
                    _batch_processing["num_speakers"],
                    _batch_processing["language"],
                )
                if _batch_processing["webhook_url"]:
                    job.metadata["webhook_url"] = _batch_processing["webhook_url"]
                await batch_transcription_queue.add_job(job)
                logger.info(
                    f"Background processor: enqueued job {job.job_id} "
                    f"for {original_name} ({job.total_segments} segments)"
                )
            except Exception as e:
                logger.exception(f"Background processor failed for {staged_name}")
                _batch_processing["errors"].append({"filename": staged_name, "error": str(e)})
            finally:
                try:
                    remove(staged_path)
                except Exception:
                    pass
                _batch_processing["processed"] += 1

    while True:
        staged_files = sorted(glob.glob(pj(_STAGED_DIR, "*")))
        if not staged_files:
            break
        await asyncio.gather(*[_process_one(p) for p in staged_files])

    _batch_processing["active"] = False
    logger.info(
        f"Background processor idle — staging empty "
        f"({_batch_processing['processed']} processed, {len(_batch_processing['errors'])} errors)"
    )


@app.post("/api/batch/upload")
async def batch_upload(
    files: list[UploadFile] = File(...),
    num_speakers: int = Form(1),
    language: str = Form(default=""),
    webhook_url: str | None = Form(None),
):
    """
    Stage uploaded files to the PVC and kick off background processing.

    Pipeline: each file is saved to the staging directory immediately
    (~1s for 30MB). If the background processor isn't running, it starts
    now — processing file 1 while the frontend uploads file 2, 3, etc.
    The processor loops until staging is empty, so files uploaded later
    are picked up automatically.
    """
    staged = []
    errors = []
    for file in files:
        try:
            original_format = get_audio_format_safe(file.filename)
            if not original_format:
                errors.append(
                    {
                        "filename": file.filename or "unknown",
                        "error": "Unsupported or missing file format",
                    }
                )
                continue
            original_name = file.filename or f"audio.{original_format}"
            staged_name = f"{uuid.uuid4().hex[:8]}_{original_name}"
            staged_path = pj(_STAGED_DIR, staged_name)
            with open(staged_path, "wb") as f:
                shutil.copyfileobj(file.file, f)
            staged.append(staged_name)
            logger.info(f"Staged upload: {staged_name} ({original_format})")
        except Exception as e:
            logger.exception(f"Failed to stage {file.filename}")
            errors.append({"filename": file.filename or "unknown", "error": str(e)})

    # Start background processor if not already running. The first upload
    # starts it; subsequent uploads just add files to staging and the
    # existing loop picks them up.
    if not _batch_processing["active"] and staged:
        _batch_processing["active"] = True
        _batch_processing["num_speakers"] = num_speakers
        _batch_processing["language"] = language
        _batch_processing["webhook_url"] = webhook_url
        _batch_processing["processed"] = 0
        _batch_processing["errors"] = []
        asyncio.create_task(_drain_staged_files())

    return {
        "status": "staged",
        "staged_files": staged,
        "total_staged": len(staged),
        "errors": errors,
    }


@app.get("/api/batch/process/status")
async def batch_process_status():
    """Poll to track background processing progress."""
    return {
        "active": _batch_processing["active"],
        "processed": _batch_processing["processed"],
        "errors": _batch_processing["errors"],
    }


@app.get("/api/batch/stream")
async def batch_status_stream(request: Request):
    """
    Server-Sent Events endpoint for real-time job status updates.
    Now reads from Redis pub/sub channel 'batch:status'.
    """

    async def event_generator():
        r = await RedisClient.get_client()
        pubsub = r.pubsub()
        await pubsub.subscribe("batch:status")
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await pubsub.get_message(timeout=5.0, ignore_subscribe_messages=True)
                    if message and message["type"] == "message":
                        payload = message["data"]
                        yield f"event: status_update\ndata: {payload}\n\n"
                except TimeoutError:
                    # Send heartbeat to keep connection alive
                    yield ":\n\n"
        finally:
            await pubsub.unsubscribe("batch:status")
            await pubsub.close()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/batch/status/{job_id}", response_model=BatchStatusResponse)
async def get_batch_status(job_id: str):
    """Get status of a specific batch job."""
    if batch_transcription_queue is None:
        raise HTTPException(status_code=500, detail="Batch system not initialised")
    job = await batch_transcription_queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return BatchStatusResponse(
        job_id=job.job_id,
        status=job.status.value,
        progress_percent=job.progress_percent,
        total_segments=job.total_segments,
        completed_segments=job.completed_segments,
        failed_segments=job.failed_segments,
        file_name=job.file_name,
        error=job.error if job.status == JobStatus.FAILED else None,
        transcript_path=job.transcript_path if job.status == JobStatus.COMPLETED else None,
        requested_speakers=job.num_speakers,
        diarization_used=job.metadata.get("diarization_used"),
    )


@app.get("/api/batch/status", response_model=BatchListResponse)
async def list_batch_jobs(status_filter: str | None = None, limit: int = 100, offset: int = 0):
    if batch_transcription_queue is None:
        raise HTTPException(status_code=500, detail="Batch system not initialised")
    all_jobs = await batch_transcription_queue.list_jobs()
    if status_filter:
        all_jobs = [j for j in all_jobs if j.status.value == status_filter]
    all_jobs.sort(key=lambda j: j.created_at, reverse=True)
    total = len(all_jobs)
    jobs = all_jobs[offset : offset + limit]

    pending = sum(1 for j in all_jobs if j.status == JobStatus.PENDING)
    processing = sum(1 for j in all_jobs if j.status == JobStatus.PROCESSING)
    completed = sum(1 for j in all_jobs if j.status == JobStatus.COMPLETED)
    failed = sum(1 for j in all_jobs if j.status == JobStatus.FAILED)

    return BatchListResponse(
        jobs=[
            BatchStatusResponse(
                job_id=j.job_id,
                status=j.status.value,
                progress_percent=j.progress_percent,
                total_segments=j.total_segments,
                completed_segments=j.completed_segments,
                failed_segments=j.failed_segments,
                file_name=j.file_name,
                error=j.error if j.status == JobStatus.FAILED else None,
                transcript_path=j.transcript_path if j.status == JobStatus.COMPLETED else None,
                requested_speakers=j.num_speakers,
                diarization_used=j.metadata.get("diarization_used"),
            )
            for j in jobs
        ],
        total=total,
        pending=pending,
        processing=processing,
        completed=completed,
        failed=failed,
    )


@app.get("/api/batch/transcript/{job_id}")
async def get_batch_transcript(job_id: str):
    """Get the transcript for a completed job."""
    if batch_transcription_queue is None:
        raise HTTPException(status_code=500, detail="Batch system not initialised")
    job = await batch_transcription_queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.COMPLETED:
        raise HTTPException(
            status_code=400,
            detail=f"Job not completed. Status: {job.status.value}",
        )
    return {
        "job_id": job.job_id,
        "file_name": job.file_name,
        "transcript": job.final_transcript,
        "segments": [s.to_dict() for s in job.segments],
    }


@app.get("/api/batch/transcripts/files")
async def list_saved_transcripts():
    """List all transcript .txt files saved on the PVC.

    Survives Redis job data eviction (arq job_result_keep=3600 deletes
    job results after 1 hour). The .txt files persist on the PVC until
    manually deleted, so this endpoint is the reliable way to browse
    past transcriptions.
    """
    import glob

    files = []
    for path in sorted(glob.glob(pj(TRANSCRIPTS_DIR, "*_transcript*.txt")), reverse=True):
        p = Path(path)
        stat = p.stat()
        files.append(
            {
                "filename": p.name,
                "size_bytes": stat.st_size,
                "size_kb": round(stat.st_size / 1024, 1),
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            }
        )
    return {"files": files, "total": len(files)}


@app.get("/api/batch/transcripts/files/{filename}")
async def download_saved_transcript(filename: str):
    """Download a specific transcript .txt file from the PVC."""
    # Prevent path traversal — only allow filenames, no slashes
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    file_path = pj(TRANSCRIPTS_DIR, filename)
    if not exists(file_path):
        raise HTTPException(status_code=404, detail="Transcript file not found")
    return FileResponse(
        file_path,
        media_type="text/plain",
        filename=filename,
    )


@app.delete("/api/batch/transcripts/files/{filename}")
async def delete_saved_transcript(filename: str):
    """Delete a transcript .txt file from the PVC."""
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    file_path = pj(TRANSCRIPTS_DIR, filename)
    if not exists(file_path):
        raise HTTPException(status_code=404, detail="Transcript file not found")
    remove(file_path)
    return {"status": "deleted", "filename": filename}


@app.delete("/api/batch/transcripts/files")
async def delete_all_saved_transcripts():
    """Delete all transcript .txt files from the PVC."""
    import glob

    deleted = []
    for path in glob.glob(pj(TRANSCRIPTS_DIR, "*_transcript*.txt")):
        remove(path)
        deleted.append(Path(path).name)
    return {"status": "deleted_all", "count": len(deleted)}


@app.post("/api/batch/cancel/{job_id}")
async def cancel_batch_job(job_id: str):
    """Cancel a pending or processing job."""
    if batch_transcription_queue is None:
        raise HTTPException(status_code=500, detail="Batch system not initialised")
    result = await batch_transcription_queue.cancel_job(job_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.delete("/api/batch/{job_id}")
async def delete_batch_job(job_id: str):
    """Delete a batch job and its associated data."""
    if batch_transcription_queue is None:
        raise HTTPException(status_code=500, detail="Batch system not initialised")
    result = await batch_transcription_queue.delete_job(job_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/api/batch/restart/{job_id}")
async def restart_stuck_job(job_id: str):
    """Restart a stuck job by resetting pending segments."""
    if batch_transcription_queue is None:
        raise HTTPException(status_code=500, detail="Batch system not initialised")
    result = await batch_transcription_queue.restart_stuck_job(job_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/api/batch/force-complete/{job_id}")
async def force_complete_job(job_id: str):
    """Force complete a job even if some segments failed."""
    if batch_transcription_queue is None:
        raise HTTPException(status_code=500, detail="Batch system not initialised")
    result = await batch_transcription_queue.force_complete_job(job_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/api/batch/cleanup")
async def cleanup_stuck_jobs():
    """Find and restart all jobs with stuck segments."""
    if batch_transcription_queue is None:
        raise HTTPException(status_code=500, detail="Batch system not initialised")
    return await batch_transcription_queue.cleanup_all_stuck_jobs()


@app.get("/api/batch/stats")
async def get_queue_stats():
    """Get overall queue statistics including memory info."""
    if batch_transcription_queue is None:
        raise HTTPException(status_code=500, detail="Batch system not initialised")
    return await batch_transcription_queue.get_job_stats()


# ==============================================================================
# Memory Monitoring Endpoints (NEW)
# ==============================================================================
@app.get("/api/batch/memory-stats")
async def get_batch_memory_stats():
    """
    Get current memory statistics for the batch transcription system.
    """
    if batch_transcription_queue and hasattr(batch_transcription_queue, "get_memory_stats"):
        return batch_transcription_queue.get_memory_stats()
    try:
        return get_memory_stats()
    except Exception:
        vm = psutil.virtual_memory()
        process = psutil.Process()
        return {
            "process_rss_mb": round(process.memory_info().rss / (1024 * 1024), 2),
            "process_vms_mb": round(process.memory_info().vms / (1024 * 1024), 2),
            "system_available_mb": round(vm.available / (1024 * 1024), 2),
            "system_used_mb": round(vm.used / (1024 * 1024), 2),
            "system_percent": round(vm.percent, 1),
            "system_total_mb": round(vm.total / (1024 * 1024), 2),
            "memory_pressure": "high" if vm.percent > 80 else "normal",
        }


@app.get("/api/system/memory")
async def get_system_memory():
    """
    Get system-wide memory statistics.
    Useful for monitoring the pod's overall memory health.
    """
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    process = psutil.Process()
    return {
        "virtual_memory": {
            "total_mb": round(vm.total / (1024 * 1024), 2),
            "available_mb": round(vm.available / (1024 * 1024), 2),
            "used_mb": round(vm.used / (1024 * 1024), 2),
            "percent": round(vm.percent, 1),
            "free_mb": round(vm.free / (1024 * 1024), 2),
        },
        "swap_memory": {
            "total_mb": round(swap.total / (1024 * 1024), 2),
            "used_mb": round(swap.used / (1024 * 1024), 2),
            "percent": round(swap.percent, 1),
        },
        "process": {
            "pid": process.pid,
            "rss_mb": round(process.memory_info().rss / (1024 * 1024), 2),
            "vms_mb": round(process.memory_info().vms / (1024 * 1024), 2),
            "percent_of_system": round(process.memory_percent(), 2),
        },
    }


# ==============================================================================
# Logging filter for health checks (reduce noise)
# ==============================================================================
class HealthCheckFilter(logging.Filter):
    HEALTH_PATHS: ClassVar[set[str]] = {"/health", "/ready"}

    def filter(self, record):
        if hasattr(record, "msg") and isinstance(record.msg, str):
            for path in self.HEALTH_PATHS:
                if f'"GET {path} HTTP' in record.msg:
                    return False
        return True


uvicorn_access = logging.getLogger("uvicorn.access")
uvicorn_access.addFilter(HealthCheckFilter())


# ==============================================================================
# Main Entry Point
# ==============================================================================
if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        ws_max_size=50 * 1024 * 1024,
        ws_max_queue=128,
        ws_ping_interval=30,
        ws_ping_timeout=10,
        loop="asyncio",
    )
