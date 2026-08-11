"""
Async HTTP client for the pyannote diarization microservice.
"""

import logging
import os
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

DIARIZATION_BASE_URL = os.environ.get(
    "DIARIZATION_BASE_URL",
    "http://conversation-toolbox-diarization:8001",
)

DIARIZATION_TIMEOUT = float(os.environ.get("DIARIZATION_TIMEOUT", "300.0"))


@dataclass
class DiarizationSegment:
    speaker: str
    start: float
    end: float


@dataclass
class DiarizationResult:
    segments: list[DiarizationSegment] = field(default_factory=list)
    exclusive_segments: list[DiarizationSegment] = field(default_factory=list)
    duration: float = 0.0
    num_speakers_detected: int = 0


async def diarize_audio(
    audio_path: str,
    num_speakers: int = 0,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    base_url: str = DIARIZATION_BASE_URL,
) -> DiarizationResult:
    """
    Send an audio file to the diarization service and return labeled segments.

    Args:
        audio_path: Path to WAV file (16kHz mono recommended).
        num_speakers: Known speaker count (0 = auto-detect).
        min_speakers: Lower bound on speaker count.
        max_speakers: Upper bound on speaker count.
        base_url: Diarization service URL.

    Returns:
        DiarizationResult with segments, exclusive_segments, duration.
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(DIARIZATION_TIMEOUT)) as client:
        with open(audio_path, "rb") as f:
            files = {"file": (os.path.basename(audio_path), f, "audio/wav")}
            data = {}
            if num_speakers > 0:
                data["num_speakers"] = str(num_speakers)
            if min_speakers is not None:
                data["min_speakers"] = str(min_speakers)
            if max_speakers is not None:
                data["max_speakers"] = str(max_speakers)

            response = await client.post(
                f"{base_url.rstrip('/')}/diarize",
                files=files,
                data=data,
            )
            response.raise_for_status()
            result = response.json()

    segments = [DiarizationSegment(**s) for s in result.get("segments", [])]
    exclusive = [DiarizationSegment(**s) for s in result.get("exclusive_segments", [])]

    return DiarizationResult(
        segments=segments,
        exclusive_segments=exclusive,
        duration=result.get("duration", 0.0),
        num_speakers_detected=result.get("num_speakers_detected", 0),
    )
