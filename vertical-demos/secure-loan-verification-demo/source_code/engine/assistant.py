"""SLVD demo assistant — a scoped assistant that answers questions about this demo.

Primary path: a full LLM call (same OpenAI-compatible endpoint the memo agent
uses) with the demo knowledge base + real case fixtures as context, so answers
are natural, phrasing-independent, and grounded in the actual demo data.

Fallback path (LLM unavailable: offline tests / SLVD_SIMULATION / no API key /
timeout): a deterministic rule-based intent matcher so the demo always answers.
"""
from __future__ import annotations

import json
import re

# ---------- knowledge base (demo facts only) ----------

WHAT = """SLVD (Secure Loan Verification Demo) shows a governed, human-in-the-loop AI
workflow for loan renewal. An AI agent researches a case by pulling credit data
from the bank's back-office systems and drafts a decision memo. A senior credit
officer then makes the final approve/reject call on large or risky cases.
Every step is recorded in an audit trail.

In one line: the AI does the analyst's research in minutes, but a human always
holds the approval pen — and every action is on the record."""

HOW = """The end-to-end flow:
1. Client (Acme) asks to renew a $5M facility.
2. Analyst (Nick) starts the case with the case id + amount.
3. AI agent pulls data from 5 governed systems: CRM, credit exposure,
   transactions, compliance (KYC/sanctions), prior memo.
4. AI drafts the credit decision memo.
5. Policy gate — if amount >= $5M or KYC is pending, it escalates to an
   approver (smaller/clean cases auto-complete).
6. Approver (Sarah) reviews and decides (console or email link).
7. Memo is published to the official credit record.
8. Client is notified by email.

The 10 tracked steps: Identity validated -> Case resolved -> CRM -> Credit exposure ->
Transactions -> Compliance -> Prior memo -> Draft memo -> Submission -> Approval decision."""

COMPONENTS = """Core components (Kubernetes namespace 'slvd', 4 pods):
- Portal — React + TS UI: analyst console, approver governance console, mail inbox.
- Workflow Engine — FastAPI (Python 3.11). The system of record: the 10-step
  pipeline, the policy gate, JWT auth (analyst vs approver), the audit trail, the
  mailer, and the gated publish.
- credit-memo-mcp — a governed MCP server. 5 read tools + 1 gated submit
  (the memo cannot be published until an approval is recorded).
- Data Store — SQLite + files on a PVC: runs, audit, draft/official memos.
- Mailpit — in-cluster SMTP sink + UI for approval/client email.

AI layer:
- OpenClaw agent (NemoClaw, ns 'nemoclaw') — the generative step; it pulls data
  via the bank-credit skill and drafts the memo.
- litellm-helm -> qwen3-8-27b (in-cluster LLM namespace) — LLM inference.

Note: llama3.1-8b is NOT supported here — its output does not fit the accepted
schema in OpenClaw, so the agent loop breaks. We use qwen3-8-27b."""

WHY_BOTH = """We keep a FastAPI engine AND an OpenClaw agent because an LLM agent is
not a trustworthy system of record:
- FastAPI = the compliance system — deterministic, unit-testable. It owns
  state, identity, the policy gate, the gated submit, the audit trail, and all
  human integration (portal, console, emails, archive).
- OpenClaw = the analyst's brain — the visible "agentic" drafting step. It is
  a pluggable backend (SLVD_AGENT_BACKEND = openclaw | direct_llm | stub); swap
  it and the whole governance layer is unchanged.
- The agent cannot bypass control — the submit tool is gated server-side, and
  the agent's draft is validated before it proceeds. The memo only publishes after
  a human approval is recorded."""

POLICY = """Approval policy: a case requires senior approval if
amount >= $5,000,000 OR KYC status is pending (the default threshold is $5M).
Everything else can auto-complete. This mirrors the video: the $5M Acme renewal
crosses the threshold (and its KYC is pending), so it escalates to the approver.
The rule is enforced by the engine (code), not by the model — the AI cannot decide
when a human is required."""

ROLES = """Roles:
- Client — Acme Industrial (CL-77821). Requests the renewal; receives the
  decision by email.
- Analyst — Nick Johnson (E102938). Starts the case and states the amount.
  Never approves anything.
- AI Agent — credit-memo-agent. Collects data from 5 systems and drafts the
  memo. Never invents a number; never makes the final call.
- Approver — Sarah Chen (E200145). Senior credit officer; makes the final
  approve/reject decision on large/risky cases."""

TOOLS = """The governed MCP tools (called in order):
1. get_crm_profile — client profile, industry, beneficial-ownership status
2. get_credit_exposure — facility, limit, utilization, risk rating, covenants
3. get_transactions — transaction / cash-flow behavior
4. get_compliance_status — KYC status + sanctions
5. get_prior_memo — prior conditions, open follow-ups
6. workflow__submit_credit_memo — submits the memo (GATED: needs approval)

The first five are read-only and authorized per-case (x-user-id). The sixth is
blocked until a human approval is recorded in the engine."""

VALUES = """Why the demo matters:
- Governance — AI prepares the work; a human makes the call. No AI decision is
  final on its own.
- Accuracy — every figure comes from a governed bank-system call; memos are
  validated before submission.
- Auditability — who did what, when, and why is recorded; the approval is logged
  under the approver's identity; the official memo is archived per case/year.
- Efficiency — the agent fetches and drafts in minutes, not days; the approver
  reviews a ready memo; the client is notified the moment a decision lands."""

CASES = """Sample cases in the demo:
- CR-2026-00451 — Acme Industrial Holdings (CL-77821), $5M renewal. Crosses the
  $5M threshold and KYC is pending -> requires approver sign-off. Expected: Approve
  with conditions.
- CR-2026-00452 — Blue Harbor Logistics, $2M, clean (no approval rule triggered)
  -> auto-completes.
- CR-2026-00453 — $2M, KYC pending -> requires approval (KYC rule)."""

HELP = """I'm the SLVD demo assistant — I explain how this demo works. Try:
- "what is this demo?"
- "how does it work?"
- "what are the components?"
- "why do you need both FastAPI and the openclaw agent?"
- "what is the approval policy?"
- "who are the roles?"
- "what are the MCP tools?"
- "why does the demo matter?"
- "tell me about CR-2026-00451"
- "generate a memo for CR-2026-00451"

I only answer about this demo — case data, the workflow, the governance, and
the components."""

# intent -> (keywords, answer)  [fallback path only]
INTENTS = [
    (["what is this", "what is slvd", "what's this", "overview", "about this demo", "what does this"], WHAT),
    (["how does it work", "how does this work", "workflow", "flow", "steps", "process", "end to end", "end-to-end"], HOW),
    (["mcp tool", "governed tool", "tools", "what tools", "the six", "submit tool"], TOOLS),
    (["why both", "why fastapi and", "why openclaw and", "need both", "fastapi and the agent", "engine and the agent"], WHY_BOTH),
    (["component", "architecture", "what is the portal", "what is the engine", "what is mcp", "what is mailpit", "tech stack", "what are the"], COMPONENTS),
    (["approval policy", "approval rule", "why approval", "when is approval", "policy gate", "what triggers approval", "threshold"], POLICY),
    (["role", "who does", "who is the analyst", "who is the approver", "who is the client", "people", "actors"], ROLES),
    (["why does the demo", "why matter", "benefit", "value", "why is this useful", "significance"], VALUES),
    (["case", "cases", "sample", "example", "what cases", "which cases", "list the"], CASES),
]


# ---------- case fixtures ----------

def _find_case(message: str):
    from .workflow import _load_case
    for cid in re.findall(r"CR-\d{4}-\d{5}", message):
        c = _load_case(cid)
        if c:
            return c
    return None


def _case_answer(case: dict) -> str:
    crm, credit, comp = case.get("crm", {}), case.get("credit", {}), case.get("compliance", {})
    prior = case.get("prior_memo", {})
    lines = [
        f"Case {case['case_id']} — {case['client']} ({case['client_code']}):",
        f"- Type: {case['type']}",
        f"- Requested amount: ${case['amount_usd']:,}",
        f"- Facility: {credit.get('facility', '—')} (limit ${credit.get('limit_usd', 0):,})",
        f"- Utilization: ${credit.get('utilization_usd', 0):,} ({credit.get('utilization_pct', 0)}% of limit)",
        f"- Risk rating: {credit.get('risk_rating', '—')}",
        f"- Covenants: {credit.get('covenant_status', '—')}",
        f"- KYC: {comp.get('kyc_status', '—')} ({comp.get('kyc_detail', '—')}); sanctions {comp.get('sanctions', '—')}",
        f"- Industry: {crm.get('industry', '—')} ({crm.get('relationship_years', '—')}y relationship)",
        f"- Prior-memo conditions: {', '.join(prior.get('conditions', [])) or '—'}",
        f"- Open follow-up: {prior.get('open_follow_up', '—')}",
        f"- Expected decision: {case.get('expected_decision', '—')}",
    ]
    from .policy import evaluate
    from .config import settings
    evalr = evaluate(case["amount_usd"], comp.get("kyc_status", "clear"))
    gate = f"- Approval required: {'YES' if evalr.approval_required else 'no'}"
    if evalr.approval_required:
        gate += " (" + " + ".join(evalr.reasons) + ")"
    lines.append(gate)
    return "\n".join(lines)


# ---------- LLM path ----------

def _system_prompt() -> str:
    from .workflow import _load_case
    cases = {}
    for cid in ("CR-2026-00451", "CR-2026-00452", "CR-2026-00453"):
        c = _load_case(cid)
        if c:
            cases[cid] = {k: v for k, v in c.items() if k != "transactions"}
    kb = "\n\n".join([
        "DEMO OVERVIEW:\n" + WHAT,
        "HOW IT WORKS:\n" + HOW,
        "COMPONENTS:\n" + COMPONENTS,
        "WHY FASTAPI + AGENT:\n" + WHY_BOTH,
        "APPROVAL POLICY:\n" + POLICY,
        "ROLES:\n" + ROLES,
        "GOVERNED MCP TOOLS:\n" + TOOLS,
        "WHY THE DEMO MATTERS:\n" + VALUES,
        "SAMPLE CASES:\n" + CASES,
        "FULL CASE DATA (ground truth — quote figures exactly):\n"
        + json.dumps(cases, indent=2, default=str),
    ])
    return (
        "You are the SLVD demo assistant embedded in the Secure Loan Verification Demo "
        "portal. You answer ONLY questions about this specific demo: its purpose, the "
        "workflow, its components, why FastAPI and the OpenClaw agent both exist, the "
        "approval policy, the roles, the governed MCP tools, why the demo matters, and "
        "the sample cases (with their real data).\n"
        "Rules:\n"
        "- Ground every fact in the knowledge base below. Never invent demo facts, "
        "names, figures, or components. Quote case figures exactly as given.\n"
        "- Be natural and conversational, but concise: 3-15 sentences for conceptual "
        "questions; a compact structured breakdown for case questions (include amount, "
        "utilization, risk rating, KYC, covenants, expected decision, and whether "
        "approval is required with the reason).\n"
        "- If the user asks for a memo for a case, say the memo is generated by the "
        "workflow and list the case's key inputs; do not write a full memo yourself.\n"
        "- If the question is not about this demo, politely say you only help with "
        "this demo and suggest one of the example questions.\n"
        "- Use plain text with light markdown (bold, short lists). No headings.\n\n"
        + kb
    )


def _llm_answer(message: str) -> str | None:
    from .config import settings
    from .llm import chat
    if settings.simulation or not settings.llm_api_key:
        return None
    try:
        content = chat(
            [
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": message},
            ],
            max_tokens=700,
            temperature=0.4,
            timeout=min(settings.llm_timeout_s, 60),
        )
        return content or None
    except Exception:  # noqa: BLE001 — fall back to rule-based on any LLM error
        return None


# ---------- rule-based fallback ----------

def _rule_answer(message: str) -> str:
    msg = message.strip()
    low = msg.lower()
    case = _find_case(msg)
    if "memo" in low and case:
        from .memo import build_stub_memo
        return build_stub_memo(case, case["amount_usd"])
    if "memo" in low and not case:
        return ("I can generate a sample memo for a specific case. Try: "
                "'generate a memo for CR-2026-00451'.")
    if case:
        return _case_answer(case)
    for keywords, ans in INTENTS:
        if any(k in low for k in keywords):
            return ans
    return HELP


def answer(message: str) -> str:
    """Answer a demo-related question. LLM-first; rule-based fallback."""
    msg = message.strip()
    # deterministic memo generation stays deterministic (structured deliverable)
    case = _find_case(msg)
    if "memo" in msg.lower() and case:
        from .memo import build_stub_memo
        return build_stub_memo(case, case["amount_usd"])
    reply = _llm_answer(msg)
    if reply:
        return reply
    return _rule_answer(msg)
