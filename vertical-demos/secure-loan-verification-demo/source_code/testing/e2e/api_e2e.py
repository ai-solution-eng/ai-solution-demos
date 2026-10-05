"""Drive the SLVD workflow end-to-end via the engine API (in-cluster).
Login -> create run (CR-2026-00451, $5M) -> poll steps/audit -> print final state.
"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8080"


def call(method, path, body=None, auth=""):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = "Bearer " + auth
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def main():
    code, d = call("POST", "/api/auth/login", {"username": "nick", "password": "analyst123"})
    assert code == 200, f"login failed {code}: {d}"
    jwt = d["token"]
    print("logged in as", d["user"]["user_id"], d["user"]["name"])

    code, d = call("POST", "/api/runs", {"case_id": "CR-2026-00451", "amount_usd": 5_000_000}, jwt)
    assert code == 200, f"create run failed {code}: {d}"
    run_id = d["run_id"]
    print("run created:", run_id, d.get("status"))

    t0 = time.time()
    last = ""
    while time.time() - t0 < 420:
        code, snap = call("GET", f"/api/runs/{run_id}", auth=jwt)
        run = snap["run"]
        steps = {s["seq"]: s["state"] for s in snap["steps"]}
        state = f"{run['status']} steps={steps}"
        if state != last:
            print(f"[{time.time()-t0:5.0f}s] {state}")
            last = state
        if run["status"] in ("COMPLETED", "REJECTED", "FAILED"):
            break
        time.sleep(5)

    code, snap = call("GET", f"/api/runs/{run_id}", auth=jwt)
    run = snap["run"]
    print("\n=== FINAL RUN ===")
    print("status:", run["status"])
    print("request_id:", run.get("request_id"))
    print("memo_draft_path:", run.get("memo_draft_path"))
    print("approved_by:", run.get("approved_by"), "| role:", run.get("approval_role"))
    print("\n=== STEPS ===")
    for s in snap["steps"]:
        print(f"  {s['seq']}. {s['name']} -> {s['state']}")
    print("\n=== AUDIT (last 20) ===")
    for a in snap["audit"][-20:]:
        print(f"  [{a['ts']}] {a['actor']}: {a['action']} {a.get('detail','')[:90]}")


if __name__ == "__main__":
    main()
