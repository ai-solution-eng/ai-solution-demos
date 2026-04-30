import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import structlog
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import select

from app.core import database
from app.core.config import settings
from app.models.meetings import Meeting
from app.models.roles import RoleAgent
from app.orchestration.graph import build_meeting_graph

logger = structlog.get_logger(__name__)

async def run_meeting_execution(meeting_id: str) -> AsyncGenerator[dict[str, Any], None]:
    """Runs a meeting session using a StateGraph and yields serializable UI events."""
    async with database.async_session_maker() as session:
        # 1. Fetch & Validate Simulation Target
        meeting_uuid = UUID(meeting_id)
        query = select(Meeting).where(Meeting.id == meeting_uuid)
        result = await session.execute(query)
        meeting = result.scalar_one_or_none()

        if not meeting or meeting.status not in ["queued", "running", "draft"]:
            yield {
                "type": "error",
                "content": "Simulation target unreachable or invalid status.",
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            }
            return

        # 2. Transition State
        meeting.status = "running"
        yield {"type": "meeting_started", "meeting_id": meeting_id, "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}

        # 3. Configure Orchestration
        attendee_query = select(RoleAgent).where(RoleAgent.id.in_(meeting.selected_attendee_ids))
        attendees = {str(r.id): r for r in (await session.execute(attendee_query)).scalars().all()}
        system_settings = settings.get_system_settings()

        graph = build_meeting_graph(attendees)

        initial_state = {
            "meeting_id": str(meeting.id),
            "brief": meeting.brief or "",
            "agenda": meeting.agenda or "",
            "objective": meeting.objective or "",
            "turn_limit": meeting.turn_limit or 50,
            "current_turn": meeting.current_turn or 0,
            "messages": [],
            "event_log": []
        }

        thread_config = {
            "configurable": {
                "thread_id": meeting_id,
                "model_settings": system_settings,
                "app_settings": system_settings,
                "attendees": attendees
            }
        }

        # 4. Resolve Persistent Checkpointer
        pg_url = settings.DATABASE_URL.replace("+asyncpg", "") if settings.DATABASE_URL else None
        
        async def _execute_with_checkpointer(cp: Any):
            """Executes graph with provided checkpointer and yields events."""
            app_graph = graph.compile(checkpointer=cp)
            has_error = False
            accumulated_events = []
            final_summary = None

            async for event in _stream_graph(app_graph, initial_state, thread_config, meeting_id):
                if event.get("type") == "error":
                    has_error = True
                
                accumulated_events.append(event)
                if event.get("is_conclusion") and event.get("reasoning"):
                    final_summary = event.get("reasoning")
                
                yield event

            # Persistence layer update
            await _update_meeting_status(
                meeting_id, 
                "failed" if has_error else "completed", 
                event_log=accumulated_events,
                final_summary=final_summary
            )
            
            if not has_error:
                yield {"type": "meeting_completed", "meeting_id": meeting_id, "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}

        # Entry logic: Attempt PG for durability, fallback to Memory for availability
        if pg_url:
            try:
                logger.info("attempting_durable_checkpoint_initialisation", meeting_id=meeting_id)
                async with AsyncConnectionPool(pg_url, max_size=5, kwargs={"autocommit": True}) as pool:
                    checkpointer = AsyncPostgresSaver(pool)
                    async with asyncio.timeout(5):
                        await checkpointer.setup()
                    
                    logger.info("durable_checkpoint_active", meeting_id=meeting_id)
                    async for event in _execute_with_checkpointer(checkpointer):
                        yield event
                    return
            except Exception as e:
                logger.warning("durable_checkpoint_failed_falling_back", error=str(e), meeting_id=meeting_id)

        # Volatile Fallback
        logger.info("activating_volatile_memory_checkpointer", meeting_id=meeting_id)
        async for event in _execute_with_checkpointer(MemorySaver()):
            yield event

async def _stream_graph(
    app_graph: Any,
    initial_state: dict[str, Any],
    thread_config: dict[str, Any],
    meeting_id: str
) -> AsyncGenerator[dict[str, Any], None]:
    """Streams orchestration updates and yields serializable events."""
    logger.info("astream_execution_starting", meeting_id=meeting_id)
    try:
        async for event in app_graph.astream(initial_state, thread_config, stream_mode="updates"):
            for node_name, state_update in event.items():
                logger.info("orchestration_update", node=node_name)

                if "event_log" in state_update and state_update["event_log"]:
                    for e in state_update["event_log"]:
                        if "timestamp" not in e:
                            e["timestamp"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                        yield e

                if node_name == "supervisor":
                    next_spk = state_update.get("next_speaker")
                    if next_spk:
                        yield {
                            "type": "supervisor_selected_next_agent",
                            "agent_id": next_spk,
                            "reasoning": state_update.get("reasoning"),
                            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                        }
                elif node_name == "__start__":
                    continue
                else:
                    # Clear typing status for a finished agent turn handled naturally by agent_spoke yield above
                    pass

    except Exception as e:
        logger.error("graph_runtime_exception", error=str(e), meeting_id=meeting_id)
        yield {
            "type": "error",
            "content": f"Nexus Runtime Exception: {str(e)}",
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        }

async def _update_meeting_status(meeting_id: str, status: str, event_log: list[dict[str, Any]] | None = None, final_summary: str | None = None) -> None:
    """Synchronizes simulation results with the persistent layer."""
    assert database.async_session_maker is not None
    async with database.async_session_maker() as session:
        query = select(Meeting).where(Meeting.id == UUID(meeting_id))
        meeting = (await session.execute(query)).scalar_one_or_none()
        if meeting:
            meeting.status = status
            if event_log is not None:
                logger.info("persisting_telemetry_logs", meeting_id=meeting_id, count=len(event_log))
                meeting.meeting_log = event_log
            if final_summary is not None:
                meeting.final_summary = final_summary
            await session.commit()
            logger.info("simulation_state_persisted", meeting_id=meeting_id, status=status, log_size=len(event_log) if event_log else 0)
