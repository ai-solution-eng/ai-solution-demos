import os, requests
import logging, sys, time, json, re
from flask import Flask, render_template, request, jsonify
from .synthetic_data import generate_synthetic_data, DATA_DIR
from .fine_tune import start_finetune, get_finetune_status, BASE_MODEL_ID

REDACT_KEYS = {"authorization", "token", "bearer_token", "base_token", "ft_token"}

ADAPTER_EXPORT_DIR = os.environ.get("ADAPTER_EXPORT_DIR", "/app/adapters")
os.makedirs(ADAPTER_EXPORT_DIR, exist_ok=True)

app = Flask(__name__)

# --- Logging config: stdout, JSON-ish lines ---
root = logging.getLogger()
if not root.handlers:
    h = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
    h.setFormatter(fmt)
    root.addHandler(h)
root.setLevel(logging.INFO)
app.logger.setLevel(logging.INFO)

# --- Helpers to mask secrets in logs ---
def _redact_payload(obj):
    try:
        if isinstance(obj, dict):
            return {k: ("***" if k.lower() in REDACT_KEYS else _redact_payload(v))
                    for k, v in obj.items()}
        if isinstance(obj, list):
            return [_redact_payload(x) for x in obj]
        return obj
    except Exception:
        return "<unserializable>"

@app.before_request
def _log_request_start():
    request._start_time = time.time()

@app.after_request
def _log_request_end(response):
    try:
        dur_ms = int((time.time() - getattr(request, "_start_time", time.time())) * 1000)
        info = {
            "event": "http_request",
            "method": request.method,
            "path": request.path,
            "status": response.status_code,
            "duration_ms": dur_ms,
            "remote_addr": request.headers.get("X-Forwarded-For", request.remote_addr),
        }
        # For POST/PUT, log sanitized body (small bodies only)
        if request.method in ("POST", "PUT", "PATCH"):
            try:
                body = request.get_json(silent=True)
                if body is not None:
                    info["body"] = _redact_payload(body)
            except Exception:
                pass
        app.logger.info(json.dumps(info))
    except Exception:
        app.logger.exception("request logging failed")
    return response

# health
@app.route("/healthz")
def healthz():
    return "ok", 200

@app.route("/event_log", methods=["POST"])
def event_log():
    data = request.get_json(silent=True) or {}
    event = (data.get("event") or "").strip()
    payload = data.get("payload")
    # redact secrets in payload
    safe = payload
    if isinstance(payload, (dict, list)):
        safe = _redact_payload(payload)
    record = {
        "event": f"ui:{event or 'unknown'}",
        "path": request.path,
        "user_agent": request.headers.get("User-Agent", ""),
        "payload": safe
    }
    app.logger.info(json.dumps(record))
    return jsonify({"status": "ok"})

def _list_datasets():
    files = []
    for name in sorted(os.listdir(DATA_DIR)):
        if name.lower().endswith(".jsonl"):
            path = os.path.join(DATA_DIR, name)
            try:
                size_bytes = os.path.getsize(path)
                # Count lines safely for small/medium files
                with open(path, "r", encoding="utf-8") as f:
                    n = sum(1 for _ in f)
            except Exception:
                size_bytes, n = 0, 0
            files.append({
                "name": name,
                "size_bytes": size_bytes,
                "num_rows": n
            })
    return files

def _safe_parse_json(r):
    # If server says it's JSON, try parsing
    ct = (r.headers.get("Content-Type") or "").lower()
    if "application/json" in ct:
        try:
            return r.json()
        except ValueError:
            pass  # fall through

    # Empty/204 bodies are common for “management” endpoints — count as success
    if r.status_code == 204 or not (r.content and r.text.strip()):
        return {"ok": True, "body": ""}

    # Last-ditch attempt: maybe it’s JSON but mislabeled
    try:
        return json.loads(r.text)
    except Exception:
        # Return raw text so the caller can still show something useful
        return {"ok": True, "body": r.text}

def _post_json(url, payload, token=None, host_header=None, timeout=30):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if host_header:
        headers["Host"] = host_header
    r = requests.post(url, json=payload, headers=headers, timeout=timeout)
    r.raise_for_status()
    return _safe_parse_json(r)

def _delete_json(url, payload, token=None, host_header=None, timeout=30):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if host_header:
        headers["Host"] = host_header
    r = requests.delete(url, json=payload, headers=headers, timeout=timeout)
    r.raise_for_status()
    return _safe_parse_json(r)


@app.route('/')
def home():
    return render_template('index.html')

@app.route('/generate_data', methods=['POST'])
def generate_data():
    data = request.json or {}
    base_url = (data.get('base_url') or '').strip()
    model = (data.get('model') or '').strip()
    bearer_token = (data.get('bearer_token') or '').strip()
    num_samples = int(data.get('num_samples', 5))
    prompt_template = (data.get('prompt_template') or '').strip() or "Generate a useful instruction-response pair."
    skip_tls_verify = bool(data.get('skip_tls_verify', False))
    request_timeout_sec = float(data.get('request_timeout_sec', 20.0))

    if not base_url:
        return jsonify({"status": "error", "message": "Base URL is required"}), 400
    if not model:
        return jsonify({"status": "error", "message": "Model name is required"}), 400
    if not bearer_token:
        return jsonify({"status": "error", "message": "Bearer token is required"}), 400

    try:
        meta = generate_synthetic_data(
            base_url=base_url,
            model=model,
            bearer_token=bearer_token,
            num_samples=num_samples,
            prompt_template=prompt_template,
            skip_tls_verify=skip_tls_verify,
            request_timeout_sec=request_timeout_sec,
        )
        return jsonify({"status": "success", "meta": meta})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 502

@app.route('/list_datasets', methods=['GET'])
def list_datasets():
    try:
        return jsonify({"status": "success", "files": _list_datasets()})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/finetune', methods=['POST'])
def finetune():
    data = request.json or {}
    selected_files = data.get("selected_files") or []
    base_model_id = (data.get("base_model_id") or BASE_MODEL_ID).strip()

    if not isinstance(selected_files, list) or not selected_files:
        return jsonify({"status": "error", "message": "selected_files must be a non-empty list"}), 400

    valid_paths = []
    for name in selected_files:
        safe = os.path.basename(name)
        path = os.path.join(DATA_DIR, safe)
        if not (os.path.exists(path) and path.endswith(".jsonl")):
            return jsonify({"status": "error", "message": f"File not found or invalid: {safe}"}), 400
        valid_paths.append(path)

    # Optional: accept a few training params from UI (fallback to defaults in FineTuneJob)
    train_args = {
        "num_epochs": int(data.get("num_epochs", 1)),
        "lr": float(data.get("learning_rate", 2e-4)),
        "per_device_train_batch_size": int(data.get("per_device_train_batch_size", 2)),
        "gradient_accumulation_steps": int(data.get("gradient_accumulation_steps", 2)),
        "max_seq_len": int(data.get("max_seq_len", 512)),
    }

    job_id = start_finetune(valid_paths, base_model_id=base_model_id, **train_args)
    return jsonify({"status": "started", "job_id": job_id})

@app.route('/finetune_status', methods=['GET'])
def finetune_status():
    job_id = (request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"status": "error", "message": "job_id query parameter is required"}), 400
    return jsonify(get_finetune_status(job_id))

@app.route('/list_exported_adapters', methods=['GET'])
def list_exported_adapters():
    try:
        items = []
        if os.path.isdir(ADAPTER_EXPORT_DIR):
            for name in sorted(os.listdir(ADAPTER_EXPORT_DIR)):
                p = os.path.join(ADAPTER_EXPORT_DIR, name)
                if os.path.isdir(p):
                    items.append({"name": name, "path": p})
        return jsonify({"status": "success", "adapters": items})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/hotload_adapter', methods=['POST'])
def hotload_adapter():
    data = request.json or {}
    base_url   = (data.get("vllm_url") or "").rstrip("/")
    token      = (data.get("token") or "").strip()
    exported_adapter = (data.get("exported_adapter") or "").strip()
    adapter    = (data.get("adapter_name") or "").strip()

    if not base_url or not exported_adapter:
        return jsonify({"status":"error","message":"vllm_url and exported_adapter are required"}), 400

    # Construct path inside vLLM container. If ADAPTER_EXPORT_DIR ends with a distinct basename (e.g. adapters),
    # mirror that under the mount root (/models-pvc). This allows flexible export roots while keeping a predictable hot-load path.
    export_root = os.environ.get("ADAPTER_EXPORT_DIR", "/app/adapters").rstrip("/")
    base_name = os.path.basename(export_root) or "adapters"
    # If export_root already mounted 1:1 at /models-pvc, we include its basename to avoid collisions.
    lora_path = f"/models-pvc/{base_name}/{exported_adapter}"

    try:
        resp = _post_json(
            base_url + "/v1/load_lora_adapter",
            {"lora_name": adapter, "lora_path": lora_path},
            token=token or None,
            timeout=60
        )
        return jsonify({"status":"success","response":resp,"adapter_name":adapter,"lora_path":lora_path})
    except requests.HTTPError as e:
        txt = e.response.text if e.response is not None else str(e)
        return jsonify({"status":"error","message":txt}), 502
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 502

@app.route('/unload_adapter', methods=['POST'])
def unload_adapter():
    data = request.json or {}
    base_url   = (data.get("vllm_url") or "").rstrip("/")
    token      = (data.get("token") or "").strip()
    adapter    = (data.get("adapter_name") or "").strip()

    if not base_url or not adapter:
        return jsonify({"status":"error","message":"vllm_url and adapter_name are required"}), 400

    try:
        resp = _delete_json(
            base_url + "/v1/unload_lora_adapter",
            {"lora_name": adapter},
            token=token or None,
            timeout=60
        )
        return jsonify({"status":"success","response":resp,"adapter_name":adapter})
    except requests.HTTPError as e:
        txt = e.response.text if e.response is not None else str(e)
        return jsonify({"status":"error","message":txt}), 502
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 502


@app.route('/compare', methods=['POST'])
def compare():
    data = request.json or {}
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"status":"error","message":"prompt is required"}), 400

    # One or two endpoints (you can use one URL for both)
    base_url   = (data.get("base_url") or "").rstrip("/")
    base_model = (data.get("base_model") or "").strip()

    ft_url     = (data.get("ft_url") or base_url).rstrip("/")
    ft_model   = (data.get("ft_model") or "").strip()

    # tokens: allow a single shared 'token' or per-endpoint tokens
    token      = (data.get("token") or "").strip()
    base_token = (data.get("base_token") or token).strip()
    ft_token   = (data.get("ft_token") or token).strip()

    max_tokens = int(data.get("max_new_tokens", 256))
    temperature = float(data.get("temperature", 0.7))
    top_p       = float(data.get("top_p", 0.95))

    if not (base_url and base_model and ft_url and ft_model):
        return jsonify({"status":"error","message":"base_url/base_model and ft_url/ft_model are required"}), 400

    # Match your SFT formatting
    formatted = f"### Instruction:\n{prompt}\n\n### Response:\n"

    def call_openai_compat(url, model, token):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = {
            "model": model,
            "messages": [{"role":"user","content": formatted}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p
        }
        r = requests.post(url + "/v1/chat/completions", json=payload, headers=headers, timeout=60)
        r.raise_for_status()
        j = r.json()
        return j["choices"][0]["message"]["content"]

    try:
        base_text = call_openai_compat(base_url, base_model, base_token)
        ft_text   = call_openai_compat(ft_url,   ft_model,   ft_token)
        return jsonify({"status":"success","base": base_text, "finetuned": ft_text})
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else str(e)
        return jsonify({"status":"error","message": body}), 502
    except Exception as e:
        return jsonify({"status":"error","message": str(e)}), 502

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
