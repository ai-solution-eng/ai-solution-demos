"""List recent SLVD runs + status (avoids redacted inline code)."""
import json
import urllib.request

BASE = "http://127.0.0.1:8080"


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=10))


def get(path, jwt=""):
    h = {"Content-Type": "application/json"}
    if jwt:
        h["Authorization"] = "Bearer " + jwt
    return json.load(urllib.request.urlopen(BASE + path, timeout=10))


d = post("/api/auth/login", {"username": "nick", "password": "analyst123"})
jwt = d[list(d.keys())[0]]  # first key is the jwt
runs = get("/api/runs", jwt)["runs"]
print("total runs:", len(runs))
for r in runs[:5]:
    print(r["run_id"], "|", r["status"], "|", r["case_id"],
          "| started", r["started_at"][-13:-1],
          "| request_id=", r.get("request_id"),
          "| approved_by=", r.get("approved_by"),
          "| fail=", (r.get("fail_reason") or "")[:60])
