from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import RecordingChunk


class ChunkRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(
        self,
        *,
        session_id: str,
        sequence_no: int,
        relative_path: str,
        mime_type: str,
        size_bytes: int,
    ) -> RecordingChunk:
        record = RecordingChunk(
            session_id=session_id,
            sequence_no=sequence_no,
            relative_path=relative_path,
            mime_type=mime_type,
            size_bytes=size_bytes,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def list_for_session(self, session_id: str) -> list[RecordingChunk]:
        result = await self.session.execute(
            select(RecordingChunk)
            .where(RecordingChunk.session_id == session_id)
            .order_by(RecordingChunk.sequence_no.asc(), RecordingChunk.id.asc())
        )
        return list(result.scalars().all())

    async def delete_for_session(self, session_id: str) -> int:
        result = await self.session.execute(
            delete(RecordingChunk).where(RecordingChunk.session_id == session_id)
        )
        return int(result.rowcount or 0)
