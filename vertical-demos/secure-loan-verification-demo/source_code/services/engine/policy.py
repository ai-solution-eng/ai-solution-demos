"""Governance policy: when does a loan memo require senior approval?

Rule (video: $5M crosses a compliance threshold; audit trail also cites
KYC pending): approval required iff amount >= threshold OR KYC pending.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import settings


@dataclass
class PolicyResult:
    approval_required: bool
    reasons: list[str]


def evaluate(amount_usd: int, kyc_status: str) -> PolicyResult:
    reasons: list[str] = []
    if amount_usd >= settings.approval_threshold_usd:
        reasons.append(f"amount >= ${settings.approval_threshold_usd:,}")
    if (kyc_status or "clear").strip().lower() == "pending":
        reasons.append("KYC pending")
    return PolicyResult(approval_required=bool(reasons), reasons=reasons)


def policy_audit_line(result: PolicyResult) -> str:
    if result.approval_required:
        return f"Policy triggered — approval required ({' + '.join(result.reasons)})"
    return "Policy evaluated — no approval required"
