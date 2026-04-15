from typing import Any

from pydantic import BaseModel, Field

from app.config import DEFAULT_TARGET_LANGUAGE


class TranscriptItem(BaseModel):
    original: str = ""
    translation: str = ""
    src: str = ""
    tgt: str = ""
    ts_ms: int | None = None


class FactCheckSource(BaseModel):
    title: str = ""
    url: str = ""
    snippet: str = ""


class FactCheckResult(BaseModel):
    status: str = ""
    motivation: str = ""
    provider: str = ""
    claim_text: str = ""
    checked_at: int | None = None
    sources: list[FactCheckSource] = Field(default_factory=list)


class ExportLlmConfig(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class ExportWhisperConfig(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class ExportRequest(BaseModel):
    transcript: list[TranscriptItem] = Field(default_factory=list)
    llm: ExportLlmConfig | None = None


class RoomExportRequest(BaseModel):
    target_language: str = DEFAULT_TARGET_LANGUAGE
    llm: ExportLlmConfig | None = None
    whisper: ExportWhisperConfig | None = None


class RoomCreateRequest(BaseModel):
    room_id: str | None = None


class RecordingControlRequest(BaseModel):
    client_session_id: str = ""


class RecordingFinalizeRequest(BaseModel):
    recording_session_id: str = ""


class ExportSegment(BaseModel):
    id: int
    start_s: float
    end_s: float
    start_label: str
    end_label: str
    original: str
    english: str = ""
    translations: dict[str, str] = Field(default_factory=dict)


class ExportChunkResult(BaseModel):
    chunk_id: int
    chunk_start_s: float
    chunk_end_s: float
    keep_from_s: float
    keep_to_s: float
    text: str = ""
    language: str = ""
    segments: list[dict[str, Any]] = Field(default_factory=list)


class MeetingPackageResult(BaseModel):
    languages: list[str]
    documents: list[dict[str, str]]
    segments: list[ExportSegment]


class JsonTranslationBatch(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)
