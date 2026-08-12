"""
Background workers for ASR, TTS, and batch transcription.
Run with: arq workers.WorkerSettings
"""

import asyncio
import base64
import json
import logging
import os
from typing import ClassVar

from arq.connections import RedisSettings
from main_components import batch_transcription as bt
from utils.audio_handling import audio_bytes_to_wave_bytesio, extract_asr_text
from utils.pcai_model_classes import VoiceModel
from utils.redis_client import RedisClient
from utils.tts_sanitizer import sanitize_text_for_tts

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    handlers=[logging.StreamHandler()],
    force=True,
)


def _get_asr_model() -> VoiceModel:
    """Lazily create and cache the ASR model (reuses HTTP connection pool)."""
    global _asr_model
    if _asr_model is None:
        _asr_model = VoiceModel(
            model_name=os.environ.get("ASR_MODEL_NAME", ""),
            url_remote=os.environ.get("ASR_BASE_URL", ""),
            api_key=os.environ.get("ASR_API_KEY", ""),
            model_usage="remote",
        )
    return _asr_model


def _get_tts_model() -> VoiceModel:
    """Lazily create and cache the TTS model (reuses HTTP connection pool)."""
    global _tts_model
    if _tts_model is None:
        _tts_model = VoiceModel(
            model_name=os.environ.get("TTS_MODEL_NAME", ""),
            url_remote=os.environ.get("TTS_BASE_URL", ""),
            api_key=os.environ.get("TTS_API_KEY", ""),
            model_usage="remote",
            model_type="TTS",
            tts_voice=os.environ.get("TTS_VOICE", "alys"),
        )
    return _tts_model


_asr_model: VoiceModel | None = None
_tts_model: VoiceModel | None = None


async def transcribe_audio(ctx, audio_data: bytes, task_id: str, session_id: str):
    """Transcribe audio segment and publish result."""
    try:
        asr = _get_asr_model()
        audio_buffer = audio_bytes_to_wave_bytesio(audio_data, sample_rate=16000)
        result = await asr.asr_async_function(file=audio_buffer)

        text = extract_asr_text(result)
    except Exception:
        text = ""
        logger.exception("ASR worker error")

    r = await RedisClient.get_client()
    payload = json.dumps({"text": text, "task_id": task_id})
    await r.publish(f"ws:{session_id}", payload)
    return {"text": text}


# --------------------------------------------------------------------------
# TTS worker
# --------------------------------------------------------------------------
async def tts_synthesize(ctx, text: str, task_id: str, session_id: str):
    """Synthesize speech and publish base64-encoded audio."""
    try:
        tts = _get_tts_model()
        text = sanitize_text_for_tts(text)
        response_audio = await tts.tts_async_function(input=text)
        audio_b64 = base64.b64encode(response_audio.content).decode("utf-8")
    except Exception:
        audio_b64 = ""
        logger.exception("tts worker error")

    r = await RedisClient.get_client()
    payload = json.dumps({"audio_b64": audio_b64, "task_id": task_id})
    await r.publish(f"ws:{session_id}", payload)
    return {"audio": audio_b64[:50] + "..."}


# --------------------------------------------------------------------------
# Batch transcription worker (per-file orchestration)
# --------------------------------------------------------------------------
async def startup(ctx):
    """Initialise Redis + batch system on worker startup."""
    await RedisClient.get_client()
    bt.init_batch_system()
    logger.info(f"arq worker ready: Redis + batch system initialised (queue={bt.batch_transcription_queue!r})")


async def process_batch_job(ctx, job_id: str):
    """arq worker entrypoint: process all segments of a batch transcription job."""
    queue = bt.batch_transcription_queue
    if queue is None:
        logger.exception("Batch system not initialised on worker")
        return

    job = await queue.get_job(job_id)
    if job is None:
        logger.exception(f"Worker received unknown job_id: {job_id}")
        return
    if job.status not in (bt.JobStatus.PENDING, bt.JobStatus.PROCESSING):
        logger.info(f"Skipping job {job_id} - already {job.status.value}")
        return

    logger.info(f"▶️ Processing job {job.job_id} ({job.file_name}) - {job.total_segments} segments")
    job.status = bt.JobStatus.PROCESSING
    job.error = ""
    await queue.update_job(job)

    try:
        asr = _get_asr_model()
        asr_func = asr.asr_async_function
    except Exception as e:
        logger.exception(f"✗ Failed to initialise ASR for job {job.job_id}")
        job.status = bt.JobStatus.FAILED
        job.error = f"ASR init failed: {e}"
        await queue.update_job(job)
        return

    language = getattr(job, "language", "") or os.environ.get("ASR_LANGUAGE", "") or "en"
    semaphore = asyncio.Semaphore(3)

    async def process_one(segment, idx: int):
        async with semaphore:
            try:
                segment.status = bt.SegmentStatus.PROCESSING
                # Lazy-load audio from PVC — only this segment's bytes
                # are in memory, not the entire job's worth.
                audio_raw = segment.load_audio()
                if not audio_raw:
                    segment.status = bt.SegmentStatus.FAILED
                    segment.error = "Audio data not found on PVC"
                    job.failed_segments += 1
                    return
                audio_buffer = audio_bytes_to_wave_bytesio(audio_raw, sample_rate=16000)
                asr_kwargs: dict = {"file": audio_buffer}
                if language:
                    asr_kwargs["language"] = language
                result = await asyncio.wait_for(asr_func(**asr_kwargs), timeout=120.0)
                text = extract_asr_text(result).strip()
                if text and len(text) > 2:
                    segment.text = text
                    segment.status = bt.SegmentStatus.COMPLETED
                    job.completed_segments += 1
                else:
                    segment.status = bt.SegmentStatus.FAILED
                    segment.error = "Empty transcription"
                    job.failed_segments += 1
            except TimeoutError:
                segment.status = bt.SegmentStatus.FAILED
                segment.error = "ASR timed out"
                job.failed_segments += 1
                logger.warning(f"Segment {idx} of job {job.job_id} timed out")
            except Exception as e:
                segment.status = bt.SegmentStatus.FAILED
                segment.error = str(e)
                job.failed_segments += 1
                logger.exception(f"Segment {idx} of job {job.job_id} failed")

            if (idx + 1) % 5 == 0:
                await queue.update_job(job)

    tasks = [process_one(seg, i) for i, seg in enumerate(job.segments)]
    await asyncio.gather(*tasks, return_exceptions=True)

    await queue.update_job(job)

    if job.total_segments > 0 and job.failed_segments > job.total_segments * 0.5:
        logger.warning(f"Job {job.job_id}: {job.failed_segments}/{job.total_segments} segments failed")
        job.status = bt.JobStatus.FAILED
        job.error = f"{job.failed_segments}/{job.total_segments} segments failed"
    else:
        job.status = bt.JobStatus.COMPLETED
        job.final_transcript = queue._generate_transcript(job)
        job.transcript_path = queue._save_transcript(job)
        logger.info(f"✓ Job {job.job_id} completed - transcript: {job.transcript_path}")

    await queue.update_job(job)


# --------------------------------------------------------------------------
# Worker settings for arq
# --------------------------------------------------------------------------
class WorkerSettings:
    functions: ClassVar[list] = [transcribe_audio, tts_synthesize, process_batch_job]
    redis_settings = RedisSettings(
        host="conversation-toolbox-redis",  # Change to your actual Redis service name
        port=6379,
        database=0,
    )
    on_startup = startup
    # High concurrency is now safe with lazy audio loading — only 1
    # segment's audio (~2MB) is in memory per concurrent ASR call.
    # 10 jobs × 3 segments = 30 concurrent ASR requests, peak audio
    # memory = 30 × 2MB = 60MB + 120MB baseline = ~180MB total.
    # This gives high throughput from a single pod without relying on
    # HPA (memory-based HPA won't fire for this I/O-bound workload
    # because memory stays under 20% of request regardless of load).
    # HPA remains as a safety net for unexpected memory spikes.
    concurrency = 10
    max_tries = 3
    job_result_keep = 3600
