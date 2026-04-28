from pydantic import BaseModel
from typing import Optional

class SystemSettingBase(BaseModel):
    key: str
    value: str
    is_secret: bool = False

class SystemSettingCreate(SystemSettingBase):
    pass

class SystemSetting(SystemSettingBase):
    class Config:
        from_attributes = True

class TableInfo(BaseModel):
    name: str

class TableRecord(BaseModel):
    data: dict
