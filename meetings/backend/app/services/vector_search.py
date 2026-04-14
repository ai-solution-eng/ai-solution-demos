from uuid import UUID

import structlog
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.documents import Document, DocumentChunk
from app.services.embedding_service import generate_embedding

logger = structlog.get_logger(__name__)

async def semantic_search(
    query_text: str,
    library_scopes: list[str],
    limit: int,
    session: AsyncSession,
    meeting_id: UUID | None = None,
    owner_agent_id: UUID | None = None
) -> list[dict]:
    # 1. Embed query
    query_embedding = await generate_embedding(query_text, session)

    # 2. Vector search with filters
    query = (
        select(DocumentChunk, Document)
        .join(Document, DocumentChunk.document_id == Document.id)
    )

    # 3. Apply Metadata Filters
    filters = []

    # Scopes
    if "company" in library_scopes:
        filters.append(Document.library_scope == "company")

    if "agent" in library_scopes and owner_agent_id:
        filters.append(and_(
            Document.library_scope == "agent",
            Document.owner_agent_id == owner_agent_id
        ))

    if "meeting" in library_scopes and meeting_id:
        filters.append(and_(
            Document.library_scope == "meeting",
            Document.meeting_id == meeting_id
        ))

    if filters:
        query = query.where(or_(*filters))

    # Order by distance
    query = query.order_by(
        DocumentChunk.embedding.cosine_distance(query_embedding)
    ).limit(limit)

    result = await session.execute(query)
    rows = result.all()

    evidence = []
    for chunk, doc in rows:
        evidence.append({
            "document_name": doc.document_name,
            "document_id": str(doc.id),
            "library_scope": doc.library_scope,
            "page_number": chunk.page_number,
            "text": chunk.text
        })

    return evidence
