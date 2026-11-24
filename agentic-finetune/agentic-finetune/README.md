# agentic-finetune

An agent layer in front of LlamaFactory. Drive a complete fine-tuning loop
(discover datasets → train a LoRA → evaluate → register) from Open-WebUI by
calling MCP tools.

Each training run is spawned as its own Kubernetes Job using the LlamaFactory
image. This framework does not depend on the LlamaFactory chart being installed
— it only needs the LF image to be pullable from the cluster.

## Architecture

```
[Open-WebUI]  ──MCP/SSE──▶  [mcp-server]  ──HTTP localhost──▶  [orchestrator]
                                                                     │
                                                                     │ creates
                                                                     ▼
                                                              [Kubernetes Job]
                                                              (LF image, 1 GPU,
                                                               /workspace PVC)
```

Single Deployment with two sidecar containers (`orchestrator` + `mcp-server`),
one PVC mounted into both. Each training run is a transient `Job` that mounts
the same PVC.

## Prerequisites

- HPE AI Essentials 1.9.1 on PCAI
- Kubernetes >= 1.27
- A `StorageClass` capable of `ReadWriteOnce` (default: `gl4f-filesystem`)
- A GPU node with the NVIDIA device plugin (`nvidia.com/gpu`)
- Istio installed (PCAI default)
- The LlamaFactory image (`hiyouga/llamafactory:latest` or PCAI-bundled) pullable
- Open-WebUI installed (`frameworks/open-webui`) and configured for MCP

## Install

Two paths — UI or CLI. Both produce the same result.

### Via the AIE Tools & Frameworks UI

1. Package the chart:
   ```bash
   helm package charts/agentic-finetune
   ```
2. In AIE: **Tools & Frameworks → Import Framework**.
3. Fill in the wizard: name `agentic-finetune`, version `0.1.0`, category
   *Data Science*, upload `ezua/icon.png` as the logo, upload the packaged
   `.tgz` as the chart.
4. Choose a target namespace and install name. Set **Wait** to true so the
   UI blocks until all pods are Ready.
5. The framework tile appears in **Tools & Frameworks** when the install
   succeeds.

### Via the CLI (for dev / CI)

```bash
helm install agentic-finetune charts/agentic-finetune \
  --namespace agentic-finetune \
  --create-namespace \
  --wait --timeout 10m
```

The `${DOMAIN_NAME}` placeholder in `ezua.virtualService.endpoint` is substituted by
the AIE platform automatically. For CLI installs, override it explicitly:

```bash
helm install agentic-finetune charts/agentic-finetune \
  --namespace agentic-finetune \
  --create-namespace \
  --set ezua.virtualService.endpoint=agentic-finetune.your-cluster.example.com \
  --wait --timeout 10m
```

The chart prints a NOTES block with the MCP SSE URL on successful install.

## Connect Open-WebUI

In Open-WebUI:

1. **Settings → Tools → Add MCP Server**
2. Transport: **SSE**
3. URL: the value printed by the install (e.g. `https://agentic-finetune.example.pcai.local/sse`)
4. After connecting, the agent should enumerate six tools:
   `list_datasets`, `dataset_prep`, `launch_training`, `get_job_status`,
   `run_eval`, `register_model`.

## Verify the loop

In Open-WebUI chat:

> *What datasets are available for fine-tuning? Pick a small one and fine-tune
> Qwen2.5-3B on it. Tell me the result.*

The agent should:

1. Call `list_datasets` and see `alpaca_demo` (1k rows).
2. Call `dataset_prep` to validate.
3. Call `launch_training` — this creates a K8s `Job` named `ft-...`. Watch with:
   ```bash
   kubectl -n <ns> get jobs -l agentic-finetune/component=trainer -w
   ```
4. Poll `get_job_status` until the loss is reported.
5. Call `run_eval` against the trained adapter. Reports `passed/total`.
6. Call `register_model` to alias the adapter.

Typical end-to-end on one H100: ~5–8 minutes for 50 LoRA steps on `alpaca_demo`.

## Uninstall

```bash
helm uninstall agentic-finetune -n agentic-finetune
```

The workspace PVC is retained by default — it holds adapters, registry files,
and Job artifacts. Delete it manually if you want a clean slate:

```bash
kubectl -n agentic-finetune delete pvc agentic-finetune-workspace
```

## Key values

| Key | Default | Purpose |
|---|---|---|
| `workspace.pvc.storageClassName` | `gl4f-filesystem` | StorageClass for the shared PVC. PCAI default. |
| `workspace.pvc.size` | `50Gi` | Adapter + dataset working set. |
| `workspace.pvc.waitJob.enabled` | `true` | Pre-install hook that defeats the PVC race condition. **Leave on.** |
| `orchestrator.defaults.baseModel` | `Qwen/Qwen2.5-3B-Instruct` | Default student. The agent can override per call. |
| `trainingJob.gpu.count` | `1` | GPUs per training Job. requests == limits enforced. |
| `trainingJob.activeDeadlineSeconds` | `7200` | Hard cap per training run. |
| `ezua.virtualService.endpoint` | `agentic-finetune.${DOMAIN_NAME}` | Hostname; `${DOMAIN_NAME}` substituted by the AIE platform. |
| `ezua.virtualService.istioGateway` | `istio-system/ezaf-gateway` | Same gateway the LF chart uses. |
| `ezua.virtualService.timeout` | `"86400s"` | SSE long-poll — matches LF chart convention. |

See `values.yaml` for the full surface.

## PCAI-specific notes

See [porting.md](porting.md) for the deviations and gotchas that shaped this chart.
