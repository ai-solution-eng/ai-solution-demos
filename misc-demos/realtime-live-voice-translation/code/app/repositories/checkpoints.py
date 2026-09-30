from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionCheckpoint


class CheckpointRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(
        self,
        *,
        session_id: str,
        window_start_ms: int | None,
        window_end_ms: int | None,
        summary_en: str,
        minutes_en: str,
    ) -> SessionCheckpoint:
        record = SessionCheckpoint(
            session_id=session_id,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
            summary_en=summary_en,
            minutes_en=minutes_en,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def list_for_session(self, session_id: str) -> list[SessionCheckpoint]:
        result = await self.session.execute(
            select(SessionCheckpoint)
            .where(SessionCheckpoint.session_id == session_id)
            .order_by(SessionCheckpoint.created_at.asc(), SessionCheckpoint.id.asc())
        )
        return list(result.scalars().all())
