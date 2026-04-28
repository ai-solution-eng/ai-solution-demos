"""
Shared utility functions for LLM and embedding configuration
"""
from sqlalchemy.orm import Session
from . import models_settings


def get_llm_config(db: Session):
    """
    Retrieve LLM configuration from settings
    
    Args:
        db: Database session
        
    Returns:
        Tuple of (endpoint, api_key) or (None, None) if not configured
    """
    endpoint = db.query(models_settings.SystemSetting).filter(
        models_settings.SystemSetting.key == "llm_endpoint"
    ).first()
    api_key = db.query(models_settings.SystemSetting).filter(
        models_settings.SystemSetting.key == "llm_api_key"
    ).first()
    
    if not endpoint or not api_key:
        return None, None
    
    return endpoint.value, api_key.value


def get_embedding_config(db: Session):
    """
    Retrieve embedding API configuration from settings
    
    Args:
        db: Database session
        
    Returns:
        Tuple of (endpoint, api_key, model_name) or (None, None, None) if not configured
    """
    endpoint = db.query(models_settings.SystemSetting).filter(
        models_settings.SystemSetting.key == "embedding_endpoint"
    ).first()
    api_key = db.query(models_settings.SystemSetting).filter(
        models_settings.SystemSetting.key == "embedding_api_key"
    ).first()
    model = db.query(models_settings.SystemSetting).filter(
        models_settings.SystemSetting.key == "embedding_model"
    ).first()
    
    if not endpoint or not api_key:
        return None, None, None
    
    # Model is optional, default to text-embedding-ada-002
    model_name = model.value if model else "text-embedding-ada-002"
    
    return endpoint.value, api_key.value, model_name
