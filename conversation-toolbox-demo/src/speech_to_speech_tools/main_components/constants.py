import logging
import os
import uuid

logger = logging.getLogger(__name__)


def return_uuid(input_name: str | None = None) -> str:
    return input_name or str(uuid.uuid4())


# Placeholder strings used when no API key is configured via env. These must
# never be sent to a model endpoint as a Bearer token, so callers resolve keys
# through ``resolve_api_key`` which treats them as empty.
_PLACEHOLDER_KEY_SENTINELS = frozenset(
    {
        "",
        "YOUR_ASR_API_KEY_HERE",
        "YOUR_LLM_API_KEY_HERE",
        "YOUR_TTS_API_KEY_HERE",
    }
)


def resolve_api_key(*candidates: str) -> str:
    """Return the first candidate that is a usable key, else ``''``.

    Placeholder sentinels and empty strings are skipped so that a missing
    server-side key correctly falls through to the next source (env var,
    registry model, etc.) instead of being sent as a bogus Bearer token.
    """
    for c in candidates:
        if c and c not in _PLACEHOLDER_KEY_SENTINELS:
            return c
    return ""


# Configurable Constants
def _hallucination_updater(hallucinations: str) -> set[str]:
    data = set()
    split_hallucinations = {x.strip().lower() for x in hallucinations.split(",")}
    periods = {x + "." for x in split_hallucinations}
    exclamations = {x + "!" for x in split_hallucinations}
    data.update(split_hallucinations)
    data.update(periods)
    data.update(exclamations)
    return data


HALLUCINATION_PATTERNS = os.environ.get(
    "HALLUCINATION_PATTERNS",
    "thank you,thanks,продолжение следует",
)
DEFAULT_CONFIG = {
    "remote": False,
    "asrBaseUrl": os.environ.get(
        "ASR_BASE_URL",
        "https://cohere-transcribe-03-2026.project-user-andrew-bydlon.serving"
        ".pcai-se-ai-application.hst.rdlabs.hpecorp.net",
    ),
    "asrApiKey": os.environ.get("ASR_API_KEY", "YOUR_ASR_API_KEY_HERE"),
    "asrModelName": os.environ.get("ASR_MODEL_NAME", ""),
    "asrHallucinationPatterns": os.environ.get("HALLUCINATION_PATTERNS", HALLUCINATION_PATTERNS),
    "asrHallucinationPatternsSet": _hallucination_updater(
        os.environ.get("HALLUCINATION_PATTERNS", HALLUCINATION_PATTERNS)
    ),
    "llmBaseUrl": os.environ.get(
        "LLM_BASE_URL",
        "https://gemma-4-31b-ab.project-user-andrew-bydlon.serving.pcai-se-ai-application.hst.rdlabs.hpecorp.net",
    ),
    "llmApiKey": os.environ.get("LLM_API_KEY", "YOUR_LLM_API_KEY_HERE"),
    "llmModelName": os.environ.get("LLM_MODEL_NAME", ""),
    "systemPrompt": os.environ.get(
        "SYSTEM_PROMPT",
        "You are a helpful, concise voice assistant. Keep responses short for voice conversation.",
    ),
    "ttsBaseUrl": os.environ.get("TTS_BASE_URL", ""),
    "ttsApiKey": os.environ.get("TTS_API_KEY", ""),
    "ttsModelName": os.environ.get("TTS_MODEL_NAME", ""),
    "ttsVoice": os.environ.get("TTS_VOICE", "alys"),
    "language": os.environ.get("LANGUAGE", ""),
    "sampleRate": int(os.environ.get("SAMPLE_RATE", "16000")),
    "vadAggression": int(os.environ.get("VAD_AGGRESSION", "2")),
    "rmsThreshold": int(os.environ.get("RMS_THRESHOLD", "200")),
    "frameDuration": 30,
    "diarizationBaseUrl": os.environ.get(
        "DIARIZATION_BASE_URL",
        "http://conversation-toolbox-diarization:8001",
    ),
    "toolCalls": None,
}

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis-service:6379/0")
TRANSCRIPTS_DIR = os.environ.get("TRANSCRIPTS_DIR", "/mnt/persistent/transcripts")
AUDIO_DIR = os.environ.get("AUDIO_DIR", "/mnt/persistent/recordings")
