import uuid

from sqlalchemy import JSON, Boolean, Column, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.models.base import Base, TimestampMixin


class RoleAgent(Base, TimestampMixin):
    __tablename__ = "role_agents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    display_name = Column(String(100), nullable=False)
    title = Column(String(100), nullable=False)
    department = Column(String(100), nullable=False)
    seniority = Column(String(50))
    summary = Column(Text)

    responsibilities = Column(JSON, default=list)  # List of strings
    kpis = Column(JSON, default=list)
    priorities = Column(JSON, default=list)
    objectives = Column(JSON, default=list)

    risk_tolerance = Column(String(50))
    tone = Column(JSON, default=list)
    collaboration_style = Column(String(100))
    challenge_style = Column(String(100))

    allowed_shared_library_access = Column(Boolean, default=True)
    private_library_id = Column(UUID(as_uuid=True), default=uuid.uuid4, unique=True)

    system_prompt = Column(Text)
    default_tools = Column(JSON, default=list)

    # Optional metadata for UI like avatar/color
    ui_metadata = Column(JSON, default=dict)
