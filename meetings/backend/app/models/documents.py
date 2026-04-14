import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Column, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import Base, TimestampMixin


class Document(Base, TimestampMixin):
    __tablename__ = "documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_name = Column(String(255), nullable=False)
    library_scope = Column(String(50), nullable=False, index=True) # 'company', 'agent', 'meeting'
    owner_agent_id = Column(UUID(as_uuid=True), ForeignKey("role_agents.id"), nullable=True, index=True)
    meeting_id = Column(UUID(as_uuid=True), ForeignKey("meetings.id"), nullable=True, index=True)
    file_type = Column(String(50))
    metadata_json = Column(JSON, default=dict)

    chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan")

class DocumentChunk(Base, TimestampMixin):
    __tablename__ = "document_chunks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    chunk_index = Column(Integer, nullable=False)
    page_number = Column(String(50))  # page "4" or section marker "§Incident Response"
    text = Column(Text, nullable=False)
    normalized_text = Column(Text)

    # 2048 is default size for large qwen3-embedding
    embedding = Column(Vector(2048))

    document = relationship("Document", back_populates="chunks")
