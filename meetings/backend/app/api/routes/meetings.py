import asyncio
import json
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database
from app.domain.meetings import MeetingCreate, MeetingResponse, MeetingDocumentLink
from app.domain.response import APIResponse
from app.models.documents import Document, DocumentChunk
from app.models.meetings import Meeting, MeetingTemplate
from app.services.meeting_executor import run_meeting_execution

logger = structlog.get_logger(__name__)
router = APIRouter()

# Global dict to track executing meeting tasks
active_meetings: dict[str, asyncio.Task] = {}

@router.get("", response_model=APIResponse)
async def list_meetings(session: AsyncSession = Depends(database.get_db_session)) -> APIResponse:
    query = select(Meeting).order_by(Meeting.created_at.desc())
    result = await session.execute(query)
    meetings = result.scalars().all()

    return APIResponse(
        status="success",
        data=[MeetingResponse.model_validate(m).model_dump() for m in meetings]
    )

@router.post("", response_model=APIResponse)
async def create_meeting(meeting_in: MeetingCreate, session: AsyncSession = Depends(database.get_db_session)) -> APIResponse:
    # Enforce only one active meeting at a time
    active_query = select(Meeting).where(Meeting.status.in_(["running", "queued"]))
    active_result = await session.execute(active_query)
    if active_result.scalars().first():
        raise HTTPException(status_code=400, detail="Cannot start a new meeting. Another meeting is currently active.")

    meeting = Meeting(
        brief=meeting_in.brief,
        agenda=meeting_in.agenda,
        objective=meeting_in.objective,
        expectations=meeting_in.expectations,
        selected_attendee_ids=[str(u) for u in meeting_in.selected_attendee_ids],
        turn_limit=meeting_in.turn_limit,
        template_id=meeting_in.template_id,
        status="draft",
        uploaded_brief_docs=[str(u) for u in meeting_in.uploaded_brief_docs]
    )

    session.add(meeting)
    await session.commit()
    await session.refresh(meeting)

    if meeting.template_id:
        template_query = select(MeetingTemplate).where(MeetingTemplate.id == meeting.template_id)
        template = (await session.execute(template_query)).scalar_one_or_none()

        if template and template.default_document_ids:
            new_uploaded_docs = list(meeting.uploaded_brief_docs)
            for doc_id_str in template.default_document_ids:
                doc_query = select(Document).where(Document.id == UUID(doc_id_str))
                doc = (await session.execute(doc_query)).scalar_one_or_none()
                if doc:
                    # clone document
                    new_doc = Document(
                        document_name=doc.document_name,
                        library_scope="meeting" if doc.library_scope == "company" else doc.library_scope,
                        owner_agent_id=doc.owner_agent_id,
                        meeting_id=meeting.id,
                        file_type=doc.file_type,
                        metadata_json=doc.metadata_json
                    )
                    session.add(new_doc)
                    await session.flush()
                    new_uploaded_docs.append(str(new_doc.id))

                    # clone chunks
                    chunks_query = select(DocumentChunk).where(DocumentChunk.document_id == doc.id)
                    chunks = (await session.execute(chunks_query)).scalars().all()
                    for chunk in chunks:
                        new_chunk = DocumentChunk(
                            document_id=new_doc.id,
                            chunk_index=chunk.chunk_index,
                            page_number=chunk.page_number,
                            text=chunk.text,
                            normalized_text=chunk.normalized_text,
                            embedding=chunk.embedding
                        )
                        session.add(new_chunk)
            meeting.uploaded_brief_docs = new_uploaded_docs
            await session.commit()
            await session.refresh(meeting)

    return APIResponse(status="success", data=MeetingResponse.model_validate(meeting).model_dump())

@router.get("/{meeting_id}", response_model=APIResponse)
async def get_meeting(meeting_id: UUID, session: AsyncSession = Depends(database.get_db_session)) -> APIResponse:
    query = select(Meeting).where(Meeting.id == meeting_id)
    result = await session.execute(query)
    meeting = result.scalar_one_or_none()

    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    return APIResponse(status="success", data=MeetingResponse.model_validate(meeting).model_dump())

@router.delete("/{meeting_id}", response_model=APIResponse)
async def delete_meeting(meeting_id: UUID, session: AsyncSession = Depends(database.get_db_session)) -> APIResponse:
    # 1. Fetch meeting
    query = select(Meeting).where(Meeting.id == meeting_id)
    result = await session.execute(query)
    meeting = result.scalar_one_or_none()

    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    # 2. Cleanup associated documents
    # Cascading deletes will handle the chunks automatically via Relationship definition
    from sqlalchemy import delete
    from app.models.documents import Document
    
    doc_delete_query = delete(Document).where(Document.meeting_id == meeting_id)
    await session.execute(doc_delete_query)

    # 3. Delete meeting
    await session.delete(meeting)
    await session.commit()

    return APIResponse(status="success", data={"deleted": str(meeting_id)})

@router.websocket("/{meeting_id}/ws")
async def meeting_websocket(websocket: WebSocket, meeting_id: str) -> None:
    await websocket.accept()
    logger.info("websocket_connected", meeting_id=meeting_id)

    execution_task = None

    async def send_events() -> None:
        try:
            async for event in run_meeting_execution(meeting_id):
                await websocket.send_json(event)
        except asyncio.CancelledError:
            # Mark terminated logically if cancelled mid-run
            logger.info("websocket_send_events_cancelled", meeting_id=meeting_id)
            assert database.async_session_maker is not None
            async with database.async_session_maker() as session:
                query = select(Meeting).where(Meeting.id == UUID(meeting_id))
                meeting = (await session.execute(query)).scalar_one_or_none()
                if meeting:
                    meeting.status = "terminated"
                    meeting.terminated = True
                    await session.commit()
        finally:
            if meeting_id in active_meetings:
                del active_meetings[meeting_id]
                logger.debug("meeting_removed_from_active_registry", meeting_id=meeting_id)

    try:
        while True:
            data = await websocket.receive_text()
            try:
                payload = json.loads(data)
                command = payload.get("command")
                logger.info("websocket_command_received", command=command, meeting_id=meeting_id)

                if command == "start_meeting":
                    if meeting_id in active_meetings:
                        logger.warning("meeting_already_running_in_process", meeting_id=meeting_id)
                        await websocket.send_json({"type": "error", "content": "Meeting already running"})
                    else:
                        # Change via db session first
                        assert database.async_session_maker is not None
                        async with database.async_session_maker() as session:
                            query = select(Meeting).where(Meeting.id == UUID(meeting_id))
                            meeting = (await session.execute(query)).scalar_one_or_none()
                            if meeting:
                                meeting.status = "queued"
                                await session.commit()
                                logger.info("meeting_status_updated_to_queued", meeting_id=meeting_id)

                        execution_task = asyncio.create_task(send_events())
                        active_meetings[meeting_id] = execution_task

                elif command in ["stop_meeting", "terminate_meeting"]:
                    logger.info("stopping_meeting", meeting_id=meeting_id)
                    if execution_task:
                        execution_task.cancel()

                    # Ensure DB status is set to terminated immediately
                    assert database.async_session_maker is not None
                    async with database.async_session_maker() as session:
                        query = select(Meeting).where(Meeting.id == UUID(meeting_id))
                        meeting = (await session.execute(query)).scalar_one_or_none()
                        if meeting:
                            meeting.status = "terminated"
                            meeting.terminated = True
                            await session.commit()

                    await websocket.send_json({"type": "meeting_terminated"})

            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "content": "Invalid JSON format."})

    except WebSocketDisconnect:
        logger.info("websocket_disconnected", meeting_id=meeting_id)
        if execution_task:
            execution_task.cancel()
@router.post("/{meeting_id}/documents/{document_id}", response_model=APIResponse)
async def add_existing_document_to_meeting(
    meeting_id: UUID, 
    document_id: UUID, 
    link_data: MeetingDocumentLink,
    session: AsyncSession = Depends(database.get_db_session)
) -> APIResponse:
    library_scope = link_data.library_scope
    owner_agent_id = link_data.owner_agent_id
    # 1. Fetch meeting
    meeting_query = select(Meeting).where(Meeting.id == meeting_id)
    meeting = (await session.execute(meeting_query)).scalar_one_or_none()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    # 2. Fetch original document
    doc_query = select(Document).where(Document.id == document_id)
    original_doc = (await session.execute(doc_query)).scalar_one_or_none()
    if not original_doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # 3. Clone document for this meeting
    new_doc = Document(
        document_name=original_doc.document_name,
        library_scope=library_scope,
        owner_agent_id=owner_agent_id,
        meeting_id=meeting_id,
        file_type=original_doc.file_type,
        metadata_json=original_doc.metadata_json
    )
    session.add(new_doc)
    await session.flush()

    # 4. Clone chunks
    chunks_query = select(DocumentChunk).where(DocumentChunk.document_id == original_doc.id)
    chunks = (await session.execute(chunks_query)).scalars().all()
    for chunk in chunks:
        new_chunk = DocumentChunk(
            document_id=new_doc.id,
            chunk_index=chunk.chunk_index,
            page_number=chunk.page_number,
            text=chunk.text,
            normalized_text=chunk.normalized_text,
            embedding=chunk.embedding
        )
        session.add(new_chunk)

    # 5. Track in meeting's brief docs
    updated_docs = list(meeting.uploaded_brief_docs or [])
    updated_docs.append(str(new_doc.id))
    meeting.uploaded_brief_docs = updated_docs

    await session.commit()
    await session.refresh(new_doc)

    return APIResponse(status="success", data={"document_id": str(new_doc.id)})
