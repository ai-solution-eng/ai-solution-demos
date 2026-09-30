"""Security: JWT (analyst identity) + HMAC-signed approval links."""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass

import jwt

from .config import settings

# Demo credentials (documented; video shows a pre-authenticated analyst).
DEMO_USERS = {
    "nick": {"password": "analyst123", "name": "Nick Johnson", "role": "Risk Analyst",
             "department": "Credit Risk", "employee_id": "E102938", "user_id": "nick"},
    "sarah": {"password": "officer123", "name": "Sarah Chen", "role": "Senior Credit Officer",
              "department": "Credit Risk", "employee_id": "E200145", "user_id": "sarah"},
}


@dataclass
class Identity:
    user_id: str
    name: str
    role: str
    employee_id: str


def authenticate(username: str, password: str) -> Identity | None:
    user = DEMO_USERS.get(username)
    if not user or user["password"] != password:
        return None
    return Identity(user["user_id"], user["name"], user["role"], user["employee_id"])


def issue_token(identity: Identity) -> str:
    now = int(time.time())
    payload = {
        "sub": identity.user_id,
        "name": identity.name,
        "role": identity.role,
        "emp_id": identity.employee_id,
        "iat": now,
        "exp": now + settings.jwt_ttl_hours * 3600,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def verify_token(token: str) -> Identity | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        return Identity(payload["sub"], payload["name"], payload["role"], payload["emp_id"])
    except jwt.PyJWTError:
        return None


def _sign(*parts: str) -> str:
    msg = "|".join(parts)
    return hmac.new(settings.hmac_secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def build_approval_link(request_id: str, decision: str, ttl_hours: int, exp_ts: int) -> str:
    """Signed single-use approval link (query-string form)."""
    sig = _sign(request_id, decision, str(ttl_hours), str(exp_ts))
    return (f"{settings.base_url}/api/approvals/{request_id}/decision"
            f"?decision={decision}&ttl_hours={ttl_hours}&exp={exp_ts}&sig={sig}")


def verify_approval_link(request_id: str, decision: str, ttl_hours: int,
                         exp_ts: int, sig: str) -> bool:
    if exp_ts < int(time.time()):
        return False
    expected = _sign(request_id, decision, str(ttl_hours), str(exp_ts))
    return hmac.compare_digest(expected, sig)
