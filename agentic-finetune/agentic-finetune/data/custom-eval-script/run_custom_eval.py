"""Custom-eval Job container script.

Runs inside the LlamaFactory image. Loads the base model + LoRA adapter,
generates deterministic responses for each probe in a probe set, scores
each response against the probe's expected pattern, and writes results
to /workspace incrementally so partial results survive a crash.

Config is read from a JSON file whose path is the sole CLI argument.
Config schema (produced by the orchestrator):

    {
      "adapter_path":   "/workspace/jobs/<training_id>/output",
      "probe_set":      "instruction_following_basic",
      "probe_file":     "/workspace/probes/instruction_following_basic.jsonl",
      "results_file":   "/workspace/jobs/<training_id>/custom_eval/<set>/results.jsonl",
      "summary_file":   "/workspace/jobs/<training_id>/custom_eval/<set>/summary.json",
      "max_new_tokens": 128,
      "eval_id":        "20260721-abc123"
    }

The base model is derived from the adapter's adapter_config.json rather than
being passed as a separate config field — this is the source of truth and
avoids drift between what the adapter was trained against and what we load.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any


# --- Scoring functions (duplicated from orchestrator.custom_eval so this
#     script has zero cross-container dependencies) ---


def score_substring(response: str, expected: str, extras: dict) -> tuple[bool, str]:
    case_sensitive = bool(extras.get("case_sensitive", False))
    if case_sensitive:
        passed = expected in response
    else:
        passed = expected.lower() in response.lower()
    reason = f"substring {'found' if passed else 'not found'}: {expected!r}"
    return passed, reason


def score_regex(response: str, expected: str, extras: dict) -> tuple[bool, str]:
    flags = 0
    flag_str = extras.get("flags", "")
    if "i" in flag_str:
        flags |= re.IGNORECASE
    if "m" in flag_str:
        flags |= re.MULTILINE
    if "s" in flag_str:
        flags |= re.DOTALL
    try:
        match = re.fullmatch(expected, response.strip(), flags)
    except re.error as e:
        return False, f"invalid regex pattern: {e}"
    passed = match is not None
    return passed, f"regex {'matched' if passed else 'did not match'}: {expected!r}"


def _extract_json_object(text: str) -> tuple[Any, str]:
    start = text.find("{")
    if start == -1:
        return None, "no '{' character found"
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start:i + 1]
                try:
                    return json.loads(candidate), ""
                except json.JSONDecodeError as e:
                    return None, f"JSON parse error: {e}"
    return None, "unbalanced braces"


def score_json_schema(response: str, expected: dict, extras: dict) -> tuple[bool, str]:
    try:
        from jsonschema import validate, ValidationError
    except ImportError:
        return False, "jsonschema not installed in eval container"
    parsed, extract_err = _extract_json_object(response)
    if parsed is None:
        return False, f"no valid JSON object in response ({extract_err})"
    try:
        validate(parsed, expected)
    except ValidationError as e:
        msg = str(e).split("\n")[0][:200]
        return False, f"schema violation: {msg}"
    return True, "schema validated"


def score_numeric(response: str, expected: float, extras: dict) -> tuple[bool, str]:
    tolerance = float(extras.get("tolerance", 0))
    match = re.search(r"-?\d+(?:\.\d+)?", response)
    if match is None:
        return False, "no numeric value in response"
    try:
        got = float(match.group())
    except ValueError:
        return False, f"could not parse {match.group()!r} as number"
    if tolerance == 0:
        passed = got == float(expected)
    else:
        passed = abs(got - float(expected)) <= tolerance
    reason = f"got {got}, expected {expected}" + (
        f" (tol ±{tolerance})" if tolerance else ""
    ) + (" — pass" if passed else " — fail")
    return passed, reason


SCORERS = {
    "substring": score_substring,
    "regex": score_regex,
    "json_schema": score_json_schema,
    "numeric": score_numeric,
}


def score_probe(probe: dict, response: str) -> tuple[bool, str]:
    scorer = probe.get("scorer")
    fn = SCORERS.get(scorer)
    if fn is None:
        return False, f"unknown scorer {scorer!r}"
    extras = {k: v for k, v in probe.items() if k not in ("id", "prompt", "scorer", "expected")}
    return fn(response, probe["expected"], extras)


# --- Model loading + generation ---


def load_model_and_tokenizer(adapter_path: str):
    """Load base model from adapter_config.json's base_model_name_or_path,
    then attach the LoRA adapter on top. Returns (model, tokenizer)."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from peft import PeftModel

    adapter_config_path = Path(adapter_path) / "adapter_config.json"
    if not adapter_config_path.exists():
        raise FileNotFoundError(f"No adapter_config.json at {adapter_config_path}")
    adapter_config = json.loads(adapter_config_path.read_text())
    base_model_name = adapter_config.get("base_model_name_or_path")
    if not base_model_name:
        raise ValueError("adapter_config.json missing base_model_name_or_path")

    print(f"[custom_eval] Loading base model: {base_model_name}", flush=True)
    # trust_remote_code=False intentionally. Setting True triggers transformers
    # to fetch model-shipped custom generation code (e.g. custom_generate/generate.py)
    # from HF Hub, which requires additional TLS trust to a CDN endpoint that
    # corporate proxies often lack certs for. Standard causal-LM generation works
    # without remote code for Qwen, Phi, Llama, Mistral, and similar.
    tokenizer = AutoTokenizer.from_pretrained(base_model_name, trust_remote_code=False)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    print(f"[custom_eval] Loading base model weights", flush=True)
    base = AutoModelForCausalLM.from_pretrained(
        base_model_name,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        trust_remote_code=False,
    )
    print(f"[custom_eval] Attaching LoRA adapter from {adapter_path}", flush=True)
    model = PeftModel.from_pretrained(base, adapter_path)
    model.eval()
    return model, tokenizer


def generate_response(model, tokenizer, prompt: str, max_new_tokens: int) -> str:
    """Deterministic decoding: temperature=0, top_p=1, do_sample=False.
    Chat template applied if the tokenizer defines one."""
    import torch

    # If the tokenizer has a chat template, use it — this is the same shape
    # the model saw during training with LlamaFactory's SFT.
    if getattr(tokenizer, "chat_template", None):
        messages = [{"role": "user", "content": prompt}]
        input_text = tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True,
        )
    else:
        input_text = prompt

    inputs = tokenizer(input_text, return_tensors="pt").to(model.device)
    prompt_len = inputs.input_ids.shape[1]

    with torch.no_grad():
        output = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            temperature=1.0,        # ignored when do_sample=False, but explicit
            top_p=1.0,
            num_beams=1,
            pad_token_id=tokenizer.pad_token_id,
        )
    # Decode only the newly-generated tokens (skip the prompt)
    generated = output[0][prompt_len:]
    return tokenizer.decode(generated, skip_special_tokens=True).strip()


# --- Main ---


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: run_custom_eval.py <config.json>", file=sys.stderr)
        return 2

    config_path = Path(sys.argv[1])
    config = json.loads(config_path.read_text())
    print(f"[custom_eval] Config: {config}", flush=True)

    probe_file = Path(config["probe_file"])
    results_file = Path(config["results_file"])
    summary_file = Path(config["summary_file"])
    max_new_tokens = int(config.get("max_new_tokens", 128))

    # Load probes
    probes = []
    for line in probe_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        probes.append(json.loads(line))
    print(f"[custom_eval] Loaded {len(probes)} probes from {probe_file}", flush=True)

    # Ensure results file starts fresh
    results_file.parent.mkdir(parents=True, exist_ok=True)
    results_file.write_text("")

    # Load model once
    t_load_start = time.time()
    try:
        model, tokenizer = load_model_and_tokenizer(config["adapter_path"])
    except Exception as e:
        print(f"[custom_eval] Model loading failed: {e}", file=sys.stderr, flush=True)
        traceback.print_exc()
        summary_file.write_text(json.dumps({
            "eval_id": config.get("eval_id"),
            "ok": False,
            "error": f"model load failed: {e}",
        }))
        return 3
    t_load_elapsed = time.time() - t_load_start
    print(f"[custom_eval] Model loaded in {t_load_elapsed:.1f}s", flush=True)

    # Generate + score, streaming results to disk as we go
    n_passed = 0
    n_failed = 0
    t_gen_start = time.time()
    with results_file.open("a") as fh:
        for i, probe in enumerate(probes, 1):
            try:
                response = generate_response(
                    model, tokenizer, probe["prompt"], max_new_tokens
                )
                passed, reason = score_probe(probe, response)
            except Exception as e:
                response = ""
                passed = False
                reason = f"exception during generation: {e}"
                traceback.print_exc()

            record = {
                "id": probe["id"],
                "prompt": probe["prompt"],
                "expected": probe["expected"],
                "actual": response,
                "passed": passed,
                "score_reason": reason,
            }
            fh.write(json.dumps(record) + "\n")
            fh.flush()

            if passed:
                n_passed += 1
                marker = "PASS"
            else:
                n_failed += 1
                marker = "FAIL"
            print(f"[custom_eval] {i}/{len(probes)} {probe['id']} {marker} — {reason[:80]}",
                  flush=True)

    t_gen_elapsed = time.time() - t_gen_start
    pass_rate = n_passed / len(probes) if probes else 0.0

    summary = {
        "eval_id": config.get("eval_id"),
        "probe_set": config.get("probe_set"),
        "adapter_path": config.get("adapter_path"),
        "n_probes": len(probes),
        "n_passed": n_passed,
        "n_failed": n_failed,
        "pass_rate": pass_rate,
        "load_seconds": round(t_load_elapsed, 1),
        "generate_seconds": round(t_gen_elapsed, 1),
        "ok": True,
    }
    summary_file.write_text(json.dumps(summary, indent=2))
    print(f"[custom_eval] Done: {n_passed}/{len(probes)} passed "
          f"({pass_rate:.1%}) in {t_gen_elapsed:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
