#!/usr/bin/env python3
"""Seed the self-improving SQL agent lesson store on a Multimodal RAG server.

Creates (idempotently) the two lesson datasets and uploads a curated seed
corpus of SQL lessons the agent recalls before writing SQL:

  <curated>            — curated lessons the agent RECALLS (read-only)
  <candidates-dataset> — write-only staging for lessons distilled later

The default seed is the GENERIC core (`seed/sql-lessons-seed.jsonl` —
domain-agnostic lessons: canonical-key identity, bounded queries, no free-text
scans, default window, RAG-vs-SQL routing). Domain-specific lessons are
layered on with `--adapter <name>`, which also seeds `adapters/<name>/<name>.jsonl`
(e.g. `--adapter toromont` adds the Toromont governed-surface lessons).

Usage:
    RAG_API_URL=https://rag.example.com \
    [RAG_LESSONS_PASSWORD=secret] \
    python seed_sql_lessons.py [--dataset sql-lessons] [--adapter toromont]

Endpoints used (Multimodal RAG api_server.py):
    POST /api/datasets                       create a dataset
    POST /api/datasets/{name}/documents      add documents
    GET  /api/datasets                       list (to skip create if exists)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

CURATED = "sql-lessons"
CANDIDATES = "sql-lessons-candidates"
DEFAULT_SEED = Path(__file__).parent / "seed" / "sql-lessons-seed.jsonl"

CURATED_DESC = (
    "Curated, validated SQL lessons an agent recalls before writing SQL. "
    "Written only by the promotion gate (candidate -> curated). One doc per "
    "lesson: content = the instruction to follow, metadata = trigger/kind/"
    "tables/tags/status."
)
CANDIDATES_DESC = (
    "Unvalidated SQL lesson candidates distilled from agent resolutions. "
    "Written by the OWUI distillation step; reviewed/promoted by the "
    "promotion gate. Never read directly by the agent."
)


def api_base() -> str:
    url = os.environ.get("RAG_API_URL", "").rstrip("/")
    if not url:
        sys.exit("Set RAG_API_URL (e.g. https://rag.example.com) — required.")
    return url


def headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    pw = os.environ.get("RAG_LESSONS_PASSWORD", "").strip()
    if pw:
        h["X-Dataset-Password"] = pw
    return h


def list_datasets(client: httpx.Client, api: str) -> set[str]:
    r = client.get(f"{api}/api/datasets")
    r.raise_for_status()
    return {d.get("name") for d in r.json().get("datasets", [])}


def create_dataset(client: httpx.Client, api: str, name: str, desc: str) -> None:
    r = client.post(
        f"{api}/api/datasets",
        json={
            "name": name,
            "description": desc,
            "caption_with_vlm": False,   # text-only lessons, no VLM needed
            "caption_with_asr": False,
            "keep_originals": True,
            "password": os.environ.get("RAG_LESSONS_PASSWORD") or None,
        },
        headers=headers(),
    )
    if r.status_code == 409:
        print(f"  dataset '{name}' already exists — ok")
        return
    r.raise_for_status()
    print(f"  created dataset '{name}'")


def upload_documents(client: httpx.Client, api: str, name: str, docs: list[dict]) -> None:
    r = client.post(f"{api}/api/datasets/{name}/documents", json=docs, headers=headers())
    r.raise_for_status()
    resp = r.json()
    print(f"  uploaded {resp.get('count', len(docs))} docs -> '{name}'")


def load_seed(path: Path) -> list[dict]:
    if not path.exists():
        sys.exit(f"Seed file not found: {path}")
    docs = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not docs:
        sys.exit(f"No documents in seed file: {path}")
    return docs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        default=CURATED,
        help="curated dataset name to seed (default: %(default)s)",
    )
    parser.add_argument(
        "--candidates-dataset",
        default=CANDIDATES,
        help="candidate staging dataset name (default: %(default)s)",
    )
    parser.add_argument(
        "--seed",
        type=Path,
        default=DEFAULT_SEED,
        help="seed corpus path (generic core by default)",
    )
    parser.add_argument(
        "--adapter",
        action="append",
        default=None,
        help="adapter directory name under adapters/ to also seed, e.g. --adapter toromont "
        "(repeatable; seeds adapters/<name>/<name>.jsonl after the core)",
    )
    args = parser.parse_args()

    curated = args.dataset.strip() or CURATED
    candidates = args.candidates_dataset.strip() or CANDIDATES

    api = api_base()
    docs = load_seed(args.seed)

    # Optional adapter seeds (domain-specific lessons layered on the core).
    adapters_dir = Path(__file__).parent / "adapters"
    for adapter in args.adapter or []:
        apath = adapters_dir / adapter / f"{adapter}.jsonl"
        extra = load_seed(apath)
        # Tag each adapter doc with its source so it stays attributable.
        for d in extra:
            d.setdefault("source", f"manual:adapter:{adapter}")
        docs.extend(extra)
        print(f"  + adapter '{adapter}': {len(extra)} lessons from {apath.name}")

    with httpx.Client(timeout=60.0) as client:
        existing = list_datasets(client, api)
        if curated not in existing:
            create_dataset(client, api, curated, CURATED_DESC)
        if candidates not in existing:
            create_dataset(client, api, candidates, CANDIDATES_DESC)
        upload_documents(client, api, curated, docs)
    print(f"\nDone. Curated lessons are in '{curated}'; candidates go to '{candidates}'.")
    print("Wire the OWUI filter recall (SQL_LESSONS_DATASET) to start recalling these.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())