from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import case, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import SESSION_TTL_HOURS
from app.db import CallSession, build_expiry, utc_now


class SessionRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        *,
        room_id: str,
        src_language: str,
        presenter_target_language: str,
        status: str = "active",
        recording_state: str = "idle",
    ) -> CallSession:
        record = CallSession(
            id=str(uuid.uuid4()),
            room_id=room_id,
            status=status,
            recording_state=recording_state,
            src_language=src_language,
            presenter_target_language=presenter_target_language,
            expires_at=build_expiry(),
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def get(self, session_id: str) -> CallSession | None:
        return await self.session.get(CallSession, session_id)

    async def get_by_room_id(self, room_id: str) -> CallSession | None:
        status_rank = case(
            (CallSession.status == "active", 0),
            (CallSession.status == "paused", 1),
            (CallSession.status == "interrupted", 2),
            else_=3,
        )
        result = await self.session.execute(
            select(CallSession)
            .where(
                CallSession.room_id == room_id,
                CallSession.expires_at.is_not(None),
                CallSession.expires_at > utc_now(),
            )
            .order_by(status_rank.asc(), CallSession.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_recoverable(self, *, now: datetime | None = None) -> list[CallSession]:
        current = now or utc_now()
        result = await self.session.execute(
            select(CallSession).where(
                CallSession.expires_at.is_not(None),
                CallSession.expires_at > current,
                CallSession.status.in_(["active", "interrupted", "paused"]),
            )
        )
        return list(result.scalars().all())

    async def mark_ended(
        self,
        session_id: str,
        *,
        status: str = "ended",
        ttl_hours: int = SESSION_TTL_HOURS,
    ) -> CallSession | None:
        record = await self.get(session_id)
        if record is None:
            return None
        ended_at = utc_now()
        record.status = status
        record.ended_at = ended_at
        record.expires_at = build_expiry(ttl_hours, reference=ended_at)
        await self.session.flush()
        return record

    async def touch(self, session_id: str) -> CallSession | None:
        record = await self.get(session_id)
        if record is None:
            return None
        record.updated_at = utc_now()
        await self.session.flush()
        return record

    async def update(self, session_id: str, **fields: object) -> CallSession | None:
        record = await self.get(session_id)
        if record is None:
            return None
        for key, value in fields.items():
            setattr(record, key, value)
        if (record.status or "").lower() in {"active", "paused", "interrupted"}:
            record.expires_at = build_expiry()
        record.updated_at = utc_now()
        await self.session.flush()
        return record

    async def list_expired(self, *, now: datetime | None = None) -> list[CallSession]:
        current = now or utc_now()
        result = await self.session.execute(
            select(CallSession).where(
                CallSession.expires_at.is_not(None), CallSession.expires_at <= current
            )
        )
        return list(result.scalars().all())

    async def delete_expired(self, *, now: datetime | None = None) -> int:
        current = now or utc_now()
        result = await self.session.execute(
            delete(CallSession).where(
                CallSession.expires_at.is_not(None), CallSession.expires_at <= current
            )
        )
        return int(result.rowcount or 0)
