"""Memo drafting by the agent (the generative AI step).

Design (robust + faithful to the video's "agent pulls data and drafts the memo"):
  - The ENGINE drives the governed MCP data calls (CRM/credit/transactions/compliance/
    prior-memo + submit) — that is the governance + data path (steps 3-7, 9).
  - The AGENT (LLM or deterministic stub) DRAFTS the memo from the collected data —
    that is the visible AI step (step 8).

Backends:
  - stub        deterministic memo from fixtures (simulation / no-LLM).
  - direct_llm  LLM (litellm) writes the memo from the collected data; validated.
  - openclaw    NemoClaw/OpenClaw agent on HPE PCAI: the engine sends the task over
                the gateway WS (method=agent); the runtime agent autonomously pulls
                the bank systems via the governed MCP (bank-credit skill), drafts and
                submits the memo, and returns it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

import httpx

from .config import settings
from .memo import build_stub_memo, validate_memo
from .security import Identity

log = logging.getLogger("slvd.agent")

DRAFT_PROMPT = """You are credit-memo-agent, a governed credit-risk analyst. Below is data
collected from the bank's systems for a loan renewal case. Draft the renewal decision memo.

Required output format (markdown), exactly these sections:
# Loan Renewal Decision Memo
A line with the client name and client code, e.g. "Client: <name> (<code>) — Renewal Request"
## Overview
- Requested renewal amount: <the requested amount, dollars, comma-formatted>
- Current utilization: <utilization dollars> (<pct>% of limit)
- Facility type: <facility>
- Risk rating: <rating>
- Status: <covenant status>
## Key Considerations
At least 3 numbered points drawn from the data (transaction history, KYC status,
prior-memo conditions, open follow-ups).
## Recommended Decision
One or two sentences: the recommended decision (Approve / Approve with conditions / Decline)
and the key conditions or rationale.

Rules:
- Use ONLY the values in the provided data. Never invent numbers or facts.
- The "Requested renewal amount" is the amount the analyst requested (given below), NOT the
  facility limit.
- Be concise and professional, like a bank credit memo.

Requested case:
{case_block}

Collected data (JSON):
{data_block}
"""


class AgentResult:
    def __init__(self, memo_md: str, ok: bool, notes: str = ""):
        self.memo_md = memo_md
        self.ok = ok
        self.notes = notes


def draft_stub(case: dict, requested_amount_usd: int) -> AgentResult:
    return AgentResult(build_stub_memo(case, requested_amount_usd), True, "stub-agent")


def _case_block(case: dict, requested_amount_usd: int) -> str:
    return (f"case_id={case['case_id']}, client={case['client']}, "
            f"client_code={case['client_code']}, "
            f"requested_renewal_amount=${requested_amount_usd:,} USD")


def _data_block(data: dict) -> str:
    return json.dumps(data, indent=2)


def draft_llm(case: dict, data: dict, requested_amount_usd: int) -> AgentResult:
    """Ask the LLM to draft the memo from collected data; validate; one re-prompt."""
    prompt = DRAFT_PROMPT.format(case_block=_case_block(case, requested_amount_usd),
                                 data_block=_data_block(data))
    messages = [
        {"role": "system", "content":
         "You are a precise bank credit-risk analyst. Output only the markdown memo."},
        {"role": "user", "content": prompt},
    ]
    memo = _chat(messages)
    if not memo:
        log.warning("LLM returned empty memo; falling back to stub")
        return AgentResult(build_stub_memo(case, requested_amount_usd), True,
                           "llm-empty-fallback-stub")
    check = validate_memo(memo, case, requested_amount_usd)
    if not check.ok:
        # one re-prompt with the missing list
        messages.append({"role": "user",
                         "content": "Your memo is missing: " + ", ".join(check.missing)
                                   + ". Rewrite the full memo now."})
        memo2 = _chat(messages)
        if memo2:
            check2 = validate_memo(memo2, case, requested_amount_usd)
            if check2.ok:
                return AgentResult(memo2, True, "direct-llm(reprompt)")
        return AgentResult(memo, False, "validation: " + ", ".join(check.missing))
    return AgentResult(memo, True, "direct-llm")


def _chat(messages: list[dict]) -> str:
    from .llm import chat
    return chat(messages, max_tokens=4096, temperature=0.2)


# ---------- openclaw (NemoClaw runtime on HPE PCAI) ----------

OPENCLAW_TASK = """Use the bank-credit skill to verify loan renewal case {case_id} for
analyst {analyst}. Requested renewal amount: ${amount:,} USD. The client is {client}
(client code {code}).

Fetch all five systems fresh (CRM profile, credit exposure, transaction behavior,
compliance status, prior memo) using the curl commands in the skill. Then draft the
renewal decision memo EXACTLY per the skill's memo format. Submit it via
workflow__submit_credit_memo (with case_id and the full memo as memo_md).

Your final reply must be ONLY the complete memo markdown starting with the heading
"# Loan Renewal Decision Memo" and the client line "Client: {client} ({code}) — Renewal Request".
Do not add any commentary before or after the memo.
"""


async def _openclaw_agent_turn(message: str, session_id: str) -> str:
    """One agent turn over the NemoClaw gateway WS. Returns the final reply text."""
    import websockets  # lazy: only the openclaw backend needs it

    if not settings.openclaw_token:
        raise RuntimeError("SLVD_OPENCLAW_TOKEN not configured")

    async with websockets.connect(settings.openclaw_url, max_size=2 ** 22,
                                  open_timeout=20) as ws:
        # 1) connect -> hello-ok
        await ws.send(json.dumps({
            "type": "req", "id": str(uuid.uuid4()), "method": "connect",
            "params": {
                "minProtocol": 3, "maxProtocol": 3,
                "client": {"id": "gateway-client", "version": "slvd-engine",
                           "platform": "linux", "mode": "backend"},
                "auth": {"token": settings.openclaw_token},
                "role": "operator", "scopes": ["operator.admin"],
            },
        }))
        deadline = time.time() + 30
        while time.time() < deadline:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 30))
            if msg.get("type") == "res":
                if not msg.get("ok", False):
                    raise RuntimeError(f"gateway connect failed: {msg.get('error')}")
                break  # hello-ok
            # ignore connect.challenge etc.

        # 2) agent turn (expect final result; the gateway sends res 'accepted',
        #    then agent stream events, then res 'ok' with the result).
        turn_id = str(uuid.uuid4())
        await ws.send(json.dumps({
            "type": "req", "id": turn_id, "method": "agent",
            "params": {
                "message": message,
                "sessionId": session_id,
                "timeout": max(60, int(settings.llm_timeout_s)),
                "idempotencyKey": uuid.uuid4().hex,
            },
        }))
        deadline = time.time() + settings.llm_timeout_s
        last_text = ""      # assistant stream carries cumulative `text`
        streamed: list[str] = []  # fallback: pure deltas when no cumulative text
        while time.time() < deadline:
            msg = json.loads(await asyncio.wait_for(ws.recv(), settings.llm_timeout_s))
            mtype = msg.get("type")
            if mtype == "event" and msg.get("event") == "agent":
                pl = msg.get("payload", {})
                if pl.get("stream") == "assistant":
                    data = pl.get("data", {})
                    if data.get("text"):
                        last_text = data["text"]
                    elif data.get("delta"):
                        streamed.append(data["delta"])
                continue
            if mtype != "res" or msg.get("id") != turn_id:
                continue  # unrelated frames
            payload = msg.get("payload", {}) or {}
            status = payload.get("status")
            if status == "accepted":
                continue  # expectFinal: keep waiting for the real result
            if not msg.get("ok", False):
                raise RuntimeError(f"agent turn failed: {msg.get('error')}")
            result = payload.get("result", {}) or {}
            texts: list[str] = [p.get("text", "") for p in result.get("payloads", [])
                                if p.get("text")]
            if not texts:
                texts = [p.get("text", "") for p in payload.get("payloads", [])
                         if p.get("text")]
            if not texts and last_text:
                texts = [last_text]
            if not texts and streamed:
                texts = ["".join(streamed)]
            if texts:
                return "\n".join(texts)
            raise RuntimeError("agent turn returned ok but no text payload")
        raise TimeoutError("no final agent result before timeout")


def _extract_memo(reply: str) -> str:
    """Pull the memo markdown out of the agent's final reply (tolerates wrapper text).

    The model sometimes emits the heading with line breaks between words
    ("# Loan\\nRenewal Decision Memo"), so match with flexible whitespace.
    """
    import re
    m = re.search(r"#\s*Loan\s+Renewal\s+Decision\s+Memo", reply)
    if m:
        return reply[m.start():].strip()
    return reply.strip()


def draft_openclaw(case: dict, data: dict, requested_amount_usd: int,
                   identity: Identity | None = None) -> AgentResult:
    """Delegate the memo to the NemoClaw/OpenClaw agent on HPE PCAI.

    The runtime agent autonomously pulls the bank systems (governed MCP via the
    bank-credit skill), drafts the memo, submits it, and returns the memo text.
    """
    analyst = identity.user_id if identity else case["allowed_users"][0]
    task = OPENCLAW_TASK.format(
        case_id=case["case_id"], analyst=analyst, amount=requested_amount_usd,
        client=case["client"], code=case["client_code"],
    )
    session_id = f"slvd-{case['case_id'].lower()}-{int(time.time())}"
    log.info("openclaw agent turn for %s (session %s)", case["case_id"], session_id)

    reply = asyncio.run(_openclaw_agent_turn(task, session_id))
    if not reply.strip():
        log.warning("openclaw returned empty reply; falling back to stub")
        return AgentResult(build_stub_memo(case, requested_amount_usd), True,
                           "openclaw-empty-fallback-stub")

    memo = _extract_memo(reply)
    check = validate_memo(memo, case, requested_amount_usd)
    if not check.ok:
        log.warning("openclaw memo validation failed (%s); one retry", ", ".join(check.missing))
        retry_task = (task + "\n\nYour previous memo was missing: "
                      + ", ".join(check.missing)
                      + ". Re-fetch the data if needed and output the complete corrected memo "
                        "only.")
        reply2 = asyncio.run(_openclaw_agent_turn(retry_task, session_id + "-retry"))
        memo2 = _extract_memo(reply2)
        check2 = validate_memo(memo2, case, requested_amount_usd)
        if check2.ok:
            return AgentResult(memo2, True, "openclaw(retry)")
        # keep the best effort (still real agent output) but flag it
        return AgentResult(memo, False, "openclaw-validation: " + ", ".join(check.missing))
    return AgentResult(memo, True, "openclaw-agent")


def draft_memo(case: dict, data: dict, requested_amount_usd: int,
               identity: Identity | None = None) -> AgentResult:
    backend = settings.agent_backend
    if backend in ("stub", "sim"):
        return draft_stub(case, requested_amount_usd)
    if backend == "direct_llm":
        try:
            res = draft_llm(case, data, requested_amount_usd)
        except Exception as e:  # noqa: BLE001
            log.error("direct_llm failed: %s — falling back to stub", e)
            return AgentResult(build_stub_memo(case, requested_amount_usd), True,
                               f"llm-error-fallback-stub:{type(e).__name__}")
        if not res.ok:
            # LLM produced a memo that failed field validation (not an exception) —
            # degrade gracefully to a valid deterministic memo so the run can complete,
            # and report it honestly in the audit.
            log.warning("direct_llm validation failed (%s) — falling back to stub", res.notes)
            return AgentResult(build_stub_memo(case, requested_amount_usd), True,
                               f"llm-validation-fallback-stub:{res.notes[:40]}")
        return res
    if backend == "openclaw":
        try:
            res = draft_openclaw(case, data, requested_amount_usd, identity)
        except Exception as e:  # noqa: BLE001
            log.error("openclaw backend failed: %s — falling back to stub", e)
            return AgentResult(build_stub_memo(case, requested_amount_usd), True,
                               f"openclaw-error-fallback-stub:{type(e).__name__}")
        if not res.ok:
            log.warning("openclaw validation failed (%s) — falling back to stub", res.notes)
            return AgentResult(build_stub_memo(case, requested_amount_usd), True,
                               f"openclaw-validation-fallback-stub:{res.notes[:40]}")
        return res
    raise ValueError(f"unknown agent backend: {backend}")
