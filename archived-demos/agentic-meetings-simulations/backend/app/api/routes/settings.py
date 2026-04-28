from datetime import datetime

import httpx
import structlog
from fastapi import APIRouter

from app.core.config import settings
from app.domain.response import APIResponse
from app.domain.settings import (
    SettingsDiscoveryRequest,
    SystemSettingsBase,
    SystemSettingsUpdate,
)
from app.orchestration.prompts import PROMPT_METADATA

logger = structlog.get_logger(__name__)
router = APIRouter()

@router.get("", response_model=APIResponse)
async def get_settings() -> APIResponse:
    logger.info("fetch_settings_requested")

    # Load from file
    config = settings._load_config_file()

    # Merge with defaults from SystemSettingsBase
    defaults = SystemSettingsBase().model_dump()

    # Extract only relevant fields for SystemSettingsResponse
    current_settings = {**defaults, **config}

    # Ensure id and timestamps are present for SystemSettingsResponse compatibility
    if "id" not in current_settings: current_settings["id"] = 1
    if "created_at" not in current_settings: current_settings["created_at"] = datetime.now()
    if "updated_at" not in current_settings: current_settings["updated_at"] = datetime.now()

    return APIResponse(
        status="success",
        data=current_settings
    )

@router.patch("", response_model=APIResponse)
async def update_settings(
    settings_data: SystemSettingsUpdate
) -> APIResponse:
    logger.info("update_settings_requested")

    update_dict = settings_data.model_dump(exclude_unset=True)
    settings.save_config(update_dict)

    # Reload and return
    config = settings._load_config_file()
    defaults = SystemSettingsBase().model_dump()
    current_settings = {**defaults, **config}

    if "id" not in current_settings: current_settings["id"] = 1
    if "created_at" not in current_settings: current_settings["created_at"] = datetime.now()
    if "updated_at" not in current_settings: current_settings["updated_at"] = datetime.now()

    return APIResponse(
        status="success",
        data=current_settings
    )


from app.core.network import normalize_v1_endpoint

@router.post("/discover", response_model=APIResponse)
async def discover_models(req: SettingsDiscoveryRequest) -> APIResponse:
    logger.info("model_discovery_requested", endpoint=req.endpoint)

    endpoint = normalize_v1_endpoint(req.endpoint)
    url = f"{endpoint}/models"
    headers = {"Authorization": f"Bearer {req.api_key}"} if req.api_key else {}

    try:
        async with httpx.AsyncClient(verify=not req.ignore_tls, timeout=10.0) as client:
            logger.info("discovery_request_attempt", url=url)
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                # Handle various response formats:
                # 1. OpenAI: {"data": [...]}
                # 2. Key-based list: {"models": [...], "results": [...]}
                # 3. Simple list: [...]
                if isinstance(data, dict):
                    models = data.get("data") or data.get("models") # Look for known keys
                    if models is None:
                        # Fallback: if 'data' is a dict but no known key, maybe the dict itself is a model?
                        # Or it's a format we don't know - return it as is but wrap in list if it looks like one.
                        models = data
                else:
                    models = data

                logger.info("discovery_success", count=len(models) if isinstance(models, list) else "unknown")
                return APIResponse(status="success", data={"models": models})
            else:
                error_msg = f"Endpoint returned status {resp.status_code}: {resp.text[:100]}"
                logger.warning("discovery_endpoint_error", status=resp.status_code, text=resp.text[:100])
                return APIResponse(
                    status="error",
                    message=error_msg,
                    data={"models": [], "error": error_msg}
                )
    except httpx.ConnectError:
        error_msg = f"Discovery failed: Could not connect to {url}. Check if the service is running."
        return APIResponse(status="error", message=error_msg, data={"models": [], "error": error_msg})
    except httpx.TimeoutException:
        error_msg = f"Discovery failed: Request to {url} timed out after 10s."
        return APIResponse(status="error", message=error_msg, data={"models": [], "error": error_msg})
    except Exception as e:
        logger.error("discovery_failed", error=str(e))
        error_msg = f"Discovery failed: {str(e)}"
        return APIResponse(status="error", message=error_msg, data={"models": [], "error": error_msg})

@router.get("/prompts/metadata", response_model=APIResponse)
async def get_prompt_metadata() -> APIResponse:
    """Returns metadata for all configurable prompts, including defaults and placeholders."""
    return APIResponse(status="success", data=PROMPT_METADATA)

