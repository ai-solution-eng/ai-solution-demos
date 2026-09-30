"""SLVD engine HTTP API (FastAPI)."""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import store
from .config import settings
from .memo import build_stub_memo
from .security import (DEMO_USERS, Identity, authenticate, build_approval_link,
                       issue_token, verify_approval_link, verify_token)
from .workflow import resubmit, start_run

app = FastAPI(title="SLVD workflow-engine", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

store.init_db()


# ---------- models ----------

class LoginIn(BaseModel):
    username: str
    password: str


class RunIn(BaseModel):
    case_id: str
    amount_usd: int


# ---------- identity ----------

@app.post("/api/auth/login")
def login(body: LoginIn):
    identity = authenticate(body.username, body.password)
    if identity is None:
        raise HTTPException(401, "invalid credentials")
    return {"token": issue_token(identity),
            "user": {"user_id": identity.user_id, "name": identity.name,
                     "role": identity.role, "employee_id": identity.employee_id,
                     "department": DEMO_USERS[identity.user_id]["department"]}}


def _auth(request: Request) -> Identity:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    identity = verify_token(header[7:].strip())
    if identity is None:
        raise HTTPException(401, "invalid or expired token")
    return identity


# ---------- runs ----------

@app.post("/api/runs")
async def create_run(body: RunIn, request: Request):
    identity = _auth(request)
    if body.amount_usd <= 0:
        raise HTTPException(422, "amount must be positive")
    if body.amount_usd > 100_000_000:
        raise HTTPException(422, "amount out of range")
    try:
        run_id = await start_run(body.case_id, identity, body.amount_usd)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    return {"run_id": run_id, "status": "PENDING"}


@app.get("/api/runs")
def list_runs(request: Request, limit: int = Query(50, le=200)):
    _auth(request)
    return {"runs": store.list_runs(limit)}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, request: Request):
    _auth(request)
    snap = store.snapshot(run_id)
    if snap["run"] is None:
        raise HTTPException(404, "unknown run")
    return snap


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request, last_event_id: int = Query(0, alias="lastEventId")):
    _auth(request)
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "unknown run")

    async def gen():
        replay, q = store.subscribe(run_id, last_event_id)
        try:
            for ev in replay:
                yield f"id: {ev['id']}\ndata: {json.dumps(ev)}\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"id: {ev['id']}\ndata: {json.dumps(ev)}\n\n"
                    if ev.get("type") == "run_terminal":
                        break
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            store.unsubscribe(run_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/runs/{run_id}/memo")
def get_memo(run_id: str, request: Request):
    _auth(request)
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "unknown run")
    path = run["memo_official_path"] or run["memo_draft_path"]
    if not path:
        raise HTTPException(404, "memo not ready")
    import os
    if not os.path.exists(path):
        raise HTTPException(404, "memo file missing")
    with open(path, "r", encoding="utf-8") as f:
        return {"run_id": run_id, "status": run["status"], "path": path,
                "memo_md": f.read()}


@app.get("/api/audit/{run_id}")
def get_audit(run_id: str, request: Request):
    _auth(request)
    if store.get_run(run_id) is None:
        raise HTTPException(404, "unknown run")
    return {"run_id": run_id, "audit": store.get_audit(run_id)}


# ---------- approvals ----------

class DecideIn(BaseModel):
    decision: str
    reason: str = ""


def _pending_approval_items() -> list[dict]:
    """All pending approval requests, enriched with case + policy context.

    Includes the full case/credit/compliance detail the approval email shows, so the
    officer's console can render the same information in a collapsible section.
    """
    from .policy import evaluate
    from .workflow import _load_case, AGENT_NAME, TOOL_HOST
    items = []
    for run in store.list_runs(limit=200):
        if run["status"] != "AWAITING_APPROVAL":
            continue
        req = store.request_for_run(run["run_id"])
        if req is None or req["status"] != "pending":
            continue
        case = _load_case(run["case_id"]) or {}
        credit = case.get("credit", {}) or {}
        compliance = case.get("compliance", {}) or {}
        policy = evaluate(run["amount_usd"],
                          (case.get("compliance") or {}).get("kyc_status", "clear"))
        items.append({
            "request_id": req["request_id"],
            "run_id": run["run_id"],
            "case_id": run["case_id"],
            "client": case.get("client", "—"),
            "client_code": case.get("client_code", "—"),
            "amount_usd": run["amount_usd"],
            "requested_by": run["requested_by"],
            "requested_emp_id": run["requested_emp_id"],
            "created_at": req["created_at"],
            "expires_at": req["expires_at"],
            "policy_reasons": policy.reasons,
            "agent": AGENT_NAME,
            "tool_host": TOOL_HOST,
            "facility": credit.get("facility", "—"),
            "limit_usd": credit.get("limit_usd", 0),
            "utilization_usd": credit.get("utilization_usd", 0),
            "utilization_pct": credit.get("utilization_pct", "—"),
            "risk_rating": credit.get("risk_rating", "—"),
            "covenant_status": credit.get("covenant_status", "—"),
            "kyc_status": compliance.get("kyc_status", "—"),
            "kyc_detail": compliance.get("kyc_detail", "—"),
            "sanctions": compliance.get("sanctions", "—"),
        })
    items.sort(key=lambda x: x["created_at"])
    return items


@app.get("/api/approvals/pending")
def pending_approvals(request: Request):
    """Approval queue for the approver console (Senior Credit Officer)."""
    identity = _auth(request)
    if identity.role != "Senior Credit Officer":
        raise HTTPException(403, "only a Senior Credit Officer can access the approval queue")
    return {"items": _pending_approval_items()}


@app.post("/api/approvals/{request_id}/decide")
def decide_via_portal(request_id: str, body: DecideIn, request: Request):
    """Portal console decision (authenticated officer identity), equivalent to the
    signed email link but recorded with the officer's JWT identity."""
    identity = _auth(request)
    if identity.role != "Senior Credit Officer":
        raise HTTPException(403, "only a Senior Credit Officer can record a decision here")
    if body.decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be 'approve' or 'reject'")
    req = store.get_approval_request(request_id)
    if req is None:
        raise HTTPException(404, "unknown request")
    if req["status"] != "pending":
        return {"request_id": request_id, "status": req["status"],
                "decision": req["decision"], "note": "already recorded (idempotent)"}
    result = store.record_decision(request_id, body.decision, 8,
                                   f"{identity.name} ({identity.employee_id})",
                                   via="portal console", reason=body.reason.strip())
    if result == "recorded":
        # The workflow's _wait_for_decision detects the recorded decision and writes
        # the canonical "Decision recorded (…)" audit entry (with "via portal console"),
        # so we do not add a duplicate line here.
        return {"request_id": request_id, "status": body.decision,
                "run_id": req["run_id"]}
    raise HTTPException(500, "failed to record decision")


@app.get("/api/approvals/{request_id}")
def get_approval(request_id: str, request: Request):
    _auth(request)
    req = store.get_approval_request(request_id)
    if req is None:
        raise HTTPException(404, "unknown request")
    return req


@app.get("/api/approvals/{request_id}/decision")
def approval_decision(request_id: str, request: Request,
                      decision: str = Query(...), ttl_hours: int = Query(8),
                      exp: int = Query(0), sig: str = Query("")):
    """Signed approval link target (the button in the email)."""
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "bad decision")
    if not verify_approval_link(request_id, decision, ttl_hours, exp, sig):
        raise HTTPException(403, "invalid, expired or tampered link")
    req = store.get_approval_request(request_id)
    if req is None:
        raise HTTPException(404, "unknown request")
    result = store.record_decision(request_id, decision, ttl_hours, "Sarah Chen")
    if result == "already_recorded":
        return {"request_id": request_id, "status": req["status"],
                "decision": req["decision"], "note": "already recorded (idempotent)"}
    if result == "recorded":
        run = store.get_run(req["run_id"])
        # decision recorded; workflow resumes from its wait loop (or simulation)
        return {"request_id": request_id, "status": decision,
                "run_id": req["run_id"],
                "run_status": run["status"] if run else None}
    raise HTTPException(500, "failed to record decision")


@app.post("/api/approvals/{request_id}/resubmit")
async def resubmit_run(request_id: str, request: Request):
    _auth(request)
    req = store.get_approval_request(request_id)
    if req is None:
        raise HTTPException(404, "unknown request")
    try:
        new_request_id = await resubmit(req["run_id"])
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"request_id": new_request_id or req["request_id"], "run_id": req["run_id"]}


# ---------- chat (agent) ----------

@app.post("/api/chat")
async def chat(request: Request, message: str = Query(...)):
    _auth(request)
    # SLVD demo assistant: explains this demo (what/how/components/policy/roles/
    # tools/values) and answers about the sample cases; can generate a memo.
    from .assistant import answer
    return {"reply": answer(message)}


# ---------- internal (MCP -> engine webhook) ----------

@app.post("/api/internal/mcp-call")
def internal_mcp_call(call: dict, x_internal_token: str = Query(default="", alias="token")):
    """Webhook from credit-memo-mcp: a governed tool was invoked (step evidence)."""
    # The MCP server calls this with the shared internal token; the engine records it as
    # audit evidence that the agent accessed governed systems.
    from .config import settings as _s
    if _s.mcp_internal_token and x_internal_token != _s.mcp_internal_token:
        raise HTTPException(403, "bad internal token")
    # The engine's workflow already records a richer per-tool audit entry (result data,
    # authorized identity, latency) for each governed call, so we do not add a duplicate
    # "Tool call" line here. The webhook still serves as the MCP -> engine liveness path.
    return {"ok": True}


def _run_for_case(case_id: str) -> str | None:
    run = store.active_run_for_case(case_id)
    return run["run_id"] if run else None


# ---------- health ----------

@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "slvd-engine", "agent_backend": settings.agent_backend,
            "simulation": settings.simulation}


@app.get("/")
def root():
    return {"service": "slvd-engine", "docs": "/docs", "health": "/healthz"}
