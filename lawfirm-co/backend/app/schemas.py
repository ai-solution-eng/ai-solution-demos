from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
from .models import CaseStatus, CaseType

class EvidenceBase(BaseModel):
    description: str
    evidence_type: str
    location_found: Optional[str] = None

class EvidenceCreate(EvidenceBase):
    pass

class Evidence(EvidenceBase):
    id: int
    case_id: int
    collected_date: datetime

    class Config:
        from_attributes = True

class DocumentBase(BaseModel):
    title: str
    content: Optional[str] = None

class DocumentCreate(DocumentBase):
    pass

class Document(DocumentBase):
    id: int
    case_id: int
    created_date: datetime

    class Config:
        from_attributes = True

class LawyerBase(BaseModel):
    full_name: str
    email: str
    specialization: str

class LawyerCreate(LawyerBase):
    pass

class Lawyer(LawyerBase):
    id: int

    class Config:
        from_attributes = True

class CaseBase(BaseModel):
    title: str
    description: Optional[str] = None
    status: str = CaseStatus.OPEN
    case_type: str = CaseType.FRAUD
    defendant_name: str

class CaseCreate(CaseBase):
    lead_attorney_id: int

class Case(CaseBase):
    id: int
    date_opened: datetime
    lead_attorney: Optional[Lawyer] = None
    evidence: List[Evidence] = []
    documents: List[Document] = []
    videos: List["CaseVideo"] = []

    class Config:
        from_attributes = True

class CaseVideoBase(BaseModel):
    filename: str
    summary: Optional[str] = None

class CaseVideoCreate(CaseVideoBase):
    pass

class CaseVideo(CaseVideoBase):
    id: int
    case_id: int
    file_path: str
    processed: int
    created_date: datetime

    class Config:
        from_attributes = True
