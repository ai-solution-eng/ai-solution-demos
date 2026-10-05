import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.audio.vad import _load_vad
from app.realtime.websocket import router as websocket_router
from app.routes.exports import router as exports_router
from app.routes.health import router as health_router
from app.routes.recordings import router as recordings_router
from app.routes.rooms import router as rooms_router
from app.routes.tts import router as tts_router
from app.services.cleanup import cleanup_expired_persisted_state, periodic_cleanup_loop
from app.services.recovery import recover_persisted_rooms

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        await recover_persisted_rooms()
    except Exception as exc:
        logger.warning("DB not ready during startup; skipping room recovery: %s", exc)
    try:
        await cleanup_expired_persisted_state()
    except Exception as exc:
        logger.warning("DB not ready during startup; skipping cleanup: %s", exc)
    cleanup_task = asyncio.create_task(periodic_cleanup_loop())
    # Preload Silero VAD off the event loop so the first presenter connection
    # doesn't pay the torch.hub load (~2.3MB + JIT warm-up) inside the WS
    # handshake — that stall was visible as a fatal-at-first-join in live
    # logs. Best-effort: a load failure must not block startup (the lazy
    # path in the WS handler still retries on first use).
    try:
        await asyncio.to_thread(_load_vad)
        logger.info("Silero VAD preloaded at startup")
    except Exception as exc:
        logger.warning("Silero VAD preload failed (will retry lazily on first use): %s", exc)
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
app.include_router(tts_router)
app.include_router(websocket_router)
