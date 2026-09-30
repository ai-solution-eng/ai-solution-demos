from uuid import UUID

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.domain.documents import DocumentResponse
from app.domain.response import APIResponse
from app.models.documents import Document
from app.services.document_processing import process_document_background

logger = structlog.get_logger(__name__)
router = APIRouter()

@router.get("", response_model=APIResponse)
async def list_documents(
    library_scope: str | None = None,
    owner_agent_id: UUID | None = None,
    meeting_id: UUID | None = None,
    session: AsyncSession = Depends(get_db_session)
) -> APIResponse:
    query = select(Document)
    if library_scope:
        query = query.where(Document.library_scope == library_scope)
    if owner_agent_id:
        query = query.where(Document.owner_agent_id == owner_agent_id)
    if meeting_id:
        query = query.where(Document.meeting_id == meeting_id)

    result = await session.execute(query)
    documents = result.scalars().all()

    return APIResponse(
        status="success",
        data=[DocumentResponse.model_validate(d).model_dump() for d in documents]
    )

@router.post("", response_model=APIResponse)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    library_scope: str = Form(...),
    owner_agent_id: UUID | None = Form(None),
    meeting_id: UUID | None = Form(None),
    session: AsyncSession = Depends(get_db_session)
) -> APIResponse:
    # Basic metadata capture
    content = await file.read()
    file_type = file.content_type

    new_doc = Document(
        document_name=file.filename or "Unnamed Document",
        library_scope=library_scope,
        owner_agent_id=owner_agent_id,
        meeting_id=meeting_id,
        file_type=file_type,
        metadata_json={"size": len(content)}
    )

    session.add(new_doc)
    await session.commit()
    await session.refresh(new_doc)

    background_tasks.add_task(
        process_document_background,
        new_doc.id,
        content,
        file_type
    )

    logger.info("document_upload_accepted", doc_id=str(new_doc.id), name=new_doc.document_name)


    return APIResponse(
        status="success",
        data=DocumentResponse.model_validate(new_doc).model_dump()
    )

@router.get("/{document_id}/content", response_model=APIResponse)
async def get_document_content(document_id: UUID, session: AsyncSession = Depends(get_db_session)) -> APIResponse:
    # Fetch doc to check it exists
    query = select(Document).where(Document.id == document_id)
    doc_result = await session.execute(query)
    doc = doc_result.scalar_one_or_none()
    
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
        
    # Fetch all chunks ordered by index
    from app.models.documents import DocumentChunk
    chunks_query = select(DocumentChunk).where(DocumentChunk.document_id == document_id).order_by(DocumentChunk.chunk_index)
    chunks_result = await session.execute(chunks_query)
    chunks = chunks_result.scalars().all()
    
    full_text = "\n".join([c.text for c in chunks])
    
    return APIResponse(
        status="success",
        data={
            "document_name": doc.document_name,
            "content": full_text,
            "metadata": doc.metadata_json,
            "file_type": doc.file_type
        }
    )

@router.delete("/{document_id}", response_model=APIResponse)
async def delete_document(document_id: UUID, session: AsyncSession = Depends(get_db_session)) -> APIResponse:
    query = select(Document).where(Document.id == document_id)
    result = await session.execute(query)
    doc = result.scalar_one_or_none()

    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    await session.delete(doc)
    await session.commit()

    return APIResponse(status="success", message="Document deleted successfully")

@router.post("/{document_id}/clone", response_model=APIResponse)
async def clone_document(
    document_id: UUID,
    library_scope: str = Form(...),
    owner_agent_id: UUID | None = Form(None),
    meeting_id: UUID | None = Form(None),
    session: AsyncSession = Depends(get_db_session)
) -> APIResponse:
    # 1. Fetch original document
    query = select(Document).where(Document.id == document_id)
    original_doc = (await session.execute(query)).scalar_one_or_none()
    if not original_doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # 2. Duplicate document entry
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

    # 3. Duplicate chunks
    from app.models.documents import DocumentChunk
    chunks_query = select(DocumentChunk).where(DocumentChunk.document_id == document_id)
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

    await session.commit()
    await session.refresh(new_doc)

    logger.info("document_cloned", source_id=str(document_id), new_id=str(new_doc.id), scope=library_scope)

    return APIResponse(
        status="success",
        data=DocumentResponse.model_validate(new_doc).model_dump()
    )
