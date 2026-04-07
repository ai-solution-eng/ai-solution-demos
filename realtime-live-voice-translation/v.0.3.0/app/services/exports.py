import asyncio
import io
import json
import re
import traceback
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from openai import AsyncOpenAI

from app.audio.chunking import build_audio_chunks, build_vad_export_chunks
from app.audio.ffmpeg import ffmpeg_convert_to_pcm_wav, wav_bytesio_from_pcm16
from app.config import (
    DEFAULT_LLM_API_KEY,
    DEFAULT_LLM_BASE_URL,
    DEFAULT_LLM_MODEL,
    DEFAULT_SOURCE_LANGUAGE,
    DEFAULT_WHISPER_API_KEY,
    DEFAULT_WHISPER_BASE_URL,
    DEFAULT_WHISPER_MODEL,
    EXPORT_CHUNK_SECONDS,
    EXPORT_MAX_ASR_CONCURRENCY,
    EXPORT_OVERLAP_SECONDS,
    EXPORT_TEXT_BATCH_CHARS,
    EXPORT_TEXT_BATCH_SEGMENTS,
    SAMPLE_RATE,
    code_to_language,
)
from app.prompts import (
    build_batch_english_cleanup_prompt,
    build_batch_translation_prompt,
    build_chunk_notes_prompt,
    build_document_translation_prompt,
    build_minutes_prompt,
    build_summary_prompt,
)
from app.schemas.api import (
    ExportChunkResult,
    ExportLlmConfig,
    ExportSegment,
    ExportWhisperConfig,
    JsonTranslationBatch,
    MeetingPackageResult,
    TranscriptItem,
)
from app.services.clients import make_client
from app.state.export_jobs import update_export_job
from app.utils.text import (
    build_multilingual_source_text,
    clean_llm_document,
    extract_json_string,
    format_seconds_label,
    get_lang_name,
    normalize_text,
    safe_json_loads,
    split_words,
    to_plain_dict,
)


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
    for idx, (item, (start_s, end_s)) in enumerate(zip(filtered_items, ranges), start=1):
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


def build_segments_from_room_segments(
    room_segments: list[dict[str, Any]],
    *,
    duration_s: float,
    speech_chunks: list[dict[str, Any]],
) -> list[ExportSegment]:
    finalized_segments = []
    for segment in room_segments:
        if not segment.get("is_final"):
            continue
        original_text = re.sub(r"\s+", " ", (segment.get("original") or "").strip())
        if not original_text:
            continue
        finalized_segments.append(
            {
                "original": original_text,
                "src": (segment.get("src") or DEFAULT_SOURCE_LANGUAGE).strip().lower(),
                "ts_ms": segment.get("ts_ms"),
                "translations": dict(segment.get("translations") or {}),
            }
        )

    if not finalized_segments:
        return []

    base_segments = build_segments_from_transcript_items(
        [
            TranscriptItem(
                original=item["original"],
                src=item["src"],
                ts_ms=item["ts_ms"],
            )
            for item in finalized_segments
        ],
        duration_s=duration_s,
        speech_chunks=speech_chunks,
    )

    for export_segment, source_segment in zip(base_segments, finalized_segments):
        translations: dict[str, str] = {}
        for code, translated_text in source_segment["translations"].items():
            normalized = (code or "").strip().lower()
            cleaned_text = re.sub(r"\s+", " ", (translated_text or "").strip())
            if normalized in code_to_language and cleaned_text:
                translations[normalized] = cleaned_text
        export_segment.translations.update(translations)
        if source_segment["src"] == "en":
            export_segment.english = source_segment["original"]
        elif translations.get("en"):
            export_segment.english = translations["en"]

    return base_segments


def transcript_items_from_room_segments(
    room_segments: list[dict[str, Any]],
) -> list[TranscriptItem]:
    items: list[TranscriptItem] = []
    for segment in room_segments:
        if not segment.get("is_final"):
            continue
        original_text = re.sub(r"\s+", " ", (segment.get("original") or "").strip())
        if not original_text:
            continue
        items.append(
            TranscriptItem(
                original=original_text,
                src=(segment.get("src") or DEFAULT_SOURCE_LANGUAGE).strip().lower(),
                ts_ms=segment.get("ts_ms"),
            )
        )
    return items


def build_segments_from_room_segments_without_audio(
    room_segments: list[dict[str, Any]],
) -> list[ExportSegment]:
    finalized_segments = []
    for segment in room_segments:
        if not segment.get("is_final"):
            continue
        original_text = re.sub(r"\s+", " ", (segment.get("original") or "").strip())
        if not original_text:
            continue
        finalized_segments.append(
            {
                "original": original_text,
                "src": (segment.get("src") or DEFAULT_SOURCE_LANGUAGE).strip().lower(),
                "ts_ms": int(segment.get("ts_ms") or 0),
                "translations": dict(segment.get("translations") or {}),
            }
        )
    if not finalized_segments:
        return []

    baseline_ts = (
        min(item["ts_ms"] for item in finalized_segments if item["ts_ms"] > 0)
        if any(item["ts_ms"] > 0 for item in finalized_segments)
        else 0
    )
    export_segments: list[ExportSegment] = []
    for idx, item in enumerate(finalized_segments, start=1):
        start_s = (
            max(0.0, (item["ts_ms"] - baseline_ts) / 1000.0)
            if baseline_ts
            else float(idx - 1)
        )
        export_segment = ExportSegment(
            id=idx,
            start_s=start_s,
            end_s=start_s,
            start_label=format_seconds_label(start_s),
            end_label=format_seconds_label(start_s),
            original=item["original"],
        )
        translations: dict[str, str] = {}
        for code, translated_text in item["translations"].items():
            normalized = (code or "").strip().lower()
            cleaned_text = re.sub(r"\s+", " ", (translated_text or "").strip())
            if normalized in code_to_language and cleaned_text:
                translations[normalized] = cleaned_text
        export_segment.translations.update(translations)
        if item["src"] == "en":
            export_segment.english = item["original"]
        elif translations.get("en"):
            export_segment.english = translations["en"]
        export_segments.append(export_segment)
    return export_segments


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


async def build_documents_from_export_segments(
    *,
    segments: list[ExportSegment],
    transcript_items: list[TranscriptItem],
    documents_source_items: list[TranscriptItem] | None,
    export_languages: list[str] | None,
    llm_client: AsyncOpenAI,
    llm_model: str,
    progress_cb: Any | None = None,
) -> MeetingPackageResult:
    async def _progress(pct: int, stage: str, detail: str) -> None:
        if progress_cb is not None:
            await progress_cb(pct, stage, detail)

    await _progress(
        78,
        "Detecting languages",
        "Identifying languages used in the meeting for translated exports.",
    )
    languages = language_codes_from_segments(
        segments,
        export_languages
        if export_languages is not None
        else language_codes_from_transcript(transcript_items),
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
    await _progress(88, "Writing meeting docs", "Generating meeting summary and minutes.")
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

    return MeetingPackageResult(languages=languages, documents=documents, segments=segments)


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
        segments.append({"start_s": abs_start, "end_s": abs_end, "text": content})

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
        if [word.lower() for word in prev_words[-k:]] == [word.lower() for word in cur_words[:k]]:
            return " ".join(cur_words[k:]).strip()
    return current_text.strip()


def merge_export_segments(chunk_results: list[ExportChunkResult]) -> list[ExportSegment]:
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


def batch_segments_for_translation(segments: list[ExportSegment]) -> list[list[ExportSegment]]:
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
    columns = ["segment", "start_s", "end_s", "original", "english"] + [
        f"translation_{lang}" for lang in languages if lang != "en"
    ]

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
    export_languages: list[str] | None,
    room_segments: list[dict[str, Any]] | None,
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
    pcm16, duration_s = await asyncio.to_thread(ffmpeg_convert_to_pcm_wav, audio_bytes, suffix)
    if duration_s <= 0 or len(pcm16) == 0:
        raise HTTPException(
            status_code=400,
            detail="The uploaded recording does not contain any decodable audio.",
        )

    await _progress(10, "Finding speech", "Detecting real speech regions in the saved recording.")
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
    if room_segments:
        segments = build_segments_from_room_segments(
            room_segments,
            duration_s=duration_s,
            speech_chunks=chunks,
        )
        await _progress(
            58,
            "Aligning live transcript",
            "Using the live transcript as the source of truth and aligning it to the recording.",
        )
    elif transcript_items:
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

    return await build_documents_from_export_segments(
        segments=segments,
        transcript_items=transcript_items,
        documents_source_items=documents_source_items,
        export_languages=export_languages,
        llm_client=llm_client,
        llm_model=llm_model,
        progress_cb=progress_cb,
    )


async def build_export_package_bytes(
    *,
    audio_bytes: bytes,
    audio_filename: str,
    transcript_items: list[TranscriptItem],
    documents_source_items: list[TranscriptItem] | None,
    export_languages: list[str] | None = None,
    room_segments: list[dict[str, Any]] | None = None,
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
        export_languages=export_languages,
        room_segments=room_segments,
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
    export_languages: list[str] | None = None,
    room_segments: list[dict[str, Any]] | None = None,
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
            export_languages=export_languages,
            room_segments=room_segments,
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
