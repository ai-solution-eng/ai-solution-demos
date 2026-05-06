import io
from uuid import UUID

import structlog
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader

from app.core import database
from app.models.documents import DocumentChunk
from app.services.embedding_service import generate_embeddings

logger = structlog.get_logger(__name__)

def process_file_content(file_bytes: bytes, file_type: str, document_id: str) -> list[dict]:
    """Parse bytes -> List of chunk dicts ready for embedding & storage."""
    chunks_data = []

    # 1. Extract text and pages
    pages_text = [] # list of (page_num, text)
    if file_type == "application/pdf":
        try:
            reader = PdfReader(io.BytesIO(file_bytes))
            for i, page in enumerate(reader.pages):
                text = page.extract_text()
                if text:
                    pages_text.append((str(i + 1), text))
        except Exception as e:
            logger.error("pdf_parsing_failed", doc_id=document_id, error=str(e))
            raise
    else:
        # Default to plain text
        try:
            text = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text = file_bytes.decode("latin-1", errors="replace")
        pages_text.append(("1", text))

    # 2. Chunking
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=200,
        separators=["\n\n", "\n", " ", ""]
    )

    chunk_index = 0
    for page_num, text in pages_text:
        if not text.strip():
            continue

        chunks = splitter.create_documents([text])
        for chunk in chunks:
            chunks_data.append({
                "document_id": document_id,
                "chunk_index": chunk_index,
                "page_number": page_num,
                "text": chunk.page_content,
                "normalized_text": chunk.page_content.lower()
            })
            chunk_index += 1

    return chunks_data

async def process_document_background(doc_id: UUID, file_bytes: bytes, file_type: str) -> None:
    logger.info("background_processing_started", doc_id=str(doc_id))
    try:
        chunks_data = process_file_content(file_bytes, file_type, str(doc_id))

        async with database.async_session_maker() as session:
            texts = [c["text"] for c in chunks_data]
            if texts:
                embeddings = await generate_embeddings(texts, session)

                db_chunks = []
                for chunk, emb in zip(chunks_data, embeddings, strict=True):
                    db_chunks.append(DocumentChunk(
                        document_id=doc_id,
                        chunk_index=chunk["chunk_index"],
                        page_number=chunk["page_number"],
                        text=chunk["text"],
                        normalized_text=chunk["normalized_text"],
                        embedding=emb
                    ))

                session.add_all(db_chunks)
                await session.commit()
                logger.info("document_processed", doc_id=str(doc_id), chunk_count=len(db_chunks))
    except Exception as e:
        logger.error("document_processing_failed_in_background", doc_id=str(doc_id), error=str(e))

