import asyncio
import io
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import traceback
import wave
import zipfile
import uuid
from pathlib import Path
from typing import Any
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
import torch
from dotenv import load_dotenv
from fastapi import (
    Body,
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

_ = load_dotenv()

TORCH_NUM_THREADS = max(1, int(os.getenv("TORCH_NUM_THREADS", "1")))
TORCH_NUM_INTEROP_THREADS = max(1, int(os.getenv("TORCH_NUM_INTEROP_THREADS", "1")))

try:
    torch.set_num_threads(TORCH_NUM_THREADS)
except Exception:
    pass

try:
    torch.set_num_interop_threads(TORCH_NUM_INTEROP_THREADS)
except Exception:
    pass

# ---------------------------
# Defaults from environment
# ---------------------------
DEFAULT_WHISPER_BASE_URL = os.getenv("WHISPER_BASE_URL", "")
DEFAULT_WHISPER_API_KEY = os.getenv("WHISPER_API_KEY", "")
DEFAULT_WHISPER_MODEL = os.getenv("WHISPER_MODEL", "openai/whisper-large-v3-turbo")

DEFAULT_LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
DEFAULT_LLM_API_KEY = os.getenv("LLM_API_KEY", "")
DEFAULT_LLM_MODEL = os.getenv("LLM_MODEL", "Qwen/Qwen3-30B-A3B-Instruct-2507-FP8")

DEFAULT_SOURCE_LANGUAGE = os.getenv("SOURCE_LANGUAGE", "en")
DEFAULT_TARGET_LANGUAGE = os.getenv("TARGET_LANGUAGE", "es")

EXPORT_CHUNK_SECONDS = int(os.getenv("EXPORT_CHUNK_SECONDS", "30"))
EXPORT_OVERLAP_SECONDS = int(os.getenv("EXPORT_OVERLAP_SECONDS", "5"))
EXPORT_MAX_ASR_CONCURRENCY = max(1, int(os.getenv("EXPORT_MAX_ASR_CONCURRENCY", "6")))
EXPORT_TEXT_BATCH_SEGMENTS = max(1, int(os.getenv("EXPORT_TEXT_BATCH_SEGMENTS", "20")))
EXPORT_TEXT_BATCH_CHARS = max(1000, int(os.getenv("EXPORT_TEXT_BATCH_CHARS", "5000")))
EXPORT_VAD_PAD_MS = max(0, int(os.getenv("EXPORT_VAD_PAD_MS", "250")))
EXPORT_VAD_MERGE_GAP_MS = max(0, int(os.getenv("EXPORT_VAD_MERGE_GAP_MS", "400")))
EXPORT_VAD_MAX_UNIT_SECONDS = max(
    4, int(os.getenv("EXPORT_VAD_MAX_UNIT_SECONDS", "15"))
)

EXPORT_JOB_TTL_SECONDS = int(os.getenv("EXPORT_JOB_TTL_SECONDS", "3600"))
RECORDING_STORAGE_ROOT = (
    Path(tempfile.gettempdir()) / "realtime-live-voice-translation-recordings"
)
RECORDING_STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
EXPORT_JOBS: dict[str, dict[str, Any]] = {}
EXPORT_JOBS_LOCK = asyncio.Lock()
ROOMS: dict[str, dict[str, Any]] = {}
ROOMS_LOCK = asyncio.Lock()

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    return {"status": "ready"}


@app.get("/defaults")
def defaults():
    return {
        "src": DEFAULT_SOURCE_LANGUAGE,
        "tgt": DEFAULT_TARGET_LANGUAGE,
        "whisper": {
            "base_url": DEFAULT_WHISPER_BASE_URL,
            "model": DEFAULT_WHISPER_MODEL,
            "has_api_key": bool(DEFAULT_WHISPER_API_KEY),
        },
        "llm": {
            "base_url": DEFAULT_LLM_BASE_URL,
            "model": DEFAULT_LLM_MODEL,
            "has_api_key": bool(DEFAULT_LLM_API_KEY),
        },
        "export": {
            "chunk_seconds": EXPORT_CHUNK_SECONDS,
            "overlap_seconds": EXPORT_OVERLAP_SECONDS,
            "max_asr_concurrency": EXPORT_MAX_ASR_CONCURRENCY,
            "vad_pad_ms": EXPORT_VAD_PAD_MS,
            "vad_merge_gap_ms": EXPORT_VAD_MERGE_GAP_MS,
            "vad_max_unit_seconds": EXPORT_VAD_MAX_UNIT_SECONDS,
        },
    }


code_to_language = {
    "ar": "Arabic",
    "da": "Danish",
    "en": "English",
    "es": "Spanish",
    "de": "German",
    "fi": "Finnish",
    "fr": "French",
    "hi": "Hindi",
    "it": "Italian",
    "ja": "Japanese",
    "km": "Khmer",
    "ms": "Malay",
    "no": "Norwegian",
    "pt": "Portuguese",
    "ru": "Russian",
    "th": "Thai",
    "tr": "Turkish",
    "vi": "Vietnamese",
    "zh": "Chinese",
}

SAMPLE_RATE = int(os.getenv("SAMPLE_RATE", "16000"))
# Silero VAD behavior
SPEECH_THRESHOLD = 0.5
LIVE_COMPLETE_SILENCE_MS = max(250, int(os.getenv("LIVE_SILENCE_THRESHOLD_MS", "1300")))
LIVE_INCOMPLETE_FINALIZE_MS = max(
    LIVE_COMPLETE_SILENCE_MS,
    int(os.getenv("LIVE_INCOMPLETE_FINALIZE_MS", "2200")),
)
MAX_SENTENCE_MS = max(3000, int(os.getenv("LIVE_MAX_SENTENCE_MS", "18000")))
MIN_SEND_TEXT_CHARS = 2
SILENCE_DBFS_THRESHOLD = -45.0
MIN_AUDIO_MS = 300
LIVE_PARTIAL_UPDATE_MS = max(250, int(os.getenv("LIVE_PARTIAL_UPDATE_MS", "750")))
LIVE_MIN_PARTIAL_AUDIO_MS = max(
    MIN_AUDIO_MS, int(os.getenv("LIVE_MIN_PARTIAL_AUDIO_MS", "700"))
)
LIVE_CONTEXT_TURNS = max(0, int(os.getenv("LIVE_CONTEXT_TURNS", "3")))


class TranscriptItem(BaseModel):
    original: str = ""
    translation: str = ""
    src: str = ""
    tgt: str = ""
    ts_ms: int | None = None


class ExportLlmConfig(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class ExportWhisperConfig(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class ExportRequest(BaseModel):
    transcript: list[TranscriptItem] = Field(default_factory=list)
    llm: ExportLlmConfig | None = None


class RoomExportRequest(BaseModel):
    target_language: str = DEFAULT_TARGET_LANGUAGE
    llm: ExportLlmConfig | None = None
    whisper: ExportWhisperConfig | None = None


class RoomCreateRequest(BaseModel):
    room_id: str | None = None


class RecordingControlRequest(BaseModel):
    client_session_id: str = ""


class RecordingFinalizeRequest(BaseModel):
    recording_session_id: str = ""


@app.post("/api/rooms")
async def create_room(payload: RoomCreateRequest | None = Body(default=None)):
    requested_room_id = None
    if payload is not None:
        requested_room_id = str(payload.room_id or "").strip().lower() or None
    room_id, _ = await get_or_create_room(requested_room_id)
    return JSONResponse({"room_id": room_id})


class ExportSegment(BaseModel):
    id: int
    start_s: float
    end_s: float
    start_label: str
    end_label: str
    original: str
    english: str = ""
    translations: dict[str, str] = Field(default_factory=dict)


class ExportChunkResult(BaseModel):
    chunk_id: int
    chunk_start_s: float
    chunk_end_s: float
    keep_from_s: float
    keep_to_s: float
    text: str = ""
    language: str = ""
    segments: list[dict[str, Any]] = Field(default_factory=list)


class MeetingPackageResult(BaseModel):
    languages: list[str]
    documents: list[dict[str, str]]
    segments: list[ExportSegment]


class JsonTranslationBatch(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)


def build_default_runtime_config() -> dict[str, Any]:
    return {
        "src": DEFAULT_SOURCE_LANGUAGE,
        "tgt": DEFAULT_TARGET_LANGUAGE,
        "whisper": {
            "base_url": DEFAULT_WHISPER_BASE_URL,
            "api_key": DEFAULT_WHISPER_API_KEY,
            "model": DEFAULT_WHISPER_MODEL,
        },
        "llm": {
            "base_url": DEFAULT_LLM_BASE_URL,
            "api_key": DEFAULT_LLM_API_KEY,
            "model": DEFAULT_LLM_MODEL,
        },
    }


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
        "src": cfg["src"],
        "presenter_tgt": cfg["tgt"],
        "whisper": dict(cfg["whisper"]),
        "llm": dict(cfg["llm"]),
        "segments": [],
        "segment_index": {},
        "connections": [],
        "recording_state": "idle",
        "recording_owner_session_id": "",
        "recording_session_id": "",
        "recording_chunk_dir": "",
        "recording_chunk_paths": [],
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
        room.get("recording_audio_bytes")
    )


def room_has_resumable_recording(room: dict[str, Any]) -> bool:
    return bool(room.get("recording_session_id")) and bool(
        room.get("recording_chunk_paths")
    )


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


def room_segments_to_transcript_items(room: dict[str, Any]) -> list[TranscriptItem]:
    presenter_tgt = (
        (room.get("presenter_tgt") or DEFAULT_TARGET_LANGUAGE).strip().lower()
    )
    items: list[TranscriptItem] = []
    for segment in room.get("segments") or []:
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
            conn
            for conn in room["connections"]
            if conn.get("websocket") is not websocket
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


# --- LOAD SILERO VAD ---
print("Loading Silero VAD...")
try:
    model, utils = torch.hub.load(
        repo_or_dir="/app/silero-vad", model="silero_vad", source="local"
    )
except Exception as e:
    print(f"Error: {e}")
    model, utils = torch.hub.load(
        repo_or_dir="snakers4/silero-vad",
        model="silero_vad",
        force_reload=False,
        trust_repo=True,
    )

(get_speech_timestamps, save_audio, read_audio, VADIterator, collect_chunks) = utils
model.eval()


def build_time_ranges_from_timestamps(
    items: list[TranscriptItem], timeline_start_s: float, timeline_end_s: float
) -> list[tuple[float, float]]:
    count = len(items)
    if count == 0:
        return []

    timeline_end_s = max(timeline_end_s, timeline_start_s)
    duration_s = max(0.0, timeline_end_s - timeline_start_s)
    if count == 1 or duration_s <= 0:
        return [(timeline_start_s, timeline_end_s)] * count

    ts_values = [item.ts_ms for item in items if item.ts_ms is not None]
    if len(ts_values) >= 2 and max(ts_values) > min(ts_values):
        first_ts = float(items[0].ts_ms or ts_values[0])
        centers: list[float] = []
        for idx, item in enumerate(items):
            raw_ts = float(
                item.ts_ms if item.ts_ms is not None else first_ts + idx * 1000
            )
            centers.append(raw_ts)
        rel = [max(0.0, center - centers[0]) / 1000.0 for center in centers]
        max_rel = max(rel) if rel else 0.0
        if max_rel > 0:
            scale = duration_s / max_rel
            centers_s = [timeline_start_s + value * scale for value in rel]
            boundaries = [timeline_start_s]
            for left, right in zip(centers_s, centers_s[1:]):
                boundaries.append((left + right) / 2.0)
            boundaries.append(timeline_end_s)
            ranges: list[tuple[float, float]] = []
            for idx in range(count):
                start_s = boundaries[idx]
                end_s = boundaries[idx + 1]
                if end_s < start_s:
                    end_s = start_s
                ranges.append((start_s, end_s))
            return ranges

    step = duration_s / count if count else 0.0
    ranges = []
    for idx in range(count):
        start_s = timeline_start_s + idx * step
        end_s = (
            timeline_end_s if idx == count - 1 else timeline_start_s + (idx + 1) * step
        )
        ranges.append((start_s, end_s))
    return ranges


def build_segments_from_transcript_items(
    items: list[TranscriptItem],
    *,
    duration_s: float,
    speech_chunks: list[dict[str, Any]],
) -> list[ExportSegment]:
    filtered_items = [item for item in items if (item.original or "").strip()]
    if not filtered_items:
        return []

    if speech_chunks:
        timeline_start_s = float(speech_chunks[0]["keep_from_s"])
        timeline_end_s = float(speech_chunks[-1]["keep_to_s"])
    else:
        timeline_start_s = 0.0
        timeline_end_s = duration_s

    ranges = build_time_ranges_from_timestamps(
        filtered_items,
        timeline_start_s=timeline_start_s,
        timeline_end_s=timeline_end_s,
    )

    segments: list[ExportSegment] = []
    for idx, (item, (start_s, end_s)) in enumerate(
        zip(filtered_items, ranges), start=1
    ):
        if end_s < start_s:
            end_s = start_s
        original_text = re.sub(r"\s+", " ", (item.original or "").strip())
        segment = ExportSegment(
            id=idx,
            start_s=start_s,
            end_s=end_s,
            start_label=format_seconds_label(start_s),
            end_label=format_seconds_label(end_s),
            original=original_text,
        )
        src_code = (item.src or "").strip().lower()
        tgt_code = (item.tgt or "").strip().lower()
        if src_code == "en":
            segment.english = original_text
        if tgt_code in code_to_language and (item.translation or "").strip():
            translated_text = re.sub(r"\s+", " ", (item.translation or "").strip())
            segment.translations[tgt_code] = translated_text
            if tgt_code == "en":
                segment.english = translated_text
        segments.append(segment)
    return segments


def export_zip_path(filename: str) -> str:
    name = (filename or "").strip()
    lower = name.lower()
    if lower.endswith(".csv"):
        return f"csv/{name}"
    if "summary" in lower:
        return f"summaries/{name}"
    if "minutes" in lower:
        return f"minutes/{name}"
    if "transcript" in lower:
        return f"transcripts/{name}"
    if (
        lower.endswith(".wav")
        or lower.endswith(".mp3")
        or lower.endswith(".webm")
        or lower.endswith(".ogg")
    ):
        return f"audio/{name}"
    if lower.endswith(".json"):
        return f"metadata/{name}"
    return name


def get_lang_name(code: str) -> str:
    return code_to_language.get(code, code)


def safe_json_loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def make_client(base_url: str, api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(base_url=base_url or "", api_key=api_key or "")


def pcm16le_bytes_to_float_tensor(data: bytes) -> torch.Tensor:
    pcm = np.frombuffer(data, dtype=np.int16)
    if pcm.size == 0:
        return torch.empty(0, dtype=torch.float32)
    return torch.from_numpy(pcm.astype(np.float32) / 32768.0)


def wav_bytesio_from_pcm16(pcm16: np.ndarray, sr: int = SAMPLE_RATE) -> io.BytesIO:
    if pcm16.dtype != np.int16:
        pcm16 = pcm16.astype(np.int16)

    buf = io.BytesIO()
    buf.name = "audio.wav"
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm16.tobytes())
    buf.seek(0)
    return buf


def rms_dbfs(pcm16: np.ndarray) -> float:
    x = pcm16.astype(np.float32) / 32768.0
    rms = np.sqrt(np.mean(x * x) + 1e-12)
    return 20 * np.log10(rms + 1e-12)


def clean_translation(text: str) -> str:
    return re.sub(r"</?[^>]+>", "", text or "").strip()


def clean_llm_document(text: str) -> str:
    cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", (text or "").strip())
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    return cleaned


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def looks_sentence_complete(text: str) -> bool:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return False
    return bool(re.search(r'[.!?](?:["\'\)\]]+)?$', cleaned))


def split_words(text: str) -> list[str]:
    return [w for w in re.split(r"\s+", (text or "").strip()) if w]


def format_seconds_label(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    total_seconds, ms = divmod(total_ms, 1000)
    hours, rem = divmod(total_seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"
    return f"{minutes:02d}:{secs:02d}.{ms:03d}"


def extract_json_string(text: str) -> str:
    text = clean_llm_document(text)
    if not text:
        return ""
    if text.startswith("{") or text.startswith("["):
        return text
    match = re.search(r"(\{.*\}|\[.*\])", text, flags=re.DOTALL)
    return match.group(1) if match else text


def language_codes_from_transcript(items: list[TranscriptItem]) -> list[str]:
    seen: list[str] = []
    for item in items:
        for code in [item.src, item.tgt]:
            code = (code or "").strip().lower()
            if code in code_to_language and code not in seen:
                seen.append(code)
    if "en" not in seen:
        seen.insert(0, "en")
    return seen


def language_codes_from_segments(
    segments: list[ExportSegment], fallback: list[str]
) -> list[str]:
    langs = list(fallback)
    if "en" not in langs:
        langs.insert(0, "en")
    return langs


def build_multilingual_source_text(items: list[TranscriptItem]) -> str:
    lines: list[str] = []
    for idx, item in enumerate(items, start=1):
        original = (item.original or "").strip()
        if not original:
            continue
        src_name = get_lang_name((item.src or "").strip().lower() or "unknown")
        timestamp = ""
        if item.ts_ms:
            try:

                timestamp = datetime.fromtimestamp(
                    item.ts_ms / 1000, ZoneInfo("America/Los_Angeles")
                ).strftime("%Y-%m-%d %H:%M:%S")

            except Exception:
                timestamp = ""
        prefix = f"[{idx}]"
        if timestamp:
            prefix += f" [{timestamp} UTC]"
        lines.append(f"{prefix} ({src_name}) {original}")
    return "\n".join(lines).strip()


def build_translator_system_prompt(src: str, tgt: str) -> str:
    src_name = get_lang_name(src)
    tgt_name = get_lang_name(tgt)
    return (
        "You are a translation engine.\n"
        f"Translate from {src_name} to {tgt_name}.\n"
        f"Return ONLY the {tgt_name} translation between <{tgt}> and </{tgt}>.\n"
        "Never add explanations, parentheses, or extra text.\n"
        "Never respond as a helpful assistant.\n"
    )


def build_live_translation_prompt(
    src: str,
    tgt: str,
    current_text: str,
    recent_items: list[TranscriptItem],
) -> str:
    src_name = get_lang_name(src)
    tgt_name = get_lang_name(tgt)
    context_items = recent_items[-LIVE_CONTEXT_TURNS:] if LIVE_CONTEXT_TURNS > 0 else []
    context_text = build_multilingual_source_text(context_items) or "None."
    return (
        "You are a real-time translation engine.\n"
        f"Translate the current active segment from {src_name} to {tgt_name}.\n"
        "The segment may be incomplete and can be revised as more words arrive.\n"
        "Use the recent finalized context only to resolve ambiguity.\n"
        "Translate only the current active segment.\n"
        "Do not summarize, explain, or answer the speaker.\n"
        "Return only the translated text with no tags or commentary.\n\n"
        f"Recent finalized context:\n{context_text}\n\n"
        f"Current active segment ({src_name}):\n{(current_text or '').strip()}"
    )


def build_summary_prompt(multilingual_text: str) -> str:
    return (
        "You are an expert meeting summarizer.\n"
        "The transcript below may contain multiple languages.\n"
        "Use the original source-language text as the source of truth.\n"
        "Write the output in English only.\n"
        "Create a concise but complete meeting summary with these sections:\n"
        "Title\n"
        "Overview\n"
        "Key discussion points\n"
        "Decisions\n"
        "Open questions\n"
        "Next steps\n\n"
        "If a section has no clear content, write 'None noted.'\n"
        "Do not mention that you are translating.\n"
        "Do not invent facts.\n\n"
        "Transcript:\n"
        f"{multilingual_text}"
    )


def build_minutes_prompt(multilingual_text: str) -> str:
    return (
        "You are an expert meeting secretary.\n"
        "The transcript below may contain multiple languages.\n"
        "Use the original source-language text as the source of truth.\n"
        "Write the output in English only.\n"
        "Create formal meeting minutes with these sections:\n"
        "Meeting minutes\n"
        "Purpose\n"
        "Agenda/topics covered\n"
        "Detailed discussion notes\n"
        "Decisions and agreements\n"
        "Action items (owner if stated, otherwise 'Owner not specified')\n"
        "Risks/blockers\n"
        "Follow-up items\n\n"
        "If a section has no clear content, write 'None noted.'\n"
        "Do not invent facts.\n\n"
        "Transcript:\n"
        f"{multilingual_text}"
    )


def build_document_translation_prompt(text: str, target_language_code: str) -> str:
    target_language_name = get_lang_name(target_language_code)
    return (
        "You are a professional translator.\n"
        f"Translate the following meeting document from English to {target_language_name}.\n"
        "Preserve headings, bullets, numbering, and structure.\n"
        "Return only the translated document content.\n"
        "Do not add commentary or notes.\n\n"
        f"Document to translate:\n{text}"
    )


def build_batch_translation_prompt(
    batch: list[ExportSegment], target_language_code: str
) -> str:
    target_language_name = get_lang_name(target_language_code)
    payload = [{"id": seg.id, "text": seg.original} for seg in batch]
    return (
        "You are a professional translator.\n"
        f"Translate every item's text into {target_language_name}.\n"
        "The source text may contain multiple languages.\n"
        "Return valid JSON only with this exact structure:\n"
        '{"items":[{"id":1,"text":"translated text"}]}\n'
        "Keep the same ids and order.\n"
        "Do not omit any item.\n"
        "Do not add commentary.\n\n"
        f"Input JSON:\n{json.dumps(payload, ensure_ascii=False)}"
    )


def build_batch_english_cleanup_prompt(batch: list[ExportSegment]) -> str:
    payload = [{"id": seg.id, "text": seg.original} for seg in batch]
    return (
        "You are a careful meeting transcription editor and translator.\n"
        "For each item, produce clear English text that preserves the original meaning.\n"
        "Fix obvious overlap repetition and punctuation mistakes, but do not summarize.\n"
        "If the source is already English, lightly clean it instead of rewriting it.\n"
        "Return valid JSON only with this exact structure:\n"
        '{"items":[{"id":1,"text":"english text"}]}\n'
        "Keep the same ids and order.\n"
        "Do not omit any item.\n"
        "Do not add commentary.\n\n"
        f"Input JSON:\n{json.dumps(payload, ensure_ascii=False)}"
    )


def build_chunk_notes_prompt(transcript_text: str) -> str:
    return (
        "You are preparing structured notes for a meeting.\n"
        "Summarize this portion of the meeting in English.\n"
        "Capture facts only.\n"
        "Include:\n"
        "- topics discussed\n"
        "- decisions\n"
        "- action items\n"
        "- risks or blockers\n"
        "- open questions\n"
        "Write concise bullet points.\n\n"
        f"Transcript portion:\n{transcript_text}"
    )


def to_plain_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    if hasattr(obj, "dict"):
        try:
            return obj.dict()
        except Exception:
            pass
    return {}


async def llm_complete_document(
    *,
    llm_client: AsyncOpenAI,
    llm_model: str,
    prompt: str,
    temperature: float = 0.2,
    max_tokens: int = 2200,
) -> str:
    response = await llm_client.chat.completions.create(
        model=llm_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return clean_llm_document(response.choices[0].message.content or "")


async def generate_export_documents(
    *,
    items: list[TranscriptItem],
    llm_client: AsyncOpenAI,
    llm_model: str,
) -> dict[str, Any]:
    multilingual_text = build_multilingual_source_text(items)
    if not multilingual_text:
        return {"languages": ["en"], "documents": []}

    english_summary = await llm_complete_document(
        llm_client=llm_client,
        llm_model=llm_model,
        prompt=build_summary_prompt(multilingual_text),
        temperature=0.2,
    )
    english_minutes = await llm_complete_document(
        llm_client=llm_client,
        llm_model=llm_model,
        prompt=build_minutes_prompt(multilingual_text),
        temperature=0.2,
    )

    languages = language_codes_from_transcript(items)
    documents: list[dict[str, str]] = [
        {
            "filename": "meeting_summary_en.txt",
            "language": "en",
            "kind": "summary",
            "content": english_summary,
        },
        {
            "filename": "meeting_minutes_en.txt",
            "language": "en",
            "kind": "minutes",
            "content": english_minutes,
        },
    ]

    for lang_code in languages:
        if lang_code == "en":
            continue
        translated_summary = await llm_complete_document(
            llm_client=llm_client,
            llm_model=llm_model,
            prompt=build_document_translation_prompt(english_summary, lang_code),
            temperature=0,
        )
        translated_minutes = await llm_complete_document(
            llm_client=llm_client,
            llm_model=llm_model,
            prompt=build_document_translation_prompt(english_minutes, lang_code),
            temperature=0,
        )
        documents.append(
            {
                "filename": f"meeting_summary_{lang_code}.txt",
                "language": lang_code,
                "kind": "summary",
                "content": translated_summary,
            }
        )
        documents.append(
            {
                "filename": f"meeting_minutes_{lang_code}.txt",
                "language": lang_code,
                "kind": "minutes",
                "content": translated_minutes,
            }
        )

    return {"languages": languages, "documents": documents}


async def transcribe_and_translate(
    *,
    pcm16_sentence: np.ndarray,
    src: str,
    tgt: str,
    whisper_client: AsyncOpenAI,
    whisper_model: str,
    llm_client: AsyncOpenAI,
    llm_model: str,
) -> tuple[str, str]:

    source_language_text = await transcribe_snapshot(
        pcm16_sentence=pcm16_sentence,
        src=src,
        whisper_client=whisper_client,
        whisper_model=whisper_model,
    )
    if not source_language_text:
        return "", ""

    target_language_text = await translate_text(
        text=source_language_text,
        src=src,
        tgt=tgt,
        llm_client=llm_client,
        llm_model=llm_model,
    )
    return source_language_text, target_language_text


async def transcribe_snapshot(
    *,
    pcm16_sentence: np.ndarray,
    src: str,
    whisper_client: AsyncOpenAI,
    whisper_model: str,
) -> str:

    duration_ms = len(pcm16_sentence) * 1000 / SAMPLE_RATE
    if duration_ms < MIN_AUDIO_MS:
        return ""

    if rms_dbfs(pcm16_sentence) < SILENCE_DBFS_THRESHOLD:
        return ""

    wav_io = wav_bytesio_from_pcm16(pcm16_sentence, sr=SAMPLE_RATE)

    print("sent audio to ASR model, waiting for transcription...")
    start_time = time.perf_counter()

    transcript_resp = await whisper_client.audio.transcriptions.create(
        model=whisper_model,
        file=wav_io,
        language=src,
    )
    print("Transcription took:", time.perf_counter() - start_time)

    source_language_text = (getattr(transcript_resp, "text", "") or "").strip()
    if len(source_language_text) <= MIN_SEND_TEXT_CHARS:
        return ""

    return source_language_text


async def translate_text(
    *,
    text: str,
    src: str,
    tgt: str,
    llm_client: AsyncOpenAI,
    llm_model: str,
) -> str:
    if not text:
        return ""

    print("Sending transcription to LLM for translation...")
    start_time = time.perf_counter()
    chat_resp = await llm_client.chat.completions.create(
        model=llm_model,
        messages=[
            {"role": "system", "content": build_translator_system_prompt(src, tgt)},
            {"role": "user", "content": text},
        ],
        temperature=0,
        max_tokens=200,
        stop=["\n\n", "```"],
    )
    print("Translation took:", time.perf_counter() - start_time)

    return clean_translation(chat_resp.choices[0].message.content or "")


async def translate_live_text(
    *,
    text: str,
    src: str,
    tgt: str,
    recent_items: list[TranscriptItem],
    llm_client: AsyncOpenAI,
    llm_model: str,
) -> str:
    if not text:
        return ""

    print("Sending live transcription to LLM for contextual translation...")
    start_time = time.perf_counter()
    chat_resp = await llm_client.chat.completions.create(
        model=llm_model,
        messages=[
            {
                "role": "user",
                "content": build_live_translation_prompt(
                    src=src,
                    tgt=tgt,
                    current_text=text,
                    recent_items=recent_items,
                ),
            }
        ],
        temperature=0,
        max_tokens=200,
        stop=["\n\n", "```"],
    )
    print("Live translation took:", time.perf_counter() - start_time)
    return clean_translation(chat_resp.choices[0].message.content or "")


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
                }
            if revision > int(segment.get("revision") or 0):
                segment["translations"] = {}
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
        }


async def clear_room_session(room_id: str) -> None:
    normalized = normalize_room_id(room_id)
    chunk_dir = ""
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            return
        room["segments"] = []
        room["segment_index"] = {}
        room["recording_state"] = "idle"
        room["recording_owner_session_id"] = ""
        room["recording_session_id"] = ""
        chunk_dir = room.get("recording_chunk_dir") or ""
        room["recording_chunk_dir"] = ""
        room["recording_chunk_paths"] = []
        room["recording_audio_bytes"] = b""
        room["recording_filename"] = ""
        room["recording_mime_type"] = ""
        room["recording_transcript_items"] = []
        room["recording_segment_ids"] = set()
        room["documents_source_items"] = []
        room["updated_at"] = time.time()
    await asyncio.to_thread(cleanup_recording_dir, chunk_dir)


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
        return serialize_room_state(room)


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
    await asyncio.to_thread(
        lambda: target_path.parent.mkdir(parents=True, exist_ok=True)
    )
    try:
        await asyncio.to_thread(target_path.write_bytes, chunk_bytes)
    except Exception:
        raise

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
        documents_source_items = room_segments_to_transcript_items(room)
        chunk_dir = room.get("recording_chunk_dir") or ""
        recording_mime_type = room.get("recording_mime_type") or ""

    media_bytes, media_suffix = await asyncio.to_thread(
        reconstruct_recording_media_bytes, chunk_paths, recording_mime_type
    )
    pcm16, _ = await asyncio.to_thread(ffmpeg_convert_to_pcm_wav, media_bytes, media_suffix)
    wav_bytes = wav_bytesio_from_pcm16(pcm16, SAMPLE_RATE).getvalue()

    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found.")
        if (room.get("recording_session_id") or "") != (recording_session_id or ""):
            raise HTTPException(
                status_code=409,
                detail="This recording session is no longer active.",
            )
        room["recording_audio_bytes"] = wav_bytes
        room["recording_filename"] = "meeting_audio.wav"
        room["recording_mime_type"] = "audio/wav"
        room["recording_state"] = "stopped"
        room["recording_owner_session_id"] = ""
        room["recording_chunk_dir"] = ""
        room["recording_chunk_paths"] = []
        room["recording_transcript_items"] = [
            item.model_dump() for item in recorded_items
        ]
        room["documents_source_items"] = [
            item.model_dump() for item in documents_source_items
        ]
        room["updated_at"] = time.time()
        payload = serialize_room_state(room)

    await asyncio.to_thread(cleanup_recording_dir, chunk_dir)
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
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            room = build_room_state(normalized)
            ROOMS[normalized] = room
        chunk_dir = room.get("recording_chunk_dir") or ""
        room["recording_audio_bytes"] = audio_bytes
        room["recording_filename"] = audio_filename
        room["recording_mime_type"] = mime_type
        room["recording_state"] = "stopped"
        room["recording_owner_session_id"] = ""
        room["recording_chunk_dir"] = ""
        room["recording_chunk_paths"] = []
        room["recording_transcript_items"] = [
            item.model_dump() for item in transcript_items
        ]
        room["recording_segment_ids"] = set()
        room["documents_source_items"] = [
            item.model_dump() for item in documents_source_items
        ]
        room["updated_at"] = time.time()
    await asyncio.to_thread(cleanup_recording_dir, chunk_dir)


async def ensure_room_translation(
    room_id: str,
    *,
    segment_id: str,
    revision: int,
    original: str,
    src: str,
    target_language: str,
    llm_client: AsyncOpenAI,
    llm_model: str,
) -> str:
    target = (target_language or "").strip().lower()
    source = (src or "").strip().lower()
    if not original:
        return ""
    if not target:
        return ""
    if target == source:
        return original

    normalized = normalize_room_id(room_id)
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            return ""
        index = room["segment_index"].get(segment_id)
        if index is not None:
            segment = room["segments"][index]
            if int(segment.get("revision") or 0) == revision:
                cached = (segment.get("translations") or {}).get(target, "")
                if cached:
                    return cached

    translated = await translate_text(
        text=original,
        src=source or DEFAULT_SOURCE_LANGUAGE,
        tgt=target,
        llm_client=llm_client,
        llm_model=llm_model,
    )

    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            return translated
        index = room["segment_index"].get(segment_id)
        if index is None:
            return translated
        segment = room["segments"][index]
        if int(segment.get("revision") or 0) == revision and translated:
            segment.setdefault("translations", {})[target] = translated
            room["updated_at"] = time.time()
        return (segment.get("translations") or {}).get(target, translated)


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
                    "src": segment.get("src")
                    or room.get("src")
                    or DEFAULT_SOURCE_LANGUAGE,
                    "ts_ms": segment.get("ts_ms"),
                    "translations": dict(segment.get("translations") or {}),
                }
                for segment in room.get("segments") or []
            ],
        }

    requested_target = (
        target_language or room_copy["presenter_tgt"] or DEFAULT_TARGET_LANGUAGE
    )
    requested_target = (
        requested_target
        if requested_target in code_to_language
        else room_copy["presenter_tgt"]
    )
    serialized_segments: list[dict[str, Any]] = []
    for segment in room_copy["segments"]:
        translation = segment["translations"].get(requested_target, "")
        if (
            segment["original"]
            and requested_target != segment["src"]
            and not translation
        ):
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


async def build_transcript_items_for_target(
    items: list[TranscriptItem],
    *,
    room_id: str,
    target_language: str,
    llm_client: AsyncOpenAI,
    llm_model: str,
) -> list[TranscriptItem]:
    result: list[TranscriptItem] = []
    for idx, item in enumerate(items, start=1):
        original = (item.original or "").strip()
        if not original:
            continue
        src = (
            item.src or DEFAULT_SOURCE_LANGUAGE
        ).strip().lower() or DEFAULT_SOURCE_LANGUAGE
        target = (target_language or DEFAULT_TARGET_LANGUAGE).strip().lower()
        translation = ""
        if target == src:
            translation = original
        elif target:
            translation = await ensure_room_translation(
                room_id,
                segment_id=f"export-{idx}-{item.ts_ms or idx}",
                revision=1,
                original=original,
                src=src,
                target_language=target,
                llm_client=llm_client,
                llm_model=llm_model,
            )
        result.append(
            TranscriptItem(
                original=original,
                translation=translation,
                src=src,
                tgt=target,
                ts_ms=item.ts_ms,
            )
        )
    return result


def ffmpeg_convert_to_pcm_wav(
    input_bytes: bytes, suffix: str
) -> tuple[np.ndarray, float]:
    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, f"input{suffix or '.bin'}")
        output_path = os.path.join(tmpdir, "normalized.wav")
        with open(input_path, "wb") as f:
            f.write(input_bytes)

        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "wav",
            output_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "ffmpeg failed").strip())

        with wave.open(output_path, "rb") as wf:
            frames = wf.readframes(wf.getnframes())
            sr = wf.getframerate()
            if sr != SAMPLE_RATE:
                raise RuntimeError(f"Unexpected sample rate after conversion: {sr}")
            samples = np.frombuffer(frames, dtype=np.int16).copy()
        duration_s = len(samples) / float(SAMPLE_RATE) if len(samples) else 0.0
        return samples, duration_s


def get_speech_timestamps_safe(pcm16: np.ndarray) -> list[dict[str, int]]:
    if len(pcm16) == 0:
        return []
    audio_tensor = torch.from_numpy(pcm16.astype(np.float32) / 32768.0)
    try:
        timestamps = get_speech_timestamps(
            audio_tensor,
            model,
            threshold=SPEECH_THRESHOLD,
            sampling_rate=SAMPLE_RATE,
            min_silence_duration_ms=LIVE_COMPLETE_SILENCE_MS,
            speech_pad_ms=EXPORT_VAD_PAD_MS,
        )
    except TypeError:
        timestamps = get_speech_timestamps(
            audio_tensor,
            model,
            threshold=SPEECH_THRESHOLD,
            sampling_rate=SAMPLE_RATE,
        )
    return [
        to_plain_dict(ts) if not isinstance(ts, dict) else ts
        for ts in (timestamps or [])
    ]


def build_vad_export_chunks(
    pcm16: np.ndarray,
    *,
    max_unit_seconds: int = EXPORT_VAD_MAX_UNIT_SECONDS,
    merge_gap_ms: int = EXPORT_VAD_MERGE_GAP_MS,
) -> list[dict[str, Any]]:
    speech_regions = get_speech_timestamps_safe(pcm16)
    if not speech_regions:
        return []

    max_unit_samples = int(max_unit_seconds * SAMPLE_RATE)
    merge_gap_samples = int(merge_gap_ms * SAMPLE_RATE / 1000.0)

    normalized_regions: list[tuple[int, int]] = []
    for region in speech_regions:
        start = max(0, int(region.get("start", 0) or 0))
        end = min(len(pcm16), int(region.get("end", start) or start))
        if end <= start:
            continue
        if (
            normalized_regions
            and start - normalized_regions[-1][1] <= merge_gap_samples
        ):
            prev_start, prev_end = normalized_regions[-1]
            normalized_regions[-1] = (prev_start, max(prev_end, end))
        else:
            normalized_regions.append((start, end))

    if not normalized_regions:
        return []

    chunks: list[dict[str, Any]] = []
    current_start = normalized_regions[0][0]
    current_end = normalized_regions[0][1]

    for start, end in normalized_regions[1:]:
        if end - current_start <= max_unit_samples:
            current_end = max(current_end, end)
            continue

        chunk_pcm = pcm16[current_start:current_end].copy()
        chunks.append(
            {
                "chunk_id": len(chunks),
                "start_s": current_start / SAMPLE_RATE,
                "end_s": current_end / SAMPLE_RATE,
                "keep_from_s": current_start / SAMPLE_RATE,
                "keep_to_s": current_end / SAMPLE_RATE,
                "pcm16": chunk_pcm,
            }
        )
        current_start = start
        current_end = end

    chunk_pcm = pcm16[current_start:current_end].copy()
    chunks.append(
        {
            "chunk_id": len(chunks),
            "start_s": current_start / SAMPLE_RATE,
            "end_s": current_end / SAMPLE_RATE,
            "keep_from_s": current_start / SAMPLE_RATE,
            "keep_to_s": current_end / SAMPLE_RATE,
            "pcm16": chunk_pcm,
        }
    )
    return chunks


def build_audio_chunks(
    pcm16: np.ndarray,
    *,
    chunk_seconds: int = EXPORT_CHUNK_SECONDS,
    overlap_seconds: int = EXPORT_OVERLAP_SECONDS,
) -> list[dict[str, Any]]:
    duration_s = len(pcm16) / float(SAMPLE_RATE) if len(pcm16) else 0.0
    if duration_s <= 0:
        return []

    if duration_s <= chunk_seconds:
        return [
            {
                "chunk_id": 0,
                "start_s": 0.0,
                "end_s": duration_s,
                "keep_from_s": 0.0,
                "keep_to_s": duration_s,
                "pcm16": pcm16,
            }
        ]

    stride_s = max(1, chunk_seconds - overlap_seconds)
    starts: list[float] = []
    start_s = 0.0
    while start_s < duration_s:
        starts.append(start_s)
        end_s = min(start_s + chunk_seconds, duration_s)
        if end_s >= duration_s:
            break
        start_s += stride_s

    chunks: list[dict[str, Any]] = []
    for idx, start_s in enumerate(starts):
        end_s = min(start_s + chunk_seconds, duration_s)
        prev_start = starts[idx - 1] if idx > 0 else None
        next_start = starts[idx + 1] if idx + 1 < len(starts) else None

        keep_from_s = start_s if prev_start is None else start_s + overlap_seconds / 2.0
        keep_to_s = end_s if next_start is None else next_start + overlap_seconds / 2.0
        keep_from_s = max(start_s, min(keep_from_s, end_s))
        keep_to_s = max(keep_from_s, min(keep_to_s, end_s))

        start_idx = max(0, int(round(start_s * SAMPLE_RATE)))
        end_idx = min(len(pcm16), int(round(end_s * SAMPLE_RATE)))
        chunk_pcm = pcm16[start_idx:end_idx].copy()
        chunks.append(
            {
                "chunk_id": idx,
                "start_s": start_s,
                "end_s": end_s,
                "keep_from_s": keep_from_s,
                "keep_to_s": keep_to_s,
                "pcm16": chunk_pcm,
            }
        )
    return chunks


async def transcribe_export_chunk(
    *,
    chunk: dict[str, Any],
    whisper_client: AsyncOpenAI,
    whisper_model: str,
    semaphore: asyncio.Semaphore,
) -> ExportChunkResult:
    async with semaphore:
        text = ""
        language = ""
        raw_segments: list[dict[str, Any]] = []
        errors: list[str] = []

        try:
            wav_io = wav_bytesio_from_pcm16(chunk["pcm16"], sr=SAMPLE_RATE)
            resp = await whisper_client.audio.transcriptions.create(
                model=whisper_model,
                file=wav_io,
                response_format="verbose_json",
                timestamp_granularities=["segment"],
            )
            payload = to_plain_dict(resp)
            text = (payload.get("text") or getattr(resp, "text", "") or "").strip()
            language = (
                (payload.get("language") or getattr(resp, "language", "") or "")
                .strip()
                .lower()
            )
            raw_segments = payload.get("segments") or []
        except Exception as exc:
            errors.append(f"verbose_json+segments failed: {exc!r}")

        if not raw_segments:
            try:
                wav_io = wav_bytesio_from_pcm16(chunk["pcm16"], sr=SAMPLE_RATE)
                resp = await whisper_client.audio.transcriptions.create(
                    model=whisper_model,
                    file=wav_io,
                    response_format="verbose_json",
                )
                payload = to_plain_dict(resp)
                text = (payload.get("text") or getattr(resp, "text", "") or "").strip()
                language = (
                    (payload.get("language") or getattr(resp, "language", "") or "")
                    .strip()
                    .lower()
                )
                raw_segments = payload.get("segments") or []
            except Exception as exc:
                errors.append(f"verbose_json failed: {exc!r}")

        if not text:
            try:
                wav_io = wav_bytesio_from_pcm16(chunk["pcm16"], sr=SAMPLE_RATE)
                resp = await whisper_client.audio.transcriptions.create(
                    model=whisper_model,
                    file=wav_io,
                )
                text = (getattr(resp, "text", "") or "").strip()
                language = (
                    (getattr(resp, "language", "") or language or "").strip().lower()
                )
            except Exception as exc:
                errors.append(f"plain transcription failed: {exc!r}")

        if not text and errors:
            raise RuntimeError("; ".join(errors))

    segments: list[dict[str, Any]] = []
    for seg in raw_segments:
        segd = to_plain_dict(seg) if not isinstance(seg, dict) else seg
        s_rel = float(segd.get("start", 0.0) or 0.0)
        e_rel = float(segd.get("end", s_rel) or s_rel)
        content = re.sub(r"\s+", " ", (segd.get("text") or "").strip())
        if not content:
            continue
        abs_start = chunk["start_s"] + s_rel
        abs_end = chunk["start_s"] + max(e_rel, s_rel)
        abs_start = min(max(abs_start, chunk["keep_from_s"]), chunk["keep_to_s"])
        abs_end = min(max(abs_end, abs_start), chunk["keep_to_s"])
        if abs_end <= abs_start:
            continue
        segments.append(
            {
                "start_s": abs_start,
                "end_s": abs_end,
                "text": content,
            }
        )

    if not segments and text:
        segments = [
            {
                "start_s": chunk["keep_from_s"],
                "end_s": chunk["keep_to_s"],
                "text": re.sub(r"\s+", " ", text),
            }
        ]

    return ExportChunkResult(
        chunk_id=chunk["chunk_id"],
        chunk_start_s=chunk["start_s"],
        chunk_end_s=chunk["end_s"],
        keep_from_s=chunk["keep_from_s"],
        keep_to_s=chunk["keep_to_s"],
        text=text,
        language=language,
        segments=segments,
    )


def trim_duplicate_prefix(
    previous_text: str, current_text: str, max_overlap_words: int = 12
) -> str:
    prev_words = split_words(previous_text)
    cur_words = split_words(current_text)
    max_k = min(max_overlap_words, len(prev_words), len(cur_words))
    for k in range(max_k, 0, -1):
        if [w.lower() for w in prev_words[-k:]] == [w.lower() for w in cur_words[:k]]:
            return " ".join(cur_words[k:]).strip()
    return current_text.strip()


def merge_export_segments(
    chunk_results: list[ExportChunkResult],
) -> list[ExportSegment]:
    all_segments: list[dict[str, Any]] = []
    for chunk in sorted(chunk_results, key=lambda c: c.chunk_start_s):
        all_segments.extend(chunk.segments)

    all_segments.sort(key=lambda s: (float(s["start_s"]), float(s["end_s"])))

    merged: list[ExportSegment] = []
    for raw in all_segments:
        text = re.sub(r"\s+", " ", (raw.get("text") or "").strip())
        if not text:
            continue
        start_s = float(raw["start_s"])
        end_s = max(float(raw["end_s"]), start_s)

        if merged:
            prev = merged[-1]
            if normalize_text(text) == normalize_text(prev.original):
                if end_s <= prev.end_s + 0.75:
                    continue
            if start_s <= prev.end_s + 0.4:
                trimmed = trim_duplicate_prefix(prev.original, text)
                if not trimmed:
                    prev.end_s = max(prev.end_s, end_s)
                    prev.end_label = format_seconds_label(prev.end_s)
                    continue
                text = trimmed
                start_s = max(start_s, prev.end_s)
                if end_s < start_s:
                    end_s = start_s

        merged.append(
            ExportSegment(
                id=len(merged) + 1,
                start_s=start_s,
                end_s=end_s,
                start_label=format_seconds_label(start_s),
                end_label=format_seconds_label(end_s),
                original=text,
            )
        )
    return merged


def batch_segments_for_translation(
    segments: list[ExportSegment],
) -> list[list[ExportSegment]]:
    batches: list[list[ExportSegment]] = []
    current: list[ExportSegment] = []
    current_chars = 0
    for seg in segments:
        seg_chars = len(seg.original)
        if current and (
            len(current) >= EXPORT_TEXT_BATCH_SEGMENTS
            or current_chars + seg_chars > EXPORT_TEXT_BATCH_CHARS
        ):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(seg)
        current_chars += seg_chars
    if current:
        batches.append(current)
    return batches


async def translate_batch_with_llm(
    *,
    batch: list[ExportSegment],
    llm_client: AsyncOpenAI,
    llm_model: str,
    prompt: str,
) -> dict[int, str]:
    response = await llm_client.chat.completions.create(
        model=llm_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=2200,
    )
    raw = response.choices[0].message.content or ""
    parsed_text = extract_json_string(raw)
    data = JsonTranslationBatch.model_validate(safe_json_loads(parsed_text, {}))
    by_id: dict[int, str] = {}
    for item in data.items:
        try:
            item_id = int(item.get("id"))
        except Exception:
            continue
        by_id[item_id] = str(item.get("text") or "").strip()
    return by_id


async def translate_segments_to_language(
    *,
    segments: list[ExportSegment],
    target_language_code: str,
    llm_client: AsyncOpenAI,
    llm_model: str,
) -> None:
    for batch in batch_segments_for_translation(segments):
        pending_batch = [
            seg
            for seg in batch
            if not (seg.translations.get(target_language_code, "") or "").strip()
        ]
        if not pending_batch:
            continue

        prompt = build_batch_translation_prompt(pending_batch, target_language_code)
        try:
            translated = await translate_batch_with_llm(
                batch=pending_batch,
                llm_client=llm_client,
                llm_model=llm_model,
                prompt=prompt,
            )
        except Exception:
            translated = {}

        if len(translated) != len(pending_batch):
            for seg in pending_batch:
                if seg.id in translated:
                    continue
                translated[seg.id] = await llm_complete_document(
                    llm_client=llm_client,
                    llm_model=llm_model,
                    prompt=(
                        f"Translate the following text into {get_lang_name(target_language_code)}. "
                        "Return only the translation with no commentary.\n\n"
                        f"Text:\n{seg.original}"
                    ),
                    temperature=0,
                    max_tokens=400,
                )

        for seg in pending_batch:
            seg.translations[target_language_code] = clean_llm_document(
                translated.get(seg.id, "")
            )


async def clean_segments_to_english(
    *,
    segments: list[ExportSegment],
    llm_client: AsyncOpenAI,
    llm_model: str,
) -> None:
    for batch in batch_segments_for_translation(segments):
        pending_batch = [seg for seg in batch if not (seg.english or "").strip()]
        for seg in batch:
            if (seg.english or "").strip():
                seg.english = clean_llm_document(seg.english)
        if not pending_batch:
            continue

        prompt = build_batch_english_cleanup_prompt(pending_batch)
        try:
            translated = await translate_batch_with_llm(
                batch=pending_batch,
                llm_client=llm_client,
                llm_model=llm_model,
                prompt=prompt,
            )
        except Exception:
            translated = {}

        if len(translated) != len(pending_batch):
            for seg in pending_batch:
                if seg.id in translated:
                    continue
                translated[seg.id] = await llm_complete_document(
                    llm_client=llm_client,
                    llm_model=llm_model,
                    prompt=(
                        "Translate this meeting transcript segment into clear English. "
                        "Fix only obvious punctuation or overlap repetition. "
                        "Do not summarize. Return only the cleaned English text.\n\n"
                        f"Segment:\n{seg.original}"
                    ),
                    temperature=0,
                    max_tokens=400,
                )

        for seg in pending_batch:
            seg.english = clean_llm_document(translated.get(seg.id, "")) or seg.original


def segments_to_timestamped_text(
    segments: list[ExportSegment], field: str, language_code: str | None = None
) -> str:
    lines: list[str] = []
    for seg in segments:
        if field == "original":
            text = seg.original
        elif field == "english":
            text = seg.english
        else:
            text = seg.translations.get(language_code or "", "")
        text = (text or "").strip()
        if not text:
            continue
        lines.append(f"[{seg.start_label} - {seg.end_label}] {text}")
    return "\n".join(lines).strip()


def build_parallel_csv(segments: list[ExportSegment], languages: list[str]) -> str:
    columns = [
        "segment",
        "start_s",
        "end_s",
        "original",
        "english",
    ] + [f"translation_{lang}" for lang in languages if lang != "en"]

    def csv_escape(text: str) -> str:
        return '"' + (text or "").replace('"', '""') + '"'

    rows = [",".join(columns)]
    for seg in segments:
        base = [
            str(seg.id),
            f"{seg.start_s:.3f}",
            f"{seg.end_s:.3f}",
            csv_escape(seg.original),
            csv_escape(seg.english),
        ]
        for lang in languages:
            if lang == "en":
                continue
            base.append(csv_escape(seg.translations.get(lang, "")))
        rows.append(",".join(base))
    return "\n".join(rows)


def split_text_for_map_reduce(text: str, max_chars: int = 12000) -> list[str]:
    lines = text.splitlines()
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) + 1 > max_chars:
            chunks.append("\n".join(current).strip())
            current = []
            size = 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current).strip())
    return [chunk for chunk in chunks if chunk]


async def build_english_summary_and_minutes(
    *,
    english_transcript_text: str,
    llm_client: AsyncOpenAI,
    llm_model: str,
) -> tuple[str, str]:
    transcript_parts = split_text_for_map_reduce(english_transcript_text)
    if len(transcript_parts) <= 1:
        summary = await llm_complete_document(
            llm_client=llm_client,
            llm_model=llm_model,
            prompt=build_summary_prompt(english_transcript_text),
            temperature=0.2,
        )
        minutes = await llm_complete_document(
            llm_client=llm_client,
            llm_model=llm_model,
            prompt=build_minutes_prompt(english_transcript_text),
            temperature=0.2,
        )
        return summary, minutes

    notes_parts: list[str] = []
    for idx, part in enumerate(transcript_parts, start=1):
        notes = await llm_complete_document(
            llm_client=llm_client,
            llm_model=llm_model,
            prompt=build_chunk_notes_prompt(part),
            temperature=0.1,
            max_tokens=1000,
        )
        notes_parts.append(f"Chunk {idx}\n{notes}")

    combined_notes = "\n\n".join(notes_parts)
    summary = await llm_complete_document(
        llm_client=llm_client,
        llm_model=llm_model,
        prompt=build_summary_prompt(combined_notes),
        temperature=0.2,
    )
    minutes = await llm_complete_document(
        llm_client=llm_client,
        llm_model=llm_model,
        prompt=build_minutes_prompt(combined_notes),
        temperature=0.2,
    )
    return summary, minutes


async def generate_meeting_package(
    *,
    audio_bytes: bytes,
    audio_filename: str,
    transcript_items: list[TranscriptItem],
    documents_source_items: list[TranscriptItem] | None,
    whisper_client: AsyncOpenAI,
    whisper_model: str,
    llm_client: AsyncOpenAI,
    llm_model: str,
    progress_cb=None,
) -> MeetingPackageResult:
    async def _progress(pct: int, stage: str, detail: str) -> None:
        if progress_cb is not None:
            await progress_cb(pct, stage, detail)

    suffix = Path(audio_filename or "meeting_audio.webm").suffix or ".webm"
    pcm16, duration_s = await asyncio.to_thread(
        ffmpeg_convert_to_pcm_wav, audio_bytes, suffix
    )
    if duration_s <= 0 or len(pcm16) == 0:
        raise HTTPException(
            status_code=400,
            detail="The uploaded recording does not contain any decodable audio.",
        )

    await _progress(
        10, "Finding speech", "Detecting real speech regions in the saved recording."
    )
    chunks = await asyncio.to_thread(build_vad_export_chunks, pcm16)
    if not chunks:
        await _progress(
            12,
            "Finding speech",
            "No reliable speech regions found with VAD, falling back to overlap chunking.",
        )
        chunks = await asyncio.to_thread(build_audio_chunks, pcm16)
    await _progress(
        18,
        "Transcribing audio",
        f"Transcribing {len(chunks)} speech chunk(s) with bounded parallelism.",
    )
    if transcript_items:
        segments = build_segments_from_transcript_items(
            transcript_items,
            duration_s=duration_s,
            speech_chunks=chunks,
        )
        await _progress(
            58,
            "Aligning live transcript",
            "Using the live transcript as the source of truth and aligning it to the recording.",
        )
    else:
        semaphore = asyncio.Semaphore(EXPORT_MAX_ASR_CONCURRENCY)
        chunk_results = await asyncio.gather(
            *[
                transcribe_export_chunk(
                    chunk=chunk,
                    whisper_client=whisper_client,
                    whisper_model=whisper_model,
                    semaphore=semaphore,
                )
                for chunk in chunks
            ]
        )

        await _progress(
            58,
            "Merging transcript",
            "Merging speech-aligned transcription segments and removing duplication.",
        )
        segments = merge_export_segments(chunk_results)

    if not segments:
        raise HTTPException(
            status_code=400,
            detail="No reliable speech was detected in the saved recording.",
        )

    await _progress(
        68,
        "Cleaning transcript",
        "Refining the stitched transcript into clean English segments.",
    )
    await clean_segments_to_english(
        segments=segments,
        llm_client=llm_client,
        llm_model=llm_model,
    )

    await _progress(
        78,
        "Detecting languages",
        "Identifying languages used in the meeting for translated exports.",
    )
    languages = language_codes_from_segments(
        segments, language_codes_from_transcript(transcript_items)
    )
    await _progress(
        82,
        "Translating transcripts",
        "Creating translated transcript exports for meeting languages.",
    )
    for lang in languages:
        if lang == "en":
            continue
        await translate_segments_to_language(
            segments=segments,
            target_language_code=lang,
            llm_client=llm_client,
            llm_model=llm_model,
        )

    original_transcript = segments_to_timestamped_text(segments, "original")
    english_transcript = segments_to_timestamped_text(segments, "english")
    await _progress(
        88, "Writing meeting docs", "Generating meeting summary and minutes."
    )
    doc_items = documents_source_items if documents_source_items else transcript_items
    doc_multilingual_text = build_multilingual_source_text(doc_items)
    if doc_multilingual_text:
        english_summary = await llm_complete_document(
            llm_client=llm_client,
            llm_model=llm_model,
            prompt=build_summary_prompt(doc_multilingual_text),
            temperature=0.2,
        )
        english_minutes = await llm_complete_document(
            llm_client=llm_client,
            llm_model=llm_model,
            prompt=build_minutes_prompt(doc_multilingual_text),
            temperature=0.2,
        )
    else:
        english_summary, english_minutes = await build_english_summary_and_minutes(
            english_transcript_text=english_transcript,
            llm_client=llm_client,
            llm_model=llm_model,
        )

    documents: list[dict[str, str]] = [
        {
            "filename": "transcript_original_timestamped.txt",
            "language": "multi",
            "kind": "transcript_original",
            "content": original_transcript,
        },
        {
            "filename": "transcript_english_timestamped.txt",
            "language": "en",
            "kind": "transcript_translation",
            "content": english_transcript,
        },
        {
            "filename": "meeting_summary_en.txt",
            "language": "en",
            "kind": "summary",
            "content": english_summary,
        },
        {
            "filename": "meeting_minutes_en.txt",
            "language": "en",
            "kind": "minutes",
            "content": english_minutes,
        },
        {
            "filename": "transcript_parallel.csv",
            "language": "multi",
            "kind": "parallel_csv",
            "content": build_parallel_csv(segments, languages),
        },
    ]

    for lang in languages:
        if lang == "en":
            continue
        transcript_text = segments_to_timestamped_text(
            segments, "translation", language_code=lang
        )
        documents.append(
            {
                "filename": f"transcript_{lang}_timestamped.txt",
                "language": lang,
                "kind": "transcript_translation",
                "content": transcript_text,
            }
        )
        translated_summary = await llm_complete_document(
            llm_client=llm_client,
            llm_model=llm_model,
            prompt=build_document_translation_prompt(english_summary, lang),
            temperature=0,
        )
        translated_minutes = await llm_complete_document(
            llm_client=llm_client,
            llm_model=llm_model,
            prompt=build_document_translation_prompt(english_minutes, lang),
            temperature=0,
        )
        documents.append(
            {
                "filename": f"meeting_summary_{lang}.txt",
                "language": lang,
                "kind": "summary",
                "content": translated_summary,
            }
        )
        documents.append(
            {
                "filename": f"meeting_minutes_{lang}.txt",
                "language": lang,
                "kind": "minutes",
                "content": translated_minutes,
            }
        )

    return MeetingPackageResult(
        languages=languages,
        documents=documents,
        segments=segments,
    )


async def update_export_job(job_id: str, **fields: Any) -> None:
    async with EXPORT_JOBS_LOCK:
        job = EXPORT_JOBS.get(job_id)
        if not job:
            return
        job.update(fields)
        job["updated_at"] = time.time()


async def create_export_job() -> str:
    job_id = uuid.uuid4().hex
    async with EXPORT_JOBS_LOCK:
        EXPORT_JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "progress": 2,
            "stage": "Queued",
            "detail": "Preparing export job.",
            "created_at": time.time(),
            "updated_at": time.time(),
            "archive_name": None,
            "zip_bytes": None,
            "error": None,
        }
    return job_id


async def cleanup_old_export_jobs() -> None:
    cutoff = time.time() - EXPORT_JOB_TTL_SECONDS
    async with EXPORT_JOBS_LOCK:
        stale = [
            jid for jid, job in EXPORT_JOBS.items() if job.get("updated_at", 0) < cutoff
        ]
        for jid in stale:
            EXPORT_JOBS.pop(jid, None)


async def build_export_package_bytes(
    *,
    audio_bytes: bytes,
    audio_filename: str,
    transcript_items: list[TranscriptItem],
    documents_source_items: list[TranscriptItem] | None,
    whisper_client: AsyncOpenAI,
    whisper_model: str,
    llm_client: AsyncOpenAI,
    llm_model: str,
    progress_cb=None,
) -> tuple[bytes, str]:
    if progress_cb is not None:
        await progress_cb(
            5, "Preparing audio", "Decoding and normalizing the uploaded recording."
        )

    meeting = await generate_meeting_package(
        audio_bytes=audio_bytes,
        audio_filename=audio_filename,
        transcript_items=transcript_items,
        documents_source_items=documents_source_items,
        whisper_client=whisper_client,
        whisper_model=whisper_model,
        llm_client=llm_client,
        llm_model=llm_model,
        progress_cb=progress_cb,
    )

    if progress_cb is not None:
        await progress_cb(
            92,
            "Finalizing audio",
            "Converting the meeting recording to WAV for the package.",
        )
    pcm16, duration_s = await asyncio.to_thread(
        ffmpeg_convert_to_pcm_wav,
        audio_bytes,
        Path(audio_filename or "meeting_audio.webm").suffix or ".webm",
    )
    wav_bytes = wav_bytesio_from_pcm16(pcm16, SAMPLE_RATE).getvalue()

    if progress_cb is not None:
        await progress_cb(
            96, "Building ZIP", "Adding documents and audio to the final ZIP package."
        )

    stamp = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d_%H-%M-%S")
    # stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.gmtime())

    archive_name = f"meeting_package_{stamp}.zip"

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for doc in meeting.documents:
            name = doc["filename"]
            if "summary" in name:
                path = f"summaries/{name}"
            elif "minutes" in name:
                path = f"minutes/{name}"
            elif "transcript" in name:
                path = f"transcripts/{name}"
            elif name.endswith(".csv"):
                path = f"csv/{name}"
            else:
                path = name
            zf.writestr(path, doc["content"])

        zf.writestr("audio/meeting_audio.wav", wav_bytes)

        manifest = {
            "languages": meeting.languages,
            "segment_count": len(meeting.segments),
            "chunk_seconds": EXPORT_CHUNK_SECONDS,
            "overlap_seconds": EXPORT_OVERLAP_SECONDS,
            "max_asr_concurrency": EXPORT_MAX_ASR_CONCURRENCY,
            "audio_duration_seconds": round(duration_s, 3),
            "audio_format": "wav",
        }
        zf.writestr("export_manifest.json", json.dumps(manifest, indent=2))

    zip_buffer.seek(0)
    if progress_cb is not None:
        await progress_cb(100, "Done", "Meeting package is ready to download.")
    return zip_buffer.getvalue(), archive_name


async def run_export_job(
    *,
    job_id: str,
    audio_bytes: bytes,
    audio_filename: str,
    transcript_items: list[TranscriptItem],
    documents_transcript_items: list[TranscriptItem] | None = None,
    llm_cfg: ExportLlmConfig,
    whisper_cfg: ExportWhisperConfig,
) -> None:
    try:
        llm_client = make_client(
            llm_cfg.base_url or DEFAULT_LLM_BASE_URL,
            llm_cfg.api_key or DEFAULT_LLM_API_KEY,
        )
        whisper_client = make_client(
            whisper_cfg.base_url or DEFAULT_WHISPER_BASE_URL,
            whisper_cfg.api_key or DEFAULT_WHISPER_API_KEY,
        )

        async def progress_cb(pct: int, stage: str, detail: str) -> None:
            await update_export_job(
                job_id,
                status="running",
                progress=max(1, min(99 if pct < 100 else 100, int(pct))),
                stage=stage,
                detail=detail,
            )

        zip_bytes, archive_name = await build_export_package_bytes(
            audio_bytes=audio_bytes,
            audio_filename=audio_filename,
            transcript_items=transcript_items,
            documents_source_items=documents_transcript_items,
            whisper_client=whisper_client,
            whisper_model=whisper_cfg.model or DEFAULT_WHISPER_MODEL,
            llm_client=llm_client,
            llm_model=llm_cfg.model or DEFAULT_LLM_MODEL,
            progress_cb=progress_cb,
        )
        await update_export_job(
            job_id,
            status="completed",
            progress=100,
            stage="Done",
            detail="Meeting package is ready to download.",
            archive_name=archive_name,
            zip_bytes=zip_bytes,
        )
    except Exception as exc:
        print("Export job failed")
        print(traceback.format_exc())
        await update_export_job(
            job_id,
            status="failed",
            progress=100,
            stage="Failed",
            detail="Export failed.",
            error=str(exc),
        )


@app.post("/api/export-documents")
async def export_documents(payload: ExportRequest):
    items = payload.transcript or []
    if not items:
        return {"languages": ["en"], "documents": []}

    llm_base_url = (payload.llm.base_url if payload.llm else "") or DEFAULT_LLM_BASE_URL
    llm_api_key = (payload.llm.api_key if payload.llm else "") or DEFAULT_LLM_API_KEY
    llm_model = (payload.llm.model if payload.llm else "") or DEFAULT_LLM_MODEL
    llm_client = make_client(llm_base_url, llm_api_key)

    result = await generate_export_documents(
        items=items,
        llm_client=llm_client,
        llm_model=llm_model,
    )
    return result


@app.post("/api/export-package")
async def export_package(
    audio: UploadFile = File(...),
    transcript_json: str = Form("[]"),
    documents_transcript_json: str = Form("[]"),
    llm_json: str = Form("{}"),
    whisper_json: str = Form("{}"),
):
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(
            status_code=400, detail="The uploaded recording file is empty."
        )

    transcript_items = [
        TranscriptItem.model_validate(item)
        for item in safe_json_loads(transcript_json, [])
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


@app.post("/api/export-package/start")
async def export_package_start(
    audio: UploadFile = File(...),
    transcript_json: str = Form("[]"),
    documents_transcript_json: str = Form("[]"),
    llm_json: str = Form("{}"),
    whisper_json: str = Form("{}"),
):
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(
            status_code=400, detail="The uploaded recording file is empty."
        )

    transcript_items = [
        TranscriptItem.model_validate(item)
        for item in safe_json_loads(transcript_json, [])
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


@app.get("/api/export-package/status/{job_id}")
async def export_package_status(job_id: str):
    async with EXPORT_JOBS_LOCK:
        job = EXPORT_JOBS.get(job_id)
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


@app.get("/api/export-package/download/{job_id}")
async def export_package_download(job_id: str):
    async with EXPORT_JOBS_LOCK:
        job = EXPORT_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Export job not found.")
        if job.get("status") != "completed" or not job.get("zip_bytes"):
            raise HTTPException(
                status_code=409, detail="Export job is not ready for download yet."
            )
        archive_name = job.get("archive_name") or f"meeting_package_{job_id}.zip"
        zip_bytes = job.get("zip_bytes")
    headers = {"Content-Disposition": f'attachment; filename="{archive_name}"'}
    return Response(content=zip_bytes, media_type="application/zip", headers=headers)


@app.post("/api/rooms/{room_id}/recording/start")
async def start_room_recording(room_id: str, payload: RecordingControlRequest):
    room_state = await start_or_resume_room_recording(
        room_id, client_session_id=payload.client_session_id
    )
    await broadcast_room_state(room_id)
    return JSONResponse(room_state)


@app.post("/api/rooms/{room_id}/recording/pause")
async def pause_room_recording_api(room_id: str, payload: RecordingControlRequest):
    room_state = await pause_room_recording(
        room_id, client_session_id=payload.client_session_id
    )
    room = await get_room_or_404(room_id)
    payload_state = room_state or serialize_room_state(room)
    await broadcast_room_state(room_id)
    return JSONResponse(payload_state)


@app.post("/api/rooms/{room_id}/recording/chunk")
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


@app.post("/api/rooms/{room_id}/recording/finalize")
async def finalize_room_recording_api(room_id: str, payload: RecordingFinalizeRequest):
    room_state = await finalize_room_recording(
        room_id, recording_session_id=payload.recording_session_id
    )
    await broadcast_room_state(room_id)
    return JSONResponse(room_state)


@app.post("/api/rooms/{room_id}/recording")
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
        TranscriptItem.model_validate(item)
        for item in safe_json_loads(transcript_json, [])
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


@app.get("/api/rooms/{room_id}")
async def get_room_state(room_id: str):
    room = await get_room_or_404(room_id)
    payload = serialize_room_state(room)
    payload["segment_count"] = len(room.get("segments") or [])
    return JSONResponse(payload)


@app.post("/api/rooms/{room_id}/export-package/start")
async def start_room_export_package(room_id: str, payload: RoomExportRequest):
    normalized = normalize_room_id(room_id)
    resumable_chunk_paths: list[str] = []
    recording_mime_type = ""
    async with ROOMS_LOCK:
        room = ROOMS.get(normalized)
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found.")
        if not room_can_download(room) and not room_has_resumable_recording(room):
            raise HTTPException(
                status_code=409,
                detail="The full package is only available after a recording has been captured.",
            )
        source_raw = list(room.get("recording_transcript_items") or [])
        documents_raw = list(room.get("documents_source_items") or [])
        room_history_items = room_segments_to_transcript_items(room)
        audio_bytes = bytes(room.get("recording_audio_bytes") or b"")
        audio_filename = room.get("recording_filename") or "meeting_audio.webm"
        resumable_chunk_paths = list(room.get("recording_chunk_paths") or [])
        recording_mime_type = room.get("recording_mime_type") or ""
        room_llm = dict(room.get("llm") or {})
        room_whisper = dict(room.get("whisper") or {})

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
    if not source_raw and room_history_items:
        source_raw = [item.model_dump() for item in room_history_items]
    if not documents_raw and room_history_items:
        documents_raw = [item.model_dump() for item in room_history_items]
    if not documents_raw and source_raw:
        documents_raw = list(source_raw)

    target_language = (payload.target_language or "").strip().lower()
    if target_language not in code_to_language:
        raise HTTPException(status_code=400, detail="Unsupported target language.")

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

    llm_client = make_client(
        llm_cfg.base_url or DEFAULT_LLM_BASE_URL, llm_cfg.api_key or DEFAULT_LLM_API_KEY
    )
    source_items = [TranscriptItem.model_validate(item) for item in source_raw]
    documents_items = [TranscriptItem.model_validate(item) for item in documents_raw]
    translated_items = await build_transcript_items_for_target(
        source_items,
        room_id=normalized,
        target_language=target_language,
        llm_client=llm_client,
        llm_model=llm_cfg.model or DEFAULT_LLM_MODEL,
    )
    translated_documents_items = await build_transcript_items_for_target(
        documents_items,
        room_id=normalized,
        target_language=target_language,
        llm_client=llm_client,
        llm_model=llm_cfg.model or DEFAULT_LLM_MODEL,
    )

    await cleanup_old_export_jobs()
    job_id = await create_export_job()
    asyncio.create_task(
        run_export_job(
            job_id=job_id,
            audio_bytes=audio_bytes,
            audio_filename=audio_filename,
            transcript_items=translated_items,
            documents_transcript_items=translated_documents_items,
            llm_cfg=llm_cfg,
            whisper_cfg=whisper_cfg,
        )
    )
    return JSONResponse({"job_id": job_id, "status": "queued"})


@app.websocket("/ws/translate")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("Client connected")

    cfg = build_default_runtime_config()

    whisper_client = make_client(cfg["whisper"]["base_url"], cfg["whisper"]["api_key"])
    llm_client = make_client(cfg["llm"]["base_url"], cfg["llm"]["api_key"])
    send_lock = asyncio.Lock()
    room_id: str | None = None
    role = "presenter"
    client_session_id = uuid.uuid4().hex
    attendee_target_language = DEFAULT_TARGET_LANGUAGE

    vad_iterator = VADIterator(
        model, threshold=SPEECH_THRESHOLD, sampling_rate=SAMPLE_RATE
    )
    sentence_pcm = np.zeros((0,), dtype=np.int16)

    is_speaking = False
    saw_end = False
    silence_after_end_ms = 0
    segment_sequence = 0
    active_segment_id: str | None = None
    active_segment_started_ts: int | None = None
    last_partial_analysis_at = 0.0
    pending_snapshot: dict[str, Any] | None = None
    analysis_task: asyncio.Task | None = None
    recent_final_items: list[TranscriptItem] = []
    emitted_segments: dict[str, dict[str, Any]] = {}
    segment_final_requested: dict[str, bool] = {}
    session_epoch = 0

    async def send_json(payload: dict[str, Any]) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    async def send_snapshot_to_current_client() -> None:
        nonlocal attendee_target_language
        if not room_id:
            return
        target = cfg["tgt"] if role == "presenter" else attendee_target_language
        snapshot = await build_room_snapshot(
            room_id,
            role=role,
            target_language=target,
            llm_client=llm_client,
            llm_model=cfg["llm"]["model"],
        )
        await send_json(snapshot)

    async def broadcast_snapshots_to_room() -> None:
        if not room_id:
            return
        normalized = normalize_room_id(room_id)
        async with ROOMS_LOCK:
            room = ROOMS.get(normalized)
            if room is None:
                return
            connections = list(room.get("connections") or [])
        failed: list[WebSocket] = []
        for connection in connections:
            conn_role = connection.get("role") or "attendee"
            conn_target = (
                cfg["tgt"]
                if conn_role == "presenter"
                else connection.get("target_language") or DEFAULT_TARGET_LANGUAGE
            )
            payload = await build_room_snapshot(
                normalized,
                role=conn_role,
                target_language=conn_target,
                llm_client=llm_client,
                llm_model=cfg["llm"]["model"],
            )
            ok = await send_room_connection_payload(connection, payload)
            if not ok and connection.get("websocket") is not None:
                failed.append(connection["websocket"])
        for stale_websocket in failed:
            await unregister_room_connection(normalized, stale_websocket)

    async def broadcast_segment_to_room(segment: dict[str, Any]) -> None:
        if not room_id:
            return
        normalized = normalize_room_id(room_id)
        async with ROOMS_LOCK:
            room = ROOMS.get(normalized)
            if room is None:
                return
            connections = list(room.get("connections") or [])
            presenter_tgt = room.get("presenter_tgt") or cfg["tgt"]
        failed: list[WebSocket] = []
        for connection in connections:
            conn_role = connection.get("role") or "attendee"
            target_language = (
                presenter_tgt
                if conn_role == "presenter"
                else connection.get("target_language") or presenter_tgt
            )
            translation = (segment.get("translations") or {}).get(target_language, "")
            if target_language == segment.get("src"):
                translation = segment.get("original") or ""
            elif (segment.get("original") or "").strip() and not translation:
                translation = await ensure_room_translation(
                    normalized,
                    segment_id=segment.get("segment_id") or "",
                    revision=int(segment.get("revision") or 0),
                    original=segment.get("original") or "",
                    src=segment.get("src") or cfg["src"],
                    target_language=target_language,
                    llm_client=llm_client,
                    llm_model=cfg["llm"]["model"],
                )
            payload = {
                "type": "segment",
                "segment_id": segment.get("segment_id") or "",
                "revision": int(segment.get("revision") or 0),
                "status": segment.get("status") or "listening",
                "is_final": bool(segment.get("is_final")),
                "original": segment.get("original") or "",
                "translation": translation,
                "src": segment.get("src") or cfg["src"],
                "tgt": target_language,
                "ts_ms": segment.get("ts_ms"),
            }
            ok = await send_room_connection_payload(connection, payload)
            if not ok and connection.get("websocket") is not None:
                failed.append(connection["websocket"])
        for stale_websocket in failed:
            await unregister_room_connection(normalized, stale_websocket)

    def next_segment_id() -> str:
        nonlocal segment_sequence
        segment_sequence += 1
        session_prefix = (
            re.sub(
                r"[^a-z0-9]",
                "",
                (client_session_id or "session").strip().lower(),
            )[:8]
            or "session"
        )
        return f"seg-{session_prefix}-{segment_sequence:04d}"

    def ensure_active_segment() -> tuple[str, int]:
        nonlocal active_segment_id, active_segment_started_ts
        if active_segment_id is None:
            active_segment_id = next_segment_id()
            active_segment_started_ts = int(time.time() * 1000)
        return active_segment_id, int(
            active_segment_started_ts or int(time.time() * 1000)
        )

    def reset_active_stream_state() -> None:
        nonlocal sentence_pcm, is_speaking, saw_end, silence_after_end_ms
        nonlocal active_segment_id, active_segment_started_ts, last_partial_analysis_at
        sentence_pcm = np.zeros((0,), dtype=np.int16)
        is_speaking = False
        saw_end = False
        silence_after_end_ms = 0
        active_segment_id = None
        active_segment_started_ts = None
        last_partial_analysis_at = 0.0
        vad_iterator.reset_states()

    async def cancel_analysis() -> None:
        nonlocal analysis_task, pending_snapshot
        pending_snapshot = None
        task = analysis_task
        analysis_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            print("Error while cancelling live analysis task")
            traceback.print_exc()

    async def emit_segment_update(
        *,
        segment_id: str,
        src: str,
        tgt: str,
        original: str,
        translation: str,
        ts_ms: int,
        is_final: bool,
    ) -> None:
        nonlocal recent_final_items
        original = (original or "").strip()
        translation = (translation or "").strip()
        if not original and not translation:
            return

        if not is_final and segment_final_requested.get(segment_id):
            return

        segment_state = emitted_segments.setdefault(
            segment_id,
            {
                "revision": 0,
                "original": "",
                "translation": "",
                "is_final": False,
            },
        )

        previous_is_final = bool(segment_state.get("is_final"))
        if (
            normalize_text(original)
            == normalize_text(segment_state.get("original", ""))
            and normalize_text(translation)
            == normalize_text(segment_state.get("translation", ""))
            and previous_is_final == is_final
        ):
            return

        segment_state["revision"] = int(segment_state.get("revision", 0)) + 1
        segment_state["original"] = original
        segment_state["translation"] = translation
        segment_state["is_final"] = is_final
        status = "final"
        if not is_final:
            status = "listening" if segment_state["revision"] == 1 else "refining"

        room_segment = await update_room_segment(
            room_id or uuid.uuid4().hex,
            segment_id=segment_id,
            revision=segment_state["revision"],
            status=status,
            is_final=is_final,
            original=original,
            src=src,
            ts_ms=ts_ms,
            tgt=tgt,
            translation=translation,
        )
        await broadcast_segment_to_room(room_segment)

        if is_final and not previous_is_final and original:
            await append_room_recording_transcript_item(
                room_id or uuid.uuid4().hex,
                segment_id=segment_id,
                item=TranscriptItem(
                    original=original,
                    translation=translation,
                    src=src,
                    tgt=tgt,
                    ts_ms=ts_ms,
                ),
            )

        if is_final and not previous_is_final and original and LIVE_CONTEXT_TURNS > 0:
            recent_final_items.append(
                TranscriptItem(
                    original=original,
                    translation=translation,
                    src=src,
                    tgt=tgt,
                    ts_ms=ts_ms,
                )
            )
            if len(recent_final_items) > LIVE_CONTEXT_TURNS:
                recent_final_items = recent_final_items[-LIVE_CONTEXT_TURNS:]

    async def analyze_snapshot(snapshot: dict[str, Any]) -> None:
        if snapshot.get("epoch") != session_epoch:
            return

        try:
            original = await transcribe_snapshot(
                pcm16_sentence=snapshot["pcm16_sentence"],
                src=snapshot["src"],
                whisper_client=whisper_client,
                whisper_model=cfg["whisper"]["model"],
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            print("Live transcription failed")
            traceback.print_exc()
            return

        if snapshot.get("epoch") != session_epoch or not original:
            return

        translation = ""
        try:
            translation = await translate_live_text(
                text=original,
                src=snapshot["src"],
                tgt=snapshot["tgt"],
                recent_items=snapshot.get("recent_items") or [],
                llm_client=llm_client,
                llm_model=cfg["llm"]["model"],
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            print("Live translation failed")
            traceback.print_exc()

        if snapshot.get("epoch") != session_epoch:
            return

        await emit_segment_update(
            segment_id=snapshot["segment_id"],
            src=snapshot["src"],
            tgt=snapshot["tgt"],
            original=original,
            translation=translation,
            ts_ms=snapshot["ts_ms"],
            is_final=bool(snapshot.get("is_final")),
        )

    async def process_pending_snapshots() -> None:
        nonlocal analysis_task, pending_snapshot
        try:
            while True:
                snapshot = pending_snapshot
                pending_snapshot = None
                if snapshot is None:
                    return
                await analyze_snapshot(snapshot)
        finally:
            analysis_task = None
            if pending_snapshot is not None:
                analysis_task = asyncio.create_task(process_pending_snapshots())

    def queue_snapshot(
        *, segment_id: str, ts_ms: int, src: str, tgt: str, is_final: bool
    ) -> None:
        nonlocal pending_snapshot, analysis_task
        if sentence_pcm.size == 0:
            return
        if is_final:
            segment_final_requested[segment_id] = True
        pending_snapshot = {
            "epoch": session_epoch,
            "segment_id": segment_id,
            "ts_ms": ts_ms,
            "src": src,
            "tgt": tgt,
            "is_final": is_final,
            "pcm16_sentence": sentence_pcm.copy(),
            "recent_items": list(recent_final_items),
        }
        if analysis_task is None:
            analysis_task = asyncio.create_task(process_pending_snapshots())

    try:
        while True:
            msg = await websocket.receive()
            if msg.get("text") is not None:
                try:
                    payload = json.loads(msg["text"])
                except Exception:
                    continue

                if payload.get("type") == "join":
                    requested_role = (
                        str(payload.get("role") or "presenter").strip().lower()
                    )
                    role = "attendee" if requested_role == "attendee" else "presenter"
                    if role == "presenter":
                        client_session_id = normalize_client_session_id(
                            payload.get("client_session_id")
                        )
                    requested_target = (
                        str(payload.get("target_language") or cfg["tgt"])
                        .strip()
                        .lower()
                    )
                    requested_room_id = (
                        str(payload.get("room_id") or "").strip().lower()
                    )
                    if role == "attendee":
                        if not is_valid_room_id(requested_room_id):
                            await send_json(
                                {
                                    "type": "error",
                                    "detail": "Enter a valid presenter room code.",
                                }
                            )
                            await websocket.close(code=1008)
                            continue
                        try:
                            room = await get_room_or_404(requested_room_id)
                        except HTTPException:
                            await send_json(
                                {
                                    "type": "error",
                                    "detail": "That room does not exist. Check the presenter room code and try again.",
                                }
                            )
                            await websocket.close(code=1008)
                            continue
                        room_id = room["room_id"]
                    else:
                        room_id, room = await get_or_create_room(requested_room_id)
                    if role == "presenter":
                        cfg["src"] = room.get("src") or cfg["src"]
                        cfg["tgt"] = room.get("presenter_tgt") or cfg["tgt"]
                        cfg["whisper"].update(room.get("whisper") or {})
                        cfg["llm"].update(room.get("llm") or {})
                        attendee_target_language = cfg["tgt"]
                    else:
                        attendee_target_language = (
                            requested_target
                            if requested_target in code_to_language
                            else room.get("presenter_tgt") or DEFAULT_TARGET_LANGUAGE
                        )
                    whisper_client = make_client(
                        cfg["whisper"]["base_url"], cfg["whisper"]["api_key"]
                    )
                    llm_client = make_client(
                        cfg["llm"]["base_url"], cfg["llm"]["api_key"]
                    )
                    await register_room_connection(
                        room_id,
                        websocket=websocket,
                        role=role,
                        target_language=attendee_target_language,
                        send_lock=send_lock,
                    )
                    await send_json(
                        {
                            "type": "joined",
                            "role": role,
                            "room_id": room_id,
                            "src": cfg["src"],
                            "tgt": (
                                cfg["tgt"]
                                if role == "presenter"
                                else attendee_target_language
                            ),
                        }
                    )
                    await send_json(serialize_room_state(room))
                    await send_snapshot_to_current_client()
                    continue

                if payload.get("type") == "config":
                    if role != "presenter" or not room_id:
                        continue
                    src = (payload.get("src") or "").strip().lower()
                    tgt = (payload.get("tgt") or "").strip().lower()
                    if src in code_to_language:
                        cfg["src"] = src
                    if tgt in code_to_language:
                        cfg["tgt"] = tgt

                    w = payload.get("whisper") or {}
                    if isinstance(w, dict):
                        if w.get("base_url") is not None:
                            new_url = str(w.get("base_url") or "").strip()
                            if new_url:
                                cfg["whisper"]["base_url"] = new_url
                        if w.get("api_key") is not None:
                            new_key = str(w.get("api_key") or "").strip()
                            if new_key:
                                cfg["whisper"]["api_key"] = new_key
                        if w.get("model") is not None:
                            cfg["whisper"]["model"] = str(w.get("model") or "").strip()

                    l = payload.get("llm") or {}
                    if isinstance(l, dict):
                        if l.get("base_url") is not None:
                            new_url = str(l.get("base_url") or "").strip()
                            if new_url:
                                cfg["llm"]["base_url"] = new_url
                        if l.get("api_key") is not None:
                            new_key = str(l.get("api_key") or "").strip()
                            if new_key:
                                cfg["llm"]["api_key"] = new_key
                        if l.get("model") is not None:
                            cfg["llm"]["model"] = str(l.get("model") or "").strip()

                    whisper_client = make_client(
                        cfg["whisper"]["base_url"], cfg["whisper"]["api_key"]
                    )
                    llm_client = make_client(
                        cfg["llm"]["base_url"], cfg["llm"]["api_key"]
                    )

                    async with ROOMS_LOCK:
                        room = ROOMS.get(room_id)
                        if room is not None:
                            room["src"] = cfg["src"]
                            room["presenter_tgt"] = cfg["tgt"]
                            room["whisper"] = dict(cfg["whisper"])
                            room["llm"] = dict(cfg["llm"])
                            room["updated_at"] = time.time()

                    session_epoch += 1
                    recent_final_items = []
                    segment_final_requested = {}
                    await cancel_analysis()
                    reset_active_stream_state()

                    await send_json(
                        {
                            "type": "ack",
                            "room_id": room_id,
                            "src": cfg["src"],
                            "tgt": cfg["tgt"],
                            "whisper": {
                                "base_url": cfg["whisper"]["base_url"],
                                "model": cfg["whisper"]["model"],
                            },
                            "llm": {
                                "base_url": cfg["llm"]["base_url"],
                                "model": cfg["llm"]["model"],
                            },
                        }
                    )
                    await broadcast_room_state(room_id)
                    await send_snapshot_to_current_client()
                    continue

                if payload.get("type") == "set_target_language":
                    if role != "attendee" or not room_id:
                        continue
                    requested = (
                        str(payload.get("target_language") or "").strip().lower()
                    )
                    if requested not in code_to_language:
                        continue
                    attendee_target_language = requested
                    async with ROOMS_LOCK:
                        room = ROOMS.get(room_id)
                        if room is not None:
                            for connection in room.get("connections") or []:
                                if connection.get("websocket") is websocket:
                                    connection["target_language"] = requested
                                    break
                            room["updated_at"] = time.time()
                    await send_snapshot_to_current_client()
                    continue

                if payload.get("type") == "recording":
                    if role != "presenter" or not room_id:
                        continue
                    action = str(payload.get("action") or "").strip().lower()
                    if action == "start":
                        await start_or_resume_room_recording(
                            room_id, client_session_id=client_session_id
                        )
                        await broadcast_room_state(room_id)
                    if action == "pause":
                        await pause_room_recording(
                            room_id, client_session_id=client_session_id
                        )
                        await broadcast_room_state(room_id)
                    continue

                if payload.get("type") == "clear_session":
                    if role != "presenter" or not room_id:
                        continue
                    session_epoch += 1
                    recent_final_items = []
                    emitted_segments = {}
                    segment_final_requested = {}
                    await cancel_analysis()
                    reset_active_stream_state()
                    await clear_room_session(room_id)
                    await broadcast_room_state(room_id)
                    await broadcast_snapshots_to_room()
                continue

            data = msg.get("bytes")
            if role != "presenter" or not room_id or not data:
                continue

            pcm_chunk = np.frombuffer(data, dtype=np.int16)
            if pcm_chunk.size == 0:
                continue

            samples_t = pcm16le_bytes_to_float_tensor(data)
            if samples_t.numel() == 0:
                continue

            chunk_ms = int(len(pcm_chunk) / SAMPLE_RATE * 1000)
            vad_event = vad_iterator(samples_t)

            if vad_event is not None:
                if "start" in vad_event:
                    if not is_speaking:
                        print("👉 Speech started")
                        is_speaking = True
                        sentence_pcm = np.zeros((0,), dtype=np.int16)
                        active_segment_id, active_segment_started_ts = (
                            ensure_active_segment()
                        )
                    saw_end = False
                    silence_after_end_ms = 0

                if "end" in vad_event and is_speaking:
                    print("🟡 Speech ended (VAD). Waiting for tail silence…")
                    saw_end = True
                    silence_after_end_ms = 0

            if is_speaking:
                sentence_pcm = np.concatenate([sentence_pcm, pcm_chunk], axis=0)
                active_segment_id, active_segment_started_ts = ensure_active_segment()

            if is_speaking and saw_end:
                silence_after_end_ms += chunk_ms

            if is_speaking:
                sentence_ms = int(len(sentence_pcm) / SAMPLE_RATE * 1000)
                now = time.monotonic()
                if (
                    active_segment_id is not None
                    and sentence_ms >= LIVE_MIN_PARTIAL_AUDIO_MS
                    and now - last_partial_analysis_at
                    >= LIVE_PARTIAL_UPDATE_MS / 1000.0
                ):
                    src, tgt = cfg["src"], cfg["tgt"]
                    queue_snapshot(
                        segment_id=active_segment_id,
                        ts_ms=int(active_segment_started_ts or int(time.time() * 1000)),
                        src=src,
                        tgt=tgt,
                        is_final=False,
                    )
                    last_partial_analysis_at = now

                if sentence_ms > MAX_SENTENCE_MS:
                    print(
                        f"⚠️ Max sentence length reached; forcing flush. [{sentence_ms} ms]"
                    )
                    src, tgt = cfg["src"], cfg["tgt"]
                    segment_id = active_segment_id
                    started_ts = int(
                        active_segment_started_ts or int(time.time() * 1000)
                    )
                    if segment_id is not None:
                        queue_snapshot(
                            segment_id=segment_id,
                            ts_ms=started_ts,
                            src=src,
                            tgt=tgt,
                            is_final=True,
                        )

                    reset_active_stream_state()
                    continue

            if is_speaking and saw_end:
                latest_original = ""
                if active_segment_id is not None:
                    latest_original = (
                        (emitted_segments.get(active_segment_id) or {}).get("original")
                        or ""
                    ).strip()
                required_silence_ms = (
                    LIVE_INCOMPLETE_FINALIZE_MS
                    if latest_original and not looks_sentence_complete(latest_original)
                    else LIVE_COMPLETE_SILENCE_MS
                )
                if silence_after_end_ms >= required_silence_ms:
                    sentence_ms = int(len(sentence_pcm) / SAMPLE_RATE * 1000)
                    print(
                        "✅ End of sentence confirmed. Processing… "
                        f"[{sentence_ms} ms, silence={silence_after_end_ms} ms, required={required_silence_ms} ms]"
                    )

                    src, tgt = cfg["src"], cfg["tgt"]
                    segment_id = active_segment_id
                    started_ts = int(
                        active_segment_started_ts or int(time.time() * 1000)
                    )
                    if segment_id is not None:
                        queue_snapshot(
                            segment_id=segment_id,
                            ts_ms=started_ts,
                            src=src,
                            tgt=tgt,
                            is_final=True,
                        )

                    reset_active_stream_state()

    except WebSocketDisconnect:
        print("Client disconnected")
    except Exception as e:
        print(f"Fatal WS error: {e}")
        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        session_epoch += 1
        await cancel_analysis()
        await unregister_room_connection(room_id, websocket)
        if role == "presenter" and room_id:
            await pause_room_recording(room_id, client_session_id=client_session_id)
            await broadcast_room_state(room_id)
