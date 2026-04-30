from fastapi import APIRouter

from app.api.routes import documents, meetings, roles, settings, system, templates

api_router = APIRouter()
api_router.include_router(settings.router, prefix="/settings", tags=["settings"])
api_router.include_router(roles.router, prefix="/roles", tags=["roles"])
api_router.include_router(templates.router, prefix="/templates", tags=["templates"])
api_router.include_router(documents.router, prefix="/documents", tags=["documents"])
api_router.include_router(meetings.router, prefix="/meetings", tags=["meetings"])
api_router.include_router(system.router, prefix="/system", tags=["system"])


