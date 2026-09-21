import asyncio
import concurrent.futures
import logging
import os
import threading
from collections.abc import Callable, Sequence
from math import ceil
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

__all__ = [
    "SYNC_POOL_SIZE",
    "cosine_sim",
    "cosine_sim_int8",
    "list_chunker",
    "quantize_int8",
    "retry_async_call",
    "retry_call",
    "sync_pool",
    "sync_wrapper_safe",
]


def _pool_size_from_env(default: int = 12) -> int:
    raw = os.environ.get("SYNC_POOL_SIZE")
    if not raw:
        return default
    try:
        val = int(raw)
        return val if val > 0 else default
    except (TypeError, ValueError):
        logger.warning("SYNC_POOL_SIZE=%r is not a positive int; using %d", raw, default)
        return default


SYNC_POOL_SIZE = _pool_size_from_env()
sync_pool = concurrent.futures.ThreadPoolExecutor(max_workers=SYNC_POOL_SIZE, thread_name_prefix="sync_wrapper")


def cosine_sim(vec1: np.ndarray, vec2: np.ndarray):
    if len(vec1.shape) == 1:
        vec1 = vec1.reshape(1, len(vec1))

    if len(vec2.shape) == 1:
        vec2 = vec2.reshape(1, len(vec2))

    assert vec1.ndim == 2
    assert vec2.ndim == 2
    assert vec1.shape[1] == vec2.shape[1]

    n, m = len(vec1), len(vec2)
    return vec1 @ vec2.T / (np.linalg.norm(vec1, axis=-1).reshape(n, 1) * np.linalg.norm(vec2, axis=-1).reshape(1, m))


# -- int8 scalar quantization (matches Qdrant's approach) --------------------


def quantize_int8(
    vectors: np.ndarray,
    quantile: float = 0.99,
    scale: float | None = None,
) -> tuple[np.ndarray, float]:
    """Quantize float32 vectors to int8 using Qdrant's scalar quantization scheme.

    Each float32 value ``v`` is mapped to a signed int8 as::

        q = clamp(round(v * 127 / scale), -128, 127)

    where *scale* is the *quantile*-th percentile of the absolute values
    across all dimensions (e.g. 0.99 ignores the top 1% of extreme values
    for robustness).  Dequantization is approximate: ``v ≈ q * scale / 127``.

    Parameters
    ----------
    vectors:
        1-D or 2-D float32/float64 array.  A 1-D array is treated as a
        single vector.
    quantile:
        Percentile of absolute values to use as the scale (default 0.99,
        matching Qdrant's default).
    scale:
        If provided, use this scale instead of computing one.  Needed
        when quantizing a query vector with the same scale that was used
        for the stored vectors.

    Returns
    -------
    (int8_vectors, scale)
        ``int8_vectors`` has the same shape as *vectors* but dtype int8.
        ``scale`` is the float scalar used for quantization.
    """
    was_1d = vectors.ndim == 1
    if was_1d:
        vectors = vectors.reshape(1, -1)
    vectors = vectors.astype(np.float64)

    if scale is None:
        abs_vals = np.abs(vectors)
        scale = float(np.quantile(abs_vals, quantile))
        if scale == 0:
            scale = 1.0

    quantized = np.clip(np.round(vectors * 127.0 / scale), -128, 127).astype(np.int8)

    if was_1d:
        quantized = quantized.reshape(-1)
    return quantized, scale


def cosine_sim_int8(
    vec1: np.ndarray,
    vec2: np.ndarray,
) -> np.ndarray:
    """Cosine similarity using int8 scalar quantization (Qdrant-style).

    Both vectors are quantized to int8 with a shared scale (the 99th
    percentile of absolute values across both inputs), then cosine
    similarity is computed on the int8 representations.

    **Why this works**: for cosine similarity the scale factor cancels::

        cos(a, b) = dot(a, b) / (|a| * |b|)

        a ≈ q_a * (scale / 127)
        b ≈ q_b * (scale / 127)

        dot(a, b) ≈ (scale² / 127²) * dot(q_a, q_b)
        |a|       ≈ (scale / 127) * |q_a|
        |b|       ≈ (scale / 127) * |q_b|

        cos(a, b) ≈ dot(q_a, q_b) / (|q_a| * |q_b|)

    The scale cancels entirely — the int8 cosine similarity is
    approximately equal to the float32 cosine similarity.  The only
    error comes from rounding in the quantization step (typically 1-2%).

    Parameters
    ----------
    vec1, vec2:
        1-D or 2-D float arrays.  2-D inputs are treated as batches and
        a similarity matrix is returned (same as :func:`cosine_sim`).

    Returns
    -------
    Similarity scalar (1-D inputs) or (n, m) matrix (2-D inputs).
    """
    if len(vec1.shape) == 1:
        vec1 = vec1.reshape(1, len(vec1))
    if len(vec2.shape) == 1:
        vec2 = vec2.reshape(1, len(vec2))

    # Compute a shared scale from the combined absolute values
    combined = np.vstack([vec1, vec2])
    scale = float(np.quantile(np.abs(combined), 0.99))
    if scale == 0:
        scale = 1.0

    q1, _ = quantize_int8(vec1, scale=scale)
    q2, _ = quantize_int8(vec2, scale=scale)

    # Convert to float for the dot product / norm computation.
    # (Qdrant uses SIMD int8 multiply-add internally, but the math is
    # identical — we use float here for clarity.)
    q1f = q1.astype(np.float64)
    q2f = q2.astype(np.float64)

    n, m = len(q1f), len(q2f)
    norms1 = np.linalg.norm(q1f, axis=-1).reshape(n, 1)
    norms2 = np.linalg.norm(q2f, axis=-1).reshape(1, m)
    return (q1f @ q2f.T) / (norms1 * norms2)


# -- Persistent background event loop for sync→async bridging ---------------
# Keeps a dedicated thread with a long-lived event loop so that async
# clients (httpx connection pools, OpenAI clients) survive across calls
# instead of being recreated on every sync_wrapper_safe invocation.

_BG_LOOP: asyncio.AbstractEventLoop | None = None
_BG_THREAD: threading.Thread | None = None
_BG_LOCK = threading.Lock()


def _bg_loop_exception_handler(loop: asyncio.AbstractEventLoop, ctx: dict) -> None:
    """Log unhandled exceptions from the background event loop instead
    of silently discarding them."""
    msg = ctx.get("message", "Unhandled exception in bg event loop")
    exc = ctx.get("exception")
    if exc is not None:
        logger.error("%s: %s", msg, exc, exc_info=exc)
    else:
        logger.error("%s", msg)


def _get_bg_loop() -> asyncio.AbstractEventLoop:
    global _BG_LOOP, _BG_THREAD
    if _BG_LOOP is not None and _BG_THREAD is not None and _BG_THREAD.is_alive():
        return _BG_LOOP

    with _BG_LOCK:
        if _BG_LOOP is not None and _BG_THREAD is not None and _BG_THREAD.is_alive():
            return _BG_LOOP

        loop = asyncio.new_event_loop()
        loop.set_exception_handler(_bg_loop_exception_handler)

        def _run_bg():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        t = threading.Thread(target=_run_bg, daemon=True, name="sync-bg-loop")
        t.start()
        _BG_LOOP = loop
        _BG_THREAD = t
        return loop


async def _bg_call(function: Callable, kwargs: dict[str, Any]) -> Any:
    return await function(**kwargs)


def sync_wrapper_safe(function: Callable, kwargs: dict[str, Any] | None = None):
    if kwargs is None:
        kwargs = {}
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None

    if loop is None or loop.is_closed():
        bg_loop = _get_bg_loop()
        fut = asyncio.run_coroutine_threadsafe(_bg_call(function, kwargs), bg_loop)
        return fut.result()

    if not loop.is_running():
        return loop.run_until_complete(function(**kwargs))

    # Loop is running on this thread (e.g. inside a FastAPI async handler).
    # We cannot nest run_until_complete (uvloop rejects it) and cannot use
    # run_coroutine_threadsafe (deadlocks on the same thread).
    # Instead, offload to the background loop.
    bg_loop = _get_bg_loop()
    fut = asyncio.run_coroutine_threadsafe(_bg_call(function, kwargs), bg_loop)
    return fut.result()


def list_chunker(input_list: Sequence[Any], chunk_size: int, optimize: bool = True) -> list:
    assert chunk_size > 0, "Require chunksize to be positive."
    num_chunks = ceil(len(input_list) / chunk_size)

    if num_chunks == 1:
        return [input_list]

    if optimize:
        # Ex, 192 with CS==128 -> [96, 96] instead of [128, 64]
        chunk_size = ceil(len(input_list) / num_chunks)

    return [input_list[i * chunk_size : (i + 1) * chunk_size] for i in range(num_chunks)]


# -- Retry helpers --------------------------------------------------------------


def retry_call(
    func: Callable,
    kwargs: dict[str, Any] | None = None,
    max_attempts: int = 3,
    base_delay: float = 2.0,
    connection_delay: float = 10.0,
) -> Any:
    """Call *func* with retries on failure.

    Uses linear backoff (``base_delay * (attempt + 1)``), with a longer
    minimum delay when the error message looks like a connection issue.

    Parameters
    ----------
    func:
        Synchronous callable to invoke.
    kwargs:
        Keyword arguments passed to *func*.
    max_attempts:
        Maximum number of attempts (including the first).
    base_delay:
        Base delay in seconds between retries.
    connection_delay:
        Minimum delay for connection-related errors (``base_delay`` is
        raised to at least this value when the error message contains
        "connection").
    """
    import time as _time

    kwargs = kwargs or {}
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return func(**kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                delay = base_delay * (attempt + 1)
                if "connection" in str(exc).lower():
                    delay = max(delay, connection_delay)
                _time.sleep(delay)
    assert last_exc is not None
    raise last_exc


async def retry_async_call(
    func: Callable,
    kwargs: dict[str, Any] | None = None,
    max_attempts: int = 3,
    base_delay: float = 2.0,
    connection_delay: float = 10.0,
) -> Any:
    """Call async *func* with retries on failure.

    Async counterpart to :func:`retry_call`.
    """
    kwargs = kwargs or {}
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await func(**kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                delay = base_delay * (attempt + 1)
                if "connection" in str(exc).lower():
                    delay = max(delay, connection_delay)
                await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc
