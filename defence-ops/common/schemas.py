"""Pydantic schemas for API request/response validation."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


# ==================
# Base Schemas
# ==================

class APIResponse(BaseModel):
    """Standard API response wrapper.
    
    Attributes:
        status: Status of the response (success or error).
        data: Optional data payload.
        message: Optional message.
        meta: Optional metadata.
        timestamp: Time the response was created.
    """

    status: str = Field(..., pattern="^(success|error)$")
    data: Optional[Any] = None
    message: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ==================
# Model Registry Schemas
# ==================

class InferenceServiceInfo(BaseModel):
    """Information about a KServe InferenceService.
    
    Attributes:
        id: Unique identifier for the service.
        name: Name of the service.
        namespace: Kubernetes namespace.
        type: Model type (vlm, llm, embedding, reranker).
        framework: Inference framework (vllm, tensorrt, triton).
        status: Preparation status (ready, pending, failed).
        url: Endpoint URL.
        metadata: Additional metadata.
        created_at: Creation timestamp.
    """

    id: str
    name: str
    namespace: str
    type: str  # vlm, llm, embedding, reranker
    framework: str  # vllm, tensorrt, triton
    status: str  # ready, pending, failed
    url: Optional[str] = None
    metadata: Dict[str, Any] = {}
    created_at: datetime


class ModelListResponse(BaseModel):
    """List of available models.
    
    Attributes:
        models: List of model info.
        total: Total number of models.
    """

    models: List[InferenceServiceInfo]
    total: int


class ModelSelectionRequest(BaseModel):
    """User's model selection with API token.
    
    Attributes:
        model_id: ID of the selected model.
        api_token: User-provided API token.
    """

    model_config = ConfigDict(protected_namespaces=())

    model_id: str
    api_token: str  # User-provided API token for inference


class ModelSelectionResponse(BaseModel):
    """Response after selecting a model.
    
    Attributes:
        model: Info about the selected model.
        token_stored: Whether the token was successfully stored.
        message: Feedback message.
    """

    model: InferenceServiceInfo
    token_stored: bool
    message: str

