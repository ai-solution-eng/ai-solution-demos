"""LLM Service for handling chat and model discovery."""

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import httpx
import structlog
from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from common.config import get_settings
from common.logging_config import setup_logging
from common.schemas import APIResponse, InferenceServiceInfo, ModelListResponse
from model_registry import ModelRegistry

# Configure structured logging
setup_logging("llm-service")
logger = structlog.get_logger(__name__)
settings = get_settings()


# Suppress successful uvicorn access logs for health checks
class HealthCheckFilter(logging.Filter):
    """Filter to suppress health check logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Filters out logs containing health check paths with 200 status.
        
        Args:
            record: The log record.
            
        Returns:
            True if the log should be kept, False otherwise.
        """
        msg = record.getMessage()
        return not ("/health" in msg and " 200" in msg)


logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())
logging.getLogger("uvicorn").addFilter(HealthCheckFilter())


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager for the FastAPI application.
    
    Args:
        app: The FastAPI application instance.
    """
    for logger_name in ["uvicorn", "uvicorn.access", "uvicorn.error"]:
        for f in logging.getLogger(logger_name).filters:
            if isinstance(f, HealthCheckFilter):
                break
        else:
            logging.getLogger(logger_name).addFilter(HealthCheckFilter())
    yield


app = FastAPI(title="LLM Service", lifespan=lifespan)
registry = ModelRegistry()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    """Request schema for the chat endpoint."""

    system_prompt: Optional[str] = "You are a helpful AI assistant."
    message: str
    image_b64: Optional[str] = None
    images: Optional[List[str]] = None
    video_b64: Optional[str] = None
    kafka_context: Optional[List[Dict[str, Any]]] = None
    model: Optional[str] = None
    temperature: Optional[float] = 0.7
    enable_thinking: Optional[bool] = False


ADMIN_URL = os.environ.get("ADMIN_SERVICE_URL", "http://app-ui:3000")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/data/config.json")


async def get_llm_config() -> Dict[str, Any]:
    """Fetches LLM configuration from the local file or admin-service.
    
    Returns:
        A dictionary containing the LLM configuration.
    """
    # 1. Try local file (PVC)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
                if "demo_services" in config:
                    logger.info("config_loaded_from_file", path=CONFIG_PATH)
                    return config["demo_services"]
        except Exception as e:
            logger.warning("config_file_read_failed", path=CONFIG_PATH, error=str(e))

    # 2. Try HTTP fetch
    urls = [ADMIN_URL, "http://admin-service:8000", "http://app-ui:3000"]
    last_error = None

    for url in urls:
        try:
            logger.debug("fetching_config", url=url)
            async with httpx.AsyncClient(verify=False) as client:
                resp = await client.get(f"{url}/api/v1/admin/demo-config", timeout=2.0)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "success":
                        logger.info("config_loaded_from_http", url=url)
                        return data.get("data", {})
                    else:
                        logger.warning("config_response_error", url=url, status=data.get("status"))
                else:
                    logger.warning("config_http_error", url=url, status_code=resp.status_code)
        except Exception as e:
            last_error = str(e)
            logger.debug("config_fetch_failed", url=url, error=str(e))

    logger.error("all_config_fetches_failed", last_error=last_error)
    return {}


@app.get("/health")
async def health_check():
    """Health check endpoint.
    
    Returns:
        A dictionary indicating the health status.
    """
    return {"status": "healthy"}


# --- Model Registry Endpoints ---


@app.get("/api/v1/models", response_model=ModelListResponse)
async def list_models(model_type: Optional[str] = None):
    """Lists all available inference models or filters by type.
    
    Args:
        model_type: Optional type filter (e.g., vlm, llm).
        
    Returns:
        A list of models and total count.
    """
    if model_type:
        models = registry.get_models_by_type(model_type)
    else:
        models = registry.list_inference_services()

    return ModelListResponse(models=models, total=len(models))


@app.get("/api/v1/llm/discovered-endpoints")
async def list_inference_service_endpoints():
    """Lists endpoints of all ready InferenceServices.
    
    Returns:
        A dictionary with status and list of endpoints.
    """
    isvcs = registry.list_inference_services()
    endpoints = []
    for svc in isvcs:
        if svc.status == "ready" and svc.url:
            endpoints.append({"name": svc.name, "url": svc.url, "type": svc.type})
    return {"status": "success", "endpoints": endpoints}


@app.post("/api/v1/llm/discover-models")
async def discover_external_models(
    endpoint: str = Body(..., embed=True), api_key: Optional[str] = Body(None, embed=True)
):
    """Validates the endpoint by checking if ENDPOINT/v1/models responds.
    
    Args:
        endpoint: The base URL of the inference endpoint.
        api_key: Optional API key for authorization.
        
    Returns:
        A dictionary with status and discovered models.
    """
    # Canonical check: ENDPOINT + /v1/models
    models_url = f"{endpoint.rstrip('/')}/v1/models"
    logger.info("discovering_models", url=models_url)

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            resp = await client.get(models_url, headers=headers)
            resp.raise_for_status()
            data = resp.json()

            # Standard OpenAI response format has a 'data' field containing models
            models = []
            if isinstance(data, dict) and "data" in data:
                models = [m.get("id") for m in data["data"] if isinstance(m, dict) and m.get("id")]
            elif isinstance(data, dict) and "models" in data:
                models = [m.get("name", m.get("id")) for m in data["models"] if isinstance(m, dict)]
            elif isinstance(data, list):
                models = data

            logger.info("discovery_success", url=models_url, count=len(models))
            return {"status": "success", "models": models}
    except Exception as e:
        logger.error("model_discovery_failed", url=models_url, error=str(e))
        error_msg = str(e)
        if "timeout" in error_msg.lower():
            error_msg = "Request timed out. The server might be slow or unreachable."
        elif "status 404" in error_msg.lower():
            error_msg = (
                f"Endpoint {models_url} returned 404. "
                "Make sure this is a valid OpenAI-compatible provider."
            )
        elif "status 401" in error_msg.lower():
            error_msg = "Unauthorized. Please check your API key if required by this provider."

        return {
            "status": "error",
            "message": f"Discovery failed on {models_url}: {error_msg}",
            "models": [],
        }


@app.get("/api/v1/models/{model_name}", response_model=InferenceServiceInfo)
async def get_model(model_name: str):
    """Gets detailed information about a specific model.
    
    Args:
        model_name: The name of the model.
        
    Returns:
        Model information.
        
    Raises:
        HTTPException: If model is not found.
    """
    model = registry.get_model_by_name(model_name)
    if not model:
        raise HTTPException(status_code=404, detail=f"Model {model_name} not found")

    return model


# --- Chat Endpoint ---


@app.post("/api/v1/llm/chat")
async def chat(req: ChatRequest):
    """Generic endpoint to send a prompt (and optional base64 images) to a remote LLM.
    
    Args:
        req: The chat request object.
        
    Returns:
        The LLM response.
        
    Raises:
        HTTPException: If LLM is not configured or request fails.
    """
    logger.info("llm_chat_request_received", message_len=len(req.message))

    config = await get_llm_config()

    raw_endpoint = config.get("llm_endpoint")
    api_key = config.get("llm_api_key")

    if not raw_endpoint:
        logger.error("llm_not_configured", config_keys=list(config.keys()))
        raise HTTPException(
            status_code=400,
            detail="LLM Endpoint is not configured. Please set it in Advanced Demo Settings.",
        )

    # Normalize endpoint for chat: if it's just base, append /v1/chat/completions
    endpoint = raw_endpoint.rstrip("/")
    if "/chat/completions" not in endpoint:
        if "/v1" not in endpoint:
            endpoint = f"{endpoint}/v1/chat/completions"
        else:
            endpoint = f"{endpoint}/chat/completions"

    logger.info("llm_using_endpoint", original=raw_endpoint, final=endpoint)

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # Constructing an OpenAI compatible message structure
    user_content = []

    # Process Kafka context if present
    full_message = req.message
    if req.kafka_context:
        logs_text = "\n".join(
            [
                f"[{l.get('severity', 'INFO')}] {l.get('message', '')}"
                for l in req.kafka_context[-20:]
            ]
        )
        full_message = f"QUERY: {req.message}\n\nLATEST TACTICAL DATA (KAFKA):\n{logs_text}"

    user_content.append({"type": "text", "text": full_message})

    # Individual legacy image
    if req.image_b64:
        user_content.append({"type": "image_url", "image_url": {"url": req.image_b64}})

    # Multiple new images list
    if req.images:
        for img in req.images[:5]:  # Safety limit to 5 images as requested
            user_content.append({"type": "image_url", "image_url": {"url": img}})

    # Video context handling
    provider = config.get("llm_provider", "ollama")
    if req.video_b64:
        if provider == "openai":
            user_content.append({"type": "video_url", "video_url": {"url": req.video_b64}})
            logger.info("submitting_video_url", provider=provider)
        else:
            # For Ollama, the frontend should have already converted video to images.
            # We log this just in case of inconsistent state.
            logger.info(
                "skipping_video_url_for_ollama", msg="Ollama provider does not support video_url"
            )

    messages = []
    if req.system_prompt:
        messages.append({"role": "system", "content": req.system_prompt})

    # Decide if we send content as a string (more compatible) or list (multi-modal)
    final_content = user_content
    if len(user_content) == 1 and user_content[0]["type"] == "text":
        final_content = user_content[0]["text"]

    if req.enable_thinking:
        # Provide clear instructions to the model
        # to ensure it actually uses the <think> tags in its output.
        thinking_prompt = (
            "\n\n[INSTRUCTION]: Please provide your internal reasoning process first, "
            "wrapped in <think> and </think> tags, before providing your final response."
        )
        if isinstance(final_content, str):
            final_content += thinking_prompt
        elif isinstance(final_content, list):
            # Append to the first text block or add a new one
            for item in final_content:
                if item.get("type") == "text":
                    item["text"] += thinking_prompt
                    break
            else:
                final_content.append({"type": "text", "text": thinking_prompt})

    messages.append({"role": "user", "content": final_content})

    # Determine which model to use
    selected_model = req.model
    if not selected_model or selected_model == "default":
        selected_model = config.get("llm_model")

    if not selected_model:
        # Final fallback if nothing is configured
        selected_model = "default"

    payload = {"model": selected_model, "messages": messages, "temperature": req.temperature}

    logger.info("llm_request_started", endpoint=endpoint)
    try:
        async with httpx.AsyncClient(timeout=600.0, verify=False) as client:
            resp = await client.post(endpoint, headers=headers, json=payload)
            resp.raise_for_status()
            result = resp.json()

            # Simple fallback parser for common LLM API formats (OpenAI format)
            reply_text = ""
            reasoning = ""
            
            if "choices" in result and len(result["choices"]) > 0:
                choice = result["choices"][0]
                if "message" in choice:
                    msg = choice["message"]
                    reply_text = msg.get("content", "")
                    # Capture reasoning_content (common in DeepSeek and some OpenAI-compatible providers)
                    reasoning = msg.get("reasoning_content", "")
                elif "text" in choice:
                    reply_text = choice["text"]
            else:
                reply_text = str(result)

            # If the model provided explicit reasoning content but not in the main text,
            # prepend it wrapped in <think> tags.
            if reasoning and "<think>" not in reply_text:
                reply_text = f"<think>\n{reasoning}\n</think>\n\n{reply_text}"

            return {"status": "success", "data": {"reply": reply_text, "raw": result}}

    except httpx.TimeoutException as e:
        logger.error("llm_timeout_error", error=str(e), endpoint=endpoint)
        raise HTTPException(
            status_code=504,
            detail=(
                f"LLM Request Timeout: {str(e)}. "
                "The model may be slow to respond or still loading. "
                "Please try again in few moments."
            ),
        )
    except httpx.HTTPStatusError as e:
        logger.error("llm_http_error", status_code=e.response.status_code, response=e.response.text)
        raise HTTPException(
            status_code=e.response.status_code, detail=f"LLM Error: {e.response.text}"
        )
    except Exception as e:
        logger.error("llm_request_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to communicate with LLM: {str(e)}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        reload=True if os.environ.get("ENV") == "development" else False,
    )

