import json
import os
from typing import Any

import structlog
from pydantic_settings import BaseSettings

logger = structlog.get_logger(__name__)

class Settings(BaseSettings):
    PROJECT_NAME: str = "Meeting Simulator"
    VERSION: str = "1.0.0"

    # Static Configuration
    DB_SCHEMA: str = "meetings"
    CONFIG_FILE_PATH: str = "/app/data/config.json"

    # Dynamic Configuration (from file/env)
    _config_data: dict[str, Any] = {}

    def _load_config_file(self) -> dict[str, Any]:
        path = self.CONFIG_FILE_PATH
        if not os.path.exists(path):
            # Try local path during development
            path = "config.json"
            if not os.path.exists(path):
                # Try old path for migration
                old_path = "db_config.json"
                if os.path.exists(old_path):
                    path = old_path
                else:
                    return {}

        try:
            with open(path) as f:
                return json.load(f)
        except Exception as e:
            logger.error("failed_to_load_config_file", error=str(e))
            return {}

    def save_config(self, data: dict[str, Any]) -> None:
        path = self.CONFIG_FILE_PATH
        if not os.path.join(os.path.dirname(path)):
            # Fallback to local
            path = "config.json"

        try:
            current = self._load_config_file()
            # Filter out None values to avoid overriding existing config or defaults with nulls
            sanitized = {k: v for k, v in data.items() if v is not None}
            current.update(sanitized)
            with open(path, "w") as f:
                json.dump(current, f, indent=4)
            logger.info("config_saved_successfully", path=path)
        except Exception as e:
            logger.error("failed_to_save_config_file", error=str(e))

    def get_config_value(self, key: str, default: Any | None = None) -> Any | None:
        config = self._load_config_file()
        return config.get(key, default)

    @property
    def DATABASE_URL(self) -> str | None:
        config = self._load_config_file()
        uri = config.get("sqlalchemy_uri")
        # Legacy support for db_config.json
        if not uri and "user" in config:
            uri = (
                f"postgresql+asyncpg://{config['user']}:{config['password']}@"
                f"{config['host']}:{config['port']}/{config['db']}"
            )

        # Fallback to env if file doesn't have it
        if not uri:
            uri = os.getenv("DATABASE_URL")
        return uri

    def get_system_settings(self) -> Any:
        from app.core.network import normalize_v1_endpoint
        from app.domain.settings import SystemSettingsBase
        config = self._load_config_file()
        # Only take non-None values to avoid overriding defaults with nulls from update payloads
        active_config = {k: v for k, v in config.items() if v is not None}
        defaults = SystemSettingsBase().model_dump()
        merged = {**defaults, **active_config}
        
        # Normalize endpoints if present
        if merged.get("inference_endpoint"):
            merged["inference_endpoint"] = normalize_v1_endpoint(merged["inference_endpoint"])
        if merged.get("embedding_endpoint"):
            merged["embedding_endpoint"] = normalize_v1_endpoint(merged["embedding_endpoint"])
            
        return SystemSettingsBase(**merged)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()

