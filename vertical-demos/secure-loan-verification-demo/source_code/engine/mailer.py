"""Email (SMTP) — approval requests to the senior officer + client notification.

Both messages are formal, branded, multipart (HTML + plain-text) so they read
like real bank governance communications in any mail client.
"""
from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .config import settings

log = logging.getLogger("slvd.mail")

# ---------- shared presentation ----------

_BRAND = "#16294f"
_ACCENT = "#1e40af"
_OK = "#166534"
_WARN = "#92400e"
_MUTE = "#5b6472"
_LINE = "#dde3ec"


def _page_header(title: str, subtitle: str, badge: str = "CREDIT RISK GOVERNANCE") -> str:
    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_BRAND};border-radius:10px 10px 0 0;">
      <tr>
        <td style="padding:18px 24px;">
          <div style="font-family:Arial,Helvetica,sans-serif;font-size:11px;letter-spacing:2px;color:#9db1d8;font-weight:bold;">{badge}</div>
          <div style="font-family:Arial,Helvetica,sans-serif;font-size:20px;color:#ffffff;font-weight:bold;margin-top:4px;">{title}</div>
          <div style="font-family:Arial,Helvetica,sans-serif;font-size:12.5px;color:#c3d0e8;margin-top:2px;">{subtitle}</div>
        </td>
      </tr>
    </table>
    """


def _footer(request_ref: str) -> str:
    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr><td style="padding:14px 24px 20px;border-top:1px solid {_LINE};">
        <div style="font-family:Arial,Helvetica,sans-serif;font-size:11px;color:{_MUTE};line-height:1.6;">
          This notification was generated automatically by the governed workflow engine
          (credit-memo-agent &rarr; credit-memo-mcp). Every action taken from this message is
          recorded in the immutable audit trail with the actor&rsquo;s identity and a timestamp.<br/>
          Reference: <b>{request_ref}</b> &middot; From: {settings.mail_from} &middot; Do not reply
          &mdash; this mailbox is not monitored.
        </div>
      </td></tr>
    </table>
    """


def _kv_table(rows: list[tuple[str, str]]) -> str:
    body = "".join(
        f'<tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:12.5px;color:{_MUTE};'
        f'padding:7px 12px;border:1px solid {_LINE};width:180px;vertical-align:top;">{k}</td>'
        f'<td style="font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#1f2937;'
        f'padding:7px 12px;border:1px solid {_LINE};vertical-align:top;">{v}</td></tr>'
        for k, v in rows)
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">{body}</table>'


def _button(href: str, label: str, color: str) -> str:
    return (f'<a href="{href}" style="display:inline-block;padding:11px 22px;margin:0 8px 8px 0;'
            f'background:{color};color:#ffffff;text-decoration:none;border-radius:6px;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:13.5px;font-weight:bold;">{label}</a>')


def _send(to: str, subject: str, html: str, text: str) -> bool:
    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = settings.mail_from
        msg["To"] = to
        msg["Subject"] = subject
        msg["X-Mailer"] = "SLVD-GovernedWorkflow/1.0"
        msg["Message-ID"] = f"<slvd-{abs(hash((to, subject, _now_ms())))}@{settings.mail_from.split('@')[-1]}>"
        msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(settings.mail_host, settings.mail_port, timeout=15) as s:
            s.send_message(msg)
        log.info("mail sent: %s -> %s", subject, to)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("mail failed (%s -> %s): %s", subject, to, e)
        return False


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


# ---------- approval request (senior officer) ----------

def send_approval_request(request_id: str, case_id: str, emp_id: str,
                          agent: str, tool_host: str, reason: str,
                          approve_24h_link: str, reject_link: str,
                          dashboard_link: str, policy, case: dict) -> bool:
    client = case.get("client", "—")
    code = case.get("client_code", "—")
    credit = case.get("credit", {})
    compliance = case.get("compliance", {})
    amount = case.get("amount_usd", 0)
    reasons = list(getattr(policy, "reasons", []) or [])

    policy_lines = "".join(
        f'<li style="font-family:Arial,Helvetica,sans-serif;font-size:13px;color:{_WARN};margin:4px 0 4px 18px;">{r}</li>'
        for r in reasons) or '<li style="font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#166534;margin:4px 0 4px 18px;">No rule triggered — auto-approved by policy.</li>'

    html = f"""
    <div style="background:#f4f6f9;padding:28px 16px;font-family:Arial,Helvetica,sans-serif;">
      <div style="max-width:640px;margin:0 auto;background:#ffffff;border:1px solid {_LINE};border-radius:10px;overflow:hidden;">
        {_page_header("Approval Required — Loan Renewal Decision Memo",
                      f"Case {case_id} &middot; {client} &middot; ${amount:,} renewal request")}
        <div style="padding:22px 24px 8px;">
          <p style="font-size:14px;color:#1f2937;line-height:1.55;margin:0 0 14px;">
            Dear Senior Credit Officer,<br/>
            The governed credit-memo agent has completed the data collection and policy
            evaluation for the loan renewal below. <b>Senior approval is required before the
            decision memo can be published</b> to the official record. Please review and record
            your decision.
          </p>

          <div style="background:#fff8e6;border:1px solid #f5d67b;border-radius:8px;padding:12px 16px;margin-bottom:16px;">
            <div style="font-size:11px;font-weight:bold;letter-spacing:1px;color:{_WARN};margin-bottom:6px;">WHY APPROVAL IS REQUIRED (POLICY)</div>
            <ul style="margin:0;padding:0;">{policy_lines}</ul>
          </div>

          <div style="font-size:11px;font-weight:bold;letter-spacing:1px;color:{_MUTE};margin:0 0 6px;">CASE SUMMARY</div>
          {_kv_table([
              ("Case ID", case_id),
              ("Client", f"{client} ({code})"),
              ("Facility", credit.get("facility", "—")),
              ("Requested renewal", f"${amount:,}"),
              ("Current utilization", f"${credit.get('utilization_usd', 0):,} ({credit.get('utilization_pct', '—')}% of ${credit.get('limit_usd', 0):,} limit)"),
              ("Risk rating", credit.get("risk_rating", "—")),
              ("Covenants", credit.get("covenant_status", "—")),
              ("KYC / sanctions", f"KYC {compliance.get('kyc_status', '—')} ({compliance.get('kyc_detail', '—')}); sanctions {compliance.get('sanctions', '—')}"),
          ])}

          <div style="font-size:11px;font-weight:bold;letter-spacing:1px;color:{_MUTE};margin:16px 0 6px;">GOVERNANCE CONTEXT</div>
          {_kv_table([
              ("Requested by", f"Analyst (employee ID {emp_id})"),
              ("Agent", agent),
              ("Governed MCP method", tool_host),
              ("Reason", reason),
              ("Approval request ID", f'<span style="font-family:monospace;font-size:12px;">{request_id}</span>'),
              ("Valid for", "24 hours from issue"),
          ])}

          <div style="margin-top:20px;">
            <div style="font-size:11px;font-weight:bold;letter-spacing:1px;color:{_MUTE};margin-bottom:10px;">RECORD YOUR DECISION</div>
            {_button(approve_24h_link, "✔ Approve — decision valid 24 h", "#15803d")}
            {_button(reject_link, "✖ Reject", "#b91c1c")}
            <div style="margin-top:10px;">
              <a href="{dashboard_link}" style="font-size:12.5px;color:{_ACCENT};text-decoration:underline;">Open the run in the Credit Risk Portal</a>
            </div>
            <div style="font-size:11.5px;color:{_MUTE};margin-top:10px;">
              Approving publishes the memo to the official credit record and notifies the client.
              Rejecting closes the run with your reason logged in the audit trail.
            </div>
          </div>
        </div>
        {_footer(request_id)}
      </div>
    </div>
    """

    text = f"""Approval Required — Loan Renewal Decision Memo
Case {case_id} | {client} | ${amount:,} renewal request

Dear Senior Credit Officer,

The governed credit-memo agent has completed data collection and policy evaluation
for this loan renewal. Senior approval is required before the decision memo can be
published to the official record.

WHY APPROVAL IS REQUIRED (POLICY)
{chr(10).join(' - ' + r for r in reasons) if reasons else ' - No rule triggered'}

CASE SUMMARY
 - Case ID: {case_id}
 - Client: {client} ({code})
 - Facility: {credit.get('facility', '—')}
 - Requested renewal: ${amount:,}
 - Current utilization: ${credit.get('utilization_usd', 0):,} ({credit.get('utilization_pct', '—')}% of limit)
 - Risk rating: {credit.get('risk_rating', '—')}
 - Covenants: {credit.get('covenant_status', '—')}
 - KYC / sanctions: KYC {compliance.get('kyc_status', '—')}; sanctions {compliance.get('sanctions', '—')}

GOVERNANCE CONTEXT
 - Requested by: Analyst (employee ID {emp_id})
 - Agent: {agent}
 - Governed MCP method: {tool_host}
 - Reason: {reason}
 - Approval request ID: {request_id}
 - Valid for: 24 hours from issue

RECORD YOUR DECISION (signed single-use links)
Approve (24 h): {approve_24h_link}
Reject:         {reject_link}
Open in portal: {dashboard_link}

--
Generated automatically by the governed workflow engine. Every action from this
message is recorded in the immutable audit trail with the actor's identity and a
timestamp. Reference {request_id}. Do not reply — this mailbox is not monitored.
"""

    subject = (f"[ACTION REQUIRED] Loan Renewal Approval — {case_id} · {client} "
               f"· ${amount:,} · ref {request_id[:8]}")
    return _send(settings.approver_email, subject, html, text)


# ---------- client notification ----------

def send_client_notification(case_id: str, client: str, amount_usd: int, run_id: str) -> bool:
    html = f"""
    <div style="background:#f4f6f9;padding:28px 16px;font-family:Arial,Helvetica,sans-serif;">
      <div style="max-width:600px;margin:0 auto;background:#ffffff;border:1px solid {_LINE};border-radius:10px;overflow:hidden;">
        {_page_header("Loan Application Approved",
                      f"Case {case_id} &middot; {client}", badge="CREDIT SERVICES")}
        <div style="padding:22px 24px 8px;">
          <p style="font-size:14px;color:#1f2937;line-height:1.55;margin:0 0 14px;">
            Good news &mdash; the renewal of your credit facility has been <b style="color:{_OK};">approved</b>
            following the governed credit review.
          </p>
          {_kv_table([
              ("Case ID", case_id),
              ("Client", client),
              ("Approved amount", f"${amount_usd:,}"),
              ("Decision reference", f'<span style="font-family:monospace;font-size:12px;">{run_id}</span>'),
              ("Status", "Approved — conditions per the decision memo"),
          ])}
          <p style="font-size:13px;color:#1f2937;line-height:1.55;margin:16px 0 0;">
            Your dedicated relationship team will follow up with the final documentation.
            If you have any questions, please contact your relationship manager directly.
          </p>
        </div>
        {_footer(run_id)}
      </div>
    </div>
    """
    text = f"""Loan Application Approved
Case {case_id} | {client}

Good news — the renewal of your credit facility has been approved following the
governed credit review.

 - Case ID: {case_id}
 - Client: {client}
 - Approved amount: ${amount_usd:,}
 - Decision reference: {run_id}
 - Status: Approved — conditions per the decision memo

Your dedicated relationship team will follow up with the final documentation.

--
Generated automatically by the governed workflow engine. Reference {run_id}.
Do not reply — this mailbox is not monitored.
"""
    return _send(settings.client_email,
                 f"Loan Application Approved — {case_id} · {client} · ${amount_usd:,}",
                 html, text)
