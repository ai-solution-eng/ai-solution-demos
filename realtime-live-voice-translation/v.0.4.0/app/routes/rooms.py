from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from app.schemas.api import RoomCreateRequest
from app.state.rooms import get_or_create_room, get_room_or_404, serialize_room_state

router = APIRouter()


@router.post("/api/rooms")
async def create_room(payload: RoomCreateRequest | None = Body(default=None)):
    requested_room_id = None
    if payload is not None:
        requested_room_id = str(payload.room_id or "").strip().lower() or None
    room_id, _ = await get_or_create_room(requested_room_id)
    return JSONResponse({"room_id": room_id})


@router.get("/api/rooms/{room_id}")
async def get_room_state(room_id: str):
    room = await get_room_or_404(room_id)
    payload = serialize_room_state(room)
    payload["segment_count"] = len(room.get("segments") or [])
    return JSONResponse(payload)
