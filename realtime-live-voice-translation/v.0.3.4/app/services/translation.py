import time

import numpy as np
from openai import AsyncOpenAI

from app.audio.ffmpeg import rms_dbfs, wav_bytesio_from_pcm16
from app.config import (
    DEFAULT_SOURCE_LANGUAGE,
    DEFAULT_TARGET_LANGUAGE,
    LIVE_CONTEXT_TURNS,
    MIN_AUDIO_MS,
    MIN_SEND_TEXT_CHARS,
    SAMPLE_RATE,
    SILENCE_DBFS_THRESHOLD,
)
from app.prompts import build_live_translation_prompt, build_translator_system_prompt
from app.schemas.api import TranscriptItem
from app.state.rooms import (
    ROOMS,
    ROOMS_LOCK,
    normalize_room_id,
    remember_room_translation_language,
)
from app.utils.text import clean_translation


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
                    recent_items=recent_items[-LIVE_CONTEXT_TURNS:] if LIVE_CONTEXT_TURNS > 0 else [],
                ),
            }
        ],
        temperature=0,
        max_tokens=200,
        stop=["\n\n", "```"],
    )
    print("Live translation took:", time.perf_counter() - start_time)
    return clean_translation(chat_resp.choices[0].message.content or "")


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
            remember_room_translation_language(room, target)
            room["updated_at"] = time.time()
        return (segment.get("translations") or {}).get(target, translated)


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
        src = (item.src or DEFAULT_SOURCE_LANGUAGE).strip().lower() or DEFAULT_SOURCE_LANGUAGE
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
