#!/usr/bin/env python3
"""Replay harness for the self-improving SQL agent loop.

Measures "improvement" instead of vibes: replay a held-out set of questions
against the agent, score each answer against its expected check, and print a
before/after comparison. Run it once BEFORE seeding lessons (baseline) and
again after N promoted lessons to see the delta.

Each eval set is adapter-specific (the checks reference real tables/domain),
so `--eval` points at an adapter's set. Two scoring modes:

  --judge-llm    ask a judge LLM whether the answer satisfies the check
  --judge-manual print each answer for a human to grade

The "executor" that actually answers a question is provided by `--executor`,
a shell command that receives the question on stdin and prints the agent's
answer on stdout (e.g. a wrapper that calls the OWUI/SQL agent). Without an
executor, only the questions are printed (dry planning mode).

Usage:
  RAG_API_URL=... python3 replay.py \
      --eval adapters/toromont/eval.jsonl \
      --executor "python3 run_agent.py --model X" \
      --judge-llm --llm-url https://vllm/v1 --llm-model deepseek-v4-flash \
      > eval/report-before.txt
  # after lessons are seeded, repeat -> eval/report-after.txt
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

JUDGE_SYSTEM_PROMPT = (
    "You are grading an LLM SQL agent's answer against an expected check. "
    "Given the question, the expected check, and the agent's answer, decide "
    "whether the answer satisfies the check (correct query intent, correct "
    "data source/table mapping, correct boundedness/window, no LIKE scans "
    "on indexed text, serial identity resolved properly). Be strict: if the "
    "answer would be wrong or misleading, grade FAIL. Respond with exactly "
    "PASS or FAIL."
)


def load_eval(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def run_executor(executor: str, question: str, timeout: int = 120) -> str:
    proc = subprocess.run(
        executor,
        shell=True,
        input=question,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        return f"<executor error> {proc.stderr[:200]}"
    return (proc.stdout or "").strip()


def judge_llm(url: str, model: str, api_key: str, q: dict, answer: str) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Question: {q['question']}\n\n"
                    f"Expected check: {q['check']}\n\n"
                    f"Agent answer:\n{answer[:2000]}"
                ),
            },
        ],
        "max_tokens": 8,
        "temperature": 0.0,
    }
    hdrs = {"Content-Type": "application/json"}
    if api_key:
        hdrs["Authorization"] = f"Bearer {api_key}"
    r = httpx.post(f"{url.rstrip('/')}/chat/completions", json=payload, headers=hdrs, timeout=60.0)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip().upper()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval", type=Path, default=Path(__file__).parent / "sql-eval.jsonl")
    parser.add_argument("--executor", default="", help="shell command that runs the agent on a question")
    parser.add_argument("--notes", default="", help="label for this run (e.g. 'before-seed')")
    parser.add_argument("--judge-llm", action="store_true")
    parser.add_argument("--judge-manual", action="store_true")
    parser.add_argument("--llm-url", default=os.environ.get("DISTILL_LLM_URL", ""))
    parser.add_argument("--llm-model", default=os.environ.get("DISTILL_LLM_MODEL", ""))
    parser.add_argument("--llm-api-key", default=os.environ.get("DISTILL_LLM_API_KEY", ""))
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    if not args.executor:
        print("No --executor given — dumping the eval set for planning.\n")
        for q in load_eval(args.eval):
            print(f"- {q['id']} | {q['question']}  [expects: {q['check']}]")
        return 0

    if not (args.judge_llm or args.judge_manual):
        print("Need --judge-llm or --judge-manual to score answers.", file=sys.stderr)
        return 2

    print(f"=== Replay: {args.notes or 'unlabelled'} | eval={args.eval} | executor={args.executor} ===")
    rows = load_eval(args.eval)
    results = []
    for i, q in enumerate(rows, 1):
        print(f"[{i}/{len(rows)}] {q['question']}")
        answer = run_executor(args.executor, q["question"], args.timeout)
        verdict = "?"
        if args.judge_llm:
            verdict = judge_llm(args.llm_url, args.llm_model, args.llm_api_key, q, answer)
        elif args.judge_manual:
            print(answer)
            verdict = input("  grade (PASS/FAIL): ").strip().upper()
        print(f"  -> {verdict}\n")
        results.append(verdict == "PASS")

    total = len(results)
    passed = sum(results)
    print(f"=== {args.notes or 'run'} : {passed}/{total} PASS ({(passed / total * 100):.1f}%) ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())