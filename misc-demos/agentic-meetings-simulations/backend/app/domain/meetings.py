from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator


class MeetingTemplateBase(BaseModel):
    name: str
    description: str | None = None
    brief: str | None = None
    objective: str | None = None
    expectations: str | None = None
    agenda: str | None = None
    default_selected_attendee_ids: list[UUID] = []
    default_document_ids: list[UUID] = []
    is_builtin: bool = False

    @field_validator('default_selected_attendee_ids', 'default_document_ids', mode='before')
    @classmethod
    def none_to_list(cls, v):
        return v or []

class MeetingTemplateCreate(MeetingTemplateBase):
    pass

class MeetingTemplateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    brief: str | None = None
    objective: str | None = None
    expectations: str | None = None
    agenda: str | None = None
    default_selected_attendee_ids: list[UUID] | None = None
    is_builtin: bool | None = None

class MeetingTemplateResponse(MeetingTemplateBase):
    id: UUID
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)

class MeetingBase(BaseModel):
    brief: str | None = None
    agenda: str | None = None
    objective: str | None = None
    expectations: str | None = None
    selected_attendee_ids: list[UUID] = []
    turn_limit: int = 50
    template_id: UUID | None = None
    uploaded_brief_docs: list[UUID] = []

    @field_validator('selected_attendee_ids', 'uploaded_brief_docs', mode='before')
    @classmethod
    def none_to_list(cls, v):
        return v or []

class MeetingCreate(MeetingBase):
    pass

class MeetingResponse(MeetingBase):
    id: UUID
    status: str
    current_turn: int
    meeting_log: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    final_summary: str | None = None
    active_agent_id: str | None = None
    stop_requested: bool
    terminated: bool
    settings_snapshot: dict[str, Any]

    @field_validator('meeting_log', 'citations', 'warnings', mode='before')
    @classmethod
    def none_to_list_response(cls, v):
        return v or []

    @field_validator('settings_snapshot', mode='before')
    @classmethod
    def none_to_dict(cls, v):
        return v or {}

    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


class MeetingDocumentLink(BaseModel):
    library_scope: str = "meeting"
    owner_agent_id: UUID | None = None
