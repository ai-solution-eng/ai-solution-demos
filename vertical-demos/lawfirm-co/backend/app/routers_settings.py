from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from . import database, models_settings, schemas_admin
import httpx

router = APIRouter(
    prefix="/settings",
    tags=["settings"],
)

def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("", response_model=List[schemas_admin.SystemSetting])
def get_settings(db: Session = Depends(get_db)):
    settings = db.query(models_settings.SystemSetting).all()
    # Mask secrets
    for s in settings:
        if s.is_secret:
            s.value = "********"
    return settings

@router.post("", response_model=schemas_admin.SystemSetting)
def create_or_update_setting(setting: schemas_admin.SystemSettingCreate, db: Session = Depends(get_db)):
    db_setting = db.query(models_settings.SystemSetting).filter(models_settings.SystemSetting.key == setting.key).first()
    if db_setting:
        db_setting.value = setting.value
        db_setting.is_secret = setting.is_secret
    else:
        db_setting = models_settings.SystemSetting(key=setting.key, value=setting.value, is_secret=setting.is_secret)
        db.add(db_setting)
    
    db.commit()
    db.refresh(db_setting)
    return db_setting

class DetectModelsRequest(schemas_admin.BaseModel):
    endpoint: str
    api_key: str = ""

@router.post("/detect-models")
async def detect_models(request: DetectModelsRequest):
    """Query /v1/models endpoint to detect available models"""
    try:
        # Normalize endpoint
        base_url = request.endpoint.rstrip('/')
        if base_url.endswith('/v1'):
            base_url = base_url.rstrip('/v1')
        
        models_url = f"{base_url}/v1/models"
        
        headers = {
            "Content-Type": "application/json"
        }
        
        # Only add auth header if we have a real API key (not masked)
        if request.api_key and request.api_key != "********":
            headers["Authorization"] = f"Bearer {request.api_key}"
        
        async with httpx.AsyncClient() as client:
            response = await client.get(models_url, headers=headers, timeout=10.0)
            
            if response.status_code == 403:
                return {
                    "success": False,
                    "error": "Authentication failed. Please check your API key. If the key shows as '********', you need to re-enter it.",
                    "models": [],
                    "count": 0
                }
            
            response.raise_for_status()
            data = response.json()
            
            models_list = data.get("data", [])
            model_ids = [m["id"] for m in models_list]
            
            return {
                "success": True,
                "models": model_ids,
                "count": len(model_ids)
            }
    except httpx.HTTPStatusError as e:
        return {
            "success": False,
            "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}",
            "models": [],
            "count": 0
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "models": [],
            "count": 0
        }
