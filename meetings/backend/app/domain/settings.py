from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SystemSettingsBase(BaseModel):
    debug: bool = False
    retrieval_limits_per_agent: int = 2
    max_evidence_per_message: int = 5
    default_turn_limit: int = 50
    cleanup_rules: str = "terminate_keeps_history"

    inference_endpoint: str | None = None
    inference_api_key: str | None = None
    inference_model_name: str | None = None
    inference_temperature: float = 0.7
    inference_ignore_tls: bool = False

    embedding_endpoint: str | None = None
    embedding_api_key: str | None = None
    embedding_model_name: str | None = None
    embedding_ignore_tls: bool = False

    supervisor_prompt: str | None = None
    agent_prompt: str | None = None

class SystemSettingsCreate(SystemSettingsBase):
    pass

class SystemSettingsUpdate(BaseModel):
    debug: bool | None = None
    retrieval_limits_per_agent: int | None = None
    max_evidence_per_message: int | None = None
    default_turn_limit: int | None = None
    cleanup_rules: str | None = None

    inference_endpoint: str | None = None
    inference_api_key: str | None = None
    inference_model_name: str | None = None
    inference_temperature: float | None = None
    inference_ignore_tls: bool | None = None

    embedding_endpoint: str | None = None
    embedding_api_key: str | None = None
    embedding_model_name: str | None = None
    embedding_ignore_tls: bool | None = None

    supervisor_prompt: str | None = None
    agent_prompt: str | None = None

class SystemSettingsResponse(SystemSettingsBase):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class SettingsDiscoveryRequest(BaseModel):
    endpoint: str
    api_key: str | None = None
    ignore_tls: bool = False

class SettingsDiscoveryResponse(BaseModel):
    models: list[dict]
    error: str | None = None
