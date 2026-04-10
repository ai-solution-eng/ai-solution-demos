import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv

try:
    import torch
except ModuleNotFoundError:
    torch = None

_ = load_dotenv()

TORCH_NUM_THREADS = max(1, int(os.getenv("TORCH_NUM_THREADS", "1")))
TORCH_NUM_INTEROP_THREADS = max(1, int(os.getenv("TORCH_NUM_INTEROP_THREADS", "1")))

if torch is not None:
    try:
        torch.set_num_threads(TORCH_NUM_THREADS)
    except Exception:
        pass

    try:
        torch.set_num_interop_threads(TORCH_NUM_INTEROP_THREADS)
    except Exception:
        pass

DEFAULT_WHISPER_BASE_URL = os.getenv("WHISPER_BASE_URL", "")
DEFAULT_WHISPER_API_KEY = os.getenv("WHISPER_API_KEY", "")
DEFAULT_WHISPER_MODEL = os.getenv("WHISPER_MODEL", "openai/whisper-large-v3-turbo")

DEFAULT_LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
DEFAULT_LLM_API_KEY = os.getenv("LLM_API_KEY", "")
DEFAULT_LLM_MODEL = os.getenv("LLM_MODEL", "Qwen/Qwen3-30B-A3B-Instruct-2507-FP8")

DEFAULT_SOURCE_LANGUAGE = os.getenv("SOURCE_LANGUAGE", "en")
DEFAULT_TARGET_LANGUAGE = os.getenv("TARGET_LANGUAGE", "es")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_POOL_SIZE = max(1, int(os.getenv("DB_POOL_SIZE", "5")))
DB_MAX_OVERFLOW = max(0, int(os.getenv("DB_MAX_OVERFLOW", "10")))
SESSION_TTL_HOURS = max(1, int(os.getenv("SESSION_TTL_HOURS", "12")))
SESSION_STORAGE_ROOT = Path(
    os.getenv(
        "SESSION_STORAGE_ROOT",
        str(Path(tempfile.gettempdir()) / "realtime-live-voice-translation-sessions"),
    )
)
SESSION_STORAGE_ROOT.mkdir(parents=True, exist_ok=True)

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
    Path(
        os.getenv(
            "RECORDING_STORAGE_ROOT",
            str(SESSION_STORAGE_ROOT / "recordings"),
        )
    )
)
RECORDING_STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
EXPORT_STORAGE_ROOT = Path(
    os.getenv("EXPORT_STORAGE_ROOT", str(SESSION_STORAGE_ROOT / "exports"))
)
EXPORT_STORAGE_ROOT.mkdir(parents=True, exist_ok=True)

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


def build_default_runtime_config() -> dict[str, object]:
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


def build_defaults_payload() -> dict[str, object]:
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
