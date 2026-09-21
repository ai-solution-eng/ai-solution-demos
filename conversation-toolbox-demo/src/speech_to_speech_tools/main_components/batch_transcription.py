"""
Scalable batch transcription system supporting 1000+ audio files.
Redis-backed job queue with worker processes consuming from Redis.
Memory-aware processing with disk offloading (optional).
"""

import json
import logging
import tempfile
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from io import BytesIO
from os import environ, getpid, makedirs, remove
from os.path import basename as bn
from os.path import dirname, exists
from os.path import join as pj
from typing import Any

import numpy as np
import psutil
from utils.audio_handling import format_timestamp
from utils.redis_client import RedisClient

from main_components.constants import TRANSCRIPTS_DIR

logger = logging.getLogger(__name__)


# ==============================================================================
# Memory Monitor (unchanged)
# ==============================================================================
class MemoryMonitor:
    def __init__(self, max_memory_percent: float = 80.0, check_interval: float = 5.0):
        self.max_memory_percent = max_memory_percent
        self.check_interval = check_interval
        self._last_check = 0
        self._memory_warning_threshold = 70.0
        self._last_warning_time = 0.0
        self._warning_interval = 60

    def is_memory_available(self, required_mb: float = 100) -> bool:
        vm = psutil.virtual_memory()
        available_mb = vm.available / (1024 * 1024)
        memory_percent = vm.percent
        current_time = datetime.now().timestamp()
        if (
            memory_percent > self._memory_warning_threshold
            and current_time - self._last_warning_time > self._warning_interval
        ):
            logger.warning(
                f"Memory pressure warning: {memory_percent:.1f}% used, "
                f"available: {available_mb:.0f}MB, required: {required_mb:.0f}MB"
            )
            self._last_warning_time = current_time
        if memory_percent > self.max_memory_percent:
            logger.warning(
                f"Memory critical: {memory_percent:.1f}% used (limit: {self.max_memory_percent}%), "
                f"deferring new work (need ~{required_mb:.0f}MB)"
            )
            return False
        if available_mb < required_mb:
            logger.warning(f"Insufficient memory: available {available_mb:.0f}MB, required: {required_mb:.0f}MB")
            return False
        return True

    def get_memory_stats(self) -> dict:
        try:
            process = psutil.Process(getpid())
            vm = psutil.virtual_memory()
            try:
                process_rss_mb = process.memory_info().rss / (1024 * 1024)
                process_vms_mb = process.memory_info().vms / (1024 * 1024)
            except (psutil.AccessDenied, AttributeError):
                process_rss_mb = 0
                process_vms_mb = 0
            return {
                "process_rss_mb": round(process_rss_mb, 2),
                "process_vms_mb": round(process_vms_mb, 2),
                "system_available_mb": round(vm.available / (1024 * 1024), 2),
                "system_used_mb": round(vm.used / (1024 * 1024), 2),
                "system_percent": round(vm.percent, 1),
                "system_total_mb": round(vm.total / (1024 * 1024), 2),
                "memory_pressure": "high" if vm.percent > self.max_memory_percent else "normal",
            }
        except Exception as e:
            logger.error(f"Failed to get memory stats: {e}")
            return {"error": str(e), "memory_pressure": "unknown"}


# ==============================================================================
# Enums and Data Classes
# ==============================================================================
class JobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SegmentStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class AudioSegment:
    segment_id: str
    start_time: float
    end_time: float
    audio_data: bytes
    status: SegmentStatus = SegmentStatus.PENDING
    text: str = ""
    error: str = ""
    retry_count: int = 0
    processing_started: datetime | None = None
    speaker: str = ""
    # Path to the .raw audio file on the PVC. Set by from_json() so the
    # worker can load audio lazily (load_audio()) instead of having all
    # segment audio in memory at once.
    _lazy_audio_path: str = ""

    def load_audio(self) -> bytes:
        """Lazily load audio data from the PVC.

        Called by the worker right before transcribing this segment.
        This keeps peak memory bounded to (concurrency × 1 segment)
        instead of (1 job × all segments).
        """
        if self.audio_data:
            return self.audio_data
        if self._lazy_audio_path and exists(self._lazy_audio_path):
            with open(self._lazy_audio_path, "rb") as f:
                self.audio_data = f.read()
            return self.audio_data
        # Fallback: try standard segment_audio directory
        path = pj(TRANSCRIPTS_DIR, "segment_audio", f"{self.segment_id}.raw")
        if exists(path):
            with open(path, "rb") as f:
                self.audio_data = f.read()
            return self.audio_data
        return b""

    def to_dict(self) -> dict:
        return {
            "segment_id": self.segment_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "status": self.status.value,
            "text": self.text,
            "error": self.error,
            "retry_count": self.retry_count,
            "processing_started": self.processing_started.isoformat() if self.processing_started else None,
            "speaker": self.speaker,
        }

    def get_size_mb(self) -> float:
        return len(self.audio_data) / (1024 * 1024)

    @staticmethod
    def _audio_dir():
        d = pj(TRANSCRIPTS_DIR, "segment_audio")
        makedirs(d, exist_ok=True)
        return d

    def _audio_path(self) -> str:
        return pj(self._audio_dir(), f"{self.segment_id}.raw")

    def _write_audio_to_disk(self):
        # Skip if audio_data is empty — this happens when the segment was
        # deserialized from Redis (lazy loading). Writing 0 bytes would
        # OVERWRITE the non-zero file the app pod wrote during job creation.
        if not self.audio_data:
            return
        path = self._audio_path()
        with open(path, "wb") as f:
            f.write(self.audio_data)

    @classmethod
    def _read_audio_from_disk(cls, segment_id: str) -> bytes:
        path = pj(cls._audio_dir(), f"{segment_id}.raw")
        with open(path, "rb") as f:
            return f.read()

    def to_json(self) -> str:
        # Write to PVC so worker pods can read segment audio from disk.
        # The base64 audio_data is intentionally OMITTED from the Redis JSON
        # to avoid OOM — 275 segments × 1.3MB base64 = 358MB in Redis.
        # from_json() falls back to reading audio_path from disk when
        # audio_data is absent (which it always is now).
        try:
            self._write_audio_to_disk()
        except Exception as e:
            logger.warning(f"Could not write segment audio to disk: {e}")
        return json.dumps(
            {
                "segment_id": self.segment_id,
                "start_time": self.start_time,
                "end_time": self.end_time,
                "audio_path": self._audio_path(),
                "status": self.status.value,
                "text": self.text,
                "error": self.error,
                "retry_count": self.retry_count,
                "processing_started": self.processing_started.isoformat() if self.processing_started else None,
                "speaker": self.speaker,
            }
        )

    @classmethod
    def from_json(cls, s: str) -> "AudioSegment":
        d = json.loads(s)
        # LAZY: don't load audio_data from disk here. Store the path so
        # the worker can call load_audio() per-segment, keeping peak
        # memory bounded to (concurrency × 1 segment) instead of
        # (1 job × all segments). This was the root cause of OOM —
        # 5 jobs × 80 segments × 1MB = 400MB/job loaded at once.
        audio_path = d.get("audio_path", "")
        return cls(
            segment_id=d["segment_id"],
            start_time=d["start_time"],
            end_time=d["end_time"],
            audio_data=b"",
            status=SegmentStatus(d["status"]),
            text=d.get("text", ""),
            error=d.get("error", ""),
            retry_count=d.get("retry_count", 0),
            processing_started=datetime.fromisoformat(d["processing_started"]) if d.get("processing_started") else None,
            speaker=d.get("speaker", ""),
            _lazy_audio_path=audio_path,
        )


@dataclass
class TranscriptionJob:
    job_id: str
    created_at: datetime
    status: JobStatus = JobStatus.PENDING
    file_name: str = ""
    original_format: str = ""
    total_segments: int = 0
    completed_segments: int = 0
    failed_segments: int = 0
    segments: list[AudioSegment] = field(default_factory=list)
    final_transcript: str = ""
    error: str = ""
    num_speakers: int = 1
    language: str = ""
    duration_seconds: float = 0.0
    transcript_path: str = ""
    temp_dir: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def progress_percent(self) -> float:
        if self.total_segments == 0:
            return 0.0
        return round((self.completed_segments / self.total_segments) * 100, 1)

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "status": self.status.value,
            "file_name": self.file_name,
            "original_format": self.original_format,
            "total_segments": self.total_segments,
            "completed_segments": self.completed_segments,
            "failed_segments": self.failed_segments,
            "final_transcript": self.final_transcript,
            "error": self.error,
            "num_speakers": self.num_speakers,
            "language": self.language,
            "duration_seconds": self.duration_seconds,
            "transcript_path": self.transcript_path,
            "progress_percent": self.progress_percent,
            "metadata": self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(
            {
                "job_id": self.job_id,
                "created_at": self.created_at.isoformat(),
                "status": self.status.value,
                "file_name": self.file_name,
                "original_format": self.original_format,
                "total_segments": self.total_segments,
                "completed_segments": self.completed_segments,
                "failed_segments": self.failed_segments,
                "segments": [s.to_json() for s in self.segments],
                "final_transcript": self.final_transcript,
                "error": self.error,
                "num_speakers": self.num_speakers,
                "language": self.language,
                "duration_seconds": self.duration_seconds,
                "transcript_path": self.transcript_path,
                "temp_dir": self.temp_dir,
                "metadata": self.metadata,
            }
        )

    @classmethod
    def from_json(cls, s: str) -> "TranscriptionJob":
        d = json.loads(s)
        segments = [AudioSegment.from_json(seg) for seg in d.get("segments", [])]
        return cls(
            job_id=d["job_id"],
            created_at=datetime.fromisoformat(d["created_at"]),
            status=JobStatus(d["status"]),
            file_name=d.get("file_name", ""),
            original_format=d.get("original_format", ""),
            total_segments=d.get("total_segments", len(segments)),
            completed_segments=d.get("completed_segments", 0),
            failed_segments=d.get("failed_segments", 0),
            segments=segments,
            final_transcript=d.get("final_transcript", ""),
            error=d.get("error", ""),
            num_speakers=d.get("num_speakers", 1),
            language=d.get("language", ""),
            duration_seconds=d.get("duration_seconds", 0.0),
            transcript_path=d.get("transcript_path", ""),
            temp_dir=d.get("temp_dir", ""),
            metadata=d.get("metadata", {}),
        )


# ==============================================================================
# Audio splitting (batch-specific wrappers using shared utilities)
# ==============================================================================
def split_audio_by_speech_energy(np_audio: np.ndarray, sample_rate: int = 16000) -> list[AudioSegment]:
    segments = []
    for start_time, end_time, segment_data in _split_energy(np_audio, sample_rate):
        segments.append(
            AudioSegment(
                segment_id=str(uuid.uuid4()),
                start_time=start_time,
                end_time=end_time,
                audio_data=segment_data.tobytes(),
            )
        )
    return segments


def _split_energy(np_audio: np.ndarray, sample_rate: int):
    from utils.audio_handling import split_audio_by_speech_energy as shared_split

    return shared_split(np_audio, sample_rate)


def split_audio_by_speech_vad(
    audio_data: bytes, sample_rate: int = 16000, vad_aggression: int = 2
) -> list[AudioSegment]:
    import webrtcvad

    vad = webrtcvad.Vad(vad_aggression)
    frame_duration = 30
    frame_size = int(sample_rate * frame_duration / 1000) * 2
    segments = []
    speech_buffer = bytearray()
    is_speaking = False
    speech_start_time = 0.0
    silence_counter = 0
    min_speech_frames = 10
    silence_frames_to_end = 30
    min_segment_duration = 1.0
    max_segment_duration = 30.0
    warmup_frames = 5
    total_frames = len(audio_data) // frame_size
    warmup_remaining = warmup_frames
    for i in range(total_frames):
        start_byte = i * frame_size
        end_byte = start_byte + frame_size
        frame = audio_data[start_byte:end_byte]
        if len(frame) < frame_size:
            break
        frame_time = (i * frame_duration) / 1000.0
        if warmup_remaining > 0:
            warmup_remaining -= 1
            continue
        try:
            is_speech = vad.is_speech(frame, sample_rate)
        except Exception:
            continue
        if is_speech:
            silence_counter = 0
            if not is_speaking:
                is_speaking = True
                speech_start_time = frame_time
                speech_buffer = bytearray()
            speech_buffer.extend(frame)
            segment_duration = frame_time - speech_start_time
            if segment_duration >= max_segment_duration:
                audio_bytes = bytes(speech_buffer)
                if len(audio_bytes) >= min_speech_frames * frame_size:
                    segments.append(
                        AudioSegment(
                            segment_id=str(uuid.uuid4()),
                            start_time=speech_start_time,
                            end_time=frame_time,
                            audio_data=audio_bytes,
                        )
                    )
                is_speaking = False
                speech_buffer = bytearray()
                speech_start_time = 0.0
        else:
            if is_speaking:
                speech_buffer.extend(frame)
                silence_counter += 1
                segment_duration = (frame_time + (silence_counter * frame_duration / 1000.0)) - speech_start_time
                if silence_counter >= silence_frames_to_end and segment_duration >= min_segment_duration:
                    audio_bytes = bytes(speech_buffer)
                    if len(audio_bytes) >= min_speech_frames * frame_size:
                        end_time = frame_time + (silence_counter * frame_duration / 1000.0)
                        segments.append(
                            AudioSegment(
                                segment_id=str(uuid.uuid4()),
                                start_time=speech_start_time,
                                end_time=end_time,
                                audio_data=audio_bytes,
                            )
                        )
                    is_speaking = False
                    speech_buffer = bytearray()
                    speech_start_time = 0.0
                    silence_counter = 0
    if is_speaking and len(speech_buffer) >= min_speech_frames * frame_size:
        end_time = (total_frames * frame_duration) / 1000.0
        if end_time - speech_start_time >= min_segment_duration:
            segments.append(
                AudioSegment(
                    segment_id=str(uuid.uuid4()),
                    start_time=speech_start_time,
                    end_time=end_time,
                    audio_data=bytes(speech_buffer),
                )
            )
    logger.info(f"VAD split: {len(segments)} segments")
    return segments


# ==============================================================================
# Redis-Backed Transcription Queue
# ==============================================================================
class TranscriptionQueue:
    """
    Redis-backed job queue for distributed batch transcription.
    Jobs are stored as JSON strings in a Redis list and processed by workers.
    """

    def __init__(
        self,
        max_workers: int = 10,
        max_retries: int = 3,
        max_concurrent_jobs: int = 5,
        max_memory_percent: float = 80.0,
    ):
        self._max_workers = max_workers
        self._max_retries = max_retries
        self._max_concurrent_jobs = max_concurrent_jobs
        self._max_memory_percent = max_memory_percent
        self._memory_monitor = MemoryMonitor(max_memory_percent=max_memory_percent)
        self._redis_key = "batch:jobs"
        self._redis_job_prefix = "batch:job:"
        self._redis_status_channel = "batch:status"
        self._redis = None
        self._arq_pool: Any = None

    async def _get_redis(self):
        if self._redis is None:
            self._redis = await RedisClient.get_client()
        return self._redis

    async def _init_redis_queue(self):
        """Ensure Redis is connected (called during startup)."""
        await self._get_redis()
        logger.info(f"Redis queue initialised (key: {self._redis_key})")

    def set_arq_pool(self, pool: Any) -> None:
        """Inject the arq pool used to enqueue batch jobs on worker pods."""
        self._arq_pool = pool
        logger.info("arq pool bound to TranscriptionQueue")

    async def _enqueue_job(self, job_id: str) -> None:
        """Enqueue a fresh arq job for the given id (used by restart/cleanup).

        We don't try to abort any existing in-flight arq job — arq 0.28's abort
        API waits for the job result, which doesn't suit synchronous restart.
        Instead, the worker's `process_batch_job` re-checks job status at start
        and bails out if the job is no longer PENDING/PROCESSING.
        """
        if self._arq_pool is None:
            raise RuntimeError("arq pool not initialised")
        await self._arq_pool.enqueue_job("process_batch_job", job_id=job_id, _job_id=job_id)

    async def add_job(self, job: TranscriptionJob) -> str:
        """Store job data in Redis and enqueue on arq pool for worker processing."""
        r = await self._get_redis()
        job.status = JobStatus.PENDING
        job_json = job.to_json()
        # Store job data FIRST so the worker can look it up when arq dispatches it.
        await r.set(f"{self._redis_job_prefix}{job.job_id}", job_json)
        await self._publish_status()
        if self._arq_pool is None:
            logger.error(f"No arq pool configured; job {job.job_id} will not be processed")
            raise RuntimeError("arq pool not initialised; batch workers cannot receive jobs")
        await self._arq_pool.enqueue_job("process_batch_job", job_id=job.job_id, _job_id=job.job_id)
        logger.info(f"Job {job.job_id} enqueued on arq pool")
        return job.job_id

    async def get_job(self, job_id: str) -> TranscriptionJob | None:
        """Retrieve job from Redis."""
        r = await self._get_redis()
        # Ensure job_id is bytes decoded if needed
        if isinstance(job_id, bytes):
            job_id = job_id.decode("utf-8")
        job_json = await r.get(f"{self._redis_job_prefix}{job_id}")
        if not job_json:
            return None
        return TranscriptionJob.from_json(job_json)

    async def update_job(self, job: TranscriptionJob):
        """Update job data in Redis."""
        r = await self._get_redis()
        await r.set(f"{self._redis_job_prefix}{job.job_id}", job.to_json())
        await self._publish_status()

    async def list_jobs(self) -> list[TranscriptionJob]:
        """List all jobs by scanning for batch:job:* keys."""
        r = await self._get_redis()
        jobs = []
        cursor = 0
        while True:
            cursor, keys = await r.scan(cursor, match=f"{self._redis_job_prefix}*", count=100)
            for key in keys:
                job_id = key.decode("utf-8").replace(self._redis_job_prefix, "")
                job = await self.get_job(job_id)
                if job:
                    jobs.append(job)
            if cursor == 0:
                break
        return jobs

    async def _publish_status(self):
        """Publish current job statuses to Redis pub/sub channel."""
        r = await self._get_redis()
        jobs = await self.list_jobs()
        statuses = {}
        for job in jobs:
            statuses[job.job_id] = {
                "status": job.status.value,
                "completed": job.completed_segments,
                "total": job.total_segments,
                "progress": job.progress_percent,
                "requested_speakers": job.num_speakers,
                "diarization_used": bool(job.metadata.get("diarization_used", False)),
            }
        # Also include counts
        counts = {
            "pending": sum(1 for j in jobs if j.status == JobStatus.PENDING),
            "processing": sum(1 for j in jobs if j.status == JobStatus.PROCESSING),
            "completed": sum(1 for j in jobs if j.status == JobStatus.COMPLETED),
            "failed": sum(1 for j in jobs if j.status == JobStatus.FAILED),
        }
        payload = json.dumps({"jobs": statuses, "counts": counts})
        await r.publish(self._redis_status_channel, payload)

    async def cancel_job(self, job_id: str) -> dict:
        job = await self.get_job(job_id)
        if not job:
            return {"error": "Job not found"}
        if job.status not in [JobStatus.PENDING, JobStatus.PROCESSING]:
            return {"error": f"Cannot cancel job with status: {job.status.value}"}
        # Mark CANCELLED in Redis; the worker's process_batch_job will see this
        # on pickup (for queued jobs) and skip. In-flight jobs are not aborted
        # (arq 0.28's Job.abort blocks on result; the original code had the same
        # limitation — cancel only takes effect for not-yet-started jobs).
        job.status = JobStatus.CANCELLED
        await self.update_job(job)
        return {"status": "cancelled", "job_id": job_id}

    async def delete_job(self, job_id: str) -> dict:
        r = await self._get_redis()
        job = await self.get_job(job_id)
        if not job:
            return {"error": "Job not found"}
        # Mark CANCELLED so a queued arq job is a no-op when the worker picks it up.
        if job.status in (JobStatus.PENDING, JobStatus.PROCESSING):
            job.status = JobStatus.CANCELLED
            await self.update_job(job)
        # Remove job data
        await r.delete(f"{self._redis_job_prefix}{job_id}")
        # Cleanup transcript file if any
        if job.transcript_path and exists(job.transcript_path):
            try:
                remove(job.transcript_path)
            except Exception:
                pass
        # Cleanup segment audio files on PVC
        for segment in job.segments:
            try:
                path = segment._audio_path()
                if exists(path):
                    remove(path)
            except Exception:
                pass
        return {"status": "deleted", "job_id": job_id}

    async def restart_stuck_job(self, job_id: str) -> dict:
        job = await self.get_job(job_id)
        if not job:
            return {"error": "Job not found"}
        reset_count = 0
        for segment in job.segments:
            if segment.status == SegmentStatus.PROCESSING:
                segment.status = SegmentStatus.PENDING
                segment.processing_started = None
                segment.retry_count += 1
                reset_count += 1
        job.completed_segments = sum(1 for s in job.segments if s.status == SegmentStatus.COMPLETED)
        job.failed_segments = sum(1 for s in job.segments if s.status == SegmentStatus.FAILED)
        if job.status == JobStatus.FAILED:
            job.status = JobStatus.PENDING
            job.error = ""
        await self.update_job(job)
        try:
            await self._enqueue_job(job_id)
        except Exception as e:
            logger.error(f"Failed to re-enqueue job {job_id} on restart: {e}")
            return {"error": f"Failed to re-enqueue: {e}", "job_id": job_id}
        return {"status": "restarted", "job_id": job_id, "segments_reset": reset_count}

    async def force_complete_job(self, job_id: str) -> dict:
        job = await self.get_job(job_id)
        if not job:
            return {"error": "Job not found"}
        for segment in job.segments:
            if segment.status != SegmentStatus.COMPLETED:
                segment.status = SegmentStatus.COMPLETED
                if not segment.text:
                    segment.text = "[transcription unavailable]"
        job.status = JobStatus.COMPLETED
        job.final_transcript = self._generate_transcript(job)
        job.transcript_path = self._save_transcript(job)
        await self.update_job(job)
        return {
            "status": "force_completed",
            "job_id": job_id,
            "transcript_path": job.transcript_path,
        }

    async def cleanup_all_stuck_jobs(self) -> dict:
        jobs = await self.list_jobs()
        cleaned = []
        for job in jobs:
            stuck_segments = [s for s in job.segments if s.status == SegmentStatus.PROCESSING]
            failed_segments = [s for s in job.segments if s.status == SegmentStatus.FAILED]
            if stuck_segments or (failed_segments and job.status == JobStatus.FAILED):
                for segment in stuck_segments + failed_segments:
                    segment.status = SegmentStatus.PENDING
                    segment.processing_started = None
                    segment.retry_count += 1
                    segment.error = ""
                job.completed_segments = sum(1 for s in job.segments if s.status == SegmentStatus.COMPLETED)
                job.failed_segments = sum(1 for s in job.segments if s.status == SegmentStatus.FAILED)
                job.status = JobStatus.PENDING
                job.error = ""
                await self.update_job(job)
                try:
                    await self._enqueue_job(job.job_id)
                except Exception as e:
                    logger.error(f"Failed to re-enqueue job {job.job_id} during cleanup: {e}")
                cleaned.append(job.job_id)
        return {
            "status": "cleanup_complete",
            "jobs_cleaned": len(cleaned),
            "job_ids": cleaned,
        }

    async def get_job_stats(self) -> dict:
        jobs = await self.list_jobs()
        pending = sum(1 for j in jobs if j.status == JobStatus.PENDING)
        processing = sum(1 for j in jobs if j.status == JobStatus.PROCESSING)
        completed = sum(1 for j in jobs if j.status == JobStatus.COMPLETED)
        failed = sum(1 for j in jobs if j.status == JobStatus.FAILED)
        cancelled = sum(1 for j in jobs if j.status == JobStatus.CANCELLED)
        stuck_segments = 0
        for job in jobs:
            stuck_segments += sum(1 for s in job.segments if s.status == SegmentStatus.PROCESSING)
        return {
            "total_jobs": len(jobs),
            "pending": pending,
            "processing": processing,
            "completed": completed,
            "failed": failed,
            "cancelled": cancelled,
            "stuck_segments": stuck_segments,
        }

    def _generate_transcript(self, job: TranscriptionJob) -> str:
        lines = []
        for segment in job.segments:
            if segment.status == SegmentStatus.COMPLETED and segment.text:
                start_str = format_timestamp(segment.start_time)
                end_str = format_timestamp(segment.end_time)
                if job.num_speakers <= 1:
                    # Single-speaker: omit speaker label entirely.
                    line = f"[{start_str} - {end_str}] {segment.text}"
                else:
                    speaker = segment.speaker or "Unknown"
                    line = f"[{start_str} - {end_str}] {speaker}: {segment.text}"
                lines.append(line)
        return "\n\n".join(lines)

    def _save_transcript(self, job: TranscriptionJob) -> str:
        original_name = job.file_name
        if original_name and "." in original_name:
            base_name = original_name.rsplit(".", 1)[0]
        else:
            base_name = original_name or f"job_{job.job_id}"
        transcript_filename = pj(TRANSCRIPTS_DIR, f"{base_name}_transcript.txt")
        counter = 1
        while exists(transcript_filename):
            transcript_filename = pj(TRANSCRIPTS_DIR, f"{base_name}_transcript_{counter}.txt")
            counter += 1
        makedirs(TRANSCRIPTS_DIR, exist_ok=True)
        header = f"""=== Batch Transcription ===
File: {job.file_name}
Original Format: {job.original_format.upper()}
Date: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
Estimated Speakers: {job.num_speakers}
Duration: {job.duration_seconds:.1f} seconds
Total Segments: {job.total_segments}
Completed: {job.completed_segments}
Failed: {job.failed_segments}
"""
        with open(transcript_filename, "w", encoding="utf-8") as f:
            f.write(header)
            f.write(job.final_transcript)
            f.write("\n\n=== End of Transcription ===\n")
        return transcript_filename


# ==============================================================================
# File Processor Class
# ==============================================================================
class BatchFileProcessor:
    def __init__(self, max_workers: int = 4):
        self._thread_executor = ThreadPoolExecutor(max_workers=max_workers)

    async def process_file(
        self,
        file_path: str,
        original_format: str,
        num_speakers: int = 1,
        language: str = "",
        job_id: str | None = None,
    ) -> TranscriptionJob:
        job_id = job_id or str(uuid.uuid4())
        temp_dir = dirname(file_path) or tempfile.gettempdir()
        wav_data = self._convert_to_wav(file_path, original_format)

        # Fast path: single-speaker content (audiobooks, lectures) skips
        # diarization AND VAD entirely. VAD's frame-by-frame silence
        # detection produces 100-160 tiny segments per 30-min file, each
        # requiring a separate ASR HTTP call. Fixed-duration chunking
        # produces ~60 segments for the same file — fewer HTTP calls,
        # less Redis overhead, faster upload-side processing (no VAD
        # iteration of 576k frames).
        if num_speakers == 1:
            wav_buffer = BytesIO(wav_data)
            with wave.open(wav_buffer, "rb") as wf:
                sample_rate = wf.getframerate()
                audio_data = wf.readframes(wf.getnframes())
            segments = self._split_by_duration(audio_data, sample_rate)
            duration = segments[-1].end_time if segments else 0.0
            logger.info(f"Single-speaker fast path: {len(segments)} chunks for {bn(file_path)}")
            return TranscriptionJob(
                job_id=job_id,
                created_at=datetime.now(),
                status=JobStatus.PENDING,
                file_name=bn(file_path),
                original_format=original_format,
                total_segments=len(segments),
                segments=segments,
                num_speakers=num_speakers,
                language=language,
                duration_seconds=duration,
                temp_dir=temp_dir,
                metadata={"diarization_used": False},
            )

        # Multi-speaker path: diarization → VAD fallback

        # Write temp WAV for diarization service
        temp_wav_path = pj(temp_dir, f"diarize_{job_id}.wav")
        with open(temp_wav_path, "wb") as f:
            f.write(wav_data)

        try:
            result = await self._diarize_and_split(wav_data, temp_wav_path, num_speakers)
        except Exception as e:
            logger.error(f"Diarization failed for {file_path}: {e}, falling back to VAD")
            wav_buffer = BytesIO(wav_data)
            with wave.open(wav_buffer, "rb") as wf:
                duration = wf.getnframes() / wf.getframerate()
                audio_data = wf.readframes(wf.getnframes())
            segments = self._split_audio(audio_data, 16000)
            duration = segments[-1].end_time if segments else 0.0
            job = TranscriptionJob(
                job_id=job_id,
                created_at=datetime.now(),
                status=JobStatus.PENDING,
                file_name=bn(file_path),
                original_format=original_format,
                total_segments=len(segments),
                segments=segments,
                num_speakers=num_speakers,
                language=language,
                duration_seconds=duration,
                temp_dir=temp_dir,
                metadata={"diarization_used": False},
            )
            return job
        finally:
            try:
                remove(temp_wav_path)
            except Exception:
                pass

        segments = result["segments"]
        duration = result["duration"]
        wav_buffer = BytesIO(wav_data)
        with wave.open(wav_buffer, "rb") as wf:
            sample_rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
        np_audio = np.frombuffer(frames, dtype=np.int16)

        audio_segments = []
        for seg in segments:
            start_sample = int(seg.start * sample_rate)
            end_sample = int(seg.end * sample_rate)
            end_sample = min(end_sample, len(np_audio))
            segment_data = np_audio[start_sample:end_sample].tobytes()
            if len(segment_data) < sample_rate * 0.3:
                continue
            audio_segments.append(
                AudioSegment(
                    segment_id=str(uuid.uuid4()),
                    start_time=seg.start,
                    end_time=seg.end,
                    audio_data=segment_data,
                    speaker=seg.speaker,
                )
            )

        job = TranscriptionJob(
            job_id=job_id,
            created_at=datetime.now(),
            status=JobStatus.PENDING,
            file_name=bn(file_path),
            original_format=original_format,
            total_segments=len(audio_segments),
            segments=audio_segments,
            num_speakers=num_speakers,
            language=language,
            duration_seconds=duration,
            temp_dir=temp_dir,
            metadata={"diarization_used": True},
        )
        return job

    async def _diarize_and_split(self, wav_data: bytes, temp_wav_path: str, num_speakers: int) -> dict:
        from utils.diarization_client import diarize_audio

        base_url = environ.get(
            "DIARIZATION_BASE_URL",
            "http://conversation-toolbox-diarization:8001",
        )
        result = await diarize_audio(
            temp_wav_path,
            num_speakers=num_speakers,
            base_url=base_url,
        )
        segments = result.exclusive_segments or result.segments
        return {"segments": segments, "duration": result.duration}

    def _split_audio(self, audio_data: bytes, sample_rate: int) -> list[AudioSegment]:
        try:
            segments = split_audio_by_speech_vad(audio_data, sample_rate, vad_aggression=2)
            if len(segments) < 3:
                logger.info(f"VAD gave only {len(segments)} segments, trying energy-based")
                np_audio = np.frombuffer(audio_data, dtype=np.int16)
                segments = split_audio_by_speech_energy(np_audio, sample_rate)
        except Exception as e:
            logger.warning(f"VAD splitting failed: {e}, using energy-based")
            np_audio = np.frombuffer(audio_data, dtype=np.int16)
            segments = split_audio_by_speech_energy(np_audio, sample_rate)
        if len(segments) == 0:
            logger.info("No segments from VAD, trying energy-based directly")
            np_audio = np.frombuffer(audio_data, dtype=np.int16)
            segments = split_audio_by_speech_energy(np_audio, sample_rate)
        logger.info(f"Audio split into {len(segments)} segments")
        return segments

    def _split_by_duration(
        self, audio_data: bytes, sample_rate: int, chunk_seconds: float | None = None
    ) -> list[AudioSegment]:
        """Split raw PCM16 audio into fixed-duration chunks.

        Used by the single-speaker fast path to skip VAD entirely.
        Default chunk size is 60s — halves ASR HTTP calls vs 30s chunks.
        Configurable via BATCH_CHUNK_SECONDS env var. For a 30-min file
        this produces 30 chunks vs VAD's 100-160.
        """
        if chunk_seconds is None:
            chunk_seconds = float(environ.get("BATCH_CHUNK_SECONDS", "60"))
        bytes_per_sample = 2  # PCM16
        chunk_bytes = int(sample_rate * chunk_seconds) * bytes_per_sample
        segments: list[AudioSegment] = []
        start_byte = 0
        while start_byte < len(audio_data):
            end_byte = min(start_byte + chunk_bytes, len(audio_data))
            start_time = start_byte / (bytes_per_sample * sample_rate)
            end_time = end_byte / (bytes_per_sample * sample_rate)
            segments.append(
                AudioSegment(
                    segment_id=str(uuid.uuid4()),
                    start_time=start_time,
                    end_time=end_time,
                    audio_data=audio_data[start_byte:end_byte],
                )
            )
            start_byte = end_byte
        logger.info(f"Duration split: {len(segments)} segments ({chunk_seconds}s each)")
        return segments

    def _convert_to_wav(self, file_path: str, original_format: str) -> bytes:
        from pydub import AudioSegment

        audio = AudioSegment.from_file(file_path)
        audio = audio.set_frame_rate(16000)
        audio = audio.set_channels(1)
        audio = audio.set_sample_width(2)
        buffer = BytesIO()
        audio.export(buffer, format="wav")
        return buffer.getvalue()

    def shutdown(self):
        self._thread_executor.shutdown(wait=True)


# ==============================================================================
# Global Instances
# ==============================================================================
batch_transcription_queue: TranscriptionQueue | None = None
file_processor: BatchFileProcessor | None = None


def init_batch_system(
    max_workers: int = 10,
    max_concurrent_jobs: int = 5,
    max_memory_percent: float = 80.0,
    use_bounded_queue: bool = True,
) -> tuple:
    """Initialise batch system with Redis-backed queue."""
    global batch_transcription_queue, file_processor
    batch_transcription_queue = TranscriptionQueue(
        max_workers=max_workers,
        max_retries=3,
        max_concurrent_jobs=max_concurrent_jobs,
        max_memory_percent=max_memory_percent,
    )
    file_processor = BatchFileProcessor(max_workers=4)
    logger.info(
        f"Initialised Redis-backed batch queue: "
        f"max_workers={max_workers}, "
        f"max_concurrent_jobs={max_concurrent_jobs}, "
        f"max_memory_percent={max_memory_percent}"
    )
    return batch_transcription_queue, file_processor


def get_memory_stats() -> dict:
    if batch_transcription_queue and hasattr(batch_transcription_queue, "_memory_monitor"):
        return batch_transcription_queue._memory_monitor.get_memory_stats()
    return MemoryMonitor().get_memory_stats()
