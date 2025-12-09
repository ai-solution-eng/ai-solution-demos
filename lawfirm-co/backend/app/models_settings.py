from sqlalchemy import Column, Integer, String, Boolean
from .database import Base

class SystemSetting(Base):
    __tablename__ = "system_settings"

    key = Column(String, primary_key=True, index=True)
    value = Column(String)
    is_secret = Column(Boolean, default=False)
