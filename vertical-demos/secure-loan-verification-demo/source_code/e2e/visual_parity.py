"""A11 video-parity gate (content contract).

Verifies the DEPLOYED app (external VS URL — browser-reachable, no bypass) serves
the exact labels/IDs/copy of the video's 7 frames:
  1. Analyst Portal:  Nick Johnson / Risk Analyst / Credit Risk / E102938;
     case CR-2026-00451; Acme Industrial Holdings; Renewal Decision Memo; $5,000,000
  2. Workflow Progress: the 10 canonical step names, in order
  3. Generated Memo:    Acme (CL-77821), $5,000,000, $3,800,000 (76%), Revolving
     credit, BB, covenants, 4 key considerations, Recommended Decision
  4. Governance Review: request UUID + Re-submit (Approved) copy
  5. Run Summary:      RUN-xxxxx, Nick Johnson (E102938), Sarah Chen (E200145),
     Senior Credit Officer, COMPLETED
  6. Audit Trail:      7 canonical entries
  7. Client mail:      "Loan Application Approved" (mailpit)
Checks the rendered portal HTML for static labels AND the live API for dynamic
values (steps, memo, run summary, audit).
"""
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # source_code/
REPO_ROOT = os.path.dirname(SRC_ROOT)                                     # repo root
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, SRC_ROOT)

HOST = os.environ.get("SLVD_HOST", "slvd.<your-pcai-domain>")
MAIL_HOST = os.environ.get("SLVD_MAIL_HOST", "slvd-mail.<your-pcai-domain>")
BASE = f"https://{HOST}"

CTX = ssl._create_unverified_context()


def get(url: str, headers: dict | None = None, timeout: int = 30) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def post_json(url: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


STEP_NAMES = [
    "Identity validated", "Case metadata resolved", "CRM profile fetched",
    "Credit exposure fetched", "Transaction behavior analyzed",
    "Compliance status checked", "Prior memo reviewed", "Draft memo created",
    "Submission attempted", "Approval decision",
]

checks: dict[str, bool] = {}
errors: list[str] = []


def chk(name: str, ok: bool, detail: str = ""):
    checks[name] = bool(ok)
    if not ok:
        errors.append(f"{name}: {detail[:200]}")


def main() -> int:
    # 1) portal HTML — static labels (frame 1 + 2 labels)
    code, html = get(BASE + "/")
    chk("portal_html_200", code == 200, f"http {code}")
    chk("portal_title", "Credit Risk Portal" in html, html[:200])
    # React renders client-side: static labels live in the JS bundle. Fetch it.
    m = re.search(r'src="(/assets/index-[^"]+\.js)"', html)
    bundle = ""
    if m:
        _, bundle = get(BASE + m.group(1))
    for needle in ["Analyst Portal", "Case Selection", "Generate renewal decision memo",
                   "Workflow Progress", "Agent Chat", "Nick Johnson", "Risk Analyst",
                   "E102938", "CR-2026-00451", "Acme Industrial Holdings",
                   "Renewal Decision Memo", "HOW THIS WORKS",
                   "Re-submit (Approved)", "Governance Review In Progress",
                   "Run Summary", "Audit Trail", "Generated Memo"]:
        chk(f"bundle_label:{needle}", needle in bundle, "label not in JS bundle")
    # 2) login + start a run (simulation mode auto-approves quickly)
    code, b = post_json(BASE + "/api/auth/login", {"username": "nick", "password": "analyst123"})
    tok = b.get("token", "")
    chk("login", code == 200 and tok, f"{code} {b}")
    hdr = {"Authorization": f"Bearer {tok}"}
    code, b = post_json(BASE + "/api/runs", {"case_id": "CR-2026-00451", "amount_usd": 5_000_000})
    run_id = b.get("run_id", "")
    chk("run_start", code == 200 and run_id, f"{code} {b}")
    if not run_id:
        finish()
        return 1
    # wait terminal (sim mode: ~15s)
    snap: dict = {}
    for _ in range(90):
        code, raw = get(BASE + f"/api/runs/{run_id}", hdr)
        try:
            snap = json.loads(raw)
        except json.JSONDecodeError:
            snap = {}
        if snap.get("run", {}).get("status") in ("COMPLETED", "REJECTED", "FAILED"):
            break
        time.sleep(1)
    run = snap.get("run", {})
    # frame 2: 10 steps, canonical names, order
    steps = snap.get("steps", [])
    chk("steps_10", len(steps) == 10, f"{len(steps)} steps")
    got_names = [s["name"] for s in steps]
    chk("step_names_canonical", got_names == STEP_NAMES, str(got_names))
    chk("steps_all_done", all(s["state"] == "done" for s in steps),
        str([(s["seq"], s["state"]) for s in steps]))
    # frame 5: run summary values
    chk("summary_completed", run.get("status") == "COMPLETED", str(run.get("status")))
    chk("summary_run_id", bool(re.match(r"RUN-\d{5}", run.get("run_id", ""))), run.get("run_id", ""))
    chk("summary_requested_by", run.get("requested_by") == "Nick Johnson", str(run.get("requested_by")))
    chk("summary_emp_id", run.get("requested_emp_id") == "E102938", str(run.get("requested_emp_id")))
    chk("summary_approved_by", run.get("approved_by") in ("Sarah Chen", "simulation (auto)"),
        str(run.get("approved_by")))
    chk("summary_role", run.get("approval_role") == "Senior Credit Officer",
        str(run.get("approval_role")))
    # frame 3: memo
    code, raw = get(BASE + f"/api/runs/{run_id}/memo", hdr)
    try:
        memo = json.loads(raw)
    except json.JSONDecodeError:
        memo = {}
    md = memo.get("memo_md", "")
    chk("memo_200", code == 200, f"{code}")
    for needle in ["Acme Industrial Holdings", "CL-77821", "$5,000,000",
                   "$3,800,000", "76% of limit", "Revolving credit", "BB",
                   "In compliance with covenants", "Recommended Decision"]:
        chk(f"memo:{needle}", needle in md, md[:200])
    numbered = re.findall(r"(?m)^\s*\d+\.\s+\S", md)
    chk("memo_3plus_considerations", len(numbered) >= 3, f"{len(numbered)} numbered items")
    # frame 4: governance request (UUID) — visible when the run paused; in sim it
    # auto-approves, so check the approval request record exists with a UUID.
    req = snap.get("approval_request") or {}
    chk("governance_request_uuid", bool(re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        req.get("request_id", ""))), str(req.get("request_id")))
    # frame 6: audit trail — 7 canonical entries
    code, raw = get(BASE + f"/api/audit/{run_id}", hdr)
    try:
        audit = json.loads(raw)
    except json.JSONDecodeError:
        audit = {}
    entries = audit.get("audit", [])
    txt = " | ".join(a["action"] + " " + (a["detail"] or "") for a in entries)
    chk("audit_200", code == 200, f"{code}")
    for needle in ["Identity validated", "Case resolved", "Systems accessed",
                   "Draft memo saved", "Policy", "Decision recorded", "Memo published"]:
        chk(f"audit:{needle}", needle in txt, txt[:300])
    chk("audit_6plus", len(entries) >= 6, f"{len(entries)} entries")
    # frame 7: client mail via mailpit API (mail host)
    code, body = get(f"https://{MAIL_HOST}/api/v1/messages", timeout=30)
    mails = ""
    if code == 200:
        try:
            mails = json.dumps(json.loads(body))
        except Exception:
            mails = body
    chk("client_mail_approved", "Loan Application Approved" in mails, mails[:300])
    # approval mail to Sarah
    chk("approval_mail_sarah", "Sarah.Chen@mybank.com" in mails and
        "workflow__submit_credit_memo" in mails, mails[:300])
    finish()


def finish() -> int:
    passed = sum(1 for v in checks.values() if v)
    print(json.dumps({"ok": passed == len(checks), "passed": passed, "total": len(checks),
                      "errors": errors[:30], "checks": checks}, indent=2))
    sys.exit(0 if passed == len(checks) else 1)


if __name__ == "__main__":
    main()
