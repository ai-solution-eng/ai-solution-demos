from __future__ import annotations

import asyncio
from pathlib import Path

from app.config import EXPORT_STORAGE_ROOT, RECORDING_STORAGE_ROOT, SESSION_STORAGE_ROOT
from app.db import database_enabled, session_scope
from app.repositories.chunks import ChunkRepository
from app.repositories.export_jobs import ExportJobRepository
from app.repositories.sessions import SessionRepository
from app.state.rooms import ROOMS, ROOMS_LOCK


def _remove_path_if_exists(path_str: str | None) -> None:
    if not path_str:
        return
    path = Path(path_str)
    try:
        if path.is_file():
            path.unlink(missing_ok=True)
    except Exception:
        pass


def _remove_dir_if_exists(path_str: str | None) -> None:
    if not path_str:
        return
    path = Path(path_str)
    try:
        if path.exists():
            for child in sorted(path.rglob("*"), reverse=True):
                if child.is_file():
                    child.unlink(missing_ok=True)
                else:
                    child.rmdir()
            path.rmdir()
    except Exception:
        pass


def _prune_empty_parents(path_str: str | None, roots: list[Path]) -> None:
    if not path_str:
        return
    path = Path(path_str)
    current = path.parent
    resolved_roots = [root.resolve() for root in roots]
    while current != current.parent:
        try:
            resolved = current.resolve()
        except Exception:
            break
        if resolved in resolved_roots:
            break
        if not any(root in resolved.parents for root in resolved_roots):
            break
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


async def cleanup_expired_persisted_state() -> dict[str, int]:
    if not database_enabled():
        return {"sessions": 0, "jobs": 0}

    expired_session_ids: list[str] = []
    chunk_paths: list[str] = []
    expired_room_ids: list[str] = []
    expired_session_dirs: list[str] = []
    artifact_paths: list[str] = []

    async with session_scope() as db_session:
        session_repo = SessionRepository(db_session)
        chunk_repo = ChunkRepository(db_session)
        export_repo = ExportJobRepository(db_session)

        expired_sessions = await session_repo.list_expired()
        expired_jobs = await export_repo.list_expired()

        for session_record in expired_sessions:
            expired_session_ids.append(session_record.id)
            expired_room_ids.append(session_record.room_id)
            expired_session_dirs.append(
                str((RECORDING_STORAGE_ROOT / session_record.room_id / session_record.id).resolve())
            )
            chunks = await chunk_repo.list_for_session(session_record.id)
            for chunk in chunks:
                path = Path(chunk.relative_path)
                chunk_paths.append(
                    str(path if path.is_absolute() else (SESSION_STORAGE_ROOT / chunk.relative_path))
                )

        for job in expired_jobs:
            if job.artifact_path:
                artifact_paths.append(job.artifact_path)

        session_count = await session_repo.delete_expired()
        job_count = await export_repo.delete_expired()

    for path_str in chunk_paths + artifact_paths:
        await asyncio.to_thread(_remove_path_if_exists, path_str)
        await asyncio.to_thread(
            _prune_empty_parents,
            path_str,
            [SESSION_STORAGE_ROOT.resolve(), EXPORT_STORAGE_ROOT.resolve()],
        )

    for session_dir in expired_session_dirs:
        await asyncio.to_thread(_remove_dir_if_exists, session_dir)

    if expired_room_ids:
        async with ROOMS_LOCK:
            for room_id in expired_room_ids:
                ROOMS.pop(room_id, None)

    return {"sessions": session_count, "jobs": job_count}


async def periodic_cleanup_loop(interval_seconds: int = 600) -> None:
    while True:
        try:
            await cleanup_expired_persisted_state()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(interval_seconds)
