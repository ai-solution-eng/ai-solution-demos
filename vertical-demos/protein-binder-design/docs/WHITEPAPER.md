# De Novo Protein Binder Design with NVIDIA BioNeMo NIM on HPE Private Cloud AI

**Authors:** Francesco Calivà (francesco.caliva@hpe.com)
**Date:** July 2026  
**Version:** 1.0

*This document was prepared using HPE Private Cloud AI and NVIDIA AI Enterprise software. The application was developed with assistance from DeepSeek V4 Flash and OpenCode AI coding assistant.*

---

## Abstract

We present a production-grade web application for **de novo protein binder design** that combines three NVIDIA BioNeMo NIM microservices — RFdiffusion, ProteinMPNN, and OpenFold3 — into an end-to-end generative pipeline. Deployed on **HPE Private Cloud AI (PCAI)** via a Helm chart, the application provides an interactive 3D-enabled interface for drug discovery researchers to design novel protein binders against therapeutic targets. This whitepaper describes both the **scientific foundation** of the generative protein design pipeline and the **computational architecture** enabling its secure, scalable deployment in enterprise private cloud environments.

---

## 1. Introduction

### 1.1 The Drug Discovery Challenge

Developing a new therapeutic drug typically takes 10–15 years and costs over $2 billion. A critical bottleneck is the identification of **protein binders** — molecules that bind specifically to a disease-relevant protein target to modulate its function. Traditional approaches rely on high-throughput screening of large chemical libraries (small molecules) or phage display of candidate protein libraries, both of which are slow, expensive, and limited in chemical space explored.

### 1.2 AI-Driven Protein Design

Recent advances in generative AI and protein structure prediction have opened a new paradigm: **computational de novo protein design**. Instead of screening existing molecules, we can now generate entirely new protein sequences optimized to bind a target of interest. This approach promises to compress the discovery timeline from years to weeks.

The NVIDIA **BioNeMo** platform provides a suite of foundation models for biology, packaged as **NVIDIA NIM** (NVIDIA Inference Microservices) — containerized, optimized inference endpoints that can run in the cloud or on-premises.

### 1.3 This Work

We integrate three BioNeMo NIMs into a unified pipeline:

| Stage | NIM | Function | Scientific Task |
|-------|-----|----------|-----------------|
| 1 | **RFdiffusion** | Backbone generation | Diffuse novel protein backbones that bind to a target |
| 2 | **ProteinMPNN** | Sequence design | Predict amino acid sequences for each backbone |
| 3 | **OpenFold3** | Structure validation | Co-fold binder + target, compute confidence scores |

The application is deployed on **HPE Private Cloud AI**, a turnkey AI infrastructure solution that combines HPE GreenLake, NVIDIA accelerated computing, and the AI Essentials software platform.

---

## 2. Scientific Background

### 2.1 Protein-Protein Interactions and Binder Design

Proteins perform most biological functions through **physical interactions** with other molecules. A **protein binder** is a protein (or protein domain) engineered to bind a specific target protein with high affinity and specificity. Therapeutic applications include:

- **Immune checkpoint blockade**: Designing binders against PD-L1 (PDB: 4Z98) to block the PD-1/PD-L1 interaction in cancer immunotherapy
- **Antiviral therapeutics**: Engineering binders against ACE2 (PDB: 1R42) to inhibit SARS-CoV-2 viral entry
- **Anti-inflammatory biologics**: Designing TNFα (PDB: 3V6B) inhibitors for autoimmune disease treatment

The fundamental challenge is **sequence-structure mapping**: the amino acid sequence of a protein determines its 3D structure, which in turn determines its binding properties. De novo design must simultaneously solve the forward problem (sequence → structure → function) and the inverse problem (desired function → binding interface → sequence).

### 2.2 The Generative Pipeline

Our pipeline approaches this through a **three-stage generative process**:

```
Target PDB → Stage 1: RFdiffusion → Backbone PDB
                                    ↓
                           Stage 2: ProteinMPNN → Amino Acid Sequences
                                                      ↓
                           Stage 3: OpenFold3 → Predicted Complex + Scores
```

#### 2.2.1 Stage 1: RFdiffusion — Backbone Generation

**Scientific basis:** RFdiffusion (RoseTTAFold Diffusion) is a generative model that produces novel protein backbone structures. It is built on the **denoising diffusion probabilistic model** framework, similar to DALL-E or Stable Diffusion but operating on 3D protein coordinates rather than pixels.

**How it works:**
- The model is trained on tens of thousands of protein structures from the Protein Data Bank (PDB)
- During training, structures are progressively corrupted by adding Gaussian noise to atom coordinates
- The model learns to reverse this process: starting from random noise and iteratively denoising to produce valid protein structures
- For binder design, the model is **conditioned** on a target protein structure — it generates backbones that geometrically complement the target surface

**Key parameters:**
- **Contigs**: A domain-specific language specifying which parts of the target to keep and what kind of binder to generate. For example, `A5-97/A105-263/0 60-90` means "keep chain A residues 5-97 and 105-263, break the chain, and generate a new binder of length 60-90 amino acids."
- **Hotspot residues**: Specific residues on the target that the binder must contact, guiding the binder to a desired epitope
- **Diffusion steps**: The number of denoising iterations (1–50, default 50). More steps produce higher-quality structures at the cost of compute.

**Output:** A PDB file containing the target structure and the newly generated backbone for the binder.

#### 2.2.2 Stage 2: ProteinMPNN — Sequence Design

**Scientific basis:** ProteinMPNN (Protein Message Passing Neural Network) solves the **inverse folding problem**: given a protein backbone structure, predict the amino acid sequence that will fold into that structure. It is a graph neural network that operates on the protein's 3D structure graph.

**How it works:**
- The protein backbone is represented as a graph where nodes are residues and edges encode spatial relationships
- The network uses **message passing** — iterative information exchange between neighboring residues — to predict which amino acid is most likely at each position
- It considers both the backbone geometry (phi/psi angles, Cα distances) and sequence-level constraints
- Multiple sequences can be sampled by adjusting the **sampling temperature**, which controls the diversity of predictions

**Key parameters:**
- **Sampling temperature**: Lower values (e.g., 0.1) produce more conservative, native-like sequences; higher values (e.g., 1.0) produce more diverse sequences
- **Number of sequences per target**: How many alternative sequences to generate for each backbone
- **Soluble model flag**: When enabled, uses a model variant optimized for soluble (non-membrane) proteins

**Output:** A multi-FASTA file with designed sequences and per-sequence scores (log-probabilities). Lower scores indicate more native-like sequences that are more likely to fold correctly.

#### 2.2.3 Stage 3: OpenFold3 — Structure Validation

**Scientific basis:** OpenFold3 is a third-generation biomolecular structure prediction model, based on the AlphaFold3 architecture. It predicts the 3D structure of protein complexes with high accuracy. Here we use it to **co-fold** each designed binder sequence together with the target sequence, validating that the binder actually binds.

**Validation metrics:**
- **pLDDT** (predicted Local Distance Difference Test): A per-residue confidence score (0–100). Values above 70 indicate reliable predictions; above 90 indicate high confidence. We report the average pLDDT across all residues.
- **ipTM** (interface pTM): A score specifically measuring the confidence in the predicted interface between binder and target. Values above 0.8 suggest a high-confidence binding interface. This is the most important metric for binder quality assessment.

**Output:** A PDB file of the predicted binder-target complex and confidence scores.

### 2.3 Why This Pipeline Works

The three-stage design mirrors the natural protein folding hierarchy: backbone structure → sequence → folded complex. Separating these stages has several advantages:

1. **Computational efficiency**: RFdiffusion explores backbone space cheaply (minutes per backbone), while expensive co-folding (OpenFold3) is reserved for the most promising candidates
2. **Design flexibility**: Multiple sequences can be designed for each backbone, and multiple backbones can be generated for each target
3. **Quality control**: The funnel structure — generate many candidates, filter by score, validate the best — mirrors antibody discovery workflows familiar to biologists

---

## 3. Computational Architecture

### 3.1 System Overview

```
┌──────────────┐     ┌──────────────────────────┐     ┌──────────────────────────────┐
│   Browser    │     │   FastAPI Proxy Server    │     │   health.api.nvidia.com/v1/  │
│  index.html  │────▶│   (K8s Pod, PCAI)        │────▶│   biology/                   │
│  + 3Dmol.js  │     │                          │     │     ├── ipd/rfdiffusion/      │
│              │     │  - NVIDIA_API_KEY         │     │     ├── ipd/proteinmpnn/      │
│              │     │    injected via K8s Secret │     │     └── openfold/openfold3/  │
└──────────────┘     │  - PDB preprocessing      │     └──────────────────────────────┘
                     │  - Request proxying        │
                     └──────────────────────────┘
```

**Key architectural decisions:**

1. **Server-side API key**: The NVIDIA API key is stored as a Kubernetes Secret and injected via environment variable into the proxy container. The browser never has direct access to the key — all API calls go through the proxy, which adds the `Authorization: Bearer` header server-side.

2. **Stateless design**: The proxy server is stateless — it forwards requests and returns responses. PDB data is processed in memory only. This enables horizontal scaling and zero-downtime deployments.

3. **3Dmol.js for visualization**: The 3D viewer runs entirely in the browser using the 3Dmol.js library, which loads PDB data and renders protein structures using WebGL. No server-side rendering is needed.

### 3.2 Pipeline Implementation

#### 3.2.1 API Endpoints

The Python FastAPI server exposes these endpoints:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Serves the SPA (`index.html`) |
| `/api/pdb/{id}` | GET | Fetches PDB from RCSB (`files.rcsb.org`) |
| `/api/analyze` | POST | Analyzes PDB, returns chains, residue ranges, suggested contigs |
| `/api/rfdiffusion/generate` | POST | Single RFdiffusion call to NVIDIA API |
| `/api/rfdiffusion/generate_batch` | POST | Multiple parallel RFdiffusion calls |
| `/api/proteinmpnn/predict` | POST | ProteinMPNN sequence design |
| `/api/openfold3/predict` | POST | OpenFold3 structure prediction |

#### 3.2.2 PDB Preprocessing

Crystal structures from the PDB often contain features that interfere with AI models. Our server performs two critical preprocessing steps:

**MSE → MET conversion:** Many crystallographic structures use selenomethionine (MSE) for phasing. MSE is stored as a `HETATM` record in PDB format, but most AI models expect standard amino acids as `ATOM` records. Our `_clean_pdb()` function detects MSE and other modified residues and converts them to their standard amino acid equivalents (e.g., MSE→MET, CSO→CYS).

**Auto-contigs detection:** The server's `/api/analyze` endpoint parses the PDB to find all continuous residue ranges per chain. This handles PDBs with missing residues (disordered loops, expression tags) automatically, generating a valid contigs string without requiring the user to manually inspect the PDB file.

```python
# Example: PDB 7C7M has residues 5-97 and 105-263 (residues 98-104 are disordered)
# Auto-detected contigs: A5-97/A105-263/0 60-90
```

#### 3.2.3 Batch Generation

The `generate_batch` endpoint creates multiple backbone designs in parallel using `asyncio.gather`, with each call using a different random seed. This reduces wall-clock time when generating multiple designs. Error handling captures individual failures (e.g., rate limits, invalid contigs) so that one failed design doesn't abort the entire batch.

### 3.3 Deployment on HPE Private Cloud AI

#### 3.3.1 Containerization

The application is packaged as a single Docker container:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install fastapi uvicorn httpx
COPY server.py .
COPY static/ ./static/
EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
```

The image is built for `linux/amd64` to match PCAI cluster nodes (typically x86_64). Multi-stage builds and `--no-cache` flags ensure reproducible builds.

#### 3.3.2 Helm Chart

The Helm chart follows PCAI conventions and includes:

```
charts/
├── Chart.yaml              # Version, name, description
├── values.yaml             # Configurable parameters
└── templates/
    ├── _helpers.tpl        # Naming helpers
    ├── secret.yaml         # NVIDIA_API_KEY as K8s Secret
    ├── frontend-deployment.yaml
    ├── frontend-service.yaml
    ├── virtualService.yaml # Istio ingress via EZUA
    └── kyverno.yaml        # Vendor app labels
```

**Key configuration in `values.yaml`:**

```yaml
nvidia:
  apiKey: "nvapi-..."        # Set via K8s Secret (never hardcoded)

images:
  frontend:
    repository: "fcaliva/protein-binder-design"
    tag: "latest"

ezua:
  domainName: "${DOMAIN_NAME}"
  virtualService:
    endpoint: "protein-binder-design.${DOMAIN_NAME}"
    istioGateway: "istio-system/ezaf-gateway"
```

### 3.3 The 3Dmol.js Viewer

The 3D viewer uses the [3Dmol.js](https://3dmol.org/) library, which renders molecular structures using WebGL in the browser. Key features:

- **Multiple views**: Two tabs in the validation step — **Predicted Complex** and **Target**
- **Interactive controls**: Rotate, pan, zoom via mouse/touch
- **Chain-aware coloring**: Unlike the spectrum (rainbow) default, the viewer now colors chains distinctly — **binder chains** in cyan/blue, **target chains** in orange, making the binding interface visually obvious
- **Multi-design overlay**: Checkboxes in the validation results table let users select any subset of designs. When multiple checkboxes are active, each design's binder chain is shown overlaid on the target in a distinct color (cyan, lime, pink, gold, purple...), allowing direct visual comparison of different binding modes
- **No server-side rendering**: All computation happens in-browser via WebGL

---

## 4. Usage Guide

### 4.1 Local Development

```bash
cd ui/
pip install fastapi uvicorn httpx
export NVIDIA_API_KEY=nvapi-your-key
uvicorn server:app --host 0.0.0.0 --port 8080
```

Open `http://localhost:8080` in a browser.

### 4.2 Building and Pushing

```bash
docker build --platform linux/amd64 -t your-registry/protein-binder-design:latest ui/
docker push your-registry/protein-binder-design:latest
```

### 4.3 Deploying to PCAI

1. Update `charts/values.yaml` with the correct image tag and NVIDIA API key
2. Package the chart: `helm package charts/ -d charts/`
3. Import `charts/protein-binder-design-{VERSION}.tgz` via the AI Essentials "Import tools and frameworks" wizard
4. Configure the domain in the AI Essentials UI

### 4.4 Running a Demo

1. Open the application URL
2. Click a **Quick demo** button (e.g., **PD-L1 (4Z98)**) — the PDB is fetched, residue ranges are auto-detected, and contigs are auto-populated
3. Click **🎯 Generate Backbones** — RFdiffusion generates candidate backbones
4. Click **Next →** to advance to Step 2, select a backbone, click **🧬 Design Sequences**
5. Click **Next →** to advance to Step 3
6. Use the **Show** checkboxes in the validation table to select which designs to visualize
7. Click **🔬 Validate Selected (OpenFold3)** to co-fold each design with the target
8. Results appear with ipTM scores, pLDDT scores, and a 3D viewer
9. Toggle the two viewer tabs:
   - **Predicted Complex**: Shows the selected design(s) on the predicted target — binder in cyan, target in orange
   - **Target**: Shows just the original target PDB (rainbow spectrum)
10. Check multiple designs to overlay them simultaneously — each binder appears in a distinct color, allowing direct comparison of binding modes

---

## 5. Conclusion

### 5.1 Summary

We have demonstrated a production-ready deployment of the NVIDIA BioNeMo protein binder design blueprint on HPE Private Cloud AI. The pipeline combines three state-of-the-art generative AI models — RFdiffusion, ProteinMPNN, and OpenFold3 — into an intuitive web application that scientists can use without specialized computational expertise.

### 5.2 Key Benefits

- **Speed**: From target PDB to validated binder designs in minutes, not months
- **Accessibility**: No GPU infrastructure needed — computation runs on NVIDIA cloud NIMs
- **Security**: Enterprise-grade deployment with server-side API key management, corporate proxy support, and custom SSL certificates
- **Scalability**: Stateless architecture enables horizontal scaling
- **Reproducibility**: All parameters are user-configurable and the full pipeline is logged

### 5.3 Future Directions

- **Protein-DNA/RNA binding**: Extending the pipeline to design binders for nucleic acid targets
- **Enzyme design**: Using the same backbone → sequence → validate pipeline for catalytic site design
- **Multi-specific binders**: Designing binders that engage two or more targets simultaneously (e.g., bispecific antibodies)
- **Active learning**: Integrating experimental feedback to iteratively improve designs
- **Self-hosted NIMs**: Deploying the BioNeMo NIMs on PCAI GPU nodes for fully air-gapped operation
- **Combined viewer with structural alignment**: Reintroduce a third viewer tab that overlays the original (unbound) target structure onto each design's predicted complex using Kabsch RMSD alignment, revealing induced-fit conformational changes between bound and unbound states

---

## References

1. Watson, J.L., Juergens, D., et al. *De novo design of protein structure and function with RFdiffusion.* Nature 620, 1089–1100 (2023).
2. Dauparas, J., et al. *Robust deep learning-based protein sequence design using ProteinMPNN.* Science 378, 49–56 (2022).
3. Abramson, J., et al. *Accurate structure prediction of biomolecular interactions with AlphaFold 3.* Nature 630, 493–500 (2024).
4. Jumper, J., et al. *Highly accurate protein structure prediction with AlphaFold.* Nature 596, 583–589 (2021).
5. NVIDIA. *NVIDIA BioNeMo — Generative AI for Drug Discovery.* https://www.nvidia.com/en-us/clara/bionemo/
6. HPE. *HPE Private Cloud AI — Turnkey AI Infrastructure.* https://www.hpe.com/us/en/hpe-private-cloud-ai.html
7. Rego, N. & Koes, D. *3Dmol.js: molecular visualization with WebGL.* Bioinformatics 31, 1322–1324 (2015).

---

*This document was prepared using HPE Private Cloud AI and NVIDIA AI Enterprise software. The application was developed with assistance from DeepSeek V4 Flash and OpenCode AI coding assistant.*
