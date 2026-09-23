from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator


class RoleAgentBase(BaseModel):
    display_name: str
    title: str
    department: str
    seniority: str | None = None
    summary: str | None = None

    responsibilities: list[str] = []
    kpis: list[str] = []
    priorities: list[str] = []
    objectives: list[str] = []

    risk_tolerance: str | None = None
    tone: list[str] = []
    collaboration_style: str | None = None
    challenge_style: str | None = None

    allowed_shared_library_access: bool = True

    system_prompt: str | None = None
    default_tools: list[str] = []
    ui_metadata: dict[str, Any] = {}

    @field_validator("tone", mode="before")
    @classmethod
    def validate_tone(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            v_stripped = v.strip()
            if v_stripped.startswith("[") and v_stripped.endswith("]"):
                try:
                    import json
                    parsed = json.loads(v_stripped)
                    if isinstance(parsed, list):
                        return [str(item) for item in parsed]
                except Exception:
                    pass
            return [t.strip() for t in v.split(",") if t.strip()]
        return v or []

class RoleAgentCreate(RoleAgentBase):
    pass

class RoleAgentUpdate(BaseModel):
    display_name: str | None = None
    title: str | None = None
    department: str | None = None
    seniority: str | None = None
    summary: str | None = None

    responsibilities: list[str] | None = None
    kpis: list[str] | None = None
    priorities: list[str] | None = None
    objectives: list[str] | None = None

    risk_tolerance: str | None = None
    tone: list[str] | None = None
    collaboration_style: str | None = None
    challenge_style: str | None = None

    allowed_shared_library_access: bool | None = None

    system_prompt: str | None = None
    default_tools: list[str] | None = None
    ui_metadata: dict[str, Any] | None = None

class RoleAgentResponse(RoleAgentBase):
    id: UUID
    private_library_id: UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
