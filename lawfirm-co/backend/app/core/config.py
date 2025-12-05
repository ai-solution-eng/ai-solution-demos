"""
Security and configuration settings for production deployment
"""

import os
from typing import List
from functools import lru_cache


class Settings:
    """
    Application settings loaded from environment variables
    """
    
    # Application
    APP_NAME: str = os.getenv("APP_NAME", "Justitia & Associates API")
    APP_VERSION: str = os.getenv("APP_VERSION", "1.0.0")
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"
    
    # Database
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql://user:password@db:5432/lawfirm"
    )

    # CORS
    ALLOWED_ORIGINS: List[str] = [
        origin.strip()
        for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
    ]
    
    # Rate Limiting
    RATE_LIMIT_ENABLED: bool = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
    RATE_LIMIT_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))
    
    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_FORMAT: str = os.getenv("LOG_FORMAT", "json")  # "json" or "text"
    LOG_FILE: str = os.getenv("LOG_FILE", "")  # Empty string means no file logging
    
    # LLM Configuration (optional - can also be set via admin panel)
    LLM_ENDPOINT: str = os.getenv("LLM_ENDPOINT", "")
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "")
    
    # Embedding Configuration (optional - can also be set via admin panel)
    EMBEDDING_ENDPOINT: str = os.getenv("EMBEDDING_ENDPOINT", "")
    EMBEDDING_API_KEY: str = os.getenv("EMBEDDING_API_KEY", "")
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "text-embedding-ada-002")
    
    # RAG Configuration
    RAG_CHUNK_SIZE: int = int(os.getenv("RAG_CHUNK_SIZE", "500"))
    RAG_CHUNK_OVERLAP: int = int(os.getenv("RAG_CHUNK_OVERLAP", "50"))
    RAG_TOP_K: int = int(os.getenv("RAG_TOP_K", "5"))
    
    # API Timeouts (seconds)
    LLM_TIMEOUT: int = int(os.getenv("LLM_TIMEOUT", "60"))
    EMBEDDING_TIMEOUT: int = int(os.getenv("EMBEDDING_TIMEOUT", "60"))
    
    @property
    def is_production(self) -> bool:
        """Check if running in production mode"""
        return not self.DEBUG
    
    def validate(self) -> None:
        """Validate critical settings"""
        if self.is_production:
            if "*" in self.ALLOWED_ORIGINS:
                raise ValueError("ALLOWED_ORIGINS cannot contain '*' in production!")
            
            if not self.DATABASE_URL.startswith("postgresql"):
                raise ValueError("DATABASE_URL must use PostgreSQL in production!")
        else:
            # Development mode - none
            pass


@lru_cache()
def get_settings() -> Settings:
    """
    Get cached settings instance
    
    Returns:
        Settings instance
    
    Example:
        from app.core.config import get_settings
        
        settings = get_settings()
        print(settings.APP_NAME)
    """
    settings = Settings()
    settings.validate()
    return settings


# Security Headers Configuration
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": "default-src 'self'",
}


# CORS Configuration
def get_cors_config(settings: Settings) -> dict:
    """
    Get CORS middleware configuration
    
    Args:
        settings: Application settings
    
    Returns:
        CORS configuration dictionary
    """
    return {
        "allow_origins": settings.ALLOWED_ORIGINS,
        "allow_credentials": True,
        "allow_methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        "allow_headers": [
            "Content-Type",
            "Authorization",
            "Accept",
            "Origin",
            "User-Agent",
            "DNT",
            "Cache-Control",
            "X-Requested-With",
        ],
        "expose_headers": ["Content-Length", "X-Request-ID"],
        "max_age": 600,  # Cache preflight requests for 10 minutes
    }
