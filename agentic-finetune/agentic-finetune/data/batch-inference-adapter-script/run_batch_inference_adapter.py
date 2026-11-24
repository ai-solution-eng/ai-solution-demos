"""Batch inference — adapter source variant.

Runs inside the LlamaFactory image. Loads the base model + LoRA adapter,
generates responses for a list of prompts, writes outputs.jsonl and
summary.json.

Config JSON path is the sole CLI argument. Shape:

    {
      "run_id":         "...",
      "source_type":    "adapter",
      "adapter_path":   "/workspace/jobs/.../output",
      "prompts":        [{"id": "prompt_000", "prompt": "..."}, ...],
      "max_new_tokens": 512,
      "temperature":    0.0,
      "concurrency":    4,          # batch size for forward passes
      "outputs_file":   "/workspace/batch_inference/<run_id>/outputs.jsonl",
      "summary_file":   "/workspace/batch_inference/<run_id>/summary.json"
    }

Key design decisions:
  - Deterministic when temperature=0 (do_sample=False, num_beams=1)
  - Batched decoding: N prompts per forward pass, left-padded for causal LM
  - trust_remote_code=False — same fix as custom_eval, avoids the
    custom_generate.py fetch that fails through corp proxy
  - Writes outputs.jsonl incrementally, one line per completed prompt,
    so partial results survive if the Job crashes mid-batch
  - Writes summary.json after each batch, so status polls can show progress
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path


def load_model_and_tokenizer(adapter_path: str):
    """Same loader as run_custom_eval.py — base + LoRA."""
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

    print(f"[batch_inference] Loading base model: {base_model_name}", flush=True)
    tokenizer = AutoTokenizer.from_pretrained(base_model_name, trust_remote_code=False)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # Left-padding required for batched causal-LM generation
    tokenizer.padding_side = "left"

    print(f"[batch_inference] Loading base model weights", flush=True)
    base = AutoModelForCausalLM.from_pretrained(
        base_model_name,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        trust_remote_code=False,
    )
    print(f"[batch_inference] Attaching LoRA adapter from {adapter_path}", flush=True)
    model = PeftModel.from_pretrained(base, adapter_path)
    model.eval()
    return model, tokenizer


def generate_batch(
    model, tokenizer, prompts: list[str], max_new_tokens: int, temperature: float,
) -> list[tuple[str, str, int, int]]:
    """Batched generation. Returns [(response, finish_reason, prompt_tokens, completion_tokens), ...]."""
    import torch

    # Apply chat template if the tokenizer has one — matches training-time format
    if getattr(tokenizer, "chat_template", None):
        texts = [
            tokenizer.apply_chat_template(
                [{"role": "user", "content": p}],
                tokenize=False, add_generation_prompt=True,
            )
            for p in prompts
        ]
    else:
        texts = prompts

    # Batched tokenization — left-padded (set on tokenizer earlier)
    inputs = tokenizer(
        texts, return_tensors="pt", padding=True, truncation=False,
    ).to(model.device)

    do_sample = temperature > 0
    generate_kwargs = {
        "max_new_tokens": max_new_tokens,
        "do_sample": do_sample,
        "num_beams": 1,
        "pad_token_id": tokenizer.pad_token_id,
    }
    if do_sample:
        generate_kwargs["temperature"] = temperature
        generate_kwargs["top_p"] = 1.0

    with torch.no_grad():
        outputs = model.generate(**inputs, **generate_kwargs)

    gen_start = inputs.input_ids.shape[1]
    results: list[tuple[str, str, int, int]] = []
    eos_id = tokenizer.eos_token_id
    for i, out in enumerate(outputs):
        gen_tokens = out[gen_start:]
        gen_list = gen_tokens.tolist()
        finish_reason = "stop" if (eos_id is not None and eos_id in gen_list) else "length"

        response = tokenizer.decode(gen_tokens, skip_special_tokens=True).strip()
        attention_mask = inputs.attention_mask[i]
        prompt_tok = int(attention_mask.sum().item())
        completion_tok = len(gen_list)
        if eos_id is not None and eos_id in gen_list:
            completion_tok = gen_list.index(eos_id) + 1

        results.append((response, finish_reason, prompt_tok, completion_tok))
    return results


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: run_batch_inference_adapter.py <config.json>", file=sys.stderr)
        return 2

    config_path = Path(sys.argv[1])
    config = json.loads(config_path.read_text())
    print(f"[batch_inference] Config: run_id={config['run_id']} "
          f"source=adapter n_prompts={len(config['prompts'])}", flush=True)

    prompts = config["prompts"]
    adapter_path = config["adapter_path"]
    max_new_tokens = int(config["max_new_tokens"])
    temperature = float(config["temperature"])
    batch_size = int(config["concurrency"])
    outputs_file = Path(config["outputs_file"])
    summary_file = Path(config["summary_file"])
    run_id = config["run_id"]

    outputs_file.parent.mkdir(parents=True, exist_ok=True)
    outputs_file.write_text("")

    def write_summary(n_completed: int, elapsed: float, ok: bool = True,
                      error: str = "") -> None:
        summary = {
            "run_id": run_id,
            "source_type": "adapter",
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
        model, tokenizer = load_model_and_tokenizer(adapter_path)
    except Exception as e:
        traceback.print_exc()
        write_summary(0, time.time() - t_start, ok=False, error=f"model load: {e}")
        return 3
    print(f"[batch_inference] Model loaded in {time.time() - t_start:.1f}s", flush=True)

    n_completed = 0
    try:
        with outputs_file.open("a") as fh:
            for batch_start in range(0, len(prompts), batch_size):
                batch = prompts[batch_start:batch_start + batch_size]
                batch_texts = [p["prompt"] for p in batch]

                try:
                    results = generate_batch(
                        model, tokenizer, batch_texts, max_new_tokens, temperature,
                    )
                except Exception as e:
                    print(f"[batch_inference] Batch failed, falling back to per-prompt: {e}",
                          flush=True)
                    traceback.print_exc()
                    results = []
                    for text in batch_texts:
                        try:
                            r = generate_batch(model, tokenizer, [text],
                                               max_new_tokens, temperature)
                            results.append(r[0])
                        except Exception:
                            results.append(("", "error", 0, 0))

                for probe, (response, finish_reason, ptok, ctok) in zip(batch, results):
                    record = {
                        "id": probe["id"],
                        "prompt": probe["prompt"],
                        "response": response,
                        "reasoning": None,
                        "finish_reason": finish_reason,
                        "prompt_tokens": ptok,
                        "completion_tokens": ctok,
                    }
                    fh.write(json.dumps(record) + "\n")
                    fh.flush()
                    n_completed += 1

                write_summary(n_completed, time.time() - t_start)
                print(f"[batch_inference] {n_completed}/{len(prompts)} completed",
                      flush=True)
    except Exception as e:
        traceback.print_exc()
        write_summary(n_completed, time.time() - t_start, ok=False,
                      error=f"generation loop: {e}")
        return 4

    elapsed = time.time() - t_start
    write_summary(n_completed, elapsed)
    print(f"[batch_inference] Done: {n_completed}/{len(prompts)} in {elapsed:.1f}s",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
