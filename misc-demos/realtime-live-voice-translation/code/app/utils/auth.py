"""Presenter authentication tokens.

Attendee joins are intentionally left unauthenticated so anyone with the room
code can follow the live translation. Only the presenter control plane
(WebSocket joins with role="presenter") is protected.

The token is a deterministic HMAC-SHA256 of the room id, keyed by a server
secret (PRESENTER_AUTH_SECRET, env). Deterministic derivation means:

- the backend hands the token to the presenter when the room is created
  (POST /api/rooms returns presenter_token);
- the presenter stores it in a cookie and replays it on WebSocket join;
- tokens stay valid across backend restarts and multi-replica deployments
  without any database migration;
- rotating the secret invalidates all previously issued tokens at once.
"""

import hashlib
import hmac
import secrets

from app.config import PRESENTER_AUTH_ENABLED, PRESENTER_AUTH_SECRET

# When the secret is unset, a per-process random key is generated at import
# time; tokens then remain self-consistent within the process but change on
# restart. has_persistent_secret() lets callers soften policy in that case
# (routes/rooms.py re-issues tokens for rooms whose tokens could not have
# survived the restart anyway) so presenters are not locked out of their own
# room after every redeploy. Set PRESENTER_AUTH_SECRET to get strict,
# restart-stable tokens.
_SECRET = (PRESENTER_AUTH_SECRET or "").strip() or secrets.token_hex(32)


def has_persistent_secret() -> bool:
    return bool((PRESENTER_AUTH_SECRET or "").strip())


def presenter_auth_enabled() -> bool:
    return PRESENTER_AUTH_ENABLED


def presenter_token_for_room(room_id: str) -> str:
    """Deterministic presenter token for a room id."""
    normalized = (room_id or "").strip().lower()
    digest = hmac.new(
        _SECRET.encode("utf-8"),
        f"presenter:{normalized}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:40]


def recovery_code_for_room(room_id: str) -> str:
    """Human-friendly recovery code: proves the presenter owns the room.

    Second deterministic derivation (different domain string from the token)
    formatted in groups for transcription from the screen. Shown ONCE at room
    creation; the presenter saves it. Losing the browser cookie is recoverable
    with room code + recovery code (POST /api/rooms/{id}/presenter-token).
    """
    normalized = (room_id or "").strip().lower()
    digest = hmac.new(
        _SECRET.encode("utf-8"),
        f"recovery:{normalized}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    # 12 hex chars in 3 groups of 4 — short enough to jot down, long enough
    # to be impractical to guess (16^12 ≈ 2.8e14) alongside a 64-char room id.
    raw = digest[:12].upper()
    return "-".join(raw[i:i + 4] for i in range(0, 12, 4))


def verify_recovery_code(room_id: str, code: str | None) -> bool:
    if not PRESENTER_AUTH_ENABLED:
        return True
    provided = (code or "").strip().upper().replace(" ", "").replace("-", "")
    if not provided:
        return False
    expected = recovery_code_for_room(room_id).replace("-", "")
    return hmac.compare_digest(provided, expected)


def verify_presenter_token(room_id: str, token: str | None) -> bool:
    if not PRESENTER_AUTH_ENABLED:
        return True
    provided = (token or "").strip().lower()
    if not provided:
        return False
    expected = presenter_token_for_room(room_id)
    return hmac.compare_digest(provided, expected)
