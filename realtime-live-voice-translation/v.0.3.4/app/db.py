from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import AsyncIterator

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import DATABASE_URL, DB_MAX_OVERFLOW, DB_POOL_SIZE, SESSION_TTL_HOURS


class Base(DeclarativeBase):
    pass


class CallSession(Base):
    __tablename__ = "call_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    room_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    recording_state: Mapped[str] = mapped_column(String(32), default="idle", nullable=False)
    src_language: Mapped[str] = mapped_column(String(16), nullable=False)
    presenter_target_language: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CallSegment(Base):
    __tablename__ = "call_segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("call_sessions.id", ondelete="CASCADE"), index=True
    )
    segment_id: Mapped[str] = mapped_column(String(128), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_final: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    included_in_recording: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_language: Mapped[str] = mapped_column(String(16), nullable=False)
    translations_json: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    ts_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RecordingChunk(Base):
    __tablename__ = "recording_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("call_sessions.id", ondelete="CASCADE"), index=True
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    relative_path: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ExportJob(Base):
    __tablename__ = "export_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("call_sessions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stage: Mapped[str] = mapped_column(String(128), default="Queued", nullable=False)
    detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    archive_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    artifact_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SessionCheckpoint(Base):
    __tablename__ = "session_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("call_sessions.id", ondelete="CASCADE"), index=True
    )
    window_start_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    window_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    summary_en: Mapped[str] = mapped_column(Text, default="", nullable=False)
    minutes_en: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def database_enabled() -> bool:
    return bool(DATABASE_URL)


def get_database_url() -> str:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    return DATABASE_URL


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            get_database_url(),
            pool_pre_ping=True,
            pool_size=DB_POOL_SIZE,
            max_overflow=DB_MAX_OVERFLOW,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    session = get_session_factory()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def utc_now() -> datetime:
    return datetime.now(UTC)


def build_expiry(hours: int = SESSION_TTL_HOURS, *, reference: datetime | None = None) -> datetime:
    base = reference or utc_now()
    return base + timedelta(hours=hours)
