import logging
import os

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.exceptions import NexusBaseException
from app.domain.response import APIResponse

# Configure structlog
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer()
    ],
    wrapper_class=structlog.make_filtering_bound_logger(20), # INFO level
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=False
)

# Standard logging filter to suppress health-check logs
# (e.g. from the polling SetupGuard)
class HealthCheckFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        # Suppress the frequent /system/status and /health success logs
        return "GET /api/v1/system/status" not in msg and "GET /health" not in msg

logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())

logger = structlog.get_logger(__name__)

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(NexusBaseException)
async def nexus_exception_handler(request: Request, exc: NexusBaseException) -> JSONResponse:
    logger.error("nexus_exception_raised", error_code=exc.code, error_message=exc.message)
    return JSONResponse(
        status_code=400,
        content=APIResponse(
            status="error",
            message=exc.message,
            meta={"code": exc.code}
        ).model_dump()
    )

@app.on_event("startup")
async def startup_event() -> None:
    logger.info("application_startup", app=settings.PROJECT_NAME, version=settings.VERSION)


@app.get("/health")
async def health_check() -> APIResponse:
    return APIResponse(status="success", message="OK")

from app.api.routes import api_router

app.include_router(api_router, prefix="/api/v1")
