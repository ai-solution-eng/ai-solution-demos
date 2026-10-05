from typing import Any

from pydantic import BaseModel, Field

from app.config import DEFAULT_TARGET_LANGUAGE


class TranscriptItem(BaseModel):
    original: str = ""
    translation: str = ""
    src: str = ""
    tgt: str = ""
    ts_ms: int | None = None


class ExportLlmConfig(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class ExportAsrConfig(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class ExportRequest(BaseModel):
    transcript: list[TranscriptItem] = Field(default_factory=list)
    llm: ExportLlmConfig | None = None


class RoomExportRequest(BaseModel):
    target_language: str = DEFAULT_TARGET_LANGUAGE
    # Languages the presenter explicitly wants in the package (reports,
    # minutes, transcript translations). Empty/missing = every language
    # touched during the meeting (previous behavior).
    export_languages: list[str] = []
    llm: ExportLlmConfig | None = None
    asr: ExportAsrConfig | None = None


class RoomCreateRequest(BaseModel):
    room_id: str | None = None
    # Presenter RECOVERY CODE: supplied to
    # POST /api/rooms/{room_id}/presenter-token to reclaim the presenter
    # token for a room whose cookie was lost.
    recovery_code: str | None = None


class PresenterTokenRecoveryRequest(BaseModel):
    """Proof-of-ownership for POST /api/rooms/{room_id}/presenter-token.

    EITHER credential suffices: the recovery code (shown once at creation)
    or the presenter token itself (saved/copied earlier — it IS the
    credential, so knowing it is proof enough).
    """
    recovery_code: str | None = None
    presenter_token: str | None = None


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
