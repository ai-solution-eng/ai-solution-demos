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
@router.get("/api/defaults")
def defaults() -> dict[str, object]:
    # Served under BOTH paths: /defaults for local dev (uvicorn direct), and
    # /api/defaults for gateway deployments — the Istio VirtualService only
    # routes /api/* to the backend, so a same-origin fetch of /defaults from
    # the deployed frontend used to receive index.html (try_files fallback)
    # and fail JSON parsing ("Unexpected token '<'").
    return build_defaults_payload()
