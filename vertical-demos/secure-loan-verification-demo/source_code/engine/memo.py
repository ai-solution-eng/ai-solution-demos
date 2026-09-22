"""Memo builder (deterministic stub) + memo validator.

The stub produces the video-identical memo from case fixtures. The LLM
agent path (direct_llm / openclaw) produces a memo that must pass
validate_memo() before it is accepted (required fields, canonical values).
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class MemoCheck:
    ok: bool
    missing: list[str]


def build_stub_memo(case: dict, requested_amount_usd: int | None = None) -> str:
    """Deterministic memo from fixtures (simulation mode / no-LLM fallback)."""
    c = case["credit"]
    tx = case["transactions"]
    comp = case["compliance"]
    prior = case["prior_memo"]
    crm = case["crm"]
    amount = requested_amount_usd or case["amount_usd"]
    conditions = " and ".join(x.lower() if i > 0 else x for i, x in enumerate(prior["conditions"]))
    kyc_line = (f"Pending KYC review ({comp['kyc_detail']})"
                if comp["kyc_status"] == "pending" else "KYC complete")
    follow_up = (f"Open follow-up: {prior['open_follow_up']}"
                 if prior["open_follow_up"] not in ("", "None") else "No open follow-ups")
    decision = case.get("expected_decision", "Approve")
    decision_body = _decision_body(decision, case)
    return f"""# Loan Renewal Decision Memo

**Client:** {case['client']} ({case['client_code']}) — Renewal Request

## Overview
- Requested renewal amount: ${amount:,}
- Current utilization: ${c['utilization_usd']:,} ({c['utilization_pct']}% of limit)
- Facility type: {c['facility']}
- Risk rating: {c['risk_rating']}
- Status: {c['covenant_status']}

## Key Considerations
1. {tx['history']}
2. {kyc_line}
3. Prior memo conditions: {conditions}
4. {follow_up}

## Recommended Decision
{decision_body}
"""


def _decision_body(decision: str, case: dict) -> str:
    if decision.startswith("Approve with conditions"):
        comp = case["compliance"]
        prior = case["prior_memo"]
        extra = ""
        if comp["kyc_status"] == "pending":
            extra = f" completion of the pending KYC review ({comp['kyc_detail']}), and"
        if prior["open_follow_up"] not in ("", "None"):
            extra += f" the open follow-up ({prior['open_follow_up']})"
        return (f"Approve with conditions — renewal of ${case['amount_usd']:,} is recommended, "
                f"subject to{extra}. Prior conditions carry forward.")
    if decision == "Approve":
        return f"Approve — renewal of ${case['amount_usd']:,} is recommended; no exceptions noted."
    return f"{decision} — see key considerations."


def validate_memo(memo_md: str, case: dict, requested_amount_usd: int | None = None) -> MemoCheck:
    """Structure + canonical-value checks (wording is LLM-variable, values are not)."""
    missing: list[str] = []
    amount = requested_amount_usd or case["amount_usd"]
    checks = {
        "client name": case["client"],
        "client code": case["client_code"],
        "amount": f"${amount:,}",
        "utilization pct": f"{case['credit']['utilization_pct']}%",
        "utilization amount": f"${case['credit']['utilization_usd']:,}",
        "risk rating": case["credit"]["risk_rating"],
        "facility": case["credit"]["facility"],
        "covenant status": case["credit"]["covenant_status"],
        "recommended decision": "Recommended Decision",
    }
    for label, needle in checks.items():
        if needle.lower() not in memo_md.lower():
            missing.append(label)
    # At least 3 numbered key considerations
    numbered = re.findall(r"(?m)^\s*\d+\.\s+\S", memo_md)
    if len(numbered) < 3:
        missing.append(">=3 numbered key considerations")
    return MemoCheck(ok=not missing, missing=missing)
