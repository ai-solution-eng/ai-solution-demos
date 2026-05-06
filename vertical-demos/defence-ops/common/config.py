"""Shared configuration for all PCAI demo services."""

from functools import lru_cache
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration for all services.
    
    Attributes:
        app_name: Name of the application.
        environment: Current environment (e.g., development, production).
        debug: Whether to enable debug mode.
        default_vlm_model: Default VLM model identifier.
        default_embedding_model: Default embedding model identifier.
        embedding_dimension: Dimension for embeddings.
        api_v1_prefix: Prefix for API version 1.
        cors_origins: List of allowed CORS origins.
        log_level: Logging level (e.g., INFO, DEBUG).
        log_format: Format of the logs (e.g., json, text).
        kserve_namespace: Namespace for KServe resources.
        kserve_api_version: API version for KServe.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False
    )

    # Application
    app_name: str = "PCAI Demo"
    environment: str = "development"
    debug: bool = True

    # Model defaults
    default_vlm_model: str = "qwen2-vl"
    default_embedding_model: str = "text-embedding-3-large"
    embedding_dimension: int = 1536

    # API
    api_v1_prefix: str = "/api/v1"
    cors_origins: List[str] = ["http://localhost:3000", "http://localhost:8000"]

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"  # json or text

    # KServe
    kserve_namespace: str = "default"
    kserve_api_version: str = "serving.kserve.io/v1beta1"


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance.
    
    Returns:
        The settings instance.
    """
    return Settings()

