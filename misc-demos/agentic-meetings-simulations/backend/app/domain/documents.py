from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class DocumentBase(BaseModel):
    document_name: str
    library_scope: str # 'company', 'agent', 'meeting'
    owner_agent_id: UUID | None = None
    meeting_id: UUID | None = None
    file_type: str | None = None
    metadata_json: dict[str, Any] = {}

class DocumentCreate(DocumentBase):
    pass

class DocumentResponse(DocumentBase):
    id: UUID
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)
