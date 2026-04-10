import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.realtime.websocket import router as websocket_router
from app.routes.exports import router as exports_router
from app.routes.health import router as health_router
from app.routes.recordings import router as recordings_router
from app.routes.rooms import router as rooms_router
from app.services.cleanup import cleanup_expired_persisted_state, periodic_cleanup_loop
from app.services.recovery import recover_persisted_rooms


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await recover_persisted_rooms()
    await cleanup_expired_persisted_state()
    cleanup_task = asyncio.create_task(periodic_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(rooms_router)
app.include_router(recordings_router)
app.include_router(exports_router)
app.include_router(websocket_router)
