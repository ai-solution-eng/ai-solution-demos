"""Logging configuration for PCAI demo services."""
import logging
import sys
from typing import Any, List

import structlog


def setup_logging(service_name: str, level: str = "INFO") -> None:
    """Configures structlog for JSON output and integrates with standard logging.
    
    Args:
        service_name: Name of the service.
        level: Logging level.
    """
    
    # Processors for structlog
    processors: List[Any] = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    # If running in a TTY (local dev), use console renderer; otherwise, use JSON
    if sys.stderr.isatty():
        processors.append(structlog.dev.ConsoleRenderer())
    else:
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Configure standard logging to use structlog
    # This ensures that even logs from libraries are handled consistently
    handler = logging.StreamHandler()
    
    # Apply global log level
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(message)s",
        level=log_level,
        handlers=[handler],
    )

    # Silence some noisy loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

