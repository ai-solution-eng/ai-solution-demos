"""Batch inference — endpoint source variant.

Runs inside python:3.11-slim, installs httpx at runtime. Sends one HTTP
request per prompt to an OpenAI-compatible endpoint, collects responses,
writes outputs.jsonl and summary.json.

Config JSON path is the sole CLI argument. Shape:

    {
      "run_id":         "...",
      "source_type":    "endpoint",
      "endpoint_url":   "https://.../serving/...",
      "model":          "qwen25-3b-alpaca-merged",  # or "/mnt/models"
      "verify_tls":     false,
      "prompts":        [{"id": "...", "prompt": "..."}, ...],
      "max_new_tokens": 512,
      "temperature":    0.0,
      "concurrency":    4,
      "outputs_file":   "/workspace/batch_inference/<run_id>/outputs.jsonl",
      "summary_file":   "/workspace/batch_inference/<run_id>/summary.json"
    }

Auth: reads MLIS_TOKEN from env (mounted from the mlis-token Secret).

MLIS response schema — the important gotcha:
  Some models (e.g. DeepSeek-V4-Flash) return message.content=null when
  the response fits entirely in the reasoning trace. Other models
  populate message.content normally and leave reasoning=null.
  Parser rule: content or reasoning or "". Both surfaced in output.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
from pathlib import Path

import httpx


async def call_endpoint(
    client: httpx.AsyncClient,
    endpoint_url: str,
    model: str,
    prompt: str,
    max_new_tokens: int,
    temperature: float,
    token: str = "",
) -> dict:
    """Single request to /v1/chat/completions. Returns parsed record.

    Never raises — errors are captured in the returned dict so the caller
    can write them to outputs.jsonl and continue with the next prompt.

    Token source order:
      1. Explicit `token` argument (from config.json, resolved by orchestrator
         from a per-deployment Secret named in source.token_secret_name)
      2. MLIS_TOKEN env var (from the default Secret mounted via chart)
    """
    url = endpoint_url.rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_new_tokens,
        "temperature": temperature,
    }
    headers = {"Content-Type": "application/json"}
    resolved_token = (token or os.environ.get("MLIS_TOKEN", "")).strip()
    if resolved_token:
        headers["Authorization"] = f"Bearer {resolved_token}"
    else:
        # Never send a bare "Bearer " header — h11 rejects it with an
        # unclear error. Fail explicitly instead so the user sees what's
        # wrong (no token available) rather than a wrapped protocol error.
        return {
            "response": "", "reasoning": None,
            "finish_reason": "error",
            "prompt_tokens": None, "completion_tokens": None,
            "error": (
                "no MLIS token available. Neither source.token_secret_name "
                "resolved a token, nor was MLIS_TOKEN env var set. Check the "
                "mlis-token Secret in the namespace, or pass token_secret_name."
            ),
        }

    try:
        resp = await client.post(url, json=payload, headers=headers, timeout=60.0)
    except httpx.HTTPError as e:
        # httpx wraps underlying transport errors (connect failures, TLS
        # errors, protocol errors) as HTTPError subclasses. The outer
        # __repr__ often loses detail — surface the full chain so we can
        # diagnose what actually broke.
        chain = []
        cur = e
        for _ in range(5):   # safety cap
            chain.append(f"{type(cur).__name__}({cur!r})")
            if cur.__cause__ is None and cur.__context__ is None:
                break
            cur = cur.__cause__ or cur.__context__
            if cur is None:
                break
        err_detail = " -> ".join(chain)
        return {
            "response": "", "reasoning": None,
            "finish_reason": "error",
            "prompt_tokens": None, "completion_tokens": None,
            "error": f"http error: {err_detail}",
        }

    if resp.status_code != 200:
        return {
            "response": "", "reasoning": None,
            "finish_reason": "error",
            "prompt_tokens": None, "completion_tokens": None,
            "error": f"http {resp.status_code}: {resp.text[:200]}",
        }

    try:
        data = resp.json()
    except ValueError as e:
        return {
            "response": "", "reasoning": None,
            "finish_reason": "error",
            "prompt_tokens": None, "completion_tokens": None,
            "error": f"non-json response: {e}",
        }

    # Parse OpenAI-compat response. Content/reasoning fallback for
    # models that only populate reasoning (reasoning-only outputs).
    choices = data.get("choices") or []
    if not choices:
        return {
            "response": "", "reasoning": None,
            "finish_reason": "error",
            "prompt_tokens": None, "completion_tokens": None,
            "error": "no choices in response",
        }
    choice = choices[0]
    message = choice.get("message") or {}
    content = message.get("content")
    reasoning = message.get("reasoning")
    response = content if content else (reasoning if reasoning else "")
    if response and isinstance(response, str):
        response = response.lstrip()  # Qwen convention adds leading space

    finish_reason = choice.get("finish_reason") or ""
    usage = data.get("usage") or {}

    return {
        "response": response or "",
        "reasoning": reasoning,
        "finish_reason": finish_reason,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
    }


async def run_prompts(
    prompts: list[dict],
    endpoint_url: str,
    model: str,
    max_new_tokens: int,
    temperature: float,
    concurrency: int,
    verify_tls: bool,
    outputs_file: Path,
    write_summary_fn,
    run_id: str,
    t_start: float,
    token: str = "",
) -> int:
    """Concurrent requests, bounded by semaphore."""

    semaphore = asyncio.Semaphore(concurrency)
    n_completed = 0
    lock = asyncio.Lock()

    async with httpx.AsyncClient(verify=verify_tls) as client:

        async def one_prompt(probe: dict) -> None:
            nonlocal n_completed
            async with semaphore:
                result = await call_endpoint(
                    client, endpoint_url, model,
                    probe["prompt"], max_new_tokens, temperature,
                    token=token,
                )
            record = {
                "id": probe["id"],
                "prompt": probe["prompt"],
                **result,
            }
            async with lock:
                with outputs_file.open("a") as fh:
                    fh.write(json.dumps(record) + "\n")
                n_completed += 1
                write_summary_fn(n_completed, time.time() - t_start)
                err = result.get("error")
                print(f"[batch_inference] {n_completed}/{len(prompts)} completed "
                      f"({probe['id']}: finish_reason={result.get('finish_reason')}"
                      f"{f', error={err}' if err else ''})",
                      flush=True)

        await asyncio.gather(*[one_prompt(p) for p in prompts])

    return n_completed


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: run_batch_inference_endpoint.py <config.json>", file=sys.stderr)
        return 2

    # Endpoint mode connects to in-cluster MLIS URLs. Corporate HTTPS_PROXY
    # env vars (inherited from trainingJob.env in the chart) route requests
    # through hpeproxy.its.hpecorp.net:8080, which cannot reach cluster IPs
    # and RSTs the connection. Strip them so httpx connects directly.
    # See porting.md §21 for the debugging history.
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        os.environ.pop(var, None)

    config_path = Path(sys.argv[1])
    config = json.loads(config_path.read_text())
    print(f"[batch_inference] Config: run_id={config['run_id']} "
          f"source=endpoint url={config['endpoint_url']} model={config['model']} "
          f"n_prompts={len(config['prompts'])}", flush=True)

    prompts = config["prompts"]
    endpoint_url = config["endpoint_url"]
    model = config["model"]
    verify_tls = bool(config.get("verify_tls", False))
    max_new_tokens = int(config["max_new_tokens"])
    temperature = float(config["temperature"])
    concurrency = int(config["concurrency"])
    outputs_file = Path(config["outputs_file"])
    summary_file = Path(config["summary_file"])
    run_id = config["run_id"]
    # Token comes from config.json when the orchestrator resolved it from
    # a per-deployment Secret. Empty string here means "fall back to
    # MLIS_TOKEN env var" (the default Secret mounted by the chart).
    token = str(config.get("token", "")).strip()
    token_source = config.get("token_source", "unknown")
    print(f"[batch_inference] Token source: {token_source} "
          f"(length: {len(token) if token else len(os.environ.get('MLIS_TOKEN', ''))})",
          flush=True)

    outputs_file.parent.mkdir(parents=True, exist_ok=True)
    outputs_file.write_text("")

    def write_summary(n_completed: int, elapsed: float, ok: bool = True,
                      error: str = "") -> None:
        summary = {
            "run_id": run_id,
            "source_type": "endpoint",
            "endpoint_url": endpoint_url,
            "model": model,
            "n_prompts": len(prompts),
            "n_completed": n_completed,
            "elapsed_seconds": round(elapsed, 1),
            "ok": ok,
        }
        if error:
            summary["error"] = error
        summary_file.write_text(json.dumps(summary, indent=2))

    t_start = time.time()
    write_summary(0, 0)

    try:
        n_completed = asyncio.run(run_prompts(
            prompts, endpoint_url, model, max_new_tokens, temperature,
            concurrency, verify_tls, outputs_file, write_summary,
            run_id, t_start, token=token,
        ))
    except Exception as e:
        traceback.print_exc()
        write_summary(0, time.time() - t_start, ok=False,
                      error=f"generation loop: {e}")
        return 4

    elapsed = time.time() - t_start
    write_summary(n_completed, elapsed)
    print(f"[batch_inference] Done: {n_completed}/{len(prompts)} in {elapsed:.1f}s",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
