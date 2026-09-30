import uuid

from sqlalchemy import JSON, Boolean, Column, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.models.base import Base, TimestampMixin


class MeetingTemplate(Base, TimestampMixin):
    __tablename__ = "meeting_templates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    description = Column(Text)
    brief = Column(Text)
    objective = Column(Text)
    expectations = Column(Text)
    agenda = Column(Text)
    default_selected_attendee_ids = Column(JSON, default=list)
    default_document_ids = Column(JSON, default=list)
    is_builtin = Column(Boolean, default=False)

class Meeting(Base, TimestampMixin):
    __tablename__ = "meetings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status = Column(String(50), default="draft") # draft, queued, running, stopping, completed, terminated, failed
    brief = Column(Text)
    agenda = Column(Text)
    objective = Column(Text)
    expectations = Column(Text)
    selected_attendee_ids = Column(JSON, default=list) # List of UUIDs
    turn_limit = Column(Integer, default=50)
    current_turn = Column(Integer, default=0)

    template_id = Column(UUID(as_uuid=True), ForeignKey("meeting_templates.id"), nullable=True)

    # Dynamic state
    meeting_log = Column(JSON, default=list)
    citations = Column(JSON, default=list)
    warnings = Column(JSON, default=list)
    final_summary = Column(Text)
    active_agent_id = Column(String(50), nullable=True) # UUID str or 'supervisor'

    stop_requested = Column(Boolean, default=False)
    terminated = Column(Boolean, default=False)
    uploaded_brief_docs = Column(JSON, default=list) # List of doc UUIDs
    settings_snapshot = Column(JSON, default=dict)
