#!/usr/bin/env bash
# One fresh end-to-end run against the deployed portal (A5). Prints JSON summary.
# Works against the external VS URL (browser-reachable) — no internal bypass.
set -u
KUBE="${KUBE:?set KUBE=<kubeconfig path>}"
export KUBECONFIG="$KUBE"
K="kubectl"
NS=slvd
HOST="${SLVD_HOST:?set SLVD_HOST=slvd.<your-pcai-domain>}"
BASE="https://$HOST"
PASS=1

login() {
  curl -sk -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
    -d '{"username":"nick","password":"analyst123"}' | python3 -c "import json,sys; print(json.load(sys.stdin).get('token',''))"
}

wait_status() { # run_id token status timeout
  local run="$1" tok="$2" want="$3" t="${4:-60}"
  local deadline=$(( $(date +%s) + t ))
  while [ $(date +%s) -lt $deadline ]; do
    st=$(curl -sk "$BASE/api/runs/$run" -H "Authorization: Bearer $tok" \
      | python3 -c "import json,sys; print(json.load(sys.stdin)['run']['status'])")
    [ "$st" = "$want" ] && { echo "$st"; return 0; }
    sleep 1
  done
  echo "TIMEOUT($st)"; return 1
}

TOK=$(login)
[ -z "$TOK" ] && { echo '{"ok":false,"error":"login failed"}'; exit 1; }

RUN=$(curl -sk -X POST "$BASE/api/runs" -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"case_id":"CR-2026-00451","amount_usd":5000000}' | python3 -c "import json,sys; print(json.load(sys.stdin).get('run_id',''))")
echo "run_id=$RUN"
[ -z "$RUN" ] && { echo '{"ok":false,"error":"run start failed"}'; exit 1; }

# wait for awaiting approval (in production mode the email is real; sim mode auto-approves)
ST=$(wait_status "$RUN" "$TOK" "AWAITING_APPROVAL" 90)
if [ "$ST" = "AWAITING_APPROVAL" ]; then
  REQ=$(curl -sk "$BASE/api/runs/$RUN" -H "Authorization: Bearer $TOK" \
    | python3 -c "import json,sys; print((json.load(sys.stdin).get('approval_request') or {}).get('request_id',''))")
  echo "awaiting approval request=$REQ (production mode: approver clicks the email link)"
  ST=$(wait_status "$RUN" "$TOK" "COMPLETED" 120)
fi
[ "$ST" = "COMPLETED" ] || { echo "{\"ok\":false,\"error\":\"run status $ST\"}"; exit 1; }

SUMMARY=$(curl -sk "$BASE/api/runs/$RUN" -H "Authorization: Bearer $TOK")
MEMO=$(curl -sk "$BASE/api/runs/$RUN/memo" -H "Authorization: Bearer $TOK")
AUDIT=$(curl -sk "$BASE/api/audit/$RUN" -H "Authorization: Bearer $TOK")

python3 - "$SUMMARY" "$MEMO" "$AUDIT" <<'PY'
import json, sys
summary = json.loads(sys.argv[1]); memo = json.loads(sys.argv[2]); audit = json.loads(sys.argv[3])
run = summary["run"]
checks = {
  "status_completed": run["status"] == "COMPLETED",
  "approved_by": run.get("approved_by") in ("Sarah Chen", "simulation (auto)", "system (auto)"),
  "run_id": run["run_id"].startswith("RUN-"),
  "memo_official_path": "/official/credit/" in (run.get("memo_official_path") or memo.get("path") or ""),
  "memo_fields": all(n in memo.get("memo_md", "") for n in
                     ["Acme Industrial Holdings", "CL-77821", "$5,000,000", "76%", "BB",
                      "Revolving credit", "Recommended Decision"]),
  "audit_entries": len(audit.get("audit", [])) >= 6,
  "audit_published": any("Memo published" in a["action"] for a in audit.get("audit", [])),
  "steps_done": all(s["state"] == "done" for s in summary.get("steps", [])),
}
ok = all(checks.values())
print(json.dumps({"ok": ok, "run_id": run["run_id"], "checks": checks}, indent=2))
sys.exit(0 if ok else 1)
PY
exit $?
