from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class APIResponse(BaseModel):
    """Standard API response wrapper"""
    status: str  # "success" or "error"
    data: Any | None = None
    message: str | None = None
    meta: dict | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
