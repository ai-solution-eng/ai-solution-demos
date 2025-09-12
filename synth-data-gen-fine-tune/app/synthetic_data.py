import os
import time
import json
from typing import List, Dict
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
DEFAULT_PROMPT = "Generate a useful instruction-response pair."

os.makedirs(DATA_DIR, exist_ok=True)

def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())

def _session_with_retries(total_retries: int = 3, backoff: float = 0.5) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=total_retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["POST", "GET"])
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

def generate_synthetic_data(
    base_url: str,
    model: str,
    bearer_token: str,
    num_samples: int = 5,
    prompt_template: str = DEFAULT_PROMPT,
    skip_tls_verify: bool = False,
    request_timeout_sec: float = 20.0  # per request
) -> Dict[str, str]:
    """
    Calls an OpenAI-compatible /v1/chat/completions endpoint to generate synthetic data,
    writes a JSONL dataset to /app/data, and returns metadata.
    """
    url = base_url.rstrip("/") + "/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json"
    }

    # Optionally suppress InsecureRequestWarning only when skipping verify
    if skip_tls_verify:
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass

    session = _session_with_retries()

    rows: List[Dict[str, str]] = []

    for _ in range(num_samples):
        payload = {
            "model": model,
            "messages": [
                {"role": "user", "content": prompt_template}
            ],
            "max_tokens": 256,
            "temperature": 0.8,
            "top_p": 0.95
        }

        # Use a (connect, read) timeout tuple to fail fast if server stalls on handshake/read
        timeout = (5, request_timeout_sec)

        resp = session.post(
            url,
            headers=headers,
            json=payload,
            timeout=timeout,
            verify=not skip_tls_verify
        )

        if resp.status_code != 200:
            # Bubble up the body for debugging in the UI
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        try:
            text = data["choices"][0]["message"]["content"].strip()
        except Exception:
            text = json.dumps(data)

        rows.append({"instruction": prompt_template, "output": text})

    fname = f"dataset_{_timestamp()}_{model.replace('/', '_')}.jsonl"
    fpath = os.path.join(DATA_DIR, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    return {"file_name": fname, "file_path": fpath, "num_rows": str(len(rows))}
