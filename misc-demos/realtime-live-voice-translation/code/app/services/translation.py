import time
from typing import Any

import numpy as np
from openai import AsyncOpenAI

from app.audio.ffmpeg import rms_dbfs, wav_bytesio_from_pcm16
from app.config import (
    DEFAULT_SOURCE_LANGUAGE,
    DEFAULT_TARGET_LANGUAGE,
    HALLUCINATION_SET,
    LIVE_CONTEXT_TURNS,
    LLM_MAX_TOKENS,
    LLM_RETRY_MAX_TOKENS,
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
from app.utils.text import clean_translation, looks_like_meta_reply


async def _chat_completion(
    *,
    llm_client: AsyncOpenAI,
    llm_model: str,
    messages: list[dict[str, str]],
) -> Any:
    """Single chat-completion entry point for all translation calls.

    Hybrid-reasoning models (e.g. GLM, Qwen with enable_thinking) may spend
    the whole token budget reasoning and return empty message.content — which
    rendered on screen as a final segment with "Awaiting translated output."
    Two guards:

    1. chat_template_kwargs {"enable_thinking": False} asks such models to
       answer directly (passed via extra_body, the SDK-sanctioned channel for
       provider-specific fields; vLLM merges it into the request payload.
       OpenAI-hosted models ignore it; also kills the stop-string truncation
       risk, since reasoning traces hit the "\n\n" stop before answer text).
    2. If content still comes back empty, retry ONCE with a generous budget
       and no stop strings, logging the reasoning/answer channel sizes so
       empty-translation incidents are diagnosable from logs.
    """
    body: dict[str, Any] = {
        "model": llm_model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": LLM_MAX_TOKENS,
        # extra_body = SDK-sanctioned passthrough for provider-specific
        # request fields. NOTE: chat_template_kwargs must be a sibling of
        # extra_body's contents, NOT a top-level kwarg — passing it directly
        # to create() raises TypeError (AsyncCompletions.create() got an
        # unexpected keyword argument).
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }
    response = await llm_client.chat.completions.create(**body)

    choice = response.choices[0]
    message = choice.message
    content = (getattr(message, "content", "") or "").strip()
    finish = getattr(choice, "finish_reason", "") or ""

    # Meta-commentary guard: a reply that narrates the task ("The user wants
    # me to translate...") is unusable on a live caption screen — treat it
    # like an empty answer and force the retry below.
    if content and looks_like_meta_reply(content):
        print(
            "LLM replied with meta-commentary instead of a translation "
            f"(starts with: {content[:80]!r}); retrying once"
        )
        content = ""

    if content:
        return response

    # Empty (or meta) answer: gather diagnosis from whichever channels the
    # model filled.
    reasoning = getattr(message, "reasoning_content", None) or getattr(
        getattr(message, "model_extra", None) or {}, "reasoning_content", ""
    ) or ""
    if not reasoning:
        print(
            "LLM returned empty content "
            f"(finish_reason={finish!r}, max_tokens={LLM_MAX_TOKENS}); "
            "retrying once without stop strings and with a larger budget"
        )

    retry_body = dict(body)
    retry_body["max_tokens"] = LLM_RETRY_MAX_TOKENS
    retry_body.pop("stop", None)
    # Force a stricter reminder on the retry: the first attempt drifted into
    # assistant mode (or produced nothing), so re-state the output contract
    # as the final user message.
    retry_messages = list(messages)
    retry_messages.append({
        "role": "user",
        "content": (
            "Output the translation only. No preamble, no explanation, no "
            "quoting of the task — reply with the translated text alone."
        ),
    })
    retry_body["messages"] = retry_messages
    retry_body["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
    response = await llm_client.chat.completions.create(**retry_body)

    message = response.choices[0].message
    content = (getattr(message, "content", "") or "").strip()
    reasoning = getattr(message, "reasoning_content", None) or getattr(
        getattr(message, "model_extra", None) or {}, "reasoning_content", ""
    ) or ""
    if not content:
        print(
            "LLM retry STILL unusable "
            f"(finish_reason={getattr(response.choices[0], 'finish_reason', '')!r}, "
            f"reasoning_chars={len(reasoning)}) — likely a reasoning-mode or "
            "template mismatch on this endpoint; consider switching LLM_MODEL "
            "to a plain instruct model"
        )
    return response


async def transcribe_and_translate(
    *,
    pcm16_sentence: np.ndarray,
    src: str,
    tgt: str,
    asr_client: AsyncOpenAI,
    asr_model: str,
    llm_client: AsyncOpenAI,
    llm_model: str,
) -> tuple[str, str]:
    source_language_text = await transcribe_snapshot(
        pcm16_sentence=pcm16_sentence,
        src=src,
        asr_client=asr_client,
        asr_model=asr_model,
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
    asr_client: AsyncOpenAI,
    asr_model: str,
    hallucination_set: set[str] = HALLUCINATION_SET,
    drop_reason: list[str] | None = None,
) -> str:
    """Transcribe a segment; returns "" when the audio/text is dropped.

    When ``drop_reason`` is provided, the first element is set to a short
    machine-readable reason ("too_short" | "silent" | "text_too_short" |
    "hallucination" | "sound_effect") so the caller can distinguish
    "nothing was said" from an ASR/API failure — silent drops here were
    leaving final segments stuck on "Awaiting translated output." with no
    diagnosable trace.
    """
    def _drop(reason: str) -> str:
        if drop_reason is not None:
            drop_reason.append(reason)
        return ""

    duration_ms = len(pcm16_sentence) * 1000 / SAMPLE_RATE
    if duration_ms < MIN_AUDIO_MS:
        return _drop("too_short")

    if rms_dbfs(pcm16_sentence) < SILENCE_DBFS_THRESHOLD:
        return _drop("silent")

    wav_io = wav_bytesio_from_pcm16(pcm16_sentence, sr=SAMPLE_RATE)

    print("sent audio to ASR model, waiting for transcription...")
    start_time = time.perf_counter()

    transcript_resp = await asr_client.audio.transcriptions.create(
        model=asr_model,
        file=wav_io,
        language=src,
    )
    print("Transcription took:", time.perf_counter() - start_time)

    source_language_text = (getattr(transcript_resp, "text", "") or "").strip()
    if len(source_language_text) <= MIN_SEND_TEXT_CHARS:
        return _drop("text_too_short")

    normalized = source_language_text.lower()

    if hallucination_set and normalized in hallucination_set:
        print(f"Hallucination filtered: '{source_language_text}'")
        return _drop("hallucination")

    if normalized.startswith("*") and normalized.endswith("*"):
        print(f"Sound effect filtered: '{source_language_text}'")
        return _drop("sound_effect")

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
    chat_resp = await _chat_completion(
        llm_client=llm_client,
        llm_model=llm_model,
        messages=[
            {"role": "system", "content": build_translator_system_prompt(src, tgt)},
            {"role": "user", "content": text},
        ],
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
    chat_resp = await _chat_completion(
        llm_client=llm_client,
        llm_model=llm_model,
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
