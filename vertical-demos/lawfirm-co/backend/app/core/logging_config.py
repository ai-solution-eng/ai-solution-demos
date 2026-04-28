"""
Production-ready logging configuration for Justitia & Associates API
Replace print() statements with proper logging using this configuration
"""

import logging
import sys
from typing import Optional
import json
from datetime import datetime


class JSONFormatter(logging.Formatter):
    """
    Custom JSON formatter for structured logging in production
    """
    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        
        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        # Add extra fields if present
        if hasattr(record, "extra_fields"):
            log_data.update(record.extra_fields)
        
        return json.dumps(log_data)


def setup_logging(
    level: str = "INFO",
    json_format: bool = False,
    log_file: Optional[str] = None
) -> None:
    """
    Configure application logging
    
    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        json_format: Use JSON format for structured logging (recommended for production)
        log_file: Optional file path for file logging
    
    Example:
        # Development
        setup_logging(level="DEBUG", json_format=False)
        
        # Production
        setup_logging(level="INFO", json_format=True, log_file="/var/log/app.log")
    """
    
    # Convert string level to logging constant
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    
    # Create formatter
    if json_format:
        formatter = JSONFormatter()
    else:
        formatter = logging.Formatter(
            fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    
    # Remove existing handlers
    root_logger.handlers.clear()
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    # File handler (optional)
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    
    # Set third-party loggers to WARNING to reduce noise
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance for a module
    
    Args:
        name: Logger name (typically __name__)
    
    Returns:
        Logger instance
    
    Example:
        logger = get_logger(__name__)
        logger.info("Processing request")
        logger.error("Failed to process", exc_info=True)
    """
    return logging.getLogger(name)


# Example usage in your modules:
"""
# At the top of your file (e.g., routers_chat.py)
from app.core.logging_config import get_logger

logger = get_logger(__name__)

# Replace print() statements with:
logger.debug(f"Query: {query[:100]}")
logger.info(f"Processing {len(documents)} documents")
logger.warning(f"Embedding API not configured")
logger.error(f"Error in RAG processing: {e}", exc_info=True)

# With extra context:
logger.info(
    "RAG processing completed",
    extra={"extra_fields": {
        "case_id": case_id,
        "chunks_retrieved": chunks_used,
        "elapsed_seconds": elapsed_time
    }}
)
"""
