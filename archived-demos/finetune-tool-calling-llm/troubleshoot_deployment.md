# NeMo Microservices 25.12.1 — PCAI Deployment Troubleshooting Guide

**Chart:** `nemo-microservices-25121` (PCAI-packaged)
**Namespace:** `nemo-microservices-25121`
**Platform:** HPE AI Essentials (AIE) 1.10.0 / PCAI
**GPU Class:** H200 (141GB VRAM → `+80GB` template tier)

---

## Issue 1: PCAI Admission Controller Rejects Duplicate App Identity

**Symptom:**
```
admission webhook "vezappconfig.kb.io" denied the request: EzAppConfig ... is invalid:
Spec.Name: Internal error: app release name already in use, UID: nemo-microservices.
Existing app nemo-microservices-25.12.0-1769613939844
```

**Root Cause:**
PCAI's admission controller tracks deployed applications by chart name from `Chart.yaml`. The new 25.12.1 chart used `name: nemo-microservices`, which collides with the existing 25.12.0 deployment.

**Solution:**
Rename the chart in `Chart.yaml` to a unique identifier:

```yaml
# Chart.yaml
name: nemo-microservices-25121  # was: nemo-microservices
```

Also update the archive root directory to match:
```bash
tar -czf nemo-microservices-25.12.1-pcai.tgz ... --transform='s/.../nemo-microservices-25121/'
```

---

## Issue 2: ClusterRole Conflict with Existing Deployment

**Symptom:**
```
Error: Unable to continue with install: ClusterRole "k8s-nim-operator-role" in namespace ""
exists and cannot be imported into the current release: invalid ownership metadata;
annotation validation error: key "meta.helm.sh/release-name" must equal
"nemo-microservices-25121": current value is "nemo-microservices"
```

**Root Cause:**
The `k8s-nim-operator` and `nemo-operator` subcharts create cluster-scoped resources (ClusterRoles, CRDs) that are already owned by the existing 25.12.0 Helm release. Helm enforces single-owner semantics on cluster-scoped resources — two releases cannot own the same ClusterRole.

**Solution:**
Disable both operator subcharts in `values.yaml`. The existing deployment's operators serve the entire cluster:

```yaml
# values.yaml
nim-operator:
  enabled: false

nemo-operator:
  enabled: false
```

**Important:** The operators are cluster-wide singletons. The existing deployment's NIM Operator and NeMo Operator will watch and serve NIMService/NemoTrainingJob CRs in all namespaces (or can be configured to do so via `watchAllNamespaces: true`).

---

## Issue 3: Missing Database Tables (`platformjob` Does Not Exist)

**Symptom:**
Core-controller enters CrashLoopBackOff. Core-api returns 500 errors:
```
asyncpg.exceptions.UndefinedTableError: relation "platformjob" does not exist
[SQL: SELECT count(platformjobstep.id) AS count_1 FROM platformjob JOIN ...]
```

The controller logs show:
```
nemo_microservices.InternalServerError: Internal Server Error
message: "Could not fetch job steps for scheduling"
message: "Loop 'job_scheduler' is unhealthy"
```

**Root Cause:**
The `nemo-core` subchart includes a Helm pre-install hook Job that runs Alembic database migrations. On PCAI, this hook Job was never created — likely because PCAI's deployment mechanism doesn't trigger Helm hooks, or the hook was filtered out during import. Without the migration, the `platformjob`, `platformjobstep`, and `platformjobattempt` tables are never created in the `jobs` database.

**Verification:**
```bash
# Confirm no migration jobs exist
kubectl get jobs -n nemo-microservices-25121
# Expected: "No resources found"

# Confirm tables are missing
kubectl exec <core-api-pod> -n nemo-microservices-25121 -- \
  python -c "
import os
for root, dirs, files in os.walk('/app'):
    for f in files:
        if 'alembic' in f.lower():
            print(os.path.join(root, f))
"
# Expected output:
#   /app/services/core/infrastructure/jobs/alembic.ini
#   /app/services/core/infrastructure/models/alembic.ini
```

**Solution:**
Run both Alembic migrations manually via `kubectl exec`. The core-api container is **distroless** (no `bash` or `sh`) — only `python` is available:

```bash
kubectl exec <core-api-pod> -n nemo-microservices-25121 -- \
  python -c "
from alembic.config import Config
from alembic import command

# Jobs migration — creates platformjob, platformjobstep, platformjobattempt tables
cfg = Config('/app/services/core/infrastructure/jobs/alembic.ini')
command.upgrade(cfg, 'head')
print('Jobs migration complete!')

# Models migration — creates model deployment tables
cfg2 = Config('/app/services/core/infrastructure/models/alembic.ini')
command.upgrade(cfg2, 'head')
print('Models migration complete!')
"
```

**Expected output:**
```
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade  -> b44b362814ea, add initial tables
INFO  [alembic.runtime.migration] Running upgrade b44b362814ea -> 2885e43caf06, attempts
Jobs migration complete!
INFO  [alembic.runtime.migration] Running upgrade  -> cac5d15995eb, Initial migration
INFO  [alembic.runtime.migration] Running upgrade cac5d15995eb -> 9b9f6beb0054, Add ModelDeploymentConfig ...
INFO  [alembic.runtime.migration] Running upgrade 9b9f6beb0054 -> c9a932a8ea13, add model_provider_id ...
INFO  [alembic.runtime.migration] Running upgrade c9a932a8ea13 -> b6aaebda9296, add served_models ...
Models migration complete!
```

After migrations complete, restart the core-controller:
```bash
kubectl delete pod -l app.kubernetes.io/name=nemo-core-controller -n nemo-microservices-25121
```

**Note:** The migrations are idempotent — running them again on an already-migrated database outputs "Context impl PostgresqlImpl / Will assume transactional DDL" with no upgrade steps.

---

## Issue 4: Customizer CrashLoopBackOff Due to NGC Filemap Fetch Timeout

**Symptom:**
Customizer pod starts successfully (Uvicorn running, database initialized, targets populated), then gets killed with exit code 143 (SIGTERM) after 3–7 minutes. Restart count climbs steadily.

Last log line before crash:
```
INFO tokio.rs:916] "nvidia/nemo/llama-3_1-8b-instruct-nemo:2.0": fetching filemap from:
https://api.ngc.nvidia.com/v2/org/nvidia/team/nemo/models/llama-3_1-8b-instruct-nemo/2.0/files
```

**Root Cause:**
During startup, the customizer fetches model filemaps from the NGC API for each model in the catalog. Each fetch takes ~4.5 minutes. With multiple models, total startup takes 9–15 minutes. The default liveness probe (`delay=30s, period=10s, failureThreshold=15`) kills the container after ~3 minutes of unresponsive health checks, well before the filemap fetches complete.

The NGC API calls may also time out entirely from within the PCAI network:
```
ConnectionError: tcp connect error, 54.203.64.172:443, Os { code: 110, kind: TimedOut,
message: "Connection timed out" }
```

When this happens, the customizer logs warnings but continues startup successfully — the affected model targets are simply skipped.

**Solution:**
Patch the customizer deployment to increase liveness and readiness probe tolerances:

```bash
kubectl patch deployment nemo-microservices-25121-customizer -n nemo-microservices-25121 \
  --type=json -p='[
    {"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/failureThreshold","value":60},
    {"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/initialDelaySeconds","value":300},
    {"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/failureThreshold","value":60},
    {"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/initialDelaySeconds","value":300}
  ]'
```

This gives the customizer 300s (5 min) before the first probe, then 60 × 10s = 10 more minutes of tolerance — 15 minutes total, enough for all NGC filemap fetches to complete or time out.

After patching, clean up old ReplicaSets:
```bash
# List customizer ReplicaSets
kubectl get rs -n nemo-microservices-25121 | grep customizer

# Scale down old ones (keep only the latest)
kubectl scale replicaset <old-replicaset-name> -n nemo-microservices-25121 --replicas=0
```

---

## Issue 5: Evaluator Container CrashLoopBackOff

**Symptom:**
Evaluator pod shows `1/2` ready — the `evaluator-internal` container runs fine (0 restarts), but the `evaluator` container enters CrashLoopBackOff.

**Root Cause:**
Two contributing factors:
1. The evaluator's startup includes an `evalfactory` runner that loads K8s config and initializes connections — this takes ~30 seconds.
2. The evaluator calls back to `http://nemo-core-api:8000` for `/v2/evaluation/jobs`, which was returning 500 errors due to the missing `platformjob` table (Issue 3).

The combination of slow startup + failing core-api callbacks triggers liveness probe failures.

**Solution:**
Apply the same liveness probe patch as the customizer:

```bash
kubectl patch deployment nemo-microservices-25121-evaluator -n nemo-microservices-25121 \
  --type=json -p='[
    {"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/failureThreshold","value":30},
    {"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/initialDelaySeconds","value":120}
  ]'
```

**Important:** Fix Issue 3 (database migrations) first. Without the `platformjob` table, the evaluator will continue to fail regardless of probe settings.

---

## Issue 6: Helm Install Fails with API Rate Limiter Error

**Symptom:**
```
Error: release nemo-microservices-25121 failed, and has been uninstalled due to atomic
being set: client rate limiter Wait returned an error: rate: Wait(n=1) would exceed
context deadline
```

**Root Cause:**
PCAI's deployment engine uses `helm install --atomic`, which rolls back and deletes the entire release if any resource creation exceeds the Kubernetes API server's rate limit. The NeMo chart creates ~200+ resources simultaneously, which can overwhelm the API server's rate limiter on busy clusters.

**Solution:**
Retry the deployment. This is a transient error — the second attempt usually succeeds because container images are cached on nodes from the first attempt, reducing the resource creation burst.

If it persists, consider disabling services not needed for the demo to reduce the resource count:
```yaml
# values.yaml — disable non-essential services
tags:
  safe-synthesizer: false
  data-designer: false
  auditor: false
```

---

## Issue 7: VirtualService Template References Non-Existent Helper

**Symptom:**
The Istio VirtualService may fail to render with a template error referencing `nemo-microservices.fullname`.

**Root Cause:**
The custom PCAI `ezua/virtualService.yaml` template was carried over from the 25.8.0 chart, which defined `nemo-microservices.fullname` in `_helpers.tpl`. The 25.12.x chart renamed all helpers to `nemo-platform.*`. The `nemo-microservices.fullname` helper no longer exists.

**Solution:**
Update the VirtualService template to use the correct helper:

```yaml
# templates/ezua/virtualService.yaml
# Change:
host: {{ include "nemo-microservices.fullname" . }}.{{ .Release.Namespace }}.svc.cluster.local
# To:
host: {{ include "nemo-platform.name" . }}.{{ .Release.Namespace }}.svc.cluster.local
```

---

## Issue 8: NemoEntityHandler CRD Not Found (Non-Blocking)

**Symptom:**
Customizer logs show 404 warnings during startup:
```
WARNING: customizer - Unable to Find NemoEntityHandler/dataset-downloader - (404)
WARNING: customizer - Unable to Find NemoEntityHandler/model-checkpoint-uploader - (404)
```

**Root Cause:**
The `nemo-operator` subchart was disabled (Issue 2), so its CRDs (`NemoEntityHandler`, `NemoTrainingJob`, etc.) are not installed in the cluster. The customizer tries to find these CRDs at startup but handles the 404 gracefully.

**Impact:** Non-blocking. The customizer operates normally without these CRDs. They are only needed for advanced model entity validation workflows.

**Solution:** No action required. If needed, the CRDs can be installed from the existing `nemo-microservices` deployment's operator, which serves the entire cluster.

---

## Issue 9: Studio UI "Unable to Load Projects" — VirtualService Missing Per-Service Routing

**Symptom:**
Studio UI loads at `https://<host>/studio/projects` but displays:
```
Loading Error
Unable to load Projects. Please try again.
```

**Root Cause:**
The PCAI ezua VirtualService template creates a single catch-all route that sends all traffic to one destination. However, NeMo Microservices 25.12.x is a multi-service architecture — each microservice has its own ClusterIP service with different ports. The Studio UI (running in the browser) makes API calls to multiple backend paths:

| API Path | Target Service | Port |
|---|---|---|
| `/studio/*` | `nemo-studio` | 3000 |
| `/v1/namespaces/*`, `/v1/projects/*`, `/v1/datasets/*`, `/v1/models/*` | `nemo-entity-store` | 8000 |
| `/v1/datastore/*` | `nemo-data-store` | 3000 |
| `/v1/customization/*` | `nemo-customizer` | 8000 |
| `/v1/evaluation/*` | `nemo-evaluator` | 7331 |
| `/v1/guardrail/*` | `nemo-guardrails` | 7331 |
| `/v1/deployment/*` | `nemo-deployment-management` | 8000 |
| `/v1/jobs/*`, `/v2/*` | `nemo-core-api` | 8000 |
| `/v1/chat/*`, `/v1/completions/*` (inference) | `nemo-nim-proxy` | 8000 |

Without path-based routing, all API calls hit `nemo-nim-proxy` (or a non-existent service), which can't serve Entity Store or Customizer endpoints.

**Solution:**
Replace the single-route VirtualService with a full path-based routing configuration. Replace `${DOMAIN_NAME}` with your actual PCAI domain:

```bash
kubectl apply -f - <<'EOF'
apiVersion: networking.istio.io/v1alpha3
kind: VirtualService
metadata:
  name: nemo-microservices-25121-nemo-microservices-25121-virtual-service
  namespace: nemo-microservices-25121
spec:
  gateways:
    - istio-system/ezaf-gateway
  hosts:
    - "nemo-microservices-25121.${DOMAIN_NAME}"
  http:
    # Studio UI
    - match:
        - uri:
            prefix: /studio
      route:
        - destination:
            host: nemo-studio.nemo-microservices-25121.svc.cluster.local
            port:
              number: 3000
    # Entity Store (namespaces, projects, models, datasets)
    - match:
        - uri:
            prefix: /v1/namespaces
      route:
        - destination:
            host: nemo-entity-store.nemo-microservices-25121.svc.cluster.local
            port:
              number: 8000
    - match:
        - uri:
            prefix: /v1/projects
      route:
        - destination:
            host: nemo-entity-store.nemo-microservices-25121.svc.cluster.local
            port:
              number: 8000
    - match:
        - uri:
            prefix: /v1/datasets
      route:
        - destination:
            host: nemo-entity-store.nemo-microservices-25121.svc.cluster.local
            port:
              number: 8000
    - match:
        - uri:
            prefix: /v1/models
      route:
        - destination:
            host: nemo-entity-store.nemo-microservices-25121.svc.cluster.local
            port:
              number: 8000
    # Data Store
    - match:
        - uri:
            prefix: /v1/datastore
      route:
        - destination:
            host: nemo-data-store.nemo-microservices-25121.svc.cluster.local
            port:
              number: 3000
    # Customizer
    - match:
        - uri:
            prefix: /v1/customization
      route:
        - destination:
            host: nemo-customizer.nemo-microservices-25121.svc.cluster.local
            port:
              number: 8000
    # Evaluator
    - match:
        - uri:
            prefix: /v1/evaluation
      route:
        - destination:
            host: nemo-evaluator.nemo-microservices-25121.svc.cluster.local
            port:
              number: 7331
    # Guardrails
    - match:
        - uri:
            prefix: /v1/guardrail
      route:
        - destination:
            host: nemo-guardrails.nemo-microservices-25121.svc.cluster.local
            port:
              number: 7331
    # Deployment Management
    - match:
        - uri:
            prefix: /v1/deployment
      route:
        - destination:
            host: nemo-deployment-management.nemo-microservices-25121.svc.cluster.local
            port:
              number: 8000
    # Core API (jobs, v2 endpoints)
    - match:
        - uri:
            prefix: /v1/jobs
      route:
        - destination:
            host: nemo-core-api.nemo-microservices-25121.svc.cluster.local
            port:
              number: 8000
    - match:
        - uri:
            prefix: /v2/
      route:
        - destination:
            host: nemo-core-api.nemo-microservices-25121.svc.cluster.local
            port:
              number: 8000
    # NIM Proxy (inference - catch-all)
    - match:
        - uri:
            prefix: /
      route:
        - destination:
            host: nemo-nim-proxy.nemo-microservices-25121.svc.cluster.local
            port:
              number: 8000
EOF
```

**Note:** The route order matters — Istio evaluates rules top-to-bottom, so specific prefixes must come before the catch-all `/` route at the bottom.

---

## Issue 10: Worker Nodes Have No Outbound Internet Access — Customizer Cannot Load Model Targets

**Symptom:**
Customizer starts successfully but loads zero targets and zero configs. Studio shows "No Models Found". Customizer logs show:

```
ConnectionError: tcp connect error, 54.188.206.109:443, Os { code: 110, kind: TimedOut }
```

or:

```
Network is unreachable (errno 101)
```

Fine-tuning jobs fail with:
```
CustomizationConfig(namespace=meta, name=llama-3.2-1b-instruct@v1.0.0+80GB) not found
```

**Root Cause:**
The PCAI worker/system nodes (where NeMo Microservices pods run) have no outbound HTTPS connectivity. The Jupyter notebook pods (on management nodes) DO have internet access — this is a split network topology.

**Verification:**
```python
# From Jupyter notebook — WORKS
import urllib.request, ssl
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
resp = urllib.request.urlopen("https://api.ngc.nvidia.com/v2/health", timeout=10, context=ctx)
print(f"Jupyter: {resp.status}")  # 401 = reachable

# From customizer pod — FAILS
kubectl exec <customizer-pod> -n nemo-microservices-25121 -- \
  python -c "
import urllib.request, ssl
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
try:
    resp = urllib.request.urlopen('https://api.ngc.nvidia.com/v2/health', timeout=10, context=ctx)
    print(f'OK {resp.status}')
except Exception as e:
    print(f'FAIL {e}')
"
```

**Solution:**
Request the infrastructure team to open outbound HTTPS (port 443) from worker nodes to:

| Endpoint | Purpose |
|---|---|
| `api.ngc.nvidia.com` | NGC API — model metadata and filemap validation |
| `nvcr.io` | NGC Container Registry — model weights download |
| `huggingface.co` | HuggingFace Hub — alternative model source |
| `cdn-lfs.hf.co` | HuggingFace large file storage |

Alternatively, if an HTTP proxy is available, configure it on the customizer pod:
```bash
kubectl set env deployment/nemo-microservices-25121-customizer \
  -n nemo-microservices-25121 \
  HTTP_PROXY=http://<proxy>:<port> \
  HTTPS_PROXY=http://<proxy>:<port> \
  NO_PROXY=.svc.cluster.local,10.0.0.0/8
```

---

## Issue 11: PCAI Internal SSL Certificates — SDK and API Calls Fail with CERTIFICATE_VERIFY_FAILED

**Symptom:**
All SDK and API calls from Jupyter notebooks fail with:
```
SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate
```

**Root Cause:**
PCAI uses internal/self-signed SSL certificates on the Istio gateway. Python's default CA bundle doesn't trust these certificates.

**Solution:**
Use the `pcai_setup.py` module (included in the demo package) which configures all three HTTP clients:

```python
# 1. NeMo Microservices SDK (httpx-based)
import httpx
nemo_client = NeMoMicroservices(
    base_url=NEMO_URL,
    inference_base_url=NIM_URL,
    http_client=httpx.Client(verify=False),
)

# 2. requests library
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
requests.post(url, data=data, verify=False)

# 3. huggingface_hub (httpx-based internally)
from huggingface_hub.utils._http import get_session
session = get_session()
session._transport = session._transport.__class__(verify=False)
```

---

## Issue 12: HfApi LFS Upload Returns HTTP 404 — Data Store LFS URL Scheme Mismatch

**Symptom:**
`hf_api.upload_file()` fails with:
```
404 Not Found for url 'http://nemo-microservices-25121.../finetuning-demos/xlam-ft-dataset.git/info/lfs/objects/...'
```

**Root Cause:**
The Data Store's Git LFS batch endpoint returns upload URLs using `http://` (matching the scheme of the incoming request), but the Istio gateway only accepts `https://`. The LFS upload goes to `http://` which either fails or hits the wrong backend.

**Solution:**
Use the internal cluster DNS for HfApi operations, bypassing Istio entirely:

```python
NDS_INTERNAL_URL = "http://nemo-data-store.nemo-microservices-25121.svc.cluster.local:3000"
hf_api = HfApi(endpoint=f"{NDS_INTERNAL_URL}/v1/hf")
```

This works because Jupyter pods run inside the same Kubernetes cluster and can reach services directly.

---

## Issue 13: NeMo Operator CrashLoopBackOff — Volcano Not Installed (Clean Cluster)

**Symptom:**
`nemo-operator-controller-manager` pod is `1/2` with one container in CrashLoopBackOff. Logs show:

```
ERROR   setup   unable to create controller   {"controller": "NemoTrainingJob",
  "error": "failed to get restmapping: no matches for kind \"PodGroup\" in version \"scheduling.volcano.sh/v1beta1\""}
```

**Root Cause:**
The NeMo Operator requires Volcano's `PodGroup` CRD for managing distributed training jobs. On clean clusters (no pre-existing NeMo deployment), Volcano is not installed. The 25.12.x chart has `volcano.enabled: false` by default — it expects Volcano to be installed separately.

**Solution:**
Install Volcano before deploying the chart (or after, then restart the operator):

```bash
kubectl apply -f https://raw.githubusercontent.com/volcano-sh/volcano/v1.9.0/installer/volcano-development.yaml

# Wait for Volcano to be ready
kubectl wait --for=condition=complete job/volcano-admission-init -n volcano-system --timeout=120s
kubectl rollout status deployment/volcano-admission -n volcano-system

# Restart the nemo-operator pod to pick up the new CRDs
kubectl delete pod -l app.kubernetes.io/name=nemo-operator -n nemo-microservices-25121
```

**Note:** This is only needed on clean clusters. Clusters with an existing NeMo Microservices deployment typically already have Volcano installed.

---

## Issue 14: Clean Cluster Deployment — Operators Must Be Enabled

**Symptom:**
Customizer crashes on startup with:
```
ERROR   Application startup failed. Exiting.
kubernetes.client.exceptions.ApiException: (404) Reason: Not Found
HTTP response body: 404 page not found
```

The customizer is trying to create `NemoEntityHandler` custom resources but the CRD doesn't exist.

**Root Cause:**
The PCAI chart was packaged with `nim-operator.enabled: false` and `nemo-operator.enabled: false` to avoid ClusterRole conflicts on clusters with an existing NeMo deployment. On clean clusters (no existing NeMo), the CRDs are never installed.

**Solution:**
Before importing the chart on a clean cluster, edit `values.yaml` to re-enable both operators:

```yaml
nim-operator:
  enabled: true

nemo-operator:
  enabled: true
```

Re-package and re-import the chart. The operators will install their CRDs and ClusterRoles.

**Decision matrix:**

| Cluster State | `nim-operator.enabled` | `nemo-operator.enabled` |
|---|---|---|
| Clean (no existing NeMo) | `true` | `true` |
| Shared (existing NeMo deployment) | `false` | `false` |

---

## Issue 15: NIMService authSecret Must Reference Opaque Secret with NGC_API_KEY

**Symptom:**
NIM pod stuck in `CreateContainerConfigError`:
```
Error: couldn't find key NGC_API_KEY in Secret nemo-microservices-25121/nvcrimagepullsecret
```

**Root Cause:**
The `authSecret` field in NIMService was set to `nvcrimagepullsecret`, which is a Docker pull secret (`.dockerconfigjson` format). NIM expects an Opaque secret containing the key `NGC_API_KEY`.

**Solution:**
Use `ngc-api` as the authSecret (created by the Helm chart with key `NGC_API_KEY`):
```bash
kubectl patch nimservice llama-3-2-1b-instruct -n nemo-microservices-25121 \
  --type=json -p='[{"op":"replace","path":"/spec/authSecret","value":"ngc-api"}]'
```

---

## Issue 16: NIM Starts Without LoRA Support — Wrong Profile Selected

**Symptom:**
NIM logs show:
```
Running NIM without LoRA. Only looking for compatible profiles that do not support LoRA.
Profile metadata: feat_lora: false
```
Fine-tuned LoRA model not discoverable via NIM.

**Root Cause:**
The `NIM_PEFT_SOURCE` environment variable was not set (or set without `http://` prefix), so NIM defaulted to a non-LoRA TensorRT-LLM profile.

**Solution:**
Set `NIM_PEFT_SOURCE` with the full `http://` URL to the Entity Store:
```yaml
spec:
  env:
    - name: NIM_PEFT_SOURCE
      value: "http://nemo-entity-store:8000"   # Must include http://
    - name: NIM_PEFT_REFRESH_INTERVAL
      value: "600"
```

NIM will auto-select a LoRA-compatible profile and synchronize LoRA adapters from the Entity Store.

---

## Issue 17: Training Job Stuck in Pending — GPU Not Available (Volcano)

**Symptom:**
`NemoTrainingJob` status shows:
```
message: '1/0 tasks in gang unschedulable: pod group is not ready, 1 minAvailable'
reason: NotEnoughResources
```
No training pod is created.

**Root Cause:**
All GPUs on the cluster are consumed by other workloads (NIM inference pods, Jupyter kernels with GPU, MLIS model deployments). Volcano's gang scheduler requires the full GPU allocation to be available before creating any pod in the group.

**Solution:**
Free up at least 1 GPU by pausing or scaling down other GPU workloads:
```bash
# Check GPU allocation
kubectl describe nodes | grep -A5 "nvidia.com/gpu"

# Check what's consuming GPUs
kubectl get pods --all-namespaces -o wide | grep -v Completed | grep -v NAME | while read ns pod rest; do
  gpu=$(kubectl get pod $pod -n $ns -o jsonpath='{.spec.containers[*].resources.requests.nvidia\.com/gpu}' 2>/dev/null)
  [ -n "$gpu" ] && echo "$ns/$pod: $gpu GPU(s)"
done
```

**Planning note:** The demo requires **at least 2 GPUs** — 1 for fine-tuning, 1 for NIM inference.

---

## Issue 18: Evaluator Returns 422 — Base Model Not Registered in Entity Store

**Symptom:**
```
UnprocessableEntityError: 422 - Model meta/llama-3.2-1b-instruct is not registered with Entity Store
```

**Root Cause:**
The evaluator checks the Entity Store for model metadata and inference endpoint configuration. The base model exists in NIM but wasn't registered as an entity in the Entity Store with an `api_endpoint`.

**Solution:**
Register the base model with the NIM endpoint:
```python
nemo_client.models.update(
    model_name="llama-3.2-1b-instruct",
    namespace="meta",
    api_endpoint={
        "url": "http://llama-3-2-1b-instruct:8000/v1",
        "model_id": "meta/llama-3.2-1b-instruct",
    },
)
```

Do the same for the fine-tuned model before running its evaluation.

---

## Pre-Deploy Prerequisites (Clean Cluster)

Before importing the chart, ensure these prerequisites are met:

```bash
# 1. Install Volcano (required for distributed training)
kubectl apply -f https://raw.githubusercontent.com/volcano-sh/volcano/v1.9.0/installer/volcano-development.yaml
kubectl wait --for=condition=complete job/volcano-admission-init -n volcano-system --timeout=120s
kubectl rollout status deployment/volcano-admission -n volcano-system

# 2. Verify Volcano is running
kubectl get pods -n volcano-system
# Expected: volcano-admission, volcano-controllers, volcano-scheduler all Running
```

## Post-Deploy Checklist

Run these commands immediately after PCAI deploys the chart:

```bash
NAMESPACE=nemo-microservices-25121
DOMAIN_NAME="<your-pcai-domain>"  # e.g., pcai-se-ai-application.hst.rdlabs.hpecorp.net

# 1. Patch liveness/readiness probes (before pods start crashing)
kubectl patch deployment nemo-microservices-25121-customizer -n $NAMESPACE \
  --type=json -p='[
    {"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/failureThreshold","value":60},
    {"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/initialDelaySeconds","value":300},
    {"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/failureThreshold","value":60},
    {"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/initialDelaySeconds","value":300}
  ]'

kubectl patch deployment nemo-microservices-25121-evaluator -n $NAMESPACE \
  --type=json -p='[
    {"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/failureThreshold","value":30},
    {"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/initialDelaySeconds","value":120}
  ]'

# 2. Wait for core-api to be running
kubectl get po -n $NAMESPACE | grep core-api
# Wait until STATUS = Running, READY = 1/1

# 3. Run database migrations
CORE_API_POD=$(kubectl get pod -n $NAMESPACE -o name | grep core-api | head -1)
kubectl exec $CORE_API_POD -n $NAMESPACE -- \
  python -c "
from alembic.config import Config
from alembic import command
cfg = Config('/app/services/core/infrastructure/jobs/alembic.ini')
command.upgrade(cfg, 'head')
print('Jobs migration complete!')
cfg2 = Config('/app/services/core/infrastructure/models/alembic.ini')
command.upgrade(cfg2, 'head')
print('Models migration complete!')
"

# 4. Restart core-controller to pick up new tables
kubectl delete pod $(kubectl get pod -n $NAMESPACE -o name | grep core-controller) -n $NAMESPACE

# 5. Clean up old customizer ReplicaSets
kubectl get rs -n $NAMESPACE | grep customizer | grep ' 0 ' | awk '{print $1}' | \
  xargs -I{} kubectl delete rs {} -n $NAMESPACE

# 6. Fix VirtualService routing for Studio and all microservice APIs
kubectl apply -f - <<VSEOF
apiVersion: networking.istio.io/v1alpha3
kind: VirtualService
metadata:
  name: nemo-microservices-25121-nemo-microservices-25121-virtual-service
  namespace: $NAMESPACE
spec:
  gateways:
    - istio-system/ezaf-gateway
  hosts:
    - "nemo-microservices-25121.$DOMAIN_NAME"
  http:
    - match:
        - uri:
            prefix: /studio
      route:
        - destination:
            host: nemo-studio.$NAMESPACE.svc.cluster.local
            port:
              number: 3000
    - match:
        - uri:
            prefix: /v1/namespaces
      route:
        - destination:
            host: nemo-entity-store.$NAMESPACE.svc.cluster.local
            port:
              number: 8000
    - match:
        - uri:
            prefix: /v1/projects
      route:
        - destination:
            host: nemo-entity-store.$NAMESPACE.svc.cluster.local
            port:
              number: 8000
    - match:
        - uri:
            prefix: /v1/datasets
      route:
        - destination:
            host: nemo-entity-store.$NAMESPACE.svc.cluster.local
            port:
              number: 8000
    - match:
        - uri:
            prefix: /v1/models
      route:
        - destination:
            host: nemo-entity-store.$NAMESPACE.svc.cluster.local
            port:
              number: 8000
    - match:
        - uri:
            prefix: /v1/datastore
      route:
        - destination:
            host: nemo-data-store.$NAMESPACE.svc.cluster.local
            port:
              number: 3000
    - match:
        - uri:
            prefix: /v1/customization
      route:
        - destination:
            host: nemo-customizer.$NAMESPACE.svc.cluster.local
            port:
              number: 8000
    - match:
        - uri:
            prefix: /v1/evaluation
      route:
        - destination:
            host: nemo-evaluator.$NAMESPACE.svc.cluster.local
            port:
              number: 7331
    - match:
        - uri:
            prefix: /v1/guardrail
      route:
        - destination:
            host: nemo-guardrails.$NAMESPACE.svc.cluster.local
            port:
              number: 7331
    - match:
        - uri:
            prefix: /v1/deployment
      route:
        - destination:
            host: nemo-deployment-management.$NAMESPACE.svc.cluster.local
            port:
              number: 8000
    - match:
        - uri:
            prefix: /v1/jobs
      route:
        - destination:
            host: nemo-core-api.$NAMESPACE.svc.cluster.local
            port:
              number: 8000
    - match:
        - uri:
            prefix: /v2/
      route:
        - destination:
            host: nemo-core-api.$NAMESPACE.svc.cluster.local
            port:
              number: 8000
    - match:
        - uri:
            prefix: /
      route:
        - destination:
            host: nemo-nim-proxy.$NAMESPACE.svc.cluster.local
            port:
              number: 8000
VSEOF

# 7. Wait 5 minutes and verify all pods are healthy
sleep 300
kubectl get po -n $NAMESPACE
# Expected: all pods 1/1 or 2/2 Running, zero CrashLoopBackOff

# 8. Verify Studio access
echo "Studio UI: https://nemo-microservices-25121.$DOMAIN_NAME/studio"
echo "API base:  https://nemo-microservices-25121.$DOMAIN_NAME"
```

---

## Key Technical Notes

- **Distroless containers:** The `nemo-core-api` container has no shell (`bash`, `sh`). All exec commands must use `python -c "..."` directly.
- **Database password:** The default postgres password is set via Kubernetes secrets (key `password` in secret `nemo-microservices-25121-customizerdb` etc.). For direct DB access: `env PGPASSWORD=<password> psql -U nemo -d jobs`.
- **Istio endpoint:** `nemo-microservices-25121.${DOMAIN_NAME}` — unique, does not collide with the existing `nemo-microservices` deployment.
- **Namespace reservation:** `nemo-platform` is reserved for NeMo Platform 26.3.0 when the Helm chart becomes publicly available.
- **VirtualService route order:** Istio evaluates HTTP match rules top-to-bottom. Specific path prefixes (`/studio`, `/v1/namespaces`, etc.) must be listed before the catch-all `/` route. The catch-all sends remaining traffic to NIM Proxy for inference endpoints.
