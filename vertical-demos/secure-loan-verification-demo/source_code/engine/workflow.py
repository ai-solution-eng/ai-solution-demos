"""Governed-run orchestrator: the 10-step workflow, policy gate, approval, publish."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import httpx

from . import store
from .agent import draft_memo
from .config import settings
from .mailer import send_approval_request, send_client_notification
from .policy import evaluate, policy_audit_line
from .security import Identity, build_approval_link

log = logging.getLogger("slvd.workflow")

AGENT_NAME = "credit-memo-agent"
TOOL_HOST = "credit-memo-mcp/workflow__submit_credit_memo"
APPROVAL_REASON = ("Agent requires approval to invoke governed MCP method: "
                   "workflow__submit_credit_memo")

# Data tool call order -> (step seq, label, tool, data key)
DATA_STEPS = [
    (3, "CRM profile fetched", "get_crm_profile", "crm"),
    (4, "Credit exposure fetched", "get_credit_exposure", "credit"),
    (5, "Transaction behavior analyzed", "get_transactions", "transactions"),
    (6, "Compliance status checked", "get_compliance_status", "compliance"),
    (7, "Prior memo reviewed", "get_prior_memo", "prior_memo"),
]


def _year() -> int:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).year


def _load_case(case_id: str) -> dict | None:
    path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "mockdata", "cases.json"))
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)["cases"].get(case_id)


def _mcp_tool(tool: str, identity: Identity, case_id: str) -> dict:
    r = httpx.post(
        f"{settings.mcp_base_url}/tools/{tool}",
        json={"case_id": case_id},
        headers={"Authorization": f"Bearer {identity.user_id}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _build_links(request_id: str) -> dict[str, str]:
    exp = int(time.time()) + settings.link_ttl_hours * 3600
    return {
        "approve_24h": build_approval_link(request_id, "approve", 24, exp),
        "reject": build_approval_link(request_id, "reject", 24, exp),
        "dashboard": f"{settings.base_url}/?run=approval:{request_id}",
    }


def _email_approval(request_id: str, case_id: str, emp_id: str, policy) -> bool:
    links = _build_links(request_id)
    return send_approval_request(request_id, case_id, emp_id, AGENT_NAME, TOOL_HOST,
                                 APPROVAL_REASON, links["approve_24h"],
                                 links["reject"], links["dashboard"], policy,
                                 _load_case(case_id) or {})


async def start_run(case_id: str, identity: Identity, amount_usd: int) -> str:
    case = _load_case(case_id)
    if case is None:
        raise ValueError("unknown case")
    if identity.user_id not in case["allowed_users"]:
        raise PermissionError("analyst not authorized for this case")
    run_id = store.create_run(case_id, identity.name, identity.employee_id, amount_usd)
    log.info("run %s started for %s by %s (%s)", run_id, case_id, identity.name,
             identity.employee_id)
    asyncio.get_running_loop().create_task(_run_workflow(run_id, case, identity, amount_usd))
    return run_id


def _system_name(tool: str) -> str:
    return {
        "get_crm_profile": "CRM",
        "get_credit_exposure": "Credit",
        "get_transactions": "Transactions",
        "get_compliance_status": "Compliance",
        "get_prior_memo": "Prior Memos",
    }.get(tool, tool)


TOOL_REASON = {
    "get_crm_profile": "the memo needs the client's relationship context (industry, tenure, ownership)",
    "get_credit_exposure": "the memo needs facility limits, utilization and risk rating",
    "get_transactions": "the memo needs repayment-behavior evidence (delinquencies, trend)",
    "get_compliance_status": "the memo and the approval policy need current KYC/sanctions status",
    "get_prior_memo": "the memo must carry forward prior conditions and open follow-ups",
}


def _data_result_line(key: str, data: dict, case: dict) -> str:
    d = data or {}
    if key == "crm":
        return (f"{case['client']} ({case['client_code']}) — {d.get('industry')}, "
                f"{d.get('relationship_years')}-year relationship, beneficial-ownership update "
                f"{d.get('beneficial_ownership_update')}")
    if key == "credit":
        return (f"{d.get('facility')} — ${d.get('limit_usd', 0):,} limit, "
                f"${d.get('utilization_usd', 0):,} utilized ({d.get('utilization_pct')}%), "
                f"risk rating {d.get('risk_rating')}, {d.get('covenant_status')}")
    if key == "transactions":
        return (f"{d.get('history')}; {d.get('delinquencies')} delinquencies, "
                f"trend {d.get('trend')}")
    if key == "compliance":
        return (f"KYC {d.get('kyc_status')} — {d.get('kyc_detail')}; sanctions "
                f"screening {d.get('sanctions')}")
    if key == "prior_memo":
        return (f"Prior conditions: {', '.join(d.get('conditions', []))}; open follow-up: "
                f"{d.get('open_follow_up')}")
    return ""


async def _run_workflow(run_id: str, case: dict, identity: Identity, amount_usd: int) -> None:
    store.set_run_status(run_id, "RUNNING")
    try:
        store.set_step(run_id, 1, "done")
        store.add_audit(run_id, f"{identity.name} ({identity.role})", "Identity validated",
                        f"{identity.name} ({identity.role}), employee ID {identity.employee_id}",
                        reason="the platform must prove who is requesting the memo before any "
                               "governed data is accessed",
                        meta={"user_id": identity.user_id, "role": identity.role,
                              "employee_id": identity.employee_id,
                              "authorized_for_case": identity.user_id in case["allowed_users"],
                              "mechanism": "JWT (HS256), 8 h TTL"})
        store.set_step(run_id, 2, "done")
        store.add_audit(run_id, "system", "Case resolved",
                        f"{case['case_id']} — {case['client']} ({case['client_code']}), "
                        f"{case['type']}, requested ${amount_usd:,}",
                        reason="the case ID from the portal is resolved to the canonical case "
                               "record the agent will work on",
                        meta={"case_id": case["case_id"], "client": case["client"],
                              "client_code": case["client_code"],
                              "requested_amount_usd": amount_usd})

        data: dict = {}
        systems: list[str] = []
        for seq, label, tool, key in DATA_STEPS:
            store.set_step(run_id, seq, "active")
            try:
                out = await asyncio.to_thread(_mcp_tool, tool, identity, case["case_id"])
            except Exception as e:  # noqa: BLE001
                store.set_step(run_id, seq, "failed")
                await _fail_run(run_id, f"{label} failed: {e}")
                return
            data[key] = out.get("data", out)
            systems.append(_system_name(tool))
            store.set_step(run_id, seq, "done")
            store.add_audit(run_id, "credit-memo-mcp", label,
                            _data_result_line(key, data[key], case),
                            reason="governed MCP tool call — " + TOOL_REASON[tool],
                            meta={"system": _system_name(tool), "tool": tool,
                                  "source": "credit-memo-mcp (governed)",
                                  "authorized_identity": identity.user_id,
                                  "latency_ms": out.get("latency_ms")})
        store.add_audit(run_id, AGENT_NAME, "Systems accessed",
                        "Systems accessed — " + ", ".join(systems),
                        reason="confirms which governed sources fed the LLM draft (provenance "
                               "for the memo)",
                        meta={"systems": systems})

        store.set_step(run_id, 8, "active")
        result = await asyncio.to_thread(draft_memo, case, data, amount_usd, identity)
        if not result.ok:
            await _fail_run(run_id, f"memo validation failed: {result.notes}")
            return
        draft_path = os.path.join(settings.drafts_dir, case["case_id"], "memo.md")
        os.makedirs(os.path.dirname(draft_path), exist_ok=True)
        with open(draft_path, "w", encoding="utf-8") as f:
            f.write(result.memo_md)
        store.set_run_status(run_id, "RUNNING", memo_draft_path=draft_path)
        store.set_step(run_id, 8, "done")
        reprompted = "reprompt" in result.notes
        store.add_audit(run_id, AGENT_NAME, "Draft memo saved",
                        f"Draft memo saved to /drafts/credit/{case['case_id']} — {result.notes}",
                        reason="the LLM agent drafts the decision memo from the five collected "
                               "data payloads, using only values present in that data",
                        meta={"backend": result.notes,
                              "model": settings.llm_model if "llm" in result.notes else None,
                              "inputs": systems,
                              "reprompted": reprompted,
                              "validated": True})

        store.set_step(run_id, 9, "active")
        policy = evaluate(amount_usd, (data.get("compliance") or {}).get("kyc_status", "clear"))
        store.add_audit(run_id, "policy-engine", "Policy evaluation",
                        policy_audit_line(policy),
                        reason="every renewal memo must pass the governance policy before it "
                               "can be published",
                        meta={"approval_required": policy.approval_required,
                              "reasons": policy.reasons,
                              "threshold_usd": settings.approval_threshold_usd,
                              "amount_usd": amount_usd,
                              "kyc_status": (data.get("compliance") or {}).get("kyc_status", "clear")})

        if not policy.approval_required:
            store.set_step(run_id, 9, "done")
            store.set_step(run_id, 10, "done")
            await _complete(run_id, case, amount_usd, "system (auto)",
                            "policy (no approval required)", "policy (auto)")
            return

        request_id = store.create_approval_request(run_id, ttl_hours=24)
        store.set_run_status(run_id, "AWAITING_APPROVAL", request_id=request_id)
        await asyncio.to_thread(_email_approval, request_id, case["case_id"],
                                identity.employee_id, policy)
        store.set_step(run_id, 10, "active")
        store.add_audit(run_id, "policy-engine", "Approval request created",
                        f"Request {request_id} issued to {settings.approver_email} "
                        "(signed links, 24 h expiry)",
                        reason="policy requires a Senior Credit Officer to sign off before "
                               "publication — the run pauses until the decision is recorded",
                        meta={"request_id": request_id, "ttl_hours": 24,
                              "approver": settings.approver_email,
                              "policy_reasons": policy.reasons})
        store.publish(run_id, {"type": "awaiting_approval", "request_id": request_id})

        if settings.simulation:
            await asyncio.sleep(settings.sim_approve_delay_s)
            req = store.get_approval_request(request_id)
            if req and req["status"] == "pending":
                store.record_decision(request_id, "approve", 8, "simulation (auto)",
                                      via="simulation (auto)")
                _apply_decision(run_id, "approve", "simulation (auto)",
                                "Simulation auto-approval (SLVD_SIMULATION=1)",
                                "simulation (auto)")
        else:
            await _wait_for_decision(run_id, request_id)
    except Exception as e:  # noqa: BLE001
        log.exception("workflow %s crashed", run_id)
        await _fail_run(run_id, f"workflow error: {e}")


async def _wait_for_decision(run_id: str, request_id: str) -> None:
    while True:
        await asyncio.sleep(2)
        req = store.get_approval_request(request_id)
        if req is None:
            return
        if req["status"] in ("approved", "rejected"):
            by = req.get("decided_by", "unknown")
            decision = req.get("decision") or ("approve" if req["status"] == "approved" else "reject")
            via = req.get("via", "signed email link")
            reason = (req.get("reason") or "").strip()
            detail = (f"Approved by {by}" if decision == "approve" else f"Rejected by {by}")
            if decision == "reject" and reason:
                detail += f" — reason: {reason}"
            _apply_decision(run_id, decision, by, detail, via)
            return
        if time.time() > float(req["expires_at"]):
            await _fail_run(run_id, "approval request expired before decision")
            return


def _apply_decision(run_id: str, decision: str, decided_by: str, audit_detail: str,
                    via: str) -> None:
    run = store.get_run(run_id)
    if run is None:
        return
    if decision == "reject":
        store.set_step(run_id, 10, "done")
        store.add_audit(run_id, decided_by, "Decision recorded (reject)",
                        f"Rejected by {decided_by} (Senior Credit Officer) via {via}",
                        reason="a Senior Credit Officer declined the renewal — the run is "
                               "closed and the memo is not published",
                        meta={"decision": "reject", "decided_by": decided_by,
                              "via": via, "role": "Senior Credit Officer"})
        store.set_run_status(run_id, "REJECTED", approved_by=decided_by,
                             approval_role="Senior Credit Officer", reject_reason=audit_detail)
        store.publish(run_id, {"type": "run_terminal", "status": "REJECTED"})
        return
    case = _load_case(run["case_id"])
    if case is None:
        return
    store.set_step(run_id, 10, "done")
    store.add_audit(run_id, decided_by, "Decision recorded (approve)",
                    f"Approved by {decided_by} (Senior Credit Officer) via {via}",
                    reason="senior sign-off grants publication of the governed memo",
                    meta={"decision": "approve", "decided_by": decided_by,
                          "via": via, "role": "Senior Credit Officer"})
    _complete_sync(run_id, case, run["amount_usd"], decided_by, "Senior Credit Officer", via)


def _complete_sync(run_id: str, case: dict, amount_usd: int, approver: str, role: str,
                   via: str) -> None:
    run = store.get_run(run_id)
    if run is None:
        return
    draft_path = run["memo_draft_path"]
    official_path = os.path.join(settings.official_dir, case["case_id"], "memo.md")
    os.makedirs(os.path.dirname(official_path), exist_ok=True)
    content = ""
    if draft_path and os.path.exists(draft_path):
        with open(draft_path, "r", encoding="utf-8") as f:
            content = f.read()
    with open(official_path, "w", encoding="utf-8") as f:
        f.write(content)
    store.set_run_status(run_id, "COMPLETED", completed_at=store._now(),
                         memo_official_path=official_path, approved_by=approver,
                         approval_role=role)
    store.add_audit(run_id, "system", "Memo published",
                    f"Memo published to /official/credit/{_year()}/{case['case_id']}",
                    reason="the approved memo is moved from drafts to the official record and "
                           "the run completes",
                    meta={"path": f"/official/credit/{_year()}/{case['case_id']}",
                          "approver": approver, "role": role, "via": via})
    store.publish(run_id, {"type": "run_terminal", "status": "COMPLETED"})
    try:
        send_client_notification(case["case_id"], case["client"], amount_usd, run_id)
    except Exception as e:  # noqa: BLE001
        log.warning("client notification failed: %s", e)


async def _complete(run_id: str, case: dict, amount_usd: int, approver: str,
                    role: str, via: str) -> None:
    await asyncio.to_thread(_complete_sync, run_id, case, amount_usd, approver, role, via)


async def _fail_run(run_id: str, reason: str) -> None:
    store.add_audit(run_id, "system", "Run failed", reason)
    store.set_run_status(run_id, "FAILED", completed_at=store._now(), fail_reason=reason)
    store.publish(run_id, {"type": "run_terminal", "status": "FAILED", "reason": reason})


async def resubmit(run_id: str) -> str:
    run = store.get_run(run_id)
    if run is None:
        raise ValueError("unknown run")
    if run["status"] == "COMPLETED":
        raise ValueError("run already completed")
    if run["status"] == "REJECTED":
        case = _load_case(run["case_id"])
        if case is None:
            raise ValueError("unknown case")
        policy = evaluate(run["amount_usd"], (case.get("compliance") or {}).get("kyc_status", "clear"))
        request_id = store.create_approval_request(run_id, ttl_hours=24)
        store.set_run_status(run_id, "AWAITING_APPROVAL", request_id=request_id)
        await asyncio.to_thread(_email_approval, request_id, run["case_id"],
                                run["requested_emp_id"], policy)
        store.set_step(run_id, 10, "active")
        store.add_audit(run_id, "system", "Re-submitted",
                        f"New approval request {request_id}",
                        reason="the previous decision was rejected — the run re-enters the "
                               "governance gate with a fresh signed request",
                        meta={"request_id": request_id, "policy_reasons": policy.reasons})
        store.publish(run_id, {"type": "awaiting_approval", "request_id": request_id})
        if settings.simulation:
            await asyncio.sleep(settings.sim_approve_delay_s)
            req = store.get_approval_request(request_id)
            if req and req["status"] == "pending":
                store.record_decision(request_id, "approve", 8, "simulation (auto)",
                                      via="simulation (auto)")
                _apply_decision(run_id, "approve", "simulation (auto)",
                                "Simulation auto-approval (SLVD_SIMULATION=1)",
                                "simulation (auto)")
            return request_id
        # Non-simulation: wait for the decision in the BACKGROUND so this call returns
        # the new request id immediately (the approver then clicks the signed link).
        asyncio.get_running_loop().create_task(_wait_for_decision(run_id, request_id))
        return request_id
    req = store.request_for_run(run_id)
    return req["request_id"] if req else ""
