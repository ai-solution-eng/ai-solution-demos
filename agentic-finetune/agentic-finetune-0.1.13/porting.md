# porting.md

PCAI-specific deviations and gotchas embedded in this chart. Each item is
documented so the next person doesn't have to re-discover it.

## 1. Istio VirtualService `timeout` must be a duration string

PCAI's Istio rejects `"0s"` (and a bare `0`). Use a duration string like
`"86400s"`. SSE long-poll connections from Open-WebUI's MCP client are
long-lived; setting a generous timeout avoids mid-stream disconnects. We match
the `frameworks/llama-factory` chart's 86400s value (which is set there for
Gradio WebSockets and is just as appropriate here for SSE).

- **Where:** `values.yaml` → `ezua.virtualService.timeout`
- **Default:** `"86400s"`
- **Symptom if wrong:** Istio admission rejects the VirtualService at apply
  time with a validation error.

## 2. GPU resources: `requests` MUST equal `limits` exactly

PCAI's Kyverno policy denies Job/Pod admission if `nvidia.com/gpu` requests
and limits differ (or if only one is set). CPU and memory follow normal K8s
semantics (request ≤ limit), but GPU is exact-match.

- **Where:** `templates/configmap-job-template.yaml` — the trainer container
  has both `requests` and `limits` set to `{{ .Values.trainingJob.gpu.count }}`.
- **Symptom if wrong:** Kyverno admission webhook denies the Job with a policy
  violation. The Job never even creates a Pod.

## 3. PVC race condition with the `gl4f-filesystem` StorageClass

Dynamic provisioning under `gl4f-filesystem` is slower than the Deployment's
default `PodScheduled` timing. Without intervention, the main Deployment's pod
comes up before the PVC is `Bound` and hangs in `ContainerCreating` with a
confusing `unable to attach volume` event.

The fix is a Helm `pre-install` hook running a tiny busybox Job that mounts the
PVC — the mount itself blocks until `Bound`, which is exactly the synchronization
we need.

- **Where:** `templates/pvc-wait-job.yaml`
- **Hook:** `pre-install,pre-upgrade`, weight `5`,
  `hook-delete-policy: before-hook-creation,hook-succeeded`
- **Toggleable:** `values.yaml` → `workspace.pvc.waitJob.enabled` (default `true`)
- **Note:** A Helm `pre-install` hook on the PVC alone is insufficient — Helm
  doesn't wait for `Bound`, only for the object to exist. The busybox Job's
  volume mount is what actually waits.

## 4. Explicit StorageClass — never rely on the default

Different PCAI clusters have different default `StorageClass` settings, and
relying on the cluster default has bitten us before. We always set
`storageClassName` explicitly.

- **Where:** `templates/pvc.yaml` + `values.yaml` → `workspace.pvc.storageClassName`
- **Default:** `gl4f-filesystem`

## 5. Kyverno compliance baked into the manifests

PCAI's Kyverno policy denies pods that:
- Run as root → all containers set `runAsNonRoot: true`, `runAsUser: 1000`.
- Allow privilege escalation → `allowPrivilegeEscalation: false`.
- Have any capabilities → `capabilities.drop: ["ALL"]`.
- Have a writable root filesystem → `readOnlyRootFilesystem: true` on both
  sidecars (with an `emptyDir` mounted at `/tmp` for any scratch).
- Lack a seccomp profile → `seccompProfile.type: RuntimeDefault` everywhere.
- Lack resource requests + limits → every container sets both.

One exception: the LlamaFactory image runs as root by default and writes to its
own paths. The trainer Job's container therefore does **not** set
`runAsNonRoot: true`. If your PCAI cluster's Kyverno policy denies even the
training Job for this reason, the workaround is to either rebuild the LF image
with a non-root user, or add a per-namespace policy exception for the
`agentic-finetune/component: trainer` label.

## 6. RBAC scoped to the release namespace

The orchestrator's ServiceAccount has a `Role` (not `ClusterRole`) with:
- `batch/jobs`: create, get, list, watch, delete
- `pods`, `pods/log`: get, list, watch
- `events`: get, list, watch

Deliberately **no** `pods/exec`, `pods/attach`, no cluster-wide permissions.
The orchestrator parses LF stdout via `pods/log` — it does not stream commands
into running pods.

- **Where:** `templates/rbac.yaml`

## 7. EZUA/BYOA endpoint convention — follows `frameworks/llama-factory`

The chart exposes its endpoint via a top-level `ezua:` block in `values.yaml`,
mirroring the convention used by `ai-solution-eng/frameworks/llama-factory/0.1.0`:

```yaml
ezua:
  virtualService:
    enabled: true
    endpoint: "agentic-finetune.${DOMAIN_NAME}"
    istioGateway: "istio-system/ezaf-gateway"
    timeout: "86400s"
  kyverno:
    enabled: true
    labels:
      sidecar.istio.io/inject: "true"
```

The `${DOMAIN_NAME}` placeholder is **substituted by the AIE platform at install time**
with the cluster's actual domain. Do NOT hand-replace it before uploading the chart.

The matching VirtualService template lives at `templates/ezua/virtualservice.yaml`
(under the `ezua/` subdirectory by convention), using
`apiVersion: networking.istio.io/v1beta1` to match the LF chart's installed pattern.

Istio sidecar injection is enabled via a **pod label**
(`sidecar.istio.io/inject: "true"`), driven by `ezua.kyverno.labels` —
this is the LF chart's pattern, not namespace-label-based injection.

## 8. Single Deployment with `Recreate` strategy

Both sidecars share `/workspace` which is `ReadWriteOnce`. A `RollingUpdate`
strategy would briefly try to schedule two pods on potentially different nodes,
fail to attach the PVC, and stall. `Recreate` is the correct choice for any
RWO-backed Deployment.

- **Where:** `templates/deployment.yaml` → `spec.strategy.type: Recreate`

## 9. ConfigMap-driven Job template, not Helm-templated at Job-creation time

The orchestrator renders Jobs at runtime by string-substituting `$((PLACEHOLDER))`
tokens in a ConfigMap. We deliberately do not use Helm to template each Job —
the orchestrator runs hundreds of Jobs over the lifetime of an install, and
each one is parameterized by request-time values, not chart values.

The `$(( ))` syntax is chosen because:
- It's not interpreted by Helm's Go templating (so the ConfigMap renders cleanly).
- It's not interpreted by bash (so a developer copy-pasting the file into a
  shell doesn't accidentally expand it).
- It's easy to grep for to find all placeholders.

- **Where:** `templates/configmap-job-template.yaml`

## 10. Image references

Three images are pulled by this chart:
- `images.orchestrator` — our FastAPI orchestrator.
- `images.mcpServer` — our FastMCP/SSE server.
- `images.llamaFactory` — the upstream LlamaFactory image. **Not deployed by
  this chart**; pulled by each spawned Job.

If your cluster is air-gapped, mirror all three to your internal registry
and override `images.*.repository` accordingly.

## 11. HPE corp proxy on training and eval Jobs

PCAI clusters on the HPE network route all external egress through
`hpeproxy.its.hpecorp.net:8080` (registered as an Istio ServiceEntry in
istio-system). Pods don't get egress to HuggingFace, Docker Hub, or anything
else by default — they have to honor the proxy env vars.

This chart sets `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` (and lowercase variants)
on training Jobs via `values.yaml` → `trainingJob.env`. The orchestrator
propagates those same vars onto the eval Job manifest at runtime so eval can
also reach HuggingFace if needed.

- **Where:** `values.yaml` → `trainingJob.env`
- **Symptom if wrong:** trainer container fails with
  `[Errno 101] Network is unreachable` when fetching HF model config.
- **For non-HPE clusters:** replace the proxy hostname with your cluster's
  egress proxy, or empty the list if you have direct internet.

## 12. EVAL_MODE=stub default

Real eval spawns a second GPU Job that loads the base model + LoRA adapter
for inference on three probes. That's 3-5 minutes per call and depends on
HF egress working from the eval Job. For D0 demos we ship `EVAL_MODE=stub`
as the default — instant synthetic 3/3 pass results, identical contract.

- **Where:** `values.yaml` → `orchestrator.env`
- **Flip to real:** set `orchestrator.env[0].value: "real"`
- **Caveat:** real eval inherits the same proxy env as the trainer Job; if
  trainer egress works, real eval should too.

## 13. Dataset seeding via initContainer (not a separate Job)

An initContainer on the main Deployment copies LlamaFactory's bundled datasets
(`/app/data/*` → `/workspace/data/*`) so `list_datasets` returns something on
first install. Idempotent: skips if `dataset_info.json` already exists.

- **Where:** `templates/deployment.yaml` → `spec.template.spec.initContainers`
- **Toggle:** `values.yaml` → `workspace.preSeed.enabled` (default `true`)
- **Disable** if you intend to mount your own datasets manually.

### Why an initContainer and not a separate Job

0.1.10 implemented this as a `helm.sh/hook: post-install` Job. That broke on
PCAI for a fundamental reason:

- The workspace PVC is `ReadWriteOnce` (the `gl4f-filesystem` StorageClass
  on AIE 1.10 does not offer RWX).
- The main Deployment's pod attaches the PVC at install time on whichever
  node the scheduler picks.
- The post-install seed Job's pod gets scheduled independently, often on a
  different node.
- Kubernetes refuses to attach the same RWO volume to two nodes
  simultaneously — the seed pod sits in `ContainerCreating` forever with
  a `Multi-Attach error for volume "..."` event.

The AIE platform's install timeout then fires, terminates the main pod,
the PVC releases, the platform rolls a new main pod, schedules a new seed
pod, and the cycle repeats indefinitely.

An initContainer avoids all of this: it runs in the same pod as the main
containers, so it attaches the PVC via the same volume mount, in the same
node, in the same pod lifecycle. No second pod, no separate Job lifecycle,
no Multi-Attach. The main containers don't start until the initContainer
exits 0.

### Why the initContainer runs as root

The LF image's `/app/data` is owned by uid 0. We need to `chown -R 1000:1000`
the copied files so the orchestrator (running as 1000) can read them. The
initContainer therefore sets `runAsNonRoot: false` and `runAsUser: 0` in
its own securityContext. Per-container security contexts override the
pod-level default, so the orchestrator/mcp-server/mcpo containers in the
same pod still run as 1000.

PCAI's Kyverno policies accept this pattern (the trainer Job uses the same
exception). If your cluster's policies are stricter, override
`workspace.preSeed.enabled` to `false` and seed `/workspace/data` manually.

### First-install cost

The initContainer uses the LF image (~10GB), so first install on a fresh
node takes the LF pull time before the main containers start. On subsequent
installs the image is cached (`imagePullPolicy: IfNotPresent`). The trainer
Job needs this image anyway, so we're paying the cost at install time
instead of on first fine-tune.


## 14. Workspace PVC must support ReadWriteMany

The orchestrator pod and every training Job pod mount the same workspace PVC,
typically on different nodes. The StorageClass therefore **must support
ReadWriteMany**. RWO causes a Multi-Attach error the moment the scheduler
places a training Job on a node other than the orchestrator's node.

This applies equally to the seed initContainer (same pod, fine on RWO) and
to the training/eval Jobs (different pods, breaks on RWO).

### Confirming RWX support on a PCAI cluster

```bash
# 1. Check the StorageClass provisioner. NFS-backed and Trident classes
#    typically support RWX. Block-storage (EBS, etc.) typically does not.
kubectl get storageclass <name> -o yaml

# 2. Trial-bind a small RWX PVC against the class.
cat <<'YAML' | kubectl apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: rwx-probe
  namespace: default
spec:
  accessModes: [ReadWriteMany]
  storageClassName: <name>
  resources:
    requests:
      storage: 1Gi
YAML
sleep 5
kubectl get pvc rwx-probe -n default
# Bound = RWX works. Pending with "access mode not supported" event = RWX does not.
kubectl delete pvc rwx-probe -n default
```

On AIE clusters using `gl4f-filesystem` (VAST CSI over NFS), RWX works.

### Upgrading from chart < 0.1.13

Charts 0.1.0 through 0.1.12 created the workspace PVC with `ReadWriteOnce`.
PVC spec is immutable for bound claims, so a `helm upgrade` from those
versions to 0.1.13+ will fail with:

```
PersistentVolumeClaim "..." is invalid: spec: Forbidden:
spec is immutable after creation except resources.requests
and volumeAttributesClassName for bound claims
```

**Manual upgrade procedure:**

```bash
# 1. Uninstall the existing release. If the chart < 0.1.13 doesn't carry the
#    helm.sh/resource-policy=keep annotation, this also deletes the PVC.
#    (If it does have the annotation, delete the PVC explicitly in step 2.)

# 2. Confirm the old PVC is gone. If it still exists, delete it manually:
kubectl delete pvc agentic-finetune-workspace -n agentic-finetune --ignore-not-found

# 3. Install chart 0.1.13. A fresh PVC is created with ReadWriteMany.
#    The seed initContainer repopulates /workspace/data automatically.
```

The HuggingFace model cache is lost in this upgrade. Re-downloading on the
first training run takes ~5 minutes for Qwen2.5-3B. There is no way around
this — RWO and RWX volumes are physically different objects on the storage
backend.

### What's deleted vs. kept on uninstall going forward

Chart 0.1.13+ annotates the PVC with `helm.sh/resource-policy: keep`, so:

- `helm uninstall agentic-finetune` removes the Deployment, Service,
  ConfigMaps, RBAC, ClusterPolicy, and VirtualService.
- The workspace PVC **survives**. HF cache, jobs.json, models.json, and
  any registered adapters are preserved.
- To wipe the workspace: `kubectl delete pvc agentic-finetune-workspace -n agentic-finetune`,
  or set `workspace.pvc.keepOnUninstall: false` before uninstall.
