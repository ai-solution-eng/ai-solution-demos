from __future__ import annotations

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import CallSegment


class SegmentRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(
        self,
        *,
        session_id: str,
        segment_id: str,
        revision: int,
        is_final: bool,
        included_in_recording: bool,
        source_text: str,
        source_language: str,
        translations_json: dict[str, str] | None = None,
        fact_check_json: dict[str, object] | None = None,
        ts_ms: int | None = None,
    ) -> CallSegment:
        record = CallSegment(
            session_id=session_id,
            segment_id=segment_id,
            revision=revision,
            is_final=is_final,
            included_in_recording=included_in_recording,
            source_text=source_text,
            source_language=source_language,
            translations_json=translations_json or {},
            fact_check_json=fact_check_json,
            ts_ms=ts_ms,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def update_fact_check(
        self,
        *,
        session_id: str,
        segment_id: str,
        revision: int,
        fact_check_json: dict[str, object] | None,
    ) -> int:
        result = await self.session.execute(
            update(CallSegment)
            .where(CallSegment.session_id == session_id)
            .where(CallSegment.segment_id == segment_id)
            .where(CallSegment.revision == revision)
            .values(fact_check_json=fact_check_json)
        )
        return int(result.rowcount or 0)

    async def list_for_session(self, session_id: str) -> list[CallSegment]:
        result = await self.session.execute(
            select(CallSegment)
            .where(CallSegment.session_id == session_id)
            .order_by(CallSegment.created_at.asc(), CallSegment.id.asc())
        )
        return list(result.scalars().all())

    async def delete_for_session(self, session_id: str) -> int:
        result = await self.session.execute(
            delete(CallSegment).where(CallSegment.session_id == session_id)
        )
        return int(result.rowcount or 0)
