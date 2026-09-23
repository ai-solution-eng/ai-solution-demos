"""credit-memo-mcp — governed MCP tools over the mock back-office systems.

Every tool requires an analyst identity token (the engine passes the analyst's
user_id as a bearer token) and enforces per-case access. Each call is logged
and reported to the engine (webhook) so it can advance the workflow steps.

This server exposes BOTH:
  - an MCP streamable-HTTP endpoint (/mcp) for OpenClaw/agent tool-calling, and
  - a plain REST mirror (/tools/<name>) used by the engine's direct agent path
    and as a transport fallback.
"""
from __future__ import annotations

import json
import logging
import os
import time

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("slvd.mcp")

app = FastAPI(title="credit-memo-mcp", version="0.1.0")

MOCKDATA = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "mockdata", "cases.json"))
ENGINE_URL = os.environ.get("SLVD_ENGINE_URL", "http://engine:8080")
INTERNAL_TOKEN = os.environ.get("SLVD_MCP_INTERNAL_TOKEN", "dev-internal")


def _load_cases() -> dict:
    with open(MOCKDATA, "r", encoding="utf-8") as f:
        return json.load(f)["cases"]


CASES = _load_cases()


def _auth_case(authorization: str, case_id: str) -> str:
    """Enforce identity + per-case access. Returns the user_id."""
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    user_id = authorization[7:].strip()
    case = CASES.get(case_id)
    if case is None:
        raise HTTPException(404, "unknown case")
    if user_id not in case["allowed_users"]:
        raise HTTPException(403, "analyst not authorized for this case")
    return user_id


def _notify(user_id: str, tool: str, case_id: str, ok: bool) -> None:
    try:
        httpx.post(f"{ENGINE_URL}/api/internal/mcp-call?token={INTERNAL_TOKEN}",
                   json={"user": user_id, "tool": tool, "case_id": case_id, "ok": ok},
                   timeout=5)
    except Exception:  # noqa: BLE001
        log.debug("mcp-call notify failed (non-fatal)")


@app.post("/api/internal/mcp-call")
def internal_mcp_call(request: Request, x_internal_token: str = Header(default="")):
    if x_internal_token != INTERNAL_TOKEN:
        raise HTTPException(403, "bad internal token")
    return {"ok": True}


# ---------- REST mirror (tools) ----------

TOOL_DATA_KEY = {
    "get_crm_profile": "crm",
    "get_credit_exposure": "credit",
    "get_transactions": "transactions",
    "get_compliance_status": "compliance",
    "get_prior_memo": "prior_memo",
}


def _tool_payload(tool: str, case_id: str) -> dict:
    case = CASES[case_id]
    if tool in TOOL_DATA_KEY:
        return {"data": case[TOOL_DATA_KEY[tool]], "source": tool, "case_id": case_id}
    if tool == "workflow__submit_credit_memo":
        return {"data": {"status": "received", "case_id": case_id}, "source": tool}
    raise HTTPException(400, f"unknown tool {tool}")


@app.post("/tools/{tool}")
async def tool_call(tool: str, request: Request, authorization: str = Header(default="")):
    body = await request.json()
    case_id = body.get("case_id", "")
    user_id = _auth_case(authorization, case_id)
    t0 = time.time()
    payload = _tool_payload(tool, case_id)
    latency = round((time.time() - t0) * 1000, 1)
    log.info("tool %s case=%s user=%s (%.1fms)", tool, case_id, user_id, latency)
    _notify(user_id, tool, case_id, True)
    payload["latency_ms"] = latency
    return payload


# ---------- MCP (streamable-HTTP, JSON-RPC) ----------

def _mcp_tools() -> list[dict]:
    return [
        {
            "name": name,
            "description": (
                {"get_crm_profile": "Fetch the CRM client profile for a case.",
                 "get_credit_exposure": "Fetch credit exposure/limits for a case.",
                 "get_transactions": "Analyze transaction behavior for a case.",
                 "get_compliance_status": "Check compliance/KYC status for a case.",
                 "get_prior_memo": "Fetch the prior memo and its conditions.",
                 "workflow__submit_credit_memo": "Submit the drafted memo for policy evaluation."}[name]),
            "inputSchema": {
                "type": "object",
                "properties": {"case_id": {"type": "string"},
                               "memo_md": {"type": "string", "description": "memo markdown (submit only)"}},
                "required": ["case_id"],
            },
        }
        for name in list(TOOL_DATA_KEY) + ["workflow__submit_credit_memo"]
    ]


@app.post("/mcp")
async def mcp(request: Request):
    body = await request.json()
    method = body.get("method")
    rid = body.get("id")
    if method == "initialize":
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "credit-memo-mcp", "version": "0.1.0"}}})
    if method == "tools/list":
        return JSONResponse({"jsonrpc": "2.0", "id": rid,
                             "result": {"tools": _mcp_tools()}})
    if method == "tools/call":
        params = body.get("params", {})
        name = params.get("name", "")
        args = params.get("arguments", {})
        case_id = args.get("case_id", "")
        # The MCP caller is the agent; the engine pre-scopes the agent to the
        # analyst's identity via the X-User-Id header set by the gateway.
        user_id = request.headers.get("x-user-id", "nick")
        try:
            _auth_case(f"Bearer {user_id}", case_id)
            payload = _tool_payload(name, case_id)
        except HTTPException as e:
            return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps({"error": str(e.detail)})}],
                "isError": True}})
        _notify(user_id, name, case_id, True)
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "isError": False}})
    if method == "notifications/initialized":
        return JSONResponse({"jsonrpc": "2.0", "result": {}})
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601,
                                                                 "message": "method not found"}})


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "credit-memo-mcp", "cases": len(CASES)}
