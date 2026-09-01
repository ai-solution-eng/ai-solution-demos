"""Prometheus metrics for MultimodalRAG (roadmap feature 7).

Import-optional by design: when ``prometheus_client`` is not installed every
helper becomes a no-op, so call sites never branch on availability and tests
run without the dependency.

Surfaces instrumented:

  * HTTP — request count + latency histogram by route template / method /
    status (API middleware; unmatched paths are labelled ``unmatched``).
  * Ingest — files by outcome (``stored`` / ``deduplicated`` / ``error``),
    chunks embedded, batch jobs by source and terminal state.
  * Qdrant — op count + latency histogram for the batched search paths in
    ``vector_store`` (query-batch is the saturation bottleneck the
    performance audits kept measuring).
  * Retrieval — text searches by lane (hybrid RRF fusion vs dense-only) on
    bm25-capable collections.
  * Caches — query-embedding cache hit/miss, RAG-cache evictions.

The registry is process-local: the API server exposes ``/metrics`` and the
MCP sidecar can expose the same module's output on its own port.

Cardinality: labels are bounded (route templates, op names, small outcome
sets) — never raw paths, dataset names (except the deliberately bounded
ingest-chunks label) or query text.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

try:  # pragma: no cover - trivial import guard
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Histogram,
        generate_latest,
    )

    _REGISTRY = CollectorRegistry(auto_describe=True)

    HTTP_REQUESTS: Any = Counter(
        "rag_http_requests_total",
        "HTTP requests by route template, method and status code",
        ("route", "method", "status"),
        registry=_REGISTRY,
    )
    HTTP_LATENCY: Any = Histogram(
        "rag_http_request_seconds",
        "HTTP request duration by route template and method",
        ("route", "method"),
        registry=_REGISTRY,
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
    )
    INGEST_FILES: Any = Counter(
        "rag_ingest_files_total",
        "Files processed by outcome",
        ("result",),
        registry=_REGISTRY,
    )
    INGEST_CHUNKS: Any = Counter(
        "rag_ingest_chunks_total",
        "Document chunks embedded per dataset",
        ("dataset",),
        registry=_REGISTRY,
    )
    INGEST_JOBS: Any = Counter(
        "rag_ingest_jobs_total",
        "Batch jobs by source and terminal state",
        ("source", "state"),
        registry=_REGISTRY,
    )
    QDRANT_OPS: Any = Counter(
        "rag_qdrant_ops_total",
        "Qdrant operations by op and error flag",
        ("op", "error"),
        registry=_REGISTRY,
    )
    QDRANT_LATENCY: Any = Histogram(
        "rag_qdrant_op_seconds",
        "Qdrant operation duration",
        ("op",),
        registry=_REGISTRY,
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    )
    CACHE_EVENTS: Any = Counter(
        "rag_cache_events_total",
        "Cache events (hit/miss/eviction) per cache",
        ("cache", "event"),
        registry=_REGISTRY,
    )
    SEARCH_HYBRID: Any = Counter(
        "rag_search_hybrid_total",
        "Text search requests on bm25-capable collections by lane "
        "(hybrid = RRF fusion, dense = dense-only: RAG_HYBRID_SEARCH=0, "
        "empty df stats, or fusion unsupported)",
        ("mode",),
        registry=_REGISTRY,
    )

    AVAILABLE = True

    def render_metrics() -> tuple[bytes, str]:
        """Return ``(body, content_type)`` for the /metrics endpoint."""
        return generate_latest(_REGISTRY), CONTENT_TYPE_LATEST

except ImportError:  # pragma: no cover - minimal installs
    AVAILABLE = False

    class _Noop:
        """Absorbs any labels()/inc()/observe() chain when the lib is absent."""

        def labels(self, *_args: Any, **_kwargs: Any) -> _Noop:
            return self

        def inc(self, _amount: float = 1.0) -> None:
            pass

        def observe(self, _amount: float) -> None:
            pass

        def time(self) -> Any:
            import contextlib

            return contextlib.nullcontext()

    HTTP_REQUESTS = _Noop()
    HTTP_LATENCY = _Noop()
    INGEST_FILES = _Noop()
    INGEST_CHUNKS = _Noop()
    INGEST_JOBS = _Noop()
    QDRANT_OPS = _Noop()
    QDRANT_LATENCY = _Noop()
    CACHE_EVENTS = _Noop()
    SEARCH_HYBRID = _Noop()

    def render_metrics() -> tuple[bytes, str]:
        return b"", "text/plain; version=0.0.4; charset=utf-8"


def observe_qdrant(op: str) -> Any:
    """Context manager timing a Qdrant op: ``with observe_qdrant("scroll"): ...``.

    Records duration + success/error outcome.  When metrics are unavailable
    it degrades to a null context.
    """
    import time
    from contextlib import contextmanager

    @contextmanager
    def _timer():
        if not AVAILABLE:
            yield
            return
        start = time.perf_counter()
        err = "0"
        try:
            yield
        except Exception:
            err = "1"
            raise
        finally:
            QDRANT_LATENCY.labels(op=op).observe(max(0.0, time.perf_counter() - start))
            QDRANT_OPS.labels(op=op, error=err).inc()

    return _timer()


def observe_ingest_results(file_results: list[dict[str, Any]], dataset_name: str | None = None) -> None:
    """Count ingest file outcomes and embedded chunks from batch results.

    ``file_results`` entries are the ``{"file", "chunks", ...}`` dicts the
    batch pipeline already produces; grouping happens here so call sites
    stay one line.  ``dataset_name`` labels the chunk counter (datasets are
    few per deployment, keeping label cardinality bounded).
    """
    total_chunks = 0
    for r in file_results or []:
        if not isinstance(r, dict):
            continue
        if r.get("error"):
            INGEST_FILES.labels(result="error").inc()
        elif r.get("deduplicated"):
            INGEST_FILES.labels(result="deduplicated").inc()
        else:
            INGEST_FILES.labels(result="stored").inc()
        try:
            total_chunks += int(r.get("chunks") or 0)
        except (TypeError, ValueError):
            continue
    if total_chunks:
        INGEST_CHUNKS.labels(dataset=dataset_name or "unknown").inc(total_chunks)
