from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import JSONResponse

from app.schemas.api import PresenterTokenRecoveryRequest, RoomCreateRequest
from app.state.rooms import (
    get_or_create_room,
    get_room_or_404,
    room_export_language_codes,
    serialize_room_state,
)
from app.utils.auth import (
    has_persistent_secret,
    presenter_auth_enabled,
    presenter_token_for_room,
    recovery_code_for_room,
    verify_presenter_token,
    verify_recovery_code,
)

router = APIRouter()


@router.post("/api/rooms")
async def create_room(payload: RoomCreateRequest | None = Body(default=None)):
    requested_room_id = None
    if payload is not None:
        requested_room_id = str(payload.room_id or "").strip().lower() or None
    room_id, room = await get_or_create_room(requested_room_id)
    response = {"room_id": room_id}
    if presenter_auth_enabled():
        pristine = not (room.get("segments") or room.get("connections"))
        # Token re-issue policy:
        # - Created/pristine rooms always get their token (normal creation,
        #   and presenter recovery before the room is used).
        # - With a PERSISTENT secret, an active room's token is never
        #   re-issued: knowing the room code is not enough to take it over.
        # - WITHOUT a persistent secret (per-process random key), tokens
        #   cannot have survived a restart — re-issue so a presenter who
        #   reopens the page after a redeploy is not locked out of their own
        #   room. The takeover window is then bounded to the process lifetime,
        #   which is exactly the lifetime of the tokens themselves.
        if pristine or not has_persistent_secret():
            response["presenter_token"] = presenter_token_for_room(room_id)
            # RECOVERY CODE: shown only where the token is already handed
            # out (creation / pristine room / non-persistent-secret mode).
            # NEVER in persistent-secret mode for an active room — the room
            # code alone (attendee knowledge) must not yield the recovery
            # code, or anyone could reclaim the presenter token.
            response["recovery_code"] = recovery_code_for_room(room_id)
    return JSONResponse(response)


@router.get("/api/rooms/{room_id}")
async def get_room_state(room_id: str):
    room = await get_room_or_404(room_id)
    payload = serialize_room_state(room)
    payload["segment_count"] = len(room.get("segments") or [])
    # Languages touched during the meeting (attendee/presenter selections +
    # stored translations) — the presenter's download dialog pre-selects these.
    payload["used_translation_languages"] = list(room_export_language_codes(room))
    return JSONResponse(payload)


@router.post("/api/rooms/{room_id}/presenter-token")
async def recover_presenter_token(
    room_id: str,
    payload: PresenterTokenRecoveryRequest | None = Body(default=None),
):
    """Re-issue the presenter token for a room the presenter is locked out of.

    Proof of ownership — EITHER credential suffices:
    - the room's RECOVERY CODE (shown once at creation), or
    - the presenter TOKEN itself (it IS the credential; knowing it is proof).

    Knowing the room code alone is not enough (attendees know that too).
    """
    normalized, room = await get_or_create_room(room_id)
    if not presenter_auth_enabled():
        return JSONResponse({"room_id": normalized, "presenter_token": presenter_token_for_room(normalized)})

    supplied_recovery = ""
    supplied_token = ""
    if payload is not None:
        supplied_recovery = str(payload.recovery_code or "").strip()
        supplied_token = str(payload.presenter_token or "").strip()

    token_valid = bool(supplied_token) and verify_presenter_token(normalized, supplied_token)
    recovery_valid = bool(supplied_recovery) and verify_recovery_code(normalized, supplied_recovery)
    if not (token_valid or recovery_valid):
        raise HTTPException(status_code=403, detail="Invalid recovery code or presenter token for this room.")

    return JSONResponse({
        "room_id": normalized,
        "presenter_token": presenter_token_for_room(normalized),
    })
