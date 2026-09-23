from datetime import UTC, datetime

from sqlalchemy import Column, DateTime
from sqlalchemy.orm import declarative_base

from app.core.config import settings

Base = declarative_base()
# Set default schema for all models inherit from this Base
Base.metadata.schema = settings.DB_SCHEMA

class TimestampMixin:
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC), nullable=False)
