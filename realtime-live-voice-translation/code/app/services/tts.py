import asyncio
import io
import os
import re
import time
import uuid
from typing import Any

import httpx
import numpy as np
from openai import AsyncOpenAI

from app.audio.ffmpeg import ffmpeg_convert_to_pcm_wav, wav_bytesio_from_pcm16, SAMPLE_RATE


_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_URL_RE = re.compile(
    r"(?:https?|ftp|s3|gs|abfs|abfss|wasb|wasbs|file|azure|sftp)://[^\s)\"'\]]+",
    re.IGNORECASE,
)
_WWW_RE = re.compile(r"\bwww\.[^\s)\"'\]]+", re.IGNORECASE)
_ELLIPSIS_RE = re.compile(r"\.{2,}|\u2026")
_MD_MARKERS_RE = re.compile(r"[*_`~]")
_WS_RE = re.compile(r"[ \t]{2,}")


def _tts_strip_v1(base: str) -> str:
    url = base.rstrip("/")
    return url[:-3] if url.endswith("/v1") else url


def sanitize_text_for_tts(text: str) -> str:
    if not text:
        return text
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub(" ", text)
    text = _WWW_RE.sub(" ", text)
    text = _ELLIPSIS_RE.sub(". ", text)
    text = _MD_MARKERS_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    return text.strip()


def convert_upload_to_wav_bytes(audio_bytes: bytes, filename: str) -> bytes:
    ext = os.path.splitext(filename or "")[1].lower() or ".webm"
    pcm_samples, _ = ffmpeg_convert_to_pcm_wav(audio_bytes, ext)
    buf = wav_bytesio_from_pcm16(pcm_samples, sr=SAMPLE_RATE)
    return buf.getvalue()


async def synthesize_speech(
    *,
    text: str,
    voice: str,
    tts_client: AsyncOpenAI,
    tts_model: str,
) -> bytes:
    cleaned = sanitize_text_for_tts(text)
    if not cleaned:
        return b""

    print(f"Sending text to TTS for synthesis (voice={voice})...")
    start_time = time.perf_counter()

    response = await tts_client.audio.speech.create(
        model=tts_model,
        voice=voice,
        input=cleaned,
        response_format="wav",
    )

    print("TTS synthesis took:", time.perf_counter() - start_time)
    return response.content


async def list_voices(*, base_url: str, api_key: str) -> dict[str, Any]:
    base = _tts_strip_v1(base_url)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        resp = await client.get(f"{base}/v1/audio/voices", headers=headers)
    return {"status": resp.status_code, "data": resp.json()}


async def upload_voice(
    *,
    base_url: str,
    api_key: str,
    audio_bytes: bytes,
    filename: str,
    name: str,
    consent: str,
    ref_text: str = "",
    speaker_description: str = "",
) -> dict[str, Any]:
    base = _tts_strip_v1(base_url)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    wav_bytes = convert_upload_to_wav_bytes(audio_bytes, filename)
    clean_name = os.path.splitext(filename or "sample")[0]
    files = {
        "audio_sample": (
            clean_name + ".wav",
            wav_bytes,
            "audio/wav",
        )
    }
    data: dict[str, str] = {"name": name, "consent": consent}
    if ref_text:
        data["ref_text"] = ref_text
    if speaker_description:
        data["speaker_description"] = speaker_description

    async with httpx.AsyncClient(verify=False, timeout=120.0) as client:
        resp = await client.post(f"{base}/v1/audio/voices", headers=headers, files=files, data=data)
    return {"status": resp.status_code, "data": resp.json()}


async def delete_voice(*, base_url: str, api_key: str, name: str) -> dict[str, Any]:
    base = _tts_strip_v1(base_url)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        resp = await client.delete(f"{base}/v1/audio/voices/{name}", headers=headers)
    return {"status": resp.status_code, "data": resp.json()}


_TTS_CACHE_MAX_ENTRIES = 200


class TTSCache:
    def __init__(self, max_entries: int = _TTS_CACHE_MAX_ENTRIES):
        self._store: dict[tuple[str, str, str], bytes] = {}
        self._lock = asyncio.Lock()
        self._max = max_entries

    async def get(self, segment_id: str, lang: str, voice: str) -> bytes | None:
        key = (segment_id, lang, voice)
        async with self._lock:
            return self._store.get(key)

    async def set(self, segment_id: str, lang: str, voice: str, audio: bytes) -> None:
        key = (segment_id, lang, voice)
        async with self._lock:
            if len(self._store) >= self._max:
                oldest_key = next(iter(self._store))
                self._store.pop(oldest_key, None)
            self._store[key] = audio

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()
