"""Demo settings saved by the UI on the shared app-data volume."""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/app/data/config.json"))
# Defaults from the Helm values (llm.model, kafka.broker, kafka.topic), used until an administrator
# saves a value of their own on System Config.
DEFAULT_ENV = {"llm_model": "LLM_MODEL", "kafka_broker": "KAFKA_BROKER", "kafka_topic": "KAFKA_TOPIC"}


def _read_demo_services() -> dict[str, Any]:
    text = CONFIG_PATH.read_text(encoding="utf-8")
    services = json.loads(text).get("demo_services", {})
    return services if isinstance(services, dict) else {}


def _with_defaults(saved: dict[str, Any]) -> dict[str, Any]:
    merged = dict(saved)
    for key, env in DEFAULT_ENV.items():
        if not merged.get(key) and os.environ.get(env):
            merged[key] = os.environ[env]
    return merged


async def load_demo_config() -> dict[str, Any]:
    """Reads the demo settings that the UI saved to the shared volume, over the chart's defaults.

    All services run in one pod and mount the same volume, so the file is the single source;
    it holds no secrets.

    Returns:
        The ``demo_services`` section, with the model, broker and topic from the environment
        where nothing has been saved; an empty dict when there is neither.
    """
    saved: dict[str, Any] = {}
    try:
        saved = await asyncio.to_thread(_read_demo_services)
    except FileNotFoundError:
        logger.info("demo_config_not_saved_yet", path=str(CONFIG_PATH))
    except (OSError, ValueError, AttributeError) as e:
        logger.error("demo_config_unreadable", path=str(CONFIG_PATH), error=str(e))
    return _with_defaults(saved)
