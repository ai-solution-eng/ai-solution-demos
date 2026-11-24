"""Merge-export Job container script.

Runs inside the LlamaFactory image (already has llamafactory-cli, torch,
transformers, peft; we add boto3 at Job container init).

Reads a config JSON whose path is the sole CLI argument. Config shape:

    {
      "adapter_path":       "/workspace/jobs/<training_id>/output",
      "base_model":         "Qwen/Qwen2.5-3B-Instruct",
      "merge_output_dir":   "/workspace/merged/<training_id>/output",
      "summary_file":       "/workspace/merged/<training_id>/summary.json",
      "s3_key_prefix":      "merged/qwen2.5-3b-alpaca",
      "merge_id":           "20260724-hhmmss-xxxxxx"
    }

Steps:
  1. Run llamafactory-cli export → merge base + adapter into merge_output_dir
  2. Read S3 credentials from env vars (S3_ENDPOINT, S3_ACCESS_KEY, etc.)
  3. Upload every file in merge_output_dir to s3://<bucket>/<key_prefix>/
  4. Write summary.json with counts, timings, S3 URI
  5. Delete merge_output_dir to reclaim ~6 GB of workspace space

Notes:
  - Determined base model comes from the config, not adapter_config.json —
    the orchestrator has already resolved it, so we trust its value.
  - llamafactory-cli export supports adapter merging via the --adapter_name_or_path
    flag alongside --model_name_or_path. Output directory becomes a full
    HuggingFace-format model (config.json + safetensors + tokenizer).
  - We keep summary.json at /workspace/merged/<training_id>/ even after
    cleaning the merged output, so the orchestrator can still read the
    upload's success record.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path


def _install_boto3() -> None:
    """LlamaFactory image doesn't have boto3. Install it at Job runtime.

    This costs ~15s per Job. Faster alternative is a custom image with
    boto3 pre-baked; for now, runtime pip install keeps the chart shipping
    the LF image without modification. Revisit if merge Jobs become frequent.
    """
    print("[merge_export] Installing boto3 at runtime", flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet",
         "--no-cache-dir", "boto3>=1.35"],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("Failed to pip install boto3")


def _run_merge(base_model: str, adapter_path: str, output_dir: Path) -> float:
    """Run llamafactory-cli export. Returns elapsed seconds."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Infer template from base_model — LF has built-in mappings for common
    # models. For Qwen/Phi/Llama, "auto" works. Explicit override could be
    # added to the config later if we hit a model where auto-detection fails.
    cmd = [
        "llamafactory-cli", "export",
        "--model_name_or_path", base_model,
        "--adapter_name_or_path", adapter_path,
        "--template", "default",
        "--export_dir", str(output_dir),
        "--export_size", "5",           # split safetensors into 5GB shards
        "--export_device", "auto",
    ]
    print(f"[merge_export] Running merge: {' '.join(cmd)}", flush=True)
    t_start = time.time()
    result = subprocess.run(cmd, check=False, capture_output=False)
    elapsed = time.time() - t_start
    if result.returncode != 0:
        raise RuntimeError(
            f"llamafactory-cli export failed with exit code {result.returncode}"
        )
    print(f"[merge_export] Merge completed in {elapsed:.1f}s", flush=True)
    return elapsed


def _iter_merged_files(output_dir: Path) -> list[tuple[Path, str]]:
    """[(local_path, key_suffix), ...] — same shape as s3_export's iterator.
    Skips hidden and oversized files."""
    entries: list[tuple[Path, str]] = []
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(output_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        try:
            if path.stat().st_size > 5 * 1024 * 1024 * 1024:
                print(f"[merge_export] Skipping {path}: exceeds 5GB single-PUT limit",
                      flush=True)
                continue
        except OSError:
            continue
        entries.append((path, "/".join(rel.parts)))
    return entries


def _upload_to_s3(output_dir: Path, s3_key_prefix: str) -> tuple[str, int, int, float]:
    """Upload merged output to S3. Returns (s3_uri, n_files, total_bytes, elapsed_seconds)."""
    import boto3
    from botocore.client import Config as BotoConfig

    endpoint = os.environ["S3_ENDPOINT"]
    access_key = os.environ["S3_ACCESS_KEY"]
    secret_key = os.environ["S3_SECRET_KEY"]
    bucket = os.environ["S3_BUCKET"]

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=BotoConfig(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=10,
            read_timeout=300,  # 5 min per file; merged models are chunked to 5GB
        ),
    )

    entries = _iter_merged_files(output_dir)
    total_bytes = 0
    t_start = time.time()
    for local_path, key_suffix in entries:
        key = f"{s3_key_prefix}/{key_suffix}"
        size = local_path.stat().st_size
        print(f"[merge_export] Uploading {local_path} ({size} bytes) → s3://{bucket}/{key}",
              flush=True)
        client.upload_file(str(local_path), bucket, key)
        total_bytes += size
    elapsed = time.time() - t_start
    s3_uri = f"s3://{bucket}/{s3_key_prefix}"
    print(f"[merge_export] Uploaded {len(entries)} files ({total_bytes} bytes) in {elapsed:.1f}s",
          flush=True)
    return s3_uri, len(entries), total_bytes, elapsed


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: run_merge_export.py <config.json>", file=sys.stderr)
        return 2

    config_path = Path(sys.argv[1])
    config = json.loads(config_path.read_text())
    print(f"[merge_export] Config: {config}", flush=True)

    adapter_path = config["adapter_path"]
    base_model = config["base_model"]
    merge_output_dir = Path(config["merge_output_dir"])
    summary_file = Path(config["summary_file"])
    s3_key_prefix = config["s3_key_prefix"]
    merge_id = config.get("merge_id")

    summary: dict = {"merge_id": merge_id, "ok": False}
    try:
        # 1. Install boto3
        _install_boto3()

        # 2. Merge
        merge_seconds = _run_merge(base_model, adapter_path, merge_output_dir)
        summary["merge_seconds"] = round(merge_seconds, 1)

        # 3. Upload
        s3_uri, n_files, total_bytes, upload_seconds = _upload_to_s3(
            merge_output_dir, s3_key_prefix,
        )
        summary.update({
            "ok": True,
            "s3_uri": s3_uri,
            "bucket": os.environ["S3_BUCKET"],
            "key_prefix": s3_key_prefix,
            "n_files": n_files,
            "total_bytes": total_bytes,
            "upload_seconds": round(upload_seconds, 1),
        })

        # 4. Cleanup — merged output is ~6GB; keep summary.json to preserve
        # the operation's record for the orchestrator to read.
        print(f"[merge_export] Cleaning {merge_output_dir}", flush=True)
        shutil.rmtree(merge_output_dir, ignore_errors=True)

    except Exception as e:
        print(f"[merge_export] Failed: {e}", file=sys.stderr, flush=True)
        traceback.print_exc()
        summary.update({"ok": False, "error": f"{type(e).__name__}: {e}"})
        summary_file.parent.mkdir(parents=True, exist_ok=True)
        summary_file.write_text(json.dumps(summary, indent=2))
        return 3

    summary_file.parent.mkdir(parents=True, exist_ok=True)
    summary_file.write_text(json.dumps(summary, indent=2))
    print(f"[merge_export] Done: {summary['s3_uri']} ({summary['n_files']} files, "
          f"{summary['total_bytes']} bytes)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
