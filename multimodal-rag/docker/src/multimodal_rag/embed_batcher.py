"""Shared (cross-process) embedding query batcher.

The embedding endpoint (vLLM pooling runner) costs ~160ms of server-side
overhead per HTTP call and only amortises it over the texts in a single
``input`` batch.  The app's per-process ``_QueryBatcher`` aggregates only
what arrives on *one* event loop, so with many workers/replicas the batches
shrink to ~1 text and throughput collapses to the model's unbatched rate.

This service is a singleton aggregator: every API/MCP pod POSTs its text-only
queries to ``/embed``, and this process collects them over the configured
window into a single ``/v1/embeddings`` call.  Because it is a single process,
the batch size is independent of the number of app processes, so app scaling
no longer degrades embedding throughput.

Run with a single worker (uvicorn workers=1); more would reintroduce the
batching split this service exists to remove.
"""

import argparse
import asyncio
import os
import threading
from dataclasses import dataclass

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from multimodal_rag.model_config import build_all
from multimodal_rag.utils.logging_utils import logging, setup_logger
from multimodal_rag.utils.model_adapters import _QueryBatcher

logger = logging.getLogger("embed-batcher")

app = FastAPI(title="embed-batcher", version="1.0.0")


@dataclass
class _BatcherState:
    """Mutable singleton state guarded by *_state_lock*."""

    batcher: _QueryBatcher | None = None
    error: str | None = None


_state_lock = threading.Lock()
_state = _BatcherState()

# Hard cap on a single flushed batch's model round-trip.  The batcher is
# single-flight: if the embedding endpoint stalls, every query queued in that
# batch would otherwise wait for it.  A healthy batch drains in ~1-4s, so this
# is a tripwire for a stalled model call — timing out lets the caller fall
# back to local batching instead of hanging the request for tens of seconds.
_FLUSH_TIMEOUT = float(os.environ.get("EMBED_BATCH_FLUSH_TIMEOUT", "15"))


class EmbedRequest(BaseModel):
    text: str


def _build_batcher() -> _QueryBatcher:
    """Construct the aggregating batcher bound to the configured embedder."""
    embedder, _, _, _ = build_all()
    if embedder is None:
        raise RuntimeError("No embedder model configured (MODEL_EMBEDDER_URL not set)")
    model = embedder.model  # MultiModalEmbeddings
    max_size = int(os.environ.get("EMBEDDING_QUERY_BATCH_SIZE", "128"))
    max_wait_ms = float(os.environ.get("EMBEDDING_QUERY_BATCH_WAIT_MS", "200"))
    return _QueryBatcher(
        embed_fn=model._embed_text_batch_async,
        max_batch_size=max_size,
        max_wait_ms=max_wait_ms,
    )


def _reload() -> None:
    """(Re)build the batcher — also used by the hot-config watcher."""
    try:
        batcher = _build_batcher()
        with _state_lock:
            _state.batcher = batcher
            _state.error = None
    except Exception as exc:
        logger.exception("Failed to build embedder for batcher")
        with _state_lock:
            _state.error = str(exc)


@app.on_event("startup")
async def _startup() -> None:
    config_dir = os.environ.get("CONFIG_DIR", "")
    if config_dir:
        from multimodal_rag.model_config import apply_config_dirs, start_config_watcher

        apply_config_dirs(config_dir)
        start_config_watcher(config_dir, _reload)
    _reload()
    logger.info("Embed batcher initialised")


@app.post("/embed")
async def embed(req: EmbedRequest) -> dict:
    with _state_lock:
        batcher = _state.batcher
        error = _state.error
    if batcher is None:
        raise HTTPException(503, f"Embedding not initialised: {error or 'building'}")
    try:
        vec = await asyncio.wait_for(batcher.submit(req.text), timeout=_FLUSH_TIMEOUT)
    except TimeoutError:
        logger.warning("Embed batch flush timed out after %.0fs", _FLUSH_TIMEOUT)
        raise HTTPException(504, f"Embedding timed out after {_FLUSH_TIMEOUT:g}s")
    except Exception as exc:
        logger.warning("Embedding failed: %s", exc)
        raise HTTPException(502, f"Embedding failed: {exc}")
    return {"embedding": vec}


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict:
    with _state_lock:
        ready = _state.batcher is not None
    if not ready:
        raise HTTPException(503, f"Embed batcher not ready: {_state.error or 'building'}")
    return {"status": "ready"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Shared embedding query batcher")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    setup_logger(level=args.log_level)
    uvicorn.run(app, host=args.host, port=args.port, workers=1, log_level=args.log_level.lower())


if __name__ == "__main__":
    main()
