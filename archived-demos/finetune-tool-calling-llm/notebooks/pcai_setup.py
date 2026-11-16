# PCAI environment setup — import this at the top of every notebook
# Handles SSL verification, HuggingFace HTTP backend, and client initialization

import os
import ssl
import urllib3
import httpx
import requests

from config import *

# === Disable SSL verification globally ===
# PCAI uses internal certificates not trusted by default CA bundles
os.environ["HF_HUB_DISABLE_SSL_VERIFY"] = "1"
os.environ["CURL_CA_BUNDLE"] = ""
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Patch requests library globally
_session = requests.Session()
_session.verify = False
requests.get = _session.get
requests.post = _session.post
requests.put = _session.put
requests.delete = _session.delete
requests.patch = _session.patch

# Patch huggingface_hub HTTP backend (uses httpx internally)
from huggingface_hub.utils._http import get_session as _hf_get_session
_hf_session = _hf_get_session()
_hf_session._transport = _hf_session._transport.__class__(verify=False)

# === Initialize NeMo Microservices SDK ===
from nemo_microservices import NeMoMicroservices

nemo_client = NeMoMicroservices(
    base_url=NEMO_URL,
    inference_base_url=NIM_URL,
    http_client=httpx.Client(verify=False),
)

# === Initialize HuggingFace API (using internal Data Store URL) ===
from huggingface_hub import HfApi

hf_api = HfApi(endpoint=f"{NDS_INTERNAL_URL}/v1/hf")

print("PCAI setup complete")
print(f"  NeMo URL:  {NEMO_URL}")
print(f"  NIM URL:   {NIM_URL}")
print(f"  Data Store (internal): {NDS_INTERNAL_URL}")
print(f"  SSL verification: disabled")
