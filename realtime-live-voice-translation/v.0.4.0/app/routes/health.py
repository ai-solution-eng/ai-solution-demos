from fastapi import APIRouter

from app.config import build_defaults_payload

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
def ready() -> dict[str, str]:
    return {"status": "ready"}


@router.get("/defaults")
def defaults() -> dict[str, object]:
    return build_defaults_payload()
