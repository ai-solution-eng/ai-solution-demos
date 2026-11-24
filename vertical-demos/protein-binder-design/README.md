# **Protein Binder Design** — NVIDIA BioNeMo NIM on HPE Private Cloud AI

| Role | Name | Email |
|---|---|---|
| Use Case Owner | Francesco Caliva | francesco.caliva@hpe.com |

## Overview

Interactive web application for **de novo protein binder design** using NVIDIA BioNeMo NIM microservices. Deployed on **HPE Private Cloud AI** via Helm chart.

### Pipeline

| Stage | NIM | Description |
|---|---|---|
| **1. Backbone Generation** | [RFdiffusion](https://build.nvidia.com/ipd/rfdiffusion) | Diffuses novel protein backbones that bind to a target |
| **2. Sequence Design** | [ProteinMPNN](https://build.nvidia.com/ipd/proteinmpnn) | Predicts amino acid sequences for each backbone |
| **3. Validation** | [Boltz-2](https://build.nvidia.com/mit/boltz2) | Co-folds binder + target to validate structure (no MSA required) |

> **Note on the validation NIM.** This stage originally used OpenFold3. The hosted
> OpenFold3 endpoint (`/v1/biology/openfold/openfold3/predict`) now returns
> `HTTP 500` for every well-formed payload — verified across single/two-chain
> inputs, query-only and multi-hit MSAs, and the documented `msa.unpaired.a3m`
> schema. Boltz-2 (`/v1/biology/mit/boltz2/predict`) is used instead: it is an
> AlphaFold3-class folding model, needs no pre-computed MSA, and returns pLDDT,
> pTM and ipTM. Set `FOLD_NIM=openfold3` to switch back if NVIDIA restores it.

### Prerequisites

- NVIDIA AI Enterprise API key (`nvapi-...`) with access to BioNeMo NIMs
- Docker (for local build)
- Helm (for PCAI deployment)
- HPE Private Cloud AI environment with Istio/EZUA

## Quick Start (Local)

```bash
cd protein-binder-design/ui

# Install dependencies
pip install fastapi uvicorn httpx

# Set your NVIDIA API key
export NVIDIA_API_KEY=nvapi-your-key-here

# Run the server
uvicorn server:app --host 0.0.0.0 --port 8080
```

Open http://localhost:8080 in your browser.

## Docker Build

```bash
cd protein-binder-design

# Build the container
docker build -t fcaliva/protein-binder-design:latest ui/

# Run locally
docker run --rm -p 8080:8080 \
  -e NVIDIA_API_KEY=nvapi-your-key-here \
  fcaliva/protein-binder-design:latest
```

## Deploy to HPE Private Cloud AI

### 1. Build and push the Docker image

```bash
docker build -t your-registry/protein-binder-design:latest ui/
docker push your-registry/protein-binder-design:latest
```

### 2. Set the image in `charts/values.yaml`

```yaml
images:
  frontend:
    repository: "your-registry/protein-binder-design"
    tag: "latest"
```

### 3. Set your NVIDIA API key

**Option A — In values.yaml (dev only):**

```yaml
nvidia:
  apiKey: "nvapi-your-key-here"
```

**Option B — Create a K8s secret (production):**

```bash
kubectl create secret generic protein-binder-design-nvapi \
  --from-literal=apiKey='nvapi-your-key-here'
```

Then set `nvidia.apiKey` to `""` in values.yaml (the secret will be created separately).

### 4. Import in AI Essentials

Within HPE Private Cloud AI AI Essentials, use the **Import tools and frameworks** wizard and import the Helm chart:

```bash
# Package the chart
helm package charts/ -d charts/

# Import the resulting .tgz via the AI Essentials UI
```

### 5. Configure EZUA domain

Set the domain in your PCAI deployment values:

```yaml
ezua:
  domainName: "your-domain.com"
  virtualService:
    endpoint: "protein-binder-design.your-domain.com"
```

## Project Structure

```
protein-binder-design/
├── ui/
│   ├── Dockerfile          # Container build
│   ├── server.py           # FastAPI proxy (NVIDIA API key stays server-side)
│   └── static/
│       ├── index.html      # SPA with 3Dmol.js + 3-stage pipeline UI
│       └── hpe-logo.jpg
├── charts/
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
│       ├── _helpers.tpl
│       ├── secret.yaml           # NVIDIA_API_KEY K8s Secret
│       ├── frontend-deployment.yaml
│       ├── frontend-service.yaml
│       ├── virtualService.yaml   # Istio ingress (EZUA)
│       └── kyverno.yaml          # Vendor app labels
├── .env.example
└── README.md
```

## Architecture

```
Browser (index.html + 3Dmol.js)
    │  POST /api/rfdiffusion/generate
    │  POST /api/proteinmpnn/predict
    │  POST /api/validate          (Boltz-2, normalised result)
    ▼
FastAPI Proxy (server.py)
    │  Authorization: Bearer $NVIDIA_API_KEY
    ▼
health.api.nvidia.com/v1/biology/
    ├── /ipd/rfdiffusion/generate    → Backbone PDB
    ├── /ipd/proteinmpnn/predict     → Sequences (FASTA)
    └── /mit/boltz2/predict          → Predicted complex (mmCIF, converted to PDB)
```

## Config Reference

| Environment Variable | Required | Default | Description |
|---|---|---|---|
| `NVIDIA_API_KEY` | Yes | — | NVIDIA AI Enterprise API key |
| `FOLD_NIM` | No | `boltz2` | Folding NIM for validation: `boltz2` or `openfold3` |
| `HTTPS_PROXY` | On locked-down clusters | — | The pod needs egress to `health.api.nvidia.com` and `files.rcsb.org`. Set `proxy.enabled=true` in values. |
| `SSL_CERT_DIR` | PCAI only | `/etc/ssl/certs` | Custom CA cert directory |

## Development Notes

This application was built with [DeepSeek V4 Flash](https://recipes.vllm.ai/deepseek-ai/DeepSeek-V4-Flash) using [OpenCode](https://opencode.ai/), deployed on HPE Private Cloud AI.

The NVIDIA API key is handled server-side and never exposed to the browser. All NIM calls are proxied through the FastAPI backend.
