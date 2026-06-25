#!/bin/bash
set -euxo pipefail

# Remove TensorFlow — not imported at inference, saves ~3GB in the image.
pip uninstall -y tensorflow tensorflow-macos tensorflow-metal 2>/dev/null || true

# Install Rust-based download accelerator for faster model downloads.
pip install -q hf-transfer 2>/dev/null || true

# Pre-cache all models at build time so inference is instant at runtime.
python -c "
import os
from mammal.model import Mammal
from fuse.data.tokenizers.modular_tokenizer.op import ModularTokenizerOp

models = [
    'ibm/biomed.omics.bl.sm.ma-ted-458m',
    'ibm/biomed.omics.bl.sm.ma-ted-458m.dti_bindingdb_pkd',
    'ibm/biomed.omics.bl.sm.ma-ted-458m.protein_solubility',
    'ibm-research/biomed.omics.bl.sm.ma-ted-458m.tcr_epitope_bind',
]
token = os.environ.get('HF_TOKEN', None)
for model_id in models:
    print(f'Caching {model_id}...')
    Mammal.from_pretrained(model_id, token=token)
    ModularTokenizerOp.from_pretrained(model_id, token=token)
    print(f'  done')
"
