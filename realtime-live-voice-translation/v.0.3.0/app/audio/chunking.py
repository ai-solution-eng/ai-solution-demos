from typing import Any

import numpy as np

from app.audio.vad import get_speech_timestamps_safe
from app.config import (
    EXPORT_CHUNK_SECONDS,
    EXPORT_OVERLAP_SECONDS,
    EXPORT_VAD_MAX_UNIT_SECONDS,
    EXPORT_VAD_MERGE_GAP_MS,
    SAMPLE_RATE,
)


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
        if normalized_regions and start - normalized_regions[-1][1] <= merge_gap_samples:
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
