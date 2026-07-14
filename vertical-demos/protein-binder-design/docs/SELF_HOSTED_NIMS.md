# Self-Hosting NVIDIA BioNeMo NIMs on HPE Private Cloud AI

**Goal:** Deploy RFdiffusion, ProteinMPNN, and optionally OpenFold3 as local GPU-accelerated Kubernetes pods in PCAI, replacing cloud API calls for the protein binder design pipeline.

**Status:** 📋 Plan — waiting on NGC API key

---

## 1. Prerequisites

### 1.1 Hardware (PCAI r102 ld7 — Verified)

| Resource | Available | Allocated per NIM |
|----------|-----------|-------------------|
| **GPU** | 4× NVIDIA L40S (48 GB each) | RFdiffusion: 1 GPU, ProteinMPNN: 1 GPU, OpenFold3: 1-2 GPUs |
| **CPU** | 128 cores | RFdiffusion: 4, ProteinMPNN: 2, OpenFold3: 8-16 |
| **RAM** | 512 GB | RFdiffusion: 16 GB, ProteinMPNN: 8 GB, OpenFold3: 64 GB |
| **Storage** | NVMe SSDs | ~50 GB total for model weights + container images |

**Node:** `scs04.pcai0102.ld7.hpecolo.net` — ProLiant DL380a Gen11  
**Current CUDA Driver:** `570.148.08` (CUDA 12.8)  
**Container Runtime:** containerd 2.0.5-hpe1

> ⚠️ **Driver requirement for OpenFold3:** Requires NVIDIA Driver ≥ 580 (CUDA 13.0+). Current driver is 570. This is a **cluster-level change** — needs coordination with PCAI admins. RFdiffusion + ProteinMPNN work on the current driver.

### 1.2 Software (Cluster — Verified)

| Component | Status | Required |
|-----------|--------|----------|
| NVIDIA GPU Operator | ✅ Deployed | Multi-instance GPU management |
| NVIDIA Container Toolkit | ✅ Deployed (v1.13.5+) | GPU passthrough to containers |
| containerd | ✅ (2.0.5-hpe1) | Container runtime |
| Helm | ✅ | Chart deployment |
| Istio/EZUA | ✅ | Ingress |

### 1.3 Credentials Needed

| Credential | Purpose | How to Get |
|------------|---------|------------|
| **NGC API Key** | Pull NIM containers from `nvcr.io` | https://ngc.nvidia.com/setup/api-key |
| **NVCF API Key** | *(optional)* For cloud API fallback | Already have: `nvapi-...` |

> 🔑 The NGC API key is **different** from the NVIDIA cloud API key (`nvapi-...`). You need an NVIDIA AI Enterprise license to access the NIM containers on NGC.

---

## 2. Architecture

### 2.1 Current (Cloud API)

```
Browser → FastAPI Proxy → health.api.nvidia.com/v1/biology/
                                ├── /ipd/rfdiffusion/generate
                                ├── /ipd/proteinmpnn/predict
                                └── /openfold/openfold3/predict
```

### 2.2 Target (Self-Hosted + Hybrid)

```
Browser → FastAPI Proxy
                │
                ├── (NIM_MODE=local) → k8s Services → GPU Pods
                │        ├── svc/rfdiffusion:8000    → Pod (GPU 1)
                │        ├── svc/proteinmpnn:8000    → Pod (GPU 2)
                │        └── svc/openfold3:8000      → Pod (GPU 3) [optional]
                │
                └── (NIM_MODE=cloud) → health.api.nvidia.com [fallback]
```

### 2.3 Name Resolution

Services are resolved via K8s DNS:
- `protein-binder-design-rfdiffusion.protein-binder-design.svc.cluster.local`
- Short form: `protein-binder-design-rfdiffusion:8000`

---

## 3. Implementation Steps

### Step 1: Create NGC Pull Secret

**File:** `charts/templates/ngc-secret.yaml`

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: {{ include "protein-binder-design.fullname" . }}-ngc
type: kubernetes.io/dockerconfigjson
data:
  .dockerconfigjson: {{ printf "{\"auths\":{\"nvcr.io\":{\"username\":\"$oauthtoken\",\"password\":\"%s\",\"auth\":\"%s\"}}}" .Values.nims.ngcApiKey (printf "$oauthtoken:%s" .Values.nims.ngcApiKey | b64enc) | b64enc }}
```

**Usage in deployment:** `imagePullSecrets` references this secret.

### Step 2: Add NIM Configuration to `values.yaml`

```yaml
# ── Self-Hosted NIM Configuration ──────────────────────────
nims:
  enabled: false                # Set true to enable local NIMs
  ngcApiKey: ""                 # NGC API key (set via --set or K8s secret)
  mode: "hybrid"                # "all" = all local, "hybrid" = OpenFold3 cloud
  
  rfdiffusion:
    image: nvcr.io/nim/ipd/rfdiffusion
    tag: "2.3.0"
    port: 8000
    resources:
      requests: { cpu: "4", memory: "16Gi", nvidia.com/gpu: "1" }
      limits:   { cpu: "8", memory: "32Gi", nvidia.com/gpu: "1" }
  
  proteinmpnn:
    image: nvcr.io/nim/ipd/proteinmpnn
    tag: "1.1.0"
    port: 8000
    resources:
      requests: { cpu: "2", memory: "8Gi", nvidia.com/gpu: "1" }
      limits:   { cpu: "4", memory: "16Gi", nvidia.com/gpu: "1" }
  
  openfold3:
    enabled: false               # Requires driver ≥ 580
    image: nvcr.io/nim/openfold/openfold3
    tag: "1.4.0"
    port: 8000
    resources:
      requests: { cpu: "8", memory: "64Gi", nvidia.com/gpu: "1" }
      limits:   { cpu: "16", memory: "128Gi", nvidia.com/gpu: "2" }
```

### Step 3: Create NIM Deployment Templates

#### RFdiffusion Deployment
**File:** `charts/templates/rfdiffusion-deployment.yaml`

```yaml
{{- if .Values.nims.enabled }}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "protein-binder-design.fullname" . }}-rfdiffusion
  labels:
    app: rfdiffusion
    hpe-ezua/type: vendor-service
    hpe-ezua/app: {{ .Chart.Name }}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: rfdiffusion
  template:
    metadata:
      labels:
        app: rfdiffusion
        nvidia.com/gpu.workload: "true"
    spec:
      imagePullSecrets:
        - name: {{ include "protein-binder-design.fullname" . }}-ngc
      nodeSelector:
        nvidia.com/gpu.product: NVIDIA-L40S
      containers:
        - name: rfdiffusion
          image: "{{ .Values.nims.rfdiffusion.image }}:{{ .Values.nims.rfdiffusion.tag }}"
          ports:
            - containerPort: {{ .Values.nims.rfdiffusion.port }}
          env:
            - name: NVIDIA_VISIBLE_DEVICES
              value: "0"  # Bind to specific GPU
          resources:
{{ toYaml .Values.nims.rfdiffusion.resources | indent 12 }}
          readinessProbe:
            httpGet:
              path: /v1/health/ready
              port: {{ .Values.nims.rfdiffusion.port }}
            initialDelaySeconds: 30
            periodSeconds: 15
          livenessProbe:
            httpGet:
              path: /v1/health/ready
              port: {{ .Values.nims.rfdiffusion.port }}
            initialDelaySeconds: 60
            periodSeconds: 30
{{- end }}
```

#### RFdiffusion Service
**File:** `charts/templates/rfdiffusion-service.yaml`

```yaml
{{- if .Values.nims.enabled }}
apiVersion: v1
kind: Service
metadata:
  name: {{ include "protein-binder-design.fullname" . }}-rfdiffusion
  labels:
    app: rfdiffusion
spec:
  type: ClusterIP
  selector:
    app: rfdiffusion
  ports:
    - name: http
      port: {{ .Values.nims.rfdiffusion.port }}
      targetPort: {{ .Values.nims.rfdiffusion.port }}
      protocol: TCP
{{- end }}
```

#### ProteinMPNN Deployment + Service
Same pattern as RFdiffusion. Create:
- `charts/templates/proteinmpnn-deployment.yaml`
- `charts/templates/proteinmpnn-service.yaml`

Copy the RFdiffusion templates and replace:
- `rfdiffusion` → `proteinmpnn`
- Image: `nvcr.io/nim/ipd/proteinmpnn:1.1.0`
- GPU binding: `NVIDIA_VISIBLE_DEVICES=1`

#### OpenFold3 Deployment + Service *(optional, needs driver ≥ 580)*

Same pattern with:
- Image: `nvcr.io/nim/openfold/openfold3:1.4.0`
- GPU binding: `NVIDIA_VISIBLE_DEVICES=2` (or dedicated GPU)
- Add NIM cache PVC for model weights (OpenFold3 needs ~30 GB):

```yaml
volumeMounts:
  - name: nim-cache
    mountPath: /opt/nim/.cache
volumes:
  - name: nim-cache
    persistentVolumeClaim:
      claimName: {{ include "protein-binder-design.fullname" . }}-nim-cache
```

### Step 4: Update `server.py` for Local NIM Mode

Add environment variable switching in `server.py`:

```python
import os

NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "")
NIM_MODE = os.environ.get("NIM_MODE", "cloud")  # "cloud" or "local"
HELM_RELEASE = os.environ.get("HELM_RELEASE", "protein-binder-design")

if NIM_MODE == "local":
    # Self-hosted NIMs via k8s service DNS
    RFDIFFUSION_URL = f"http://{HELM_RELEASE}-rfdiffusion:8000/biology/ipd/rfdiffusion/generate"
    PROTEINMPNN_URL = f"http://{HELM_RELEASE}-proteinmpnn:8000/biology/ipd/proteinmpnn/predict"
    NVIDIA_HEADERS = {"Content-Type": "application/json"}  # No auth needed locally
else:
    # Cloud API
    NVIDIA_BASE = "https://health.api.nvidia.com/v1/biology"
    RFDIFFUSION_URL = f"{NVIDIA_BASE}/ipd/rfdiffusion/generate"
    PROTEINMPNN_URL = f"{NVIDIA_BASE}/ipd/proteinmpnn/predict"
    NVIDIA_HEADERS = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
    }
```

Then update the `_nvidia_post` helper and the rfdiffusion/proteinmpnn endpoints to use these URLs instead of the generic `_nvidia_post` path.

### Step 5: Update `values.yaml` — Frontend Changes

Add the `NIM_MODE` env var to the frontend deployment:

```yaml
env:
  NIM_MODE: "local"        # or "cloud"
  HELM_RELEASE: "{{ .Release.Name }}"
```

And pass the NVIDIA API key only when in cloud mode (optional in local mode):

```yaml
{{- if ne .Values.nims.mode "local" }}
- name: NVIDIA_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "protein-binder-design.fullname" . }}-nvapi
      key: apiKey
{{- end }}
```

### Step 6: GPU Node Selector

The worker node `scs04` has the label:
```yaml
nvidia.com/gpu.product: NVIDIA-L40S
```

Use this as `nodeSelector` in NIM deployments to ensure they land on the GPU node. The frontend proxy (no GPU) can run on any node.

### Step 7: NIM Cache Persistent Volume

OpenFold3 needs a NIM cache. If deploying OpenFold3:

```yaml
# charts/templates/nim-cache-pvc.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{ include "protein-binder-design.fullname" . }}-nim-cache
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 50Gi
  storageClassName: vastdata-nfs  # or whatever is available
```

RFdiffusion and ProteinMPNN may also benefit from a cache, but they typically download weights on first start and cache in the container layer.

---

## 4. GPU Allocation Strategy

### 4.1 Dedicated GPU per NIM (Recommended)

```
GPU 0 → RFdiffusion     (~8 GB used, 40 GB free)
GPU 1 → ProteinMPNN     (~4 GB used, 44 GB free)
GPU 2 → OpenFold3*      (~24 GB used, 24 GB free)  [if driver upgraded]
GPU 3 → Unused          (available for other workloads)
```

Set `NVIDIA_VISIBLE_DEVICES=0`, `=1`, `=2` in each deployment to bind GPUs explicitly.

### 4.2 Shared GPU (If GPUs are scarce)

All three NIMs can share a single L40S if memory permits:
- Total estimated usage: ~36 GB of 48 GB
- Set `NVIDIA_VISIBLE_DEVICES=all` (or omit, letting the GPU operator manage)
- Use Kubernetes resource requests/limits per container

---

## 5. NIM API Compatibility

### 5.1 Endpoint Mapping

| NIM | Cloud Path | Local Path | Same Format? |
|-----|-----------|-----------|:------------:|
| RFdiffusion | `/v1/biology/ipd/rfdiffusion/generate` | `/biology/ipd/rfdiffusion/generate` | ✅ Identical |
| ProteinMPNN | `/v1/biology/ipd/proteinmpnn/predict` | `/biology/ipd/proteinmpnn/predict` | ✅ Identical |
| OpenFold3 | `/v1/biology/openfold/openfold3/predict` | `/biology/openfold/openfold3/predict` | ✅ Identical |

The request/response payloads are the same whether running locally or on the cloud. No frontend (index.html) changes needed — only the server-side proxy URL changes.

### 5.2 Health Check Endpoint

All local NIMs expose:
```
GET /v1/health/ready → {"status": "ready"}
```

This is used for readiness/liveness probes.

---

## 6. Testing Strategy

### 6.1 Smoke Test (Port-Forward)

```bash
# After deploying, test each NIM individually:
kubectl port-forward -n protein-binder-design svc/protein-binder-design-rfdiffusion 8001:8000
curl http://localhost:8001/v1/health/ready
```

### 6.2 Pipeline Test

Set `NIM_MODE=local` and run the full pipeline through the web UI:
1. Fetch PDB (still uses cloud/RCSB)
2. Generate backbones → should hit local RFdiffusion
3. Design sequences → should hit local ProteinMPNN
4. Validate → hits cloud OpenFold3 (or local if driver upgraded)

### 6.3 Rollback

Set `NIM_MODE=cloud` and the frontend immediately reverts to cloud APIs. No downtime.

---

## 7. Files to Create/Modify

### New Files (6)

| File | Content |
|------|---------|
| `charts/templates/ngc-secret.yaml` | Docker pull secret for nvcr.io |
| `charts/templates/rfdiffusion-deployment.yaml` | GPU deployment for RFdiffusion |
| `charts/templates/rfdiffusion-service.yaml` | ClusterIP service |
| `charts/templates/proteinmpnn-deployment.yaml` | GPU deployment for ProteinMPNN |
| `charts/templates/proteinmpnn-service.yaml` | ClusterIP service |
| `charts/templates/nim-cache-pvc.yaml` | *(optional)* PVC for OpenFold3 cache |

### Modified Files (2)

| File | Change |
|------|--------|
| `charts/values.yaml` | Add `nims:` section with image tags, GPU resources, NGC key |
| `ui/server.py` | Add `NIM_MODE` env var → route to local URLs or cloud URLs |

### No Changes Needed (3)

| File | Reason |
|------|--------|
| `ui/static/index.html` | All API calls go through server.py — frontend is agnostic |
| `charts/templates/frontend-deployment.yaml` | Add `NIM_MODE` env var (minor) |
| `Chart.yaml` | Version bump only |

---

## 8. OpenFold3 Driver Issue

OpenFold3 NIM requires **NVIDIA Driver ≥ 580**. Your cluster has **Driver 570**.

| Option | Pros | Cons |
|--------|------|------|
| **A) Hybrid** — RFdiffusion + ProteinMPNN local, OpenFold3 cloud | ✅ Works now, no driver change | Still need cloud for validation |
| **B) All local** — Request driver upgrade to ≥ 580 | ✅ Fully air-gapped | Requires PCAI admin coordination, node reboot |
| **C) All local + time-slicing** — Keep OpenFold3 on cloud while waiting for upgrade | ✅ Immediate benefit, no driver change | Partial cloud dependency |

**Recommendation:** Start with **Option A (hybrid)**. It works immediately, gives you the main benefit, and you can add OpenFold3 later when the driver is upgraded.

---

## 9. PCAI Import Instructions

After implementing, package the chart and import via AI Essentials:

```bash
# Update Chart.yaml version
sed -i '' 's/version: .*/version: 0.5.0/' charts/Chart.yaml

# Package
helm package charts/ -d charts/

# Import protein-binder-design-0.5.0.tgz via AI Essentials UI
```

When importing, set these parameters in the PCAI wizard:

| Parameter | Value |
|-----------|-------|
| `nims.enabled` | `true` |
| `nims.ngcApiKey` | *(your NGC key)* |
| `nims.mode` | `hybrid` |
| `images.frontend.tag` | `v0.5.0` |

---

## 10. Verification Checklist

After deployment, verify each component:

- [ ] NGC pull secret created: `kubectl get secret -n protein-binder-design protein-binder-design-ngc`
- [ ] RFdiffusion pod running: `kubectl get pods -n protein-binder-design -l app=rfdiffusion`
- [ ] RFdiffusion healthy: `kubectl exec -n protein-binder-design deploy/protein-binder-design-rfdiffusion -- curl -s localhost:8000/v1/health/ready`
- [ ] ProteinMPNN pod running: `kubectl get pods -n protein-binder-design -l app=proteinmpnn`
- [ ] ProteinMPNN healthy: `kubectl exec -n protein-binder-design deploy/protein-binder-design-proteinmpnn -- curl -s localhost:8000/v1/health/ready`
- [ ] Frontend `NIM_MODE=local` set: `kubectl exec -n protein-binder-design deploy/protein-binder-design-frontend -- env | grep NIM_MODE`
- [ ] Full pipeline test: PDB fetch → RFdiffusion → ProteinMPNN → OpenFold3 (cloud)
- [ ] Rollback test: `NIM_MODE=cloud` → verify cloud API fallback works

---

> **Estimated effort:** 1-2 days for implementation, testing, and deployment.  
> **Prerequisite blocker:** NGC API key from https://ngc.nvidia.com/setup/api-key  
> **When ready, set `nims.enabled: true` in values.yaml and run `helm upgrade --install`**
