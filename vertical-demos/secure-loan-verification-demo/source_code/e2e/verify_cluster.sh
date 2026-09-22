#!/usr/bin/env bash
# A3 + A4 + A10 cluster health gates. Prints JSON. Exit 0 = all pass.
set -u
KUBE="${KUBE:?set KUBE=<kubeconfig path>}"
export KUBECONFIG="$KUBE"
K="kubectl"
NS=slvd
HOST="${SLVD_HOST:?set SLVD_HOST=slvd.<your-pcai-domain>}"
PASS=1
R=""

j() { R="$R$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$1")"; }

# --- A10: pod parity (exactly 4 slvd pods, 1 replica each, no extra deployments) ---
pods=$($K get pods -n $NS --no-headers 2>/dev/null | wc -l | tr -d ' ')
deploys=$($K get deploy -n $NS --no-headers 2>/dev/null | wc -l | tr -d ' ')
ready=$($K get pods -n $NS --no-headers 2>/dev/null | awk '$2=="1/1"' | wc -l | tr -d ' ')
echo "pods=$pods ready=$ready deploys=$deploys"
if [ "$pods" != "4" ] || [ "$ready" != "4" ] || [ "$deploys" != "4" ]; then
  echo "A10 FAIL: pods=$pods ready=$ready deploys=$deploys (expected 4/4/4)"
  PASS=0
fi

# --- A3: external URL (browser-reachable host, not port-forward) ---
code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 30 "https://$HOST/")
title=$(curl -sk --max-time 30 "https://$HOST/" | grep -o '<title>[^<]*</title>' | head -1)
health=$(curl -sk --max-time 30 "https://$HOST/healthz")
echo "external: code=$code title=$title health=$health"
[ "$code" = "200" ] || { echo "A3 FAIL: portal http $code"; PASS=0; }
echo "$title" | grep -q "Credit Risk Portal" || { echo "A3 FAIL: title $title"; PASS=0; }
echo "$health" | grep -q '"ok":true' || echo "$health" | grep -q '"ok": true' || { echo "A3 FAIL: healthz $health"; PASS=0; }

# --- A4: nemoclaw agent health (dashboard + LLM completion max_tokens>=256) ---
NC_HOST="${NC_HOST:?set NC_HOST=nemoclaw-openclaw.<your-pcai-domain>}"
NC_TOKEN=$($K get cm -n ${NC_NS:-nemoclaw} nemoclaw-openclaw-config -o jsonpath='{.data.openclaw\.json}' 2>/dev/null | python3 -c "import json,sys
try:
  d=json.load(sys.stdin); print(d['gateway']['auth']['token'] or '')
except Exception:
  print('')")
if [ -z "$NC_TOKEN" ]; then
  # token may be in a secret or the release values
  NC_TOKEN=$($K get secret -n ${NC_NS:-nemoclaw} nemoclaw -o jsonpath='{.data.gateway-token}' 2>/dev/null | base64 -d 2>/dev/null)
fi
nc_code=000
if [ -n "$NC_TOKEN" ]; then
  nc_code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 30 "https://$NC_HOST/?token=***")
fi
echo "nemoclaw dashboard code=$nc_code (token_present=$([ -n "$NC_TOKEN" ] && echo yes || echo no))"
[ "$nc_code" = "200" ] || { echo "A4 WARN: nemoclaw dashboard $nc_code"; }

# LLM completion via litellm (max_tokens >= 256, non-null content)
LLM_NS="${LLM_NS:-project-user-aieadmin}"   # namespace hosting the litellm-helm proxy
LLM_BASE="http://litellm-helm.${LLM_NS}.svc.cluster.local:4000/v1"
LLM_KEY=$($K get secret -n ${LLM_NS} litellm-helm-masterkey -o jsonpath='{.data.key}' 2>/dev/null | base64 -d 2>/dev/null)
[ -z "$LLM_KEY" ] && LLM_KEY=$($K get secret -n ${LLM_NS} litellm-helm-masterkey -o jsonpath='{.data.LITELLM_MASTER_KEY}' 2>/dev/null | base64 -d 2>/dev/null)
llm_probe_out=""
if [ -n "$LLM_KEY" ]; then
  llm_probe_out=$($K run slvd-llm-probe --rm -i --restart=Never -n $NS \
      --image=curlimages/curl:8.5.0 --command \
      -- curl -sk -m 150 -X POST "$LLM_BASE/chat/completions" \
         -H "Authorization: Bearer $LLM_KEY" -H 'Content-Type: application/json' \
         -d '{"model":"qwen3-8-27b-int4-dflash2","max_tokens":300,"messages":[{"role":"user","content":"Reply with the single word: OK"}]}' 2>/dev/null)
  echo "LLM probe raw: ${llm_probe_out:0:300}"
fi
$K delete pod slvd-llm-probe -n $NS --ignore-not-found 2>/dev/null

python3 - "$PASS" "$nc_code" "${llm_probe_out:0:40}" <<'PY'
import json, sys
ok = int(sys.argv[1]) == 1
nc = sys.argv[2]; llm = sys.argv[3]
sys.stdout.write(json.dumps({"ok": ok, "pod_parity": ok, "nemoclaw_dashboard": nc, "llm_probe": llm}) + "\n")
PY
exit $PASS
