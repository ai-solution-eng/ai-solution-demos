"""Local e2e scenario suite (S1-S8). Boots engine + mcp-server + SMTP sink in-process,
runs the full governed workflow via the engine HTTP API, asserts outcomes.

Usage:  python e2e/run_e2e.py            (runs all scenarios)
        python e2e/run_e2e.py S1 S4      (runs a subset)
Exit 0 = all pass. Prints a JSON summary.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.request
import urllib.error

SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # source_code/
REPO_ROOT = os.path.dirname(SRC_ROOT)                                     # repo root
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, SRC_ROOT)
sys.path.insert(0, os.path.join(SRC_ROOT, "mcp-server"))

import uvicorn  # noqa: E402

ENGINE_PORT = 18180
MCP_PORT = 18100
SMTP_PORT = 18125
DATA = "/tmp/slvd-e2e-data"

os.environ["SLVD_DATA_DIR"] = DATA
os.environ["SLVD_MCP_BASE_URL"] = f"http://127.0.0.1:{MCP_PORT}"
os.environ["SLVD_MAIL_HOST"] = "127.0.0.1"
os.environ["SLVD_MAIL_PORT"] = str(SMTP_PORT)
os.environ["SLVD_AGENT_BACKEND"] = "stub"
os.environ["SLVD_BASE_URL"] = f"http://127.0.0.1:{ENGINE_PORT}"

import engine.api  # noqa: E402
import engine.store as store  # noqa: E402
import engine.security as sec  # noqa: E402
import server as mcp  # noqa: E402
from e2e.smtp_sink import SmtpSink  # noqa: E402


def _free(port: int) -> None:
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", port))
        raise RuntimeError(f"port {port} busy")
    except OSError:
        s.close()


def start_servers(sink: SmtpSink) -> None:
    _free(ENGINE_PORT); _free(MCP_PORT); _free(SMTP_PORT)
    sink.start()
    store.reset_for_tests()
    store.init_db()
    threading.Thread(target=lambda: uvicorn.run(engine.api.app, host="127.0.0.1",
                       port=ENGINE_PORT, log_level="warning"), daemon=True).start()
    threading.Thread(target=lambda: uvicorn.run(mcp.app, host="127.0.0.1",
                       port=MCP_PORT, log_level="warning"), daemon=True).start()
    _wait_health(f"http://127.0.0.1:{ENGINE_PORT}/healthz")
    _wait_health(f"http://127.0.0.1:{MCP_PORT}/healthz")


def _wait_health(url: str, tries: int = 50) -> None:
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"health not ready: {url}")


def api(path: str, method: str = "GET", body: dict | None = None, token: str | None = None):
    url = path if path.startswith("http") else f"http://127.0.0.1:{ENGINE_PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data,
                                 method=method,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def login(u: str, p: str) -> str:
    _, b = api("/api/auth/login", "POST", {"username": u, "password": p})
    return b["token"]


def wait_terminal(run_id: str, token: str, timeout: int = 60) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        _, last = api(f"/api/runs/{run_id}", token=token)
        if last["run"]["status"] in ("COMPLETED", "REJECTED", "FAILED"):
            return last
        time.sleep(0.4)
    raise TimeoutError(f"run {run_id} not terminal in {timeout}s: {last['run']['status'] if last else '?'}")


def wait_awaiting(run_id: str, token: str, timeout: int = 30) -> dict:
    deadline = time.time() + timeout
    last_status = "PENDING"
    while time.time() < deadline:
        _, snap = api(f"/api/runs/{run_id}", token=token)
        last_status = snap["run"]["status"]
        if last_status == "AWAITING_APPROVAL":
            return snap
        if last_status in ("COMPLETED", "FAILED", "REJECTED"):
            break
        time.sleep(0.3)
    raise AssertionError(f"never reached AWAITING_APPROVAL (got {last_status})")


def _wait_status(run_id: str, token: str, wanted: set, timeout: int = 30) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        _, last = api(f"/api/runs/{run_id}", token=token)
        if last["run"]["status"] in wanted:
            return last
        time.sleep(0.3)
    raise TimeoutError(f"run {run_id} did not reach {wanted}; last={last['run']['status'] if last else '?'}")

def mail_for_request(sink: SmtpSink, request_id: str) -> dict | None:
    for m in sink.mails:
        if ("[ACTION REQUIRED]" in m.subject or "[Agent Platform] Access Request" in m.subject) \
                and request_id in m.body:
            return {"subject": m.subject, "body": m.body, "to": m.to, "from": m.from_}
    return None


def client_mail(sink: SmtpSink) -> dict | None:
    for m in sink.mails:
        if "Loan Application Approved" in m.subject or "Loan Renewal Approved" in m.subject:
            return {"subject": m.subject, "body": m.body}
    return None


def extract_link(body: str, decision: str, ttl: int) -> str:
    for line in body.split("\n"):
        if "href=" not in line or f"decision={decision}" not in line:
            continue
        seg = line.split("href=\"", 1)[1].split("\"", 1)[0]
        if f"ttl_hours={ttl}" in seg:
            return seg
    return ""


def signed_link(request_id: str, decision: str, ttl: int) -> str:
    exp = int(time.time()) + 3600
    return sec.build_approval_link(request_id, decision, ttl, exp)


# ---------------- scenarios ----------------

def s1_happy(sink, results, label):
    """Video path: $5M Acme -> approval email -> approve -> COMPLETED + official memo + client mail."""
    tok = login("nick", "analyst123")
    _, b = api("/api/runs", "POST", {"case_id": "CR-2026-00451", "amount_usd": 5_000_000}, tok)
    run_id = b["run_id"]
    snap = wait_awaiting(run_id, tok)
    req_id = snap["approval_request"]["request_id"]
    time.sleep(0.5)
    mail = mail_for_request(sink, req_id)
    assert mail, f"S1: no approval email for {req_id}"
    for needle in ["Approval request ID", "E102938", "credit-memo-agent",
                   "credit-memo-mcp/workflow__submit_credit_memo",
                   "workflow__submit_credit_memo", "Approve", "Reject",
                   "Credit Risk Portal", "24 hours", "WHY APPROVAL IS REQUIRED"]:
        assert needle in mail["body"], f"S1: approval mail missing {needle!r}"
    assert mail["to"] == "Sarah.Chen@mybank.com", f"S1: mail to {mail['to']}"
    link = extract_link(mail["body"], "approve", 24)
    assert link, "S1: no approve link in email"
    code, b2 = api(link)
    assert code == 200 and b2.get("status") == "approve", f"S1: approve link failed {code} {b2}"
    code2, b3 = api(link)
    assert code2 == 200 and b3.get("note") == "already recorded (idempotent)", f"S1: idempotency broken {b3}"
    snap = wait_terminal(run_id, tok)
    assert snap["run"]["status"] == "COMPLETED", f"S1: status {snap['run']['status']}"
    assert snap["run"]["approved_by"] == "Sarah Chen"
    assert snap["run"]["approval_role"] == "Senior Credit Officer"
    _, memo = api(f"/api/runs/{run_id}/memo", token=tok)
    assert "/official/credit/" in memo["path"], f"S1: memo not in official: {memo['path']}"
    for needle in ["Acme Industrial Holdings", "CL-77821", "$5,000,000", "76%", "BB",
                   "Revolving credit", "Recommended Decision"]:
        assert needle in memo["memo_md"], f"S1: memo missing {needle!r}"
    audit_txt = " | ".join(a["action"] + " " + (a["detail"] or "") for a in snap["audit"])
    for needle in ["Identity validated", "Case resolved", "Systems accessed",
                   "Draft memo saved", "Policy", "Approved by Sarah Chen", "Memo published"]:
        assert needle in audit_txt, f"S1: audit missing {needle!r}"
    deadline = time.time() + 5
    while time.time() < deadline and not client_mail(sink):
        time.sleep(0.3)
    cm = client_mail(sink)
    assert cm and "CR-2026-00451" in cm["body"], "S1: no client 'Loan Application Approved' mail"
    assert all(s["state"] == "done" for s in snap["steps"]), f"S1: steps {[(s['seq'], s['state']) for s in snap['steps']]}"
    results[label] = "PASS"


def s2_below_threshold(sink, results, label):
    tok = login("nick", "analyst123")
    before = len(sink.mails)
    _, b = api("/api/runs", "POST", {"case_id": "CR-2026-00452", "amount_usd": 2_000_000}, tok)
    snap = wait_terminal(b["run_id"], tok)
    assert snap["run"]["status"] == "COMPLETED", f"S2: {snap['run']['status']}"
    time.sleep(0.5)
    new_mails = sink.mails[before:]
    assert not any(m.subject.startswith("[Agent Platform] Access Request") for m in new_mails), \
        "S2: should be NO approval email for this run"
    audit_txt = " ".join(a["action"] for a in snap["audit"])
    assert "Policy" in audit_txt
    results[label] = "PASS"


def s3_kyc_pending_below(sink, results, label):
    tok = login("nick", "analyst123")
    _, b = api("/api/runs", "POST", {"case_id": "CR-2026-00453", "amount_usd": 2_000_000}, tok)
    snap = wait_awaiting(b["run_id"], tok)
    req_id = snap["approval_request"]["request_id"]
    code, _ = api(signed_link(req_id, "approve", 8))
    assert code == 200
    snap = wait_terminal(b["run_id"], tok)
    assert snap["run"]["status"] == "COMPLETED", f"S3: {snap['run']['status']}"
    # policy audit line should cite KYC pending (not amount)
    audit_txt = " ".join(a["detail"] or "" for a in snap["audit"])
    assert "KYC pending" in audit_txt, f"S3: audit should cite KYC pending: {audit_txt}"
    results[label] = "PASS"


def s4_reject(sink, results, label):
    tok = login("nick", "analyst123")
    _, b = api("/api/runs", "POST", {"case_id": "CR-2026-00451", "amount_usd": 5_000_000}, tok)
    run_id = b["run_id"]
    snap = wait_awaiting(run_id, tok)
    req_id = snap["approval_request"]["request_id"]
    time.sleep(0.5)
    mail = mail_for_request(sink, req_id)
    link = extract_link(mail["body"], "reject", 24)
    code, _ = api(link)
    assert code == 200, f"S4: reject click {code}"
    # Wait until the engine's background task has applied the rejection (terminal REJECTED).
    snap = _wait_status(run_id, tok, {"REJECTED"}, timeout=30)
    assert snap["run"]["status"] == "REJECTED", f"S4: {snap['run']['status']}"
    audit_txt = " ".join((a["action"] + " " + (a["detail"] or "")) for a in snap["audit"])
    assert "Rejected by" in audit_txt, f"S4: audit missing rejection: {audit_txt}"
    _, memob = api(f"/api/runs/{run_id}/memo", token=tok)
    assert "/official/" not in memob.get("path", ""), f"S4: should not publish official: {memob}"
    # Resubmit after a brief settle (engine just applied the decision).
    time.sleep(1.0)
    code, b2 = api(f"/api/approvals/{req_id}/resubmit", "POST", None, tok)
    assert code == 200, f"S4: resubmit {code} {b2}"
    new_req = b2["request_id"]
    code, _ = api(signed_link(new_req, "approve", 8))
    assert code == 200
    snap = _wait_status(run_id, tok, {"COMPLETED"}, timeout=30)
    assert snap["run"]["status"] == "COMPLETED", f"S4: after resubmit {snap['run']['status']}"
    results[label] = "PASS"


def s5_identity_failure(sink, results, label):
    code, _ = api("/api/runs", "POST", {"case_id": "CR-2026-00451", "amount_usd": 5_000_000}, "garbagetoken")
    assert code == 401, f"S5: expected 401 got {code}"
    code, b = api("/api/runs", "POST", {"case_id": "CR-9999-99999", "amount_usd": 5_000_000},
                  login("nick", "analyst123"))
    assert code == 404, f"S5: unknown case expected 404 got {code} {b}"
    code, b = api("/api/runs", "POST", {"case_id": "CR-2026-00451", "amount_usd": 0},
                  login("nick", "analyst123"))
    assert code == 422, f"S5: bad amount expected 422 got {code}"
    code, b = api("/api/auth/login", "POST", {"username": "nick", "password": "wrong"})
    assert code == 401, f"S5: bad login expected 401 got {code}"
    results[label] = "PASS"


def s6_link_tamper(sink, results, label):
    tok = login("nick", "analyst123")
    _, b = api("/api/runs", "POST", {"case_id": "CR-2026-00451", "amount_usd": 5_000_000}, tok)
    run_id = b["run_id"]
    snap = wait_awaiting(run_id, tok)
    req_id = snap["approval_request"]["request_id"]
    exp = int(time.time()) + 3600
    bad = signed_link(req_id, "approve", 8).replace("sig=", "sig=deadbeef")
    code, _ = api(bad)
    assert code == 403, f"S6: tampered expected 403 got {code}"
    expired = sec.build_approval_link(req_id, "approve", 8, int(time.time()) - 10)
    code, _ = api(expired)
    assert code == 403, f"S6: expired expected 403 got {code}"
    _, req = api(f"/api/approvals/{req_id}", token=tok)
    assert req["status"] == "pending", f"S6: decision should stay pending, got {req['status']}"
    results[label] = "PASS"


def s7_agent_failure(sink, results, label):
    """MCP down mid-run -> run FAILED (no silent hang)."""
    import asyncio
    import engine.config as cfg
    import engine.workflow as wf
    from engine.security import Identity
    ident = Identity("nick", "Nick Johnson", "Risk Analyst", "E102938")
    with open(os.path.join(SRC_ROOT, "mockdata", "cases.json")) as f:
        case = json.load(f)["cases"]["CR-2026-00451"]
    orig = cfg.settings.mcp_base_url
    cfg.settings.mcp_base_url = "http://127.0.0.1:1"  # dead
    run_id = store.create_run("CR-2026-00451", "Nick Johnson", "E102938", 5_000_000)
    try:
        asyncio.run(wf._run_workflow(run_id, case, ident, 5_000_000))
    finally:
        cfg.settings.mcp_base_url = orig
    time.sleep(0.3)
    run = store.get_run(run_id)
    assert run["status"] == "FAILED", f"S7: expected FAILED got {run['status']}"
    assert run["fail_reason"], "S7: no fail_reason"
    results[label] = "PASS"


def s8_resubmit_after_complete(sink, results, label):
    """Re-submit after COMPLETED -> 409 (idempotency guard)."""
    tok = login("nick", "analyst123")
    _, b = api("/api/runs", "POST", {"case_id": "CR-2026-00451", "amount_usd": 5_000_000}, tok)
    run2 = b["run_id"]
    snap = wait_awaiting(run2, tok)
    req_id = snap["approval_request"]["request_id"]
    api(signed_link(req_id, "approve", 8))
    wait_terminal(run2, tok)
    code, b3 = api(f"/api/approvals/{req_id}/resubmit", "POST", None, tok)
    assert code == 409, f"S8: resubmit-after-complete expected 409 got {code} {b3}"
    results[label] = "PASS"


SCENARIOS = {
    "S1": s1_happy, "S2": s2_below_threshold, "S3": s3_kyc_pending_below,
    "S4": s4_reject, "S5": s5_identity_failure, "S6": s6_link_tamper,
    "S7": s7_agent_failure, "S8": s8_resubmit_after_complete,
}


def main(argv):
    want = [a for a in argv if a in SCENARIOS] or list(SCENARIOS)
    sink = SmtpSink(port=SMTP_PORT)
    start_servers(sink)
    results: dict[str, str] = {}
    try:
        for name in want:
            label = name
            try:
                SCENARIOS[name](sink, results, label)
            except Exception as e:  # noqa: BLE001
                results[label] = f"FAIL: {type(e).__name__}: {e}"
            print(f"[{label}] {results[label]}", flush=True)
    finally:
        sink.stop()
    passed = sum(1 for v in results.values() if v == "PASS")
    print(json.dumps({"passed": passed, "total": len(want), "results": results}))
    sys.exit(0 if passed == len(want) else 1)


if __name__ == "__main__":
    main(sys.argv[1:])
