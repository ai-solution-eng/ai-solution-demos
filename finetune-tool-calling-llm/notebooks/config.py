# NeMo Microservices 25.12.1 on PCAI — Tool-Calling Fine-Tuning Demo
# Adapted from: https://github.com/NVIDIA/GenerativeAIExamples/tree/main/nemo/data-flywheel/tool-calling

import os

# === PCAI Environment ===
# Set your PCAI hostname (Istio VirtualService endpoint)
PCAI_HOST = os.environ.get(
    "PCAI_HOST",
    "https://nemo-microservices-25121.ai-application.pcai0209.tr7.hpecolo.net"
)

# NeMo Microservices URLs — all services share the same Istio hostname
NDS_URL = PCAI_HOST   # Data Store (/v1/datastore/*, /v1/hf/*)
NEMO_URL = PCAI_HOST  # Customizer, Entity Store, Evaluator, Guardrails
NIM_URL = PCAI_HOST   # NIM Proxy (/v1/chat/*, /v1/completions/*)

# Internal Data Store URL — used for HfApi uploads from within the PCAI cluster
# (bypasses Istio to avoid LFS upload URL scheme mismatch)
NDS_INTERNAL_URL = "http://nemo-data-store.nemo-microservices-25121.svc.cluster.local:3000"

# === SSL Configuration ===
# PCAI uses internal SSL certificates — set to False to disable verification
VERIFY_SSL = False

# === Authentication ===
# HuggingFace Token — required to download xLAM dataset and model weights
HF_TOKEN = os.environ.get("HF_TOKEN", "<YOUR-HF-TOKEN>")

# WandB (optional) — set to track training metrics
WANDB_API_KEY = os.environ.get("WANDB_API_KEY", "")

# NeMo Data Store token
NDS_TOKEN = "token"

# === Model Configuration ===
NMS_NAMESPACE = "default" 
DATASET_NAME = "xlam-ft-dataset-latest"

# Base model — must match a customizer target in the deployment
BASE_MODEL = "meta/llama-3.2-1b-instruct"
BASE_MODEL_VERSION = "v1.0.0+40GB"  # L40S template class

# Output model name after fine-tuning
CUSTOM_MODEL = f"{NMS_NAMESPACE}/llama-3.2-1b-xlam-toolcalling@latest"
