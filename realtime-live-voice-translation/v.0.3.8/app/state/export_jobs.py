import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

from app.config import EXPORT_JOB_TTL_SECONDS
from app.db import database_enabled, session_scope
from app.repositories.export_jobs import ExportJobRepository

EXPORT_JOBS: dict[str, dict[str, Any]] = {}
EXPORT_JOBS_LOCK = asyncio.Lock()


async def update_export_job(job_id: str, **fields: Any) -> None:
    if database_enabled():
        async with session_scope() as db_session:
            repo = ExportJobRepository(db_session)
            await repo.update(job_id, **fields)
        return
    async with EXPORT_JOBS_LOCK:
        job = EXPORT_JOBS.get(job_id)
        if not job:
            return
        job.update(fields)
        job["updated_at"] = time.time()


async def create_export_job(session_id: str | None = None) -> str:
    if database_enabled():
        async with session_scope() as db_session:
            repo = ExportJobRepository(db_session)
            record = await repo.create(
                session_id=session_id,
                progress=2,
                stage="Queued",
                detail="Preparing export job.",
            )
            return record.id

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
    if database_enabled():
        async with session_scope() as db_session:
            repo = ExportJobRepository(db_session)
            await repo.delete_expired()
        return

    cutoff = time.time() - EXPORT_JOB_TTL_SECONDS
    async with EXPORT_JOBS_LOCK:
        stale = [
            jid for jid, job in EXPORT_JOBS.items() if job.get("updated_at", 0) < cutoff
        ]
        for jid in stale:
            EXPORT_JOBS.pop(jid, None)


async def get_export_job(job_id: str) -> dict[str, Any] | None:
    if database_enabled():
        async with session_scope() as db_session:
            repo = ExportJobRepository(db_session)
            record = await repo.get(job_id)
            if record is None:
                return None
            return {
                "job_id": record.id,
                "status": record.status,
                "progress": record.progress,
                "stage": record.stage,
                "detail": record.detail,
                "archive_name": record.archive_name,
                "artifact_path": record.artifact_path,
                "error": record.error,
            }

    async with EXPORT_JOBS_LOCK:
        job = EXPORT_JOBS.get(job_id)
        return dict(job) if job else None


async def load_export_job_artifact(job_id: str) -> tuple[str, bytes] | None:
    if database_enabled():
        async with session_scope() as db_session:
            repo = ExportJobRepository(db_session)
            record = await repo.get(job_id)
            if (
                record is None
                or record.status != "completed"
                or not record.artifact_path
                or not record.archive_name
            ):
                return None
            artifact_path = Path(record.artifact_path)
            if not artifact_path.exists():
                return None
            return record.archive_name, artifact_path.read_bytes()

    async with EXPORT_JOBS_LOCK:
        job = EXPORT_JOBS.get(job_id)
        if not job or job.get("status") != "completed" or not job.get("zip_bytes"):
            return None
        return job.get("archive_name") or f"meeting_package_{job_id}.zip", job.get("zip_bytes")
