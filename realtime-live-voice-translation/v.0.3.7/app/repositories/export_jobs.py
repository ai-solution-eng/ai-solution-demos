from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import SESSION_TTL_HOURS
from app.db import ExportJob, build_expiry, utc_now


class ExportJobRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        *,
        session_id: str | None,
        status: str = "queued",
        progress: int = 0,
        stage: str = "Queued",
        detail: str = "",
        ttl_hours: int = SESSION_TTL_HOURS,
    ) -> ExportJob:
        record = ExportJob(
            id=str(uuid.uuid4()),
            session_id=session_id,
            status=status,
            progress=progress,
            stage=stage,
            detail=detail,
            expires_at=build_expiry(ttl_hours),
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def get(self, job_id: str) -> ExportJob | None:
        return await self.session.get(ExportJob, job_id)

    async def update(
        self,
        job_id: str,
        **fields: object,
    ) -> ExportJob | None:
        record = await self.get(job_id)
        if record is None:
            return None
        for key, value in fields.items():
            setattr(record, key, value)
        if "expires_at" not in fields:
            record.expires_at = build_expiry()
        record.updated_at = utc_now()
        await self.session.flush()
        return record

    async def list_expired(self, *, now: datetime | None = None) -> list[ExportJob]:
        current = now or utc_now()
        result = await self.session.execute(
            select(ExportJob).where(
                ExportJob.expires_at.is_not(None), ExportJob.expires_at <= current
            )
        )
        return list(result.scalars().all())

    async def delete_expired(self, *, now: datetime | None = None) -> int:
        current = now or utc_now()
        result = await self.session.execute(
            delete(ExportJob).where(
                ExportJob.expires_at.is_not(None), ExportJob.expires_at <= current
            )
        )
        return int(result.rowcount or 0)

    async def list_for_session(self, session_id: str) -> list[ExportJob]:
        result = await self.session.execute(
            select(ExportJob)
            .where(ExportJob.session_id == session_id)
            .order_by(ExportJob.created_at.desc())
        )
        return list(result.scalars().all())
