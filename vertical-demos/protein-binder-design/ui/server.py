import os
import asyncio
import random
import logging
from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
import httpx

try:
    import gemmi
except ImportError:  # optional: only needed for mmCIF -> PDB conversion
    gemmi = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "")
NVIDIA_BASE = "https://health.api.nvidia.com/v1/biology"

# Folding NIM used for the validation stage.
# OpenFold3 (/openfold/openfold3/predict) currently returns HTTP 500 for every
# well-formed payload, so Boltz-2 is the default. Both are protein-complex
# folding models; Boltz-2 does not require a pre-computed MSA.
FOLD_NIM = os.environ.get("FOLD_NIM", "boltz2")
BOLTZ2_PATH = "/mit/boltz2/predict"
OPENFOLD3_PATH = "/openfold/openfold3/predict"

app = FastAPI(title="Protein Binder Design")

app.mount("/static", StaticFiles(directory="static"), name="static")

RCSB_HEADERS = {
    "User-Agent": "ProteinBinderDesign/1.0"
}

NVIDIA_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}

# Common modified residues found in PDB files (HETATM → standard ATOM)
_MODIFIED_RESIDUES = {
    'MSE': 'MET',  # selenomethionine → methionine
    'CSO': 'CYS', 'CSX': 'CYS', 'CSD': 'CYS', 'CSS': 'CYS', 'CSW': 'CYS',
    'KCX': 'LYS', 'MLY': 'LYS', 'M3L': 'LYS',
    'TPO': 'THR', 'SEP': 'SER', 'PTR': 'TYR', 'PHD': 'ASP',
    'HIC': 'HIS', 'HIP': 'HIS', 'HID': 'HIS', 'HIE': 'HIS',
    'ASH': 'ASP', 'GLH': 'GLU', 'CYX': 'CYS',
    'SNC': 'CYS', 'SOC': 'CYS',
}

def _clean_pdb(pdb_text: str) -> str:
    """Preprocess PDB for RFdiffusion: convert modified HETATM residues to standard ATOM."""
    if not pdb_text:
        return pdb_text
    lines_out = []
    for line in pdb_text.split('\n'):
        if line.startswith('HETATM') and len(line) > 20:
            resname = line[17:20].strip()
            if resname in _MODIFIED_RESIDUES:
                std = _MODIFIED_RESIDUES[resname]
                line = 'ATOM  ' + line[6:17] + f'{std:<3s}' + line[20:]
        lines_out.append(line)
    return '\n'.join(lines_out)

async def _nvidia_post(path: str, payload: dict, timeout: int = 600):
    if not NVIDIA_API_KEY:
        raise HTTPException(503, "NVIDIA_API_KEY not configured on server")
    url = f"{NVIDIA_BASE}{path}"
    headers = {**NVIDIA_HEADERS, "Authorization": f"Bearer {NVIDIA_API_KEY}"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=headers)
    if resp.status_code != 200:
        detail = resp.text[:1000]
        logger.error("NVIDIA API error %s: %s", resp.status_code, detail)
        raise HTTPException(resp.status_code, f"NVIDIA API error: {detail}")
    return resp.json()


def _cif_to_pdb(cif_text: str) -> str:
    """Convert an mmCIF structure to PDB text so the browser viewer keeps working."""
    if not cif_text:
        return ""
    if gemmi is None:
        logger.warning("gemmi not installed - returning mmCIF unconverted")
        return cif_text
    try:
        block = gemmi.cif.read_string(cif_text).sole_block()
        structure = gemmi.make_structure_from_block(block)
        return structure.make_pdb_string()
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("mmCIF->PDB conversion failed: %s", exc)
        return ""


def _first(value, default=None):
    """NIM score fields are lists (one entry per sample)."""
    if isinstance(value, list):
        return value[0] if value else default
    return value if value is not None else default

@app.get("/")
async def root():
    return FileResponse("static/index.html", headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

def _parse_pdb_ranges(pdb_text: str) -> dict:
    """Parse PDB to find chains and their continuous residue ranges."""
    pdb_text = _clean_pdb(pdb_text)
    chains = {}
    for line in pdb_text.split('\n'):
        if line.startswith('ATOM') and len(line) > 26:
            chain = line[21]
            try:
                res_num = int(line[22:27].strip())
            except ValueError:
                continue
            if chain not in chains:
                chains[chain] = set()
            chains[chain].add(res_num)
    result = {}
    for chain, nums in sorted(chains.items()):
        sorted_nums = sorted(nums)
        ranges = []
        start = sorted_nums[0]
        end = sorted_nums[0]
        for n in sorted_nums[1:]:
            if n == end + 1:
                end = n
            else:
                ranges.append(f"{chain}{start}-{end}" if start != end else f"{chain}{start}")
                start = n
                end = n
        ranges.append(f"{chain}{start}-{end}" if start != end else f"{chain}{start}")
        result[chain] = {
            "ranges": ranges,
            "count": len(sorted_nums),
            "contigs": "/".join(ranges),
        }
    return result

@app.post("/api/analyze")
async def analyze_pdb(request: Request):
    body = await request.json()
    pdb_text = body.get("pdb", "")
    if not pdb_text:
        raise HTTPException(400, "No PDB data provided")
    chains = _parse_pdb_ranges(pdb_text)
    main_chain = list(chains.keys())[0] if chains else "A"
    contigs = chains[main_chain]["contigs"] + "/0 60-90" if main_chain in chains else "A1-100/0 60-90"
    return {"chains": chains, "suggested_contigs": contigs}

@app.get("/api/pdb/{pdb_id:str}")
async def fetch_pdb(pdb_id: str):
    pdb_id = pdb_id.upper().strip()
    url = f"https://files.rcsb.org/download/{pdb_id}.pdb"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=RCSB_HEADERS)
    if resp.status_code != 200:
        raise HTTPException(404, f"PDB {pdb_id} not found on RCSB")
    return PlainTextResponse(resp.text, media_type="text/plain")

@app.post("/api/rfdiffusion/generate")
async def rfdiffusion_generate(request: Request):
    body = await request.json()
    if 'input_pdb' in body:
        body['input_pdb'] = _clean_pdb(body['input_pdb'])
    result = await _nvidia_post("/ipd/rfdiffusion/generate", body)
    return result

@app.post("/api/rfdiffusion/generate_batch")
async def rfdiffusion_generate_batch(request: Request):
    """Generate multiple backbones by looping the RFdiffusion call."""
    body = await request.json()
    if 'input_pdb' in body:
        body['input_pdb'] = _clean_pdb(body['input_pdb'])
    num_designs = int(body.pop("num_designs", 1))
    tasks = []
    async with httpx.AsyncClient(timeout=600) as client:
        url = f"{NVIDIA_BASE}/ipd/rfdiffusion/generate"
        headers = {**NVIDIA_HEADERS, "Authorization": f"Bearer {NVIDIA_API_KEY}"}
        for i in range(num_designs):
            payload = {**body}
            if "random_seed" not in payload:
                payload["random_seed"] = random.randint(1, 999999)
            tasks.append(client.post(url, json=payload, headers=headers))
        responses = await asyncio.gather(*tasks, return_exceptions=True)
    backbones = []
    for i, resp in enumerate(responses):
        if isinstance(resp, Exception):
            backbones.append({"id": f"bb_{i:03d}", "error": str(resp)})
        elif resp.status_code != 200:
            backbones.append({"id": f"bb_{i:03d}", "error": resp.text[:500]})
        else:
            data = resp.json()
            backbones.append({"id": f"bb_{i:03d}", "pdb": data.get("output_pdb", "")})
    return {"backbones": backbones}

@app.post("/api/proteinmpnn/predict")
async def proteinmpnn_predict(request: Request):
    body = await request.json()
    result = await _nvidia_post("/ipd/proteinmpnn/predict", body)
    return result

@app.post("/api/openfold3/predict")
async def openfold3_predict(request: Request):
    """Raw OpenFold3 passthrough (kept for debugging - currently HTTP 500 upstream)."""
    body = await request.json()
    result = await _nvidia_post(OPENFOLD3_PATH, body)
    return result

@app.post("/api/boltz2/predict")
async def boltz2_predict(request: Request):
    """Raw Boltz-2 passthrough."""
    body = await request.json()
    result = await _nvidia_post(BOLTZ2_PATH, body)
    return result

@app.post("/api/validate")
async def validate_design(request: Request):
    """Co-fold binder + target and return a normalised result.

    Accepts {"binder_sequence": str, "target_sequence": str} and returns
    {"structure": <PDB text>, "format": "pdb", "plddt": float, "iptm": float,
     "ptm": float, "model": str}.
    """
    body = await request.json()
    binder = (body.get("binder_sequence") or "").strip()
    target = (body.get("target_sequence") or "").strip()
    if not binder:
        raise HTTPException(400, "binder_sequence is required")

    if FOLD_NIM == "openfold3":
        # Legacy path: OpenFold3 wants pre-computed MSAs per chain.
        molecules = [{
            "type": "protein", "sequence": binder,
            "msa": {"unpaired": {"a3m": {"alignment": f">query\n{binder}\n", "format": "a3m"}}},
        }]
        if target:
            molecules.append({
                "type": "protein", "sequence": target,
                "msa": {"unpaired": {"a3m": {"alignment": f">query\n{target}\n", "format": "a3m"}}},
            })
        raw = await _nvidia_post(OPENFOLD3_PATH, {
            "inputs": [{"input_id": "design_0", "molecules": molecules, "output_format": "pdb"}],
        })
        sws = (raw.get("outputs") or [{}])[0].get("structures_with_scores") or [{}]
        sws = sws[0] if sws else {}
        return {
            "structure": sws.get("structure", ""),
            "format": "pdb",
            "plddt": _first(sws.get("complex_plddt_score")),
            "iptm": _first(sws.get("iptm_score")),
            "ptm": _first(sws.get("ptm_score")),
            "model": "openfold3",
        }

    # Default: Boltz-2. No MSA required - it runs its own.
    polymers = [{"id": "A", "molecule_type": "protein", "sequence": binder}]
    if target:
        polymers.append({"id": "B", "molecule_type": "protein", "sequence": target})

    raw = await _nvidia_post(BOLTZ2_PATH, {"polymers": polymers})

    structures = raw.get("structures") or []
    cif = structures[0].get("structure", "") if structures else ""
    plddt = _first(raw.get("complex_plddt_scores"))
    iptm = _first(raw.get("iptm_scores"))
    ptm = _first(raw.get("ptm_scores"))

    return {
        "structure": _cif_to_pdb(cif),
        "format": "pdb",
        # complex_plddt comes back on a 0-1 scale; present it 0-100 like pLDDT
        "plddt": (plddt * 100.0) if isinstance(plddt, (int, float)) else plddt,
        "iptm": iptm,
        "ptm": ptm,
        "model": "boltz2",
        "metrics": raw.get("metrics"),
    }
