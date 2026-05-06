import os
import subprocess
from datetime import datetime, timezone
import httpx

import structlog
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from app.core.config import settings
from app.core.database import check_db_ready, drop_all_tables, init_db, create_all_tables
from app.core.network import normalize_v1_endpoint
from app.domain.response import APIResponse

logger = structlog.get_logger(__name__)
router = APIRouter()

class DBSetupRequest(BaseModel):
    sqlalchemy_uri: str
    recreate: bool = False

    # LLM Settings
    inference_endpoint: str | None = None
    inference_api_key: str | None = None
    inference_model_name: str | None = None
    inference_ignore_tls: bool | None = None

    # Embedding Settings
    embedding_endpoint: str | None = None
    embedding_api_key: str | None = None
    embedding_model_name: str | None = None
    embedding_ignore_tls: bool | None = None

async def _verify_endpoint(endpoint: str | None, model_name: str | None, api_key: str | None, ignore_tls: bool) -> tuple[bool, str]:
    if not endpoint or not model_name:
        return False, "Configuration missing"
    
    # Normalize endpoint
    base_url = normalize_v1_endpoint(endpoint)
    url = f"{base_url}/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    
    try:
        async with httpx.AsyncClient(verify=not ignore_tls, timeout=5.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                models = []
                if isinstance(data, dict):
                    models = data.get("data") or data.get("models") or []
                elif isinstance(data, list):
                    models = data
                
                # Check if our model_name is in the list
                found_names = []
                for m in models:
                    m_id = m if isinstance(m, str) else m.get("id")
                    found_names.append(m_id)
                    if m_id == model_name:
                        return True, "Verified"
                
                return False, f"Model '{model_name}' not found. Available: {', '.join(found_names[:3])}..."
            
            return False, f"HTTP Error {resp.status_code}"
    except Exception as e:
        logger.warning("verification_failed", error=str(e), url=url)
        return False, f"Connection Failed: {str(e)[:50]}"

def get_config_path():
    return settings.CONFIG_FILE_PATH

@router.get("/status", response_model=APIResponse)
async def system_status() -> APIResponse:
    config = settings._load_config_file()
    last_op = config.get("last_operation")
    
    status_data = {
        "db_configured": False,
        "inference_configured": False,
        "inference_verified": False,
        "inference_status": "Not checked",
        "embedding_configured": False,
        "embedding_verified": False,
        "embedding_status": "Not checked",
        "configured": False,
        "ready": False,
        "reasons": [],
        "last_op": last_op
    }

    # 1. Check Database
    if settings.DATABASE_URL:
        try:
            db_status = await check_db_ready()
            if db_status == "ready":
                status_data["db_configured"] = True
            else:
                status_data["reasons"].append(f"Database: {db_status}")
        except Exception as e:
            logger.error("db_check_failed", error=str(e))
            status_data["reasons"].append("Database check error")
    else:
        status_data["reasons"].append("Database NOT configured")

    # 2. Check Inference (Verified check)
    ss = settings.get_system_settings()
    status_data["inference_configured"] = bool(ss.inference_endpoint and ss.inference_model_name)
    if status_data["inference_configured"]:
        v_ok, v_msg = await _verify_endpoint(
            ss.inference_endpoint, ss.inference_model_name, ss.inference_api_key, ss.inference_ignore_tls
        )
        status_data["inference_verified"] = v_ok
        status_data["inference_status"] = v_msg
        if not v_ok:
            status_data["reasons"].append(f"Inference: {v_msg}")
    else:
        status_data["reasons"].append("Inference NOT configured")
    
    # 3. Check Embedding (Verified check)
    status_data["embedding_configured"] = bool(ss.embedding_endpoint and ss.embedding_model_name)
    if status_data["embedding_configured"]:
        v_ok, v_msg = await _verify_endpoint(
            ss.embedding_endpoint, ss.embedding_model_name, ss.embedding_api_key, ss.embedding_ignore_tls
        )
        status_data["embedding_verified"] = v_ok
        status_data["embedding_status"] = v_msg
        if not v_ok:
            status_data["reasons"].append(f"Embedding: {v_msg}")
    else:
        status_data["reasons"].append("Embedding NOT configured")

    # Final State Logic
    status_data["configured"] = bool(
        settings.DATABASE_URL and 
        status_data["inference_configured"] and 
        status_data["embedding_configured"]
    )

    # Ready means configured AND database has data AND endpoints are verified
    if status_data["configured"] and status_data["db_configured"] and status_data["inference_verified"] and status_data["embedding_verified"]:
        status_data["ready"] = True
    
    return APIResponse(status="success", data=status_data)

@router.post("/setup-db", response_model=APIResponse)
async def setup_database(req: DBSetupRequest, background_tasks: BackgroundTasks) -> APIResponse:
    test_url = req.sqlalchemy_uri

    # Normalise and validate URI
    if test_url.startswith("postgres://"):
        test_url = test_url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif test_url.startswith("postgresql://"):
        test_url = test_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif not test_url.startswith("postgresql+asyncpg://"):
        # Log the failed URI for debugging (security: only log prefix or if it's safe)
        logger.warning("invalid_db_uri_attempt", uri_prefix=test_url[:15] if test_url else "empty")
        raise HTTPException(
            status_code=400,
            detail="Invalid URI. Must be a valid PostgreSQL URI (e.g. postgresql://...)"
        )

    # Save everything to config file for persistence
    config_to_save = req.model_dump(exclude={"recreate"})
    config_to_save["sqlalchemy_uri"] = test_url
    config_to_save["last_operation"] = {"status": "pending", "type": "reset" if req.recreate else "setup"}
    settings.save_config(config_to_save)

    try:
        init_db(test_url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to initialize engine: {str(e)}")

    env = os.environ.copy()
    env["DATABASE_URL"] = test_url

    async def run_setup_sequence(env_dict: dict, recreate: bool):
        op_type = "reset" if recreate else "setup"
        try:
            if recreate:
                logger.info("recreating_database_requested")
                await drop_all_tables()

            logger.info("creating_database_tables")
            await create_all_tables()

            logger.info("running_database_seeds")
            subprocess.run(["python", "-m", "scripts.seed"], check=True, env=env_dict)

            settings.save_config({"last_operation": {"status": "success", "type": op_type, "timestamp": datetime.now(timezone.utc).isoformat()}})
            logger.info("database_setup_sequence_complete")
        except Exception as e:
            logger.error("database_setup_sequence_failed", error=str(e))
            settings.save_config({
                "last_operation": {
                    "status": "error",
                    "type": op_type,
                    "message": str(e),
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
            })

    background_tasks.add_task(run_setup_sequence, env, req.recreate)
    return APIResponse(
        status="success",
        message="Database operation initiated.",
        data={"db_configured": False, "reason": "initializing"}
    )
