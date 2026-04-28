from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Text, Enum
from sqlalchemy.orm import relationship
from .database import Base
from .models_settings import SystemSetting
import datetime
import enum

class CaseStatus(str, enum.Enum):
    OPEN = "Open"
    CLOSED = "Closed"
    PENDING_TRIAL = "Pending Trial"
    UNDER_INVESTIGATION = "Under Investigation"
    DISMISSED = "Dismissed"

class CaseType(str, enum.Enum):
    FRAUD = "Fraud"
    HOMICIDE = "Homicide"
    THEFT = "Theft"
    ASSAULT = "Assault"
    CYBERCRIME = "Cybercrime"
    NARCOTICS = "Narcotics"

class Lawyer(Base):
    __tablename__ = "lawyers"

    id = Column(Integer, primary_key=True, index=True)
    full_name = Column(String, index=True)
    email = Column(String, unique=True, index=True)
    specialization = Column(String)
    cases = relationship("Case", back_populates="lead_attorney")

class Case(Base):
    __tablename__ = "cases"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, index=True)
    description = Column(Text)
    status = Column(String, default=CaseStatus.OPEN) # Storing as string for simplicity with Enum
    case_type = Column(String, default=CaseType.FRAUD)
    date_opened = Column(DateTime, default=datetime.datetime.utcnow)
    defendant_name = Column(String)
    
    lead_attorney_id = Column(Integer, ForeignKey("lawyers.id"))
    lead_attorney = relationship("Lawyer", back_populates="cases")
    
    evidence = relationship("Evidence", back_populates="case")
    documents = relationship("Document", back_populates="case")
    videos = relationship("CaseVideo", back_populates="case")

class Evidence(Base):
    __tablename__ = "evidence"

    id = Column(Integer, primary_key=True, index=True)
    description = Column(String)
    evidence_type = Column(String) # Physical, Digital, Testimonial
    collected_date = Column(DateTime, default=datetime.datetime.utcnow)
    location_found = Column(String)
    
    case_id = Column(Integer, ForeignKey("cases.id"))
    case = relationship("Case", back_populates="evidence")

class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String)
    content = Column(Text) # Extracted text or description
    created_date = Column(DateTime, default=datetime.datetime.utcnow)
    
    case_id = Column(Integer, ForeignKey("cases.id"))
    case = relationship("Case", back_populates="documents")

class CaseVideo(Base):
    __tablename__ = "case_videos"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String)
    file_path = Column(String) # Local path to the video file
    processed = Column(Integer, default=0) # 0=No, 1=Yes
    summary = Column(Text, nullable=True)
    created_date = Column(DateTime, default=datetime.datetime.utcnow)
    
    case_id = Column(Integer, ForeignKey("cases.id"))
    case = relationship("Case", back_populates="videos")
