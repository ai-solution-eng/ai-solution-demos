import asyncio
import time
import uuid
from typing import Any

from app.config import EXPORT_JOB_TTL_SECONDS

EXPORT_JOBS: dict[str, dict[str, Any]] = {}
EXPORT_JOBS_LOCK = asyncio.Lock()


async def update_export_job(job_id: str, **fields: Any) -> None:
    async with EXPORT_JOBS_LOCK:
        job = EXPORT_JOBS.get(job_id)
        if not job:
            return
        job.update(fields)
        job["updated_at"] = time.time()


async def create_export_job() -> str:
    job_id = uuid.uuid4().hex
    async with EXPORT_JOBS_LOCK:
        EXPORT_JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "progress": 2,
            "stage": "Queued",
            "detail": "Preparing export job.",
            "created_at": time.time(),
            "updated_at": time.time(),
            "archive_name": None,
            "zip_bytes": None,
            "error": None,
        }
    return job_id


async def cleanup_old_export_jobs() -> None:
    cutoff = time.time() - EXPORT_JOB_TTL_SECONDS
    async with EXPORT_JOBS_LOCK:
        stale = [
            jid for jid, job in EXPORT_JOBS.items() if job.get("updated_at", 0) < cutoff
        ]
        for jid in stale:
            EXPORT_JOBS.pop(jid, None)
